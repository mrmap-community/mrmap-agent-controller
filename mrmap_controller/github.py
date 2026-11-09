"""GitHub adapter (read-only through the existing ``gh`` login, plus one
explicitly allowed board alert).

Only read endpoints are implemented and the runner refuses any ``gh api`` call
with an explicit HTTP method or body, so a write through the API is not
possible. The single exception is :meth:`GitHub.post_issue_comment`: the stall
watchdog (W4) must reach a human, and it does so with one deduped comment on
the work board issue. It shells out to ``gh issue comment`` -- every merge,
review, close, label and api-write command stays denied by the runner, and no
other call site uses this method. Issue dependency relations (native
sub-issues, "Blocked by" tasklists and blocking links) are deliberately never
read: scheduling depends on the human approval label alone.
"""
import json
import re

from .logging_util import redact

PR_FIELDS = ('number,state,isDraft,headRefName,headRefOid,mergedAt,closedAt,url,'
             'title,reviewDecision,mergeable,mergeStateStatus,statusCheckRollup,'
             'author,updatedAt')

THREADS_QUERY = '''
query($owner:String!,$name:String!,$number:Int!){
  repository(owner:$owner,name:$name){
    pullRequest(number:$number){
      reviewThreads(first:100){nodes{isResolved isOutdated
        comments(first:1){nodes{databaseId}}}}
    }
  }
}'''


class GithubError(Exception):
    """GitHub could not be queried reliably: the caller must fail closed."""


class GitHub:
    def __init__(self, runner, repo):
        self.runner = runner
        self.owner, self.name = repo.split('/', 1)

    # ------------------------------------------------------------ plumbing --
    def _json(self, argv):
        try:
            proc = self.runner.run(argv)
        except Exception as exc:                      # CommandError, OSError, Denied
            raise GithubError(redact(exc)) from exc
        try:
            return json.loads(proc.stdout)
        except ValueError as exc:
            raise GithubError(f'unparsable GitHub response from '
                              f'{" ".join(argv[:4])}') from exc

    def _paginate(self, endpoint):
        pages = self._json(['gh', 'api', '--paginate', '--slurp', endpoint])
        if not isinstance(pages, list) or not all(isinstance(p, list) for p in pages):
            raise GithubError(f'unexpected paginated shape for {endpoint}')
        return [item for page in pages for item in page]

    # ------------------------------------------------------------ queries ---
    def whoami(self):
        data = self._json(['gh', 'api', 'user'])
        login = data.get('login') if isinstance(data, dict) else None
        if not login:
            raise GithubError('gh api user returned no login')
        return login

    def open_issues_with_label(self, label):
        endpoint = (f'repos/{self.owner}/{self.name}/issues?state=open'
                    f'&labels={label.replace(" ", "%20")}&per_page=100')
        data = self._paginate(endpoint)
        return [i for i in data if isinstance(i, dict) and 'pull_request' not in i]

    def issue(self, number):
        data = self._json(['gh', 'api', f'repos/{self.owner}/{self.name}'
                                        f'/issues/{number}'])
        if not isinstance(data, dict) or data.get('number') != number:
            raise GithubError(f'unexpected issue payload for #{number}')
        return data

    def issue_label_events(self, number, label):
        """Every labeled/unlabeled event for one label, oldest first."""
        events = self._paginate(f'repos/{self.owner}/{self.name}/issues/{number}'
                                f'/events?per_page=100')
        labelled = [e for e in events if isinstance(e, dict)
                    and e.get('event') in ('labeled', 'unlabeled')
                    and (e.get('label') or {}).get('name') == label]
        labelled.sort(key=lambda e: (e.get('created_at') or '', int(e.get('id') or 0)))
        return labelled

    def open_pulls(self):
        prs = self._paginate(f'repos/{self.owner}/{self.name}/pulls?state=open'
                             f'&per_page=100')
        if not all(isinstance(p, dict) for p in prs):
            raise GithubError('unexpected pull request listing')
        return prs

    def pr_view(self, number):
        argv = ['gh', 'pr', 'view', str(number), '--repo', f'{self.owner}/{self.name}',
                '--json', PR_FIELDS]
        data = self._json(argv)
        if not isinstance(data, dict) or data.get('number') != number:
            raise GithubError(f'unexpected pull request payload for PR #{number}')
        return data

    def pr_reviews(self, number):
        return [r for r in self._paginate(f'repos/{self.owner}/{self.name}/pulls/'
                                          f'{number}/reviews?per_page=100')
                if isinstance(r, dict)]

    def pr_review_comments(self, number):
        return [c for c in self._paginate(f'repos/{self.owner}/{self.name}/pulls/'
                                          f'{number}/comments?per_page=100')
                if isinstance(c, dict)]

    def pr_issue_comments(self, number):
        return [c for c in self._paginate(f'repos/{self.owner}/{self.name}/issues/'
                                          f'{number}/comments?per_page=100')
                if isinstance(c, dict)]

    def pr_resolved_thread_comment_ids(self, number):
        """databaseIds of inline comments living in a resolved or outdated thread."""
        argv = ['gh', 'api', 'graphql', '-f', f'owner={self.owner}',
                '-f', f'name={self.name}', '-F', f'number={number}',
                '-f', f'query={THREADS_QUERY}']
        data = self._json(argv)
        try:
            nodes = data['data']['repository']['pullRequest']['reviewThreads']['nodes']
        except (KeyError, TypeError):
            raise GithubError(f'unexpected review thread payload for PR #{number}') from None
        resolved = set()
        for thread in nodes:
            if thread.get('isResolved') or thread.get('isOutdated'):
                for comment in (thread.get('comments') or {}).get('nodes') or []:
                    if comment.get('databaseId') is not None:
                        resolved.add(str(comment['databaseId']))
        return resolved

    # ------------------------------------------------------------ writing ---
    def post_issue_comment(self, number, body):
        """Post one comment on an issue -- the watchdog's board alert only.

        ``gh issue comment`` carries no HTTP verb or field, so the runner's
        deny list still stands for everything else; this is the only write in
        the adapter and its only intended target is the board issue.
        """
        argv = ['gh', 'issue', 'comment', str(number), '--repo',
                f'{self.owner}/{self.name}', '--body', body]
        try:
            self.runner.run(argv)
        except Exception as exc:
            raise GithubError(redact(exc)) from exc

    # ------------------------------------------------------------ helpers ---
    @staticmethod
    def pr_references_issue(pr, issue):
        """Conservative reference test, shared with the pilot's rule."""
        n = str(issue)
        text = (pr.get('title') or '') + '\n' + (pr.get('body') or '')
        branch = ((pr.get('head') or {}).get('ref') or pr.get('headRefName') or '')
        return (bool(re.search(r'(?<!\d)#' + n + r'(?!\d)', text))
                or bool(re.search(r'(?<!\d)(?:issue[-_/]|/)' + n + r'(?!\d)', branch, re.I)))


