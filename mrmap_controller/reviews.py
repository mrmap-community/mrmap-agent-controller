"""Review feedback collection and trust filtering.

Human feedback arrives in three shapes: pull request reviews, inline review
comments, and issue-style comments on the pull request. Bot output is ignored,
and the text of every item kept is treated as data to implement, never as
authority: the caller quotes it into the worker message inside explicit
untrusted-content markers and the controller itself takes no further GitHub
action from it.
"""
BOT_SUFFIX = '[bot]'
ACTIONABLE_REVIEW_STATES = {'CHANGES_REQUESTED', 'COMMENTED'}


def actor_of(payload):
    user = payload.get('user') or payload.get('author') or {}
    return user.get('login'), (user.get('type') or '')


class Item:
    def __init__(self, kind, source_id, author, body, association, location, url):
        self.kind = kind
        self.source_id = str(source_id)
        self.author = author
        self.body = body or ''
        self.association = (association or '').upper()
        self.location = location
        self.url = url or ''

    @property
    def sort_key(self):
        try:
            return (0, int(self.source_id))
        except ValueError:
            return (1, self.source_id)

    def describe(self, max_chars):
        body = self.body.strip().replace('\r\n', '\n')
        if len(body) > max_chars:
            body = body[:max_chars] + '\n[... quoted text truncated ...]'
        head = f'- {self.kind} by {self.author} (association {self.association or "?"})'
        if self.location:
            head += f' at {self.location}'
        if self.url:
            head += f' -- {self.url}'
        return (f'{head}:\n  UNTRUSTED CONTENT BEGIN\n'
                + '\n'.join('  | ' + line for line in body.split('\n'))
                + '\n  UNTRUSTED CONTENT END')


def is_bot(login, user_type, association):
    return (not login) or BOT_SUFFIX in (login or '') or (user_type or '') == 'Bot' \
        or (association or '').upper() == 'BOT'


class ReviewCollector:
    def __init__(self, cfg, gh, log):
        self.cfg = cfg
        self.gh = gh
        self.log = log

    def trusted(self, login, association):
        if login in self.cfg.review_allowlist:
            return True
        return (association or '').upper() in {a.upper() for a in self.cfg.review_associations}

    def collect(self, pr_number, seen_ids):
        """Return (actionable items, ids to mark as seen without action)."""
        actionable, dismiss = [], set()
        resolved_ids = set()
        if self.cfg.use_thread_resolution:
            try:
                resolved_ids = self.gh.pr_resolved_thread_comment_ids(pr_number)
            except Exception as exc:
                # Fail open on dispatch here: skipping this read must not cause a
                # human comment to be ignored forever.
                self.log.warning('thread_resolution_unavailable', pr=pr_number,
                                 reason=str(exc))
        for review in self.gh.pr_reviews(pr_number):
            login, utype = actor_of(review)
            association = review.get('author_association')
            source_id = review.get('id')
            if source_id is None or str(source_id) in seen_ids.get('review', set()):
                continue
            if is_bot(login, utype, association) or login in self.cfg.ignore_authors:
                dismiss.add(('review', source_id))
                continue
            if not self.trusted(login, association):
                self.log.warning('untrusted_review_author_ignored', pr=pr_number,
                                 detail=f'{login}/{association}')
                dismiss.add(('review', source_id))
                continue
            if review.get('state') in ACTIONABLE_REVIEW_STATES and (review.get('body') or '').strip():
                actionable.append(Item('review', source_id, login, review.get('body'),
                                       association, None, review.get('html_url')))
            else:
                # APPROVED / DISMISSED / empty review: seen, never a work item.
                dismiss.add(('review', source_id))
        for comment in self.gh.pr_review_comments(pr_number):
            login, utype = actor_of(comment)
            association = comment.get('author_association')
            source_id = comment.get('id')
            if source_id is None or str(source_id) in seen_ids.get('review_comment', set()):
                continue
            if str(source_id) in resolved_ids:
                dismiss.add(('review_comment', source_id))
                continue
            if is_bot(login, utype, association) or login in self.cfg.ignore_authors:
                dismiss.add(('review_comment', source_id))
                continue
            if not self.trusted(login, association):
                self.log.warning('untrusted_comment_author_ignored', pr=pr_number,
                                 detail=f'{login}/{association}')
                dismiss.add(('review_comment', source_id))
                continue
            location = comment.get('path') or ''
            if comment.get('line'):
                location = f'{location}:{comment["line"]}'
            actionable.append(Item('inline comment', source_id, login, comment.get('body'),
                                   association, location or None, comment.get('html_url')))
        for comment in self.gh.pr_issue_comments(pr_number):
            login, utype = actor_of(comment)
            association = comment.get('author_association')
            source_id = comment.get('id')
            if source_id is None or str(source_id) in seen_ids.get('issue_comment', set()):
                continue
            if is_bot(login, utype, association) or login in self.cfg.ignore_authors:
                dismiss.add(('issue_comment', source_id))
                continue
            if not self.trusted(login, association):
                self.log.warning('untrusted_issue_comment_author_ignored',
                                 pr=pr_number, detail=f'{login}/{association}')
                dismiss.add(('issue_comment', source_id))
                continue
            body = (comment.get('body') or '')
            if not body.strip() or body.lstrip().startswith('<!-- agent-'):
                dismiss.add(('issue_comment', source_id))
                continue
            actionable.append(Item('comment', source_id, login, body, association,
                                   None, comment.get('html_url')))
        actionable.sort(key=lambda i: i.sort_key)
        return actionable, dismiss
