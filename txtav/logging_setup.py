"""Structured JSON logging with mandatory redaction.

This process holds a live authenticated session for a business account whose
access the company depends on. A cookie or a one time code reaching a log file
is a real incident, not a tidiness problem, so redaction runs as a processor on
every record rather than being left to call sites.

The filter is deliberately blunt. A redacted value that did not need redacting
costs nothing; a leaked session cookie costs the account.
"""

from __future__ import annotations

import logging
import logging.handlers
import re
from pathlib import Path
from typing import Any

import structlog

REDACTED = "[REDACTED]"

# Key names whose value is replaced wholesale, matched case insensitively
# anywhere in the key.
SENSITIVE_KEYS = (
    "cookie",
    "set-cookie",
    "authorization",
    "auth",
    "password",
    "passwd",
    "secret",
    "token",
    "session",
    "credential",
    "otp",
    "2fa",
    "mfa",
    "webhook",
)

# Patterns scrubbed from free text, wherever they appear.
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Cookie headers and name=value pairs for known session cookie shapes.
    (re.compile(r"(?i)\b(set-cookie|cookie)\s*:\s*[^\n\r]+"), r"\1: " + REDACTED),
    (re.compile(r"(?i)\b(ASP\.NET_SessionId|__RequestVerificationToken|\.AspNet\.\w+)=[^;\s]+"),
     r"\1=" + REDACTED),
    # Bearer and basic credentials.
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 " + REDACTED),
    # Anything that looks like a password or token assignment.
    (re.compile(r"(?i)\b(password|passwd|secret|api[_-]?key|token)\s*[=:]\s*\S+"),
     r"\1=" + REDACTED),
    # Email addresses: the portal account and the shared mailbox are both
    # personal data and both identify the client.
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), REDACTED),
    # One time codes, matched only next to a word that announces one. The
    # portal's codes are six digits, but so are 2,930 of the part numbers and
    # the integer part of most prices, so a bare digit run cannot be the
    # trigger: redacting those would make the logs useless for the one thing
    # they exist for.
    (
        re.compile(
            r"(?i)\b(security\s+code|verification\s+code|auth(?:entication)?\s+code"
            r"|code|otp|pin|2fa|mfa)\b([^\n\r]{0,24}?)\b\d{4,8}\b"
        ),
        r"\1\2" + REDACTED,
    ),
)


def scrub_text(value: str) -> str:
    """Apply every redaction pattern to a string."""
    for pattern, replacement in _PATTERNS:
        value = pattern.sub(replacement, value)
    return value


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in SENSITIVE_KEYS)


def _scrub(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return value
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, dict):
        return {
            k: REDACTED if _is_sensitive_key(str(k)) else _scrub(v, depth + 1)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        scrubbed = [_scrub(v, depth + 1) for v in value]
        return type(value)(scrubbed) if isinstance(value, tuple) else scrubbed
    return value


def redact_processor(
    _logger: Any, _name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """structlog processor that redacts every key and value in the record."""
    return {
        key: REDACTED if _is_sensitive_key(str(key)) else _scrub(value)
        for key, value in event_dict.items()
    }


# A 14 day run at one part every 11 seconds writes roughly 110,000 records.
# Rotation is sized so a stoppage can still be traced back through several
# days of history without the directory growing without limit.
LOG_MAX_BYTES = 20 * 1024 * 1024
LOG_BACKUPS = 10


def configure(log_dir: Path | str | None = None, level: str = "INFO") -> None:
    """Install JSON logging to stderr, and to a rotating file when given a dir."""
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_dir is not None:
        directory = Path(log_dir)
        directory.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                directory / "txtav.log",
                maxBytes=LOG_MAX_BYTES,
                backupCount=LOG_BACKUPS,
                encoding="utf-8",
            )
        )

    logging.basicConfig(
        format="%(message)s",
        level=getattr(logging, level.upper(), logging.INFO),
        handlers=handlers,
        force=True,
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact_processor,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "txtav") -> Any:
    return structlog.get_logger(name)