# ------------------------------------------------------------- CI facts -----
FAILING_CONCLUSIONS = {'FAILURE', 'CANCELLED', 'TIMED_OUT', 'ACTION_REQUIRED',
                       'ERROR', 'STARTUP_FAILURE', 'INVALID'}
OK_CONCLUSIONS = {'SUCCESS', 'SKIPPED', 'NEUTRAL'}
PENDING_STATES = {'QUEUED', 'IN_PROGRESS', 'WAITING', 'PENDING', 'REQUESTED'}


def interpret_ci(rollup, expected_sha=None):
    """Fold GitHub's check rollup into pending | success | failure | unknown.

    Unknown is returned when there is nothing to trust -- an empty rollup or
    checks reported for a different head commit -- so callers never act on it.
    """
    if not isinstance(rollup, list) or not rollup:
        return 'unknown', []
    pending, failing, ok = [], [], []
    for check in rollup:
        if not isinstance(check, dict):
            return 'unknown', []
        name = check.get('name') or check.get('context') or 'unknown check'
        status = (check.get('status') or '').upper()
        conclusion = (check.get('conclusion') or '').upper()
        details = check.get('detailsUrl') or check.get('targetUrl') or ''
        sha = check.get('headSha')
        if expected_sha and sha and sha != expected_sha:
            return 'unknown', []
        entry = {'name': name, 'conclusion': conclusion or status, 'url': details}
        if status in PENDING_STATES or (status and status != 'COMPLETED'):
            pending.append(entry)
        elif conclusion in FAILING_CONCLUSIONS:
            failing.append(entry)
        elif conclusion in OK_CONCLUSIONS:
            ok.append(entry)
        else:
            pending.append(entry)
    if failing:
        return 'failure', failing
    if pending:
        return 'pending', pending
    return ('success', ok) if ok else ('unknown', [])
