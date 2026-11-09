"""One GitHub issue number, kept identical through a whole work item (W6).

Incident A.3 / B-4: AO names a worktree branch from the *spawn prompt text*,
and nothing pinned the issue number to that name. A generic prompt
("Implement GitHub issue mrmap-community/mrmap#682 …") produced
``ao/implement-github-issue-5/root`` for issue **682**, ``-6`` for **948** and
``-7`` for **981** -- the trailing digit is an artifact of the prompt slug, not
an issue. Anything downstream that grepped ``issue[-_]?(\\d+)`` out of the
branch then "discovered" issues #5, #6 and #7, which had been closed since
2021, and wrote claims and a board row onto them.

The rule this module enforces:

* the number the controller was assigned is carried explicitly -- into the
  branch it asks AO to create, into the spawn prompt, and into the claim row;
* a branch only ever *declares* an issue when a path **segment starts with**
  ``issue-<digits>`` or ``#<digits>`` (``ao/issue-981-mapper``,
  ``feature/issue-831-frontend``). A number buried inside a prose slug is a
  word, not a reference, so ``ao/implement-github-issue-5/root`` declares
  nothing. This is the same rule ``scripts/wip`` adopted on the repository
  side, so the two tools cannot disagree about what a branch means;
* a declaration that contradicts the assignment is an ambiguity, and an
  ambiguity is reported, never resolved by guessing.
"""
import re

#: Branches this controller asks AO to create start with this segment.
BRANCH_PREFIX = 'ao/issue-'

#: A branch segment that starts with ``issue-<digits>`` (or ``#<digits>``)
#: declares that issue. Anchored at a segment start on purpose: the incident's
#: slug ``implement-github-issue-5`` contains the same letters and means
#: something else entirely.
#:
#: This is the same pattern ``scripts/wip`` uses on the repository side
#: (``r"(?:^|/)(?:issue[-_]?|#)(\d+)"``) and it is kept byte-for-byte
#: identical: the controller and the board tool have to agree about what a
#: branch means, or they write two different stories about one work item.
BRANCH_ISSUE_RE = re.compile(r'(?:^|/)(?:issue[-_]?|#)(\d+)')

#: Slug length in a generated branch name, so the branch stays readable.
SLUG_MAX = 40

_WORD_RE = re.compile(r'[^a-z0-9]+')


def issue_from_branch(branch):
    """The issue number a branch declares, or None when it declares none.

    ``ao/issue-981-mapper`` -> 981; ``ao/issue-981`` -> 981;
    ``feature/issue-831-frontend`` -> 831;
    ``ao/implement-github-issue-5/root`` -> None (a prose slug, not a
    reference); ``ao/polling-resource/root`` -> None.
    """
    if not branch:
        return None
    match = BRANCH_ISSUE_RE.search(str(branch))
    return int(match.group(1)) if match else None


def slug(title, limit=SLUG_MAX):
    """A short, lower-case, ASCII-only branch slug built from an issue title."""
    words = [w for w in _WORD_RE.split(str(title or '').lower()) if w]
    slug = '-'.join(words)
    if len(slug) > limit:
        slug = slug[:limit].rstrip('-')
        words = slug.split('-')
        # never end a branch on a truncated word: cut back to the last whole one
        slug = '-'.join(words[:-1]) if len(words) > 1 else (words[0] if words else 'work')
    return slug or 'work'


def branch_for(issue, title=''):
    """The branch the controller asks AO for an issue: ``ao/issue-<N>-<slug>``.

    The number comes first and comes from the assignment, never from the text,
    so the branch is a statement about identity and not a prompt echo.
    """
    number = int(issue)
    if number < 1:
        raise ValueError(f'not an issue number: {issue!r}')
    return f'{BRANCH_PREFIX}{number}-{slug(title)}'


def verify_assignment(issue, branch):
    """Whether a branch is compatible with an assigned issue.

    ``(True, reason)`` when the branch agrees with the assignment or declares
    nothing (a pre-W6 session keeps working: compatibility is deliberate), and
    ``(False, reason)`` when it declares a *different* issue -- which is the
    contradiction that put work on closed 2021 issues, and the controller must
    not act on it.
    """
    declared = issue_from_branch(branch)
    if declared is None:
        return True, None
    if declared == int(issue):
        return True, f'branch {branch} declares issue {declared}'
    return False, (f'branch {branch} declares issue {declared}, but the '
                   f'assignment is issue {issue}; refusing to act on either')
