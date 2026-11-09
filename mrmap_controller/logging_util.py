"""Structured logging with credential redaction.

Events are JSON lines so an operator can grep or pipe them; human-readable text
goes to stderr. Redaction is applied to every string that can reach a log line
or a database note, because command stderr can echo an authenticated URL.
"""
import json
import logging
import logging.handlers
import re
import sys

_SECRET_PATTERNS = [
    re.compile(r'gh[pousr]_[A-Za-z0-9_]{16,}'),
    re.compile(r'github_pat_[A-Za-z0-9_]{16,}'),
    re.compile(r'x-access-token:[^@\s]+'),
    re.compile(r'(?:https?|ssh)://[^/\s:@]+:([^@\s]+)@'),
    re.compile(r'(?i)\b(?:authorization|token|password|secret)\b\s*[:=]\s*\S+'),
]

MAX_FIELD = 400


def redact(text):
    if text is None:
        return None
    text = str(text)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub('[redacted]', text)
    if len(text) > MAX_FIELD:
        text = text[:MAX_FIELD] + '...[truncated]'
    return text


class JsonLineFormatter(logging.Formatter):
    def format(self, record):
        event = {
            'ts': self.formatTime(record, '%Y-%m-%dT%H:%M:%S%z'),
            'level': record.levelname,
            'event': getattr(record, 'event', record.getMessage()),
            'pid': record.process,
        }
        for key in FIELDS:
            val = getattr(record, key, None)
            if val is not None:
                event[key] = redact(val) if isinstance(val, str) else val
        return json.dumps(event, ensure_ascii=False, sort_keys=True)


FIELDS = ('mode', 'issue', 'pr', 'session', 'sha', 'kind', 'state', 'detail',
          'reason', 'outcome', 'attempt')


class EventLogger:
    """``log.info('SPAWNED', issue=7, session='mrmap-9')`` -> one JSON object.

    Structured fields are passed to :mod:`logging` as ``extra`` so they land on
    the record; the bare-kwargs form would be silently dropped there.
    """

    def __init__(self, logger):
        self._logger = logger

    def log(self, level, event, extra=None, **fields):
        merged = dict(extra or {})
        merged.update({k: v for k, v in fields.items() if k in FIELDS})
        self._logger.log(level, event, extra=merged)

    def debug(self, event, **fields):
        self.log(logging.DEBUG, event, **fields)

    def info(self, event, **fields):
        self.log(logging.INFO, event, **fields)

    def warning(self, event, **fields):
        self.log(logging.WARNING, event, **fields)

    def error(self, event, **fields):
        self.log(logging.ERROR, event, **fields)

    def __getattr__(self, name):
        return getattr(self._logger, name)


def setup(log_dir, verbose=False):
    log_dir = __import__('pathlib').Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger('mrmap-controller')
    logger.setLevel(logging.DEBUG)
    for stale in logger.handlers:
        stale.close()
        logger.removeHandler(stale)
    logger.propagate = False
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / 'controller.jsonl', maxBytes=5 * 1024 * 1024, backupCount=5)
    file_handler.setFormatter(JsonLineFormatter())
    file_handler.setLevel(logging.INFO)
    logger.addHandler(file_handler)
    stream = logging.StreamHandler(sys.stderr)
    stream.setLevel(logging.DEBUG if verbose else logging.INFO)
    stream.setFormatter(JsonLineFormatter())
    logger.addHandler(stream)
    return EventLogger(logger)
