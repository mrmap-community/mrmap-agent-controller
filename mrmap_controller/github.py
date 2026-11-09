"""GitHub adapter (read-only through the existing ``gh`` login, plus one
explicitly allowed comment path).

Only read endpoints are implemented and the runner refuses any ``gh api`` call
with an explicit HTTP method or body, so a write through the API is not
possible. The single exception is :meth:`GitHub.post_issue_comment`: the stall
watchdog (W4) and the closing reconciliation (W2) must each reach a human with
one deduped comment -- the watchdog on the work board issue, W2 on the issue
whose merged pull request did not close it. It shells out to ``gh issue
comment`` -- every merge, review, close, label and api-write command stays
denied by the runner, and no other call site uses this method. Issue dependency
relations (native sub-issues as a *scheduling* input, "Blocked by" tasklists
and blocking links) are deliberately never used to schedule work; the sub-issue
parent lookup :meth:`GitHub.issue_parent` only routes an already verified human
answer to the waiting worker that asked (W3).
"""
import json
import re

from . import identity
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

#: The REAL closing linkage: issues GitHub closed because the PR body carried a
#: closing keyword and the PR merged. This is the ``closed #N`` line in the
#: merge box (the closed-references connection); prose that merely mentions an
#: issue number never appears here.
CLOSED_ISSUES_QUERY = '''
query($owner:String!,$name:String!,$number:Int!){
  repository(owner:$owner,name:$name){
    pullRequest(number:$number){
      closedIssues(first:50){nodes{number state}}
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

    def issue_comments(self, number):
        """Plain comments on an issue (or PR -- the endpoint and numbering
        space are shared). REST includes ``author_association`` per comment,
        which is what the question/answer verification reads (W3)."""
        return [c for c in self._paginate(f'repos/{self.owner}/{self.name}/issues/'
                                          f'{number}/comments?per_page=100')
                if isinstance(c, dict)]

    def issue_parent(self, number):
        """The native sub-issue parent of ``number``, or None.

        GitHub answers 404 (and gh exits non-zero) when the issue has no
        parent, so every read failure maps to None: a missed parent can only
        miss a wake on this tick, never take a wrong action. Retried next tick.
        """
        try:
            data = self._json(['gh', 'api', f'repos/{self.owner}/{self.name}'
                                            f'/issues/{number}/parent'])
        except GithubError:
            return None
        if not isinstance(data, dict):
            return None
        return data.get('number')

    def pr_closes_issue(self, pr_number, issue):
        """True when GitHub's own closing linkage ties this PR to closing that
        issue (W2) -- the closed-references connection that backs the
        ``closes #N`` line in the merge box, read through the GraphQL
        ``closedIssues`` field.

        This is deliberately not a ``#N`` grep of title or body: prose that
        only mentions an issue (the #981/#995 pattern) never appears here, so
        a merged PR whose linkage did not fire is caught by the controller
        instead of silently leaving the issue open and re-dispatchable.
        """
        argv = ['gh', 'api', 'graphql', '-f', f'owner={self.owner}',
                '-f', f'name={self.name}', '-F', f'number={pr_number}',
                '-f', f'query={CLOSED_ISSUES_QUERY}']
        data = self._json(argv)
        try:
            nodes = data['data']['repository']['pullRequest']['closedIssues']['nodes']
        except (KeyError, TypeError):
            raise GithubError(f'unexpected closed-issues payload for PR #{pr_number}') \
                from None
        if not isinstance(nodes, list):
            raise GithubError(f'unexpected closed-issues payload for PR #{pr_number}')
        for node in nodes:
            if isinstance(node, dict) and node.get('number') == issue \
                    and (node.get('state') or '').upper() == 'CLOSED':
                return True
        return False

    # ------------------------------------------------------------ writing ---
    def post_issue_comment(self, number, body):
        """Post one comment on an issue -- board alerts and closing asks only.

        ``gh issue comment`` carries no HTTP verb or field, so the runner's
        deny list still stands for everything else; this is the only write in
        the adapter and its intended targets are the board issue (watchdog)
        and one issue whose merged pull request did not close it (W2 ask).
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
        """Reference test: an explicit number in title/body, or the branch.

        The branch counts only when it *declares* this issue under the shared
        segment rule (:mod:`mrmap_controller.identity`). The old test matched
        ``issue[-_]/<digits>`` anywhere inside the branch string, which is how
        ``ao/implement-github-issue-5/root`` was read as a pull request for
        issue #5.
        """
        n = str(issue)
        text = (pr.get('title') or '') + '\n' + (pr.get('body') or '')
        if re.search(r'(?<!\d)#' + n + r'(?!\d)', text):
            return True
        branch = ((pr.get('head') or {}).get('ref') or pr.get('headRefName') or '')
        try:
            number = int(n)
        except ValueError:
            return False
        return identity.issue_from_branch(branch) == number


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
