"""Verbatim GitHub fixtures for the question/answer tests (W3).

The JSON files in ``tests/fixtures/`` were fetched with
``gh issue view <N> --repo mrmap-community/mrmap --json comments`` on
2026-11-09 and are stored byte-for-byte as GitHub returned them:

* ``1175-comments.json`` -- the ``scripts/decisions`` request
  ``dr-85921e8f`` (jokiefer, blocking Q1-Q3), the human answer
  "Q1: B / Q2: A / Q3: B", and the ``decision-audit`` trail comment the bot
  posted afterwards (a machine comment that must never count as an answer).
* ``831-comments.json`` -- the two machine ``agent-claim`` comments and the
  ``scripts/wip ask`` question ``0b014d9f`` (client ``519ca074bf12:mrmap-70``).
  No human answer existed when this was fetched.

The controller reads comments through the REST endpoint, whose payload shape
differs from ``gh issue view``'s GraphQL shape (numeric ``id``, ``user.login``,
``author_association``, ISO ``created_at``). :func:`rest_comments` maps the
stored bodies, logins, associations and timestamps verbatim into that shape;
only ``user.type`` is supplied here (REST carries it, this fetch did not), set
to ``Bot`` for the bot account exactly as GitHub reports it.
"""
import json
import pathlib

_HERE = pathlib.Path(__file__).resolve().parent
BOT_LOGINS = {'mrmap-agent-bot'}


def raw_comments(issue):
    return json.loads((_HERE / 'fixtures' / f'{issue}-comments.json')
                      .read_text(encoding='utf-8'))


def rest_comment(entry):
    """One stored ``gh issue view`` comment as a REST issue-comment payload."""
    login = entry['author']['login']
    numeric_id = int(entry['url'].rsplit('#issuecomment-', 1)[-1])
    return {
        'id': numeric_id,
        'node_id': entry['id'],
        'body': entry['body'],
        'user': {'login': login, 'type': 'Bot' if login in BOT_LOGINS else 'User'},
        'author_association': entry['authorAssociation'],
        'created_at': entry['createdAt'],
        'updated_at': entry.get('updatedAt'),
        'html_url': entry['url'],
    }


def _find(issue, marker):
    for entry in raw_comments(issue):
        if marker in entry['body']:
            return rest_comment(entry)
    raise AssertionError(f'no {marker} comment in fixture {issue}')


def request_comment(issue):
    """The stored decision-request comment as a REST payload (verbatim body)."""
    return _find(issue, 'decision-request')


def question_comment(issue):
    """The stored scripts/wip agent-question comment as a REST payload."""
    return _find(issue, 'agent-question')


def rest_comments(issue, answers=(), drop=()):
    """All stored comments for an issue (minus ``drop``, numeric ids -- a test
    simulating a *partial* answer leaves the real full answer out), plus
    synthetic human answers.

    Each synthetic answer is a ``(body, login, association)`` tuple placed
    after every stored comment (the human answer of a question always
    postdates it); ``id`` numbering continues past the stored range.
    """
    out = [rest_comment(c) for c in raw_comments(issue)]
    out = [c for c in out if c['id'] not in set(drop)]
    base = max((c['id'] for c in out), default=1000)
    last_ts = max((c['created_at'] for c in out), default='2026-11-09T00:00:00Z')
    for n, (body, login, association) in enumerate(answers, start=1):
        out.append({
            'id': base + n,
            'body': body,
            'user': {'login': login,
                     'type': 'Bot' if login in BOT_LOGINS else 'User'},
            'author_association': association,
            'created_at': last_ts,       # same second, ordered by id like wip
            'updated_at': None,
            'html_url': f'https://github.com/mrmap-community/mrmap/issues/'
                        f'{issue}#issuecomment-{base + n}',
        })
    return out
