"""Keep secret material out of the logs, including the exception path.

The contract's acceptance row T22 observes that sensitive prompts, secrets and
tokens pass through normal *and* exceptional logging without ever appearing in
plaintext. Two things follow from that wording, and this module does both:

* Values the process *knows* to be secret are masked exactly. The credential
  store, the approval tokens, the export redeem tokens and the webhook secrets
  register their material here the moment it is minted, so a later log line
  cannot leak it even when the message is built from arbitrary text.
* Values that merely *look* like secrets are masked by shape: bearer tokens,
  ``key=value`` pairs whose key names a credential, PEM private-key blocks and
  common provider key prefixes.

The filter runs on the logger, not on a handler, so it applies whatever logging
configuration the process ends up with. It also pre-renders ``exc_info`` into a
redacted ``exc_text``, because a traceback is otherwise formatted by the handler
long after the filter has seen the record -- the exception path is exactly the
one the row calls out.
"""

from __future__ import annotations

import logging
import re
import traceback
from typing import Any

#: Values registered at runtime. Kept short deliberately: a length floor keeps a
#: caller from registering a value that would mangle unrelated log text.
_KNOWN_SECRETS: set[str] = set()
_MIN_SECRET_LENGTH = 8

_MASK = "***"
#: The number of leading characters a masked value keeps, so operators can still
#: tell two credentials apart in a log without learning either one.
_PREFIX_KEPT = 4

_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Authorization headers and query strings that carry a bearer credential.
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/-]{8,}=*"),
    # key=value / key: value pairs whose key names a credential.
    re.compile(
        r"(?i)\b(password|passwd|secret|token|api[_-]?key|access[_-]?key|"
        r"client[_-]?secret|private[_-]?key|authorization)\b(\s*[:=]\s*)"
        r"(\"[^\"]*\"|'[^']*'|[^\s,;&\"']+)"
    ),
    # PEM blocks of any kind.
    re.compile(
        r"-----BEGIN [A-Z ]*(?:PRIVATE KEY|CERTIFICATE)[A-Z ]*-----.*?"
        r"-----END [A-Z ]*(?:PRIVATE KEY|CERTIFICATE)[A-Z ]*-----",
        re.DOTALL,
    ),
    # Provider-shaped keys. The prefix is kept: it names the provider, not the key.
    re.compile(r"\b(sk|pk|rk|ghp|gho|xox[baprs])-[A-Za-z0-9_-]{12,}"),
    # Three-part JWTs, which are what a leaked access token looks like in a log.
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
)


def register_secret(value: str | None) -> None:
    """Remember one minted secret so no later log line can print it."""
    if value and len(value) >= _MIN_SECRET_LENGTH:
        _KNOWN_SECRETS.add(value)


def _mask(value: str) -> str:
    if len(value) <= _PREFIX_KEPT:
        return _MASK
    return f"{value[:_PREFIX_KEPT]}{_MASK}"


def redact_text(text: str) -> str:
    """Mask every known secret and every secret-shaped value in ``text``."""
    redacted = text
    # Longest first: a secret that contains another one must not be half-masked.
    for secret in sorted(_KNOWN_SECRETS, key=len, reverse=True):
        if secret in redacted:
            redacted = redacted.replace(secret, _mask(secret))
    for pattern in _PATTERNS:
        redacted = pattern.sub(_replace_match, redacted)
    return redacted


def _replace_match(match: re.Match[str]) -> str:
    text = match.group(0)
    if match.re is _PATTERNS[0]:  # bearer <credential>
        return f"{match.group(1)} {_MASK}"
    if match.re is _PATTERNS[1]:  # key = value
        quoted = match.group(3)[:1] if match.group(3)[:1] in {"'", '"'} else ""
        return f"{match.group(1)}{match.group(2)}{quoted}{_MASK}{quoted}"
    if match.re is _PATTERNS[3]:  # provider prefix-key
        return f"{text.split('-', 1)[0]}-{_MASK}"
    return _MASK


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, tuple):
        return tuple(_redact_value(item) for item in value)
    if isinstance(value, dict):
        return {key: _redact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    return value


class RedactingFilter(logging.Filter):
    """Mask secrets in a record's message, its arguments and its traceback."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_text(record.msg)
        elif record.args:
            record.msg = _redact_value(record.msg)
        if record.args:
            record.args = _redact_value(record.args)
        if record.stack_info:
            record.stack_info = redact_text(record.stack_info)
        if record.exc_info and not record.exc_text:
            # Pre-render so the redaction covers the traceback too; the
            # formatter reuses a non-empty ``exc_text`` instead of re-rendering.
            record.exc_text = redact_text(
                "".join(traceback.format_exception(*record.exc_info)).rstrip("\n")
            )
        return True


_INSTALLED: set[str] = set()


def install_log_redaction(*logger_names: str) -> None:
    """Attach the filter once to each named logger (idempotent per name)."""
    names = logger_names or (
        "",  # the root logger
        "octop",
        "uvicorn",
        "uvicorn.error",
        "uvicorn.access",
    )
    for name in names:
        if name in _INSTALLED:
            continue
        target = logging.getLogger(name)
        if not any(isinstance(item, RedactingFilter) for item in target.filters):
            target.addFilter(RedactingFilter())
        _INSTALLED.add(name)
