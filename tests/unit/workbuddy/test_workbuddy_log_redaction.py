"""T22: secrets must not survive a trip through the log stream.

The row watches two paths -- the normal one and the exceptional one -- so both
are asserted against the *formatted* output rather than the record's fields,
because the traceback is only rendered when a handler formats the record.
"""

from __future__ import annotations

import io
import logging

from octop.infra.workbuddy.log_redaction import (
    RedactingFilter,
    install_log_redaction,
    redact_text,
    register_secret,
)

SECRET = "wb-credential-2f8a41c9d3e04b7ea1f6"  # noqa: S105 - a fixture value
BEARER = "wb-session-9c1d77ab43e5f208"


def _capture(logger: logging.Logger) -> io.StringIO:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return stream


def _logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.filters.clear()
    logger.addFilter(RedactingFilter())
    return logger


def test_registered_secret_never_reaches_the_log() -> None:
    register_secret(SECRET)
    logger = _logger("test.redaction.registered")
    stream = _capture(logger)

    logger.info("stored credential %s for tenant %s", SECRET, "t-1")
    output = stream.getvalue()

    assert SECRET not in output
    assert "wb-c***" in output, output
    assert "t-1" in output, output


def test_secret_shapes_are_masked_without_registration() -> None:
    text = (
        f"sent header Bearer {BEARER}\n"
        "Authorization: Bearer wb-session-2c9d77ab43e5f208\n"
        "connection password=hunter2-correct-horse\n"
        "provider key sk-live-8f2b41c9d3e04b7ea1f6c2\n"
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----"
    )

    masked = redact_text(text)

    for plaintext in (
        BEARER,
        "wb-session-2c9d77ab43e5f208",
        "hunter2-correct-horse",
        "sk-live-8f2b41c9d3e04b7ea1f6c2",
        "MIIEowIBAAKCAQEA",
    ):
        assert plaintext not in masked, masked
    # The shape is kept: an operator can still see *which* thing was masked.
    assert "Bearer ***" in masked, masked  # a bare bearer credential
    assert "Authorization:" in masked, masked  # a labelled one keeps its key
    assert "password=***" in masked, masked
    assert "sk-***" in masked, masked


def test_exception_traceback_is_redacted_too() -> None:
    register_secret(SECRET)
    logger = _logger("test.redaction.exception")
    stream = _capture(logger)

    try:
        raise RuntimeError(f"credential {SECRET} rejected by the backend")
    except RuntimeError:
        logger.exception("secret backend refused the rotation")

    output = stream.getvalue()

    assert "Traceback (most recent call last)" in output, output
    assert SECRET not in output
    assert "wb-c***" in output, output


def test_short_values_are_not_registered_so_ordinary_text_survives() -> None:
    register_secret("abc")

    assert redact_text("abc is a three letter word") == "abc is a three letter word"
    assert redact_text("password=abc") == "password=***"


def test_install_is_idempotent_per_logger() -> None:
    install_log_redaction("test.redaction.install")
    install_log_redaction("test.redaction.install")

    filters = list(logging.getLogger("test.redaction.install").filters)
    assert sum(isinstance(item, RedactingFilter) for item in filters) == 1, filters
