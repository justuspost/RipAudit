"""Security helpers: password hashing, redaction, path confinement, URL validation."""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import os
import re
import secrets
from pathlib import Path
from urllib.parse import urlsplit

# scrypt parameters: N=2**15, r=8, p=1 (~32 MiB, OWASP-acceptable).
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**15, 8, 1
_MAXMEM = 64 * 1024 * 1024


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
                        maxmem=_MAXMEM, dklen=32)
    salt_b64, dk_b64 = base64.b64encode(salt).decode(), base64.b64encode(dk).decode()
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt_b64}${dk_b64}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, n, r, p, salt_b64, dk_b64 = encoded.split("$")
        if algo != "scrypt":
            return False
        expected = base64.b64decode(dk_b64)
        dk = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt_b64), n=int(n), r=int(r),
                            p=int(p), maxmem=_MAXMEM, dklen=len(expected))
        return hmac.compare_digest(dk, expected)
    except (ValueError, TypeError):
        return False


def password_problems(password: str) -> list[str]:
    problems = []
    if len(password) < 12:
        problems.append("Password must be at least 12 characters.")
    if password.lower() in {"password1234", "ripaudit1234", "123456789012"}:
        problems.append("Password is too common.")
    return problems


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def tokens_match(a: str | None, b: str | None) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(str(a), str(b))


# ----- redaction --------------------------------------------------------
_TOKEN_PATTERNS = [
    re.compile(r"(X-Plex-Token=)[^&\s\"']+", re.I),
    re.compile(r"(api_key=)[^&\s\"']+", re.I),
    re.compile(r"(Authorization:\s*Bearer\s+)\S+", re.I),
    re.compile(r"(\"?(?:token|credential|password|api_key)\"?\s*[:=]\s*\"?)[^\"\s,}]+", re.I),
]


def redact(text: str | None, secrets_list: list[str] | None = None) -> str:
    if not text:
        return text or ""
    out = str(text)
    for value in secrets_list or []:
        if value and len(value) >= 4:
            out = out.replace(value, "[REDACTED]")
    for pattern in _TOKEN_PATTERNS:
        out = pattern.sub(r"\1[REDACTED]", out)
    return out


# ----- filesystem confinement -------------------------------------------
def resolve_within(path: str | Path, roots: list[str]) -> Path | None:
    """Return the real path if it is inside one of the real media roots, else None.

    Resolving symlinks first defeats both ``..`` traversal and symlink escape.
    """
    try:
        real = Path(os.path.realpath(path))
    except (OSError, ValueError):
        return None
    for root in roots:
        try:
            real_root = Path(os.path.realpath(root))
        except (OSError, ValueError):
            continue
        if real == real_root or real_root in real.parents:
            return real
    return None


# ----- outbound URL validation ------------------------------------------
def validate_service_url(url: str) -> list[str]:
    """Validate an admin-configured service URL (Plex, ntfy, webhook).

    Private/LAN addresses are allowed on purpose (Plex usually lives on the LAN);
    only scheme, embedded credentials, and obviously unusable hosts are rejected.
    """
    problems: list[str] = []
    if not url:
        return problems
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        problems.append("URL must start with http:// or https://.")
    if parts.username or parts.password:
        problems.append("Do not embed credentials in the URL; use the token field instead.")
    if not parts.hostname:
        problems.append("URL must include a host name.")
    else:
        try:
            ip = ipaddress.ip_address(parts.hostname)
            if ip.is_multicast or ip.is_unspecified:
                problems.append("URL host is not a usable address.")
            if ip.is_link_local:
                problems.append("Link-local addresses (for example cloud metadata services) are not allowed.")
        except ValueError:
            pass
    if parts.query and re.search(r"token|api_key", parts.query, re.I):
        problems.append("Do not put tokens in the URL query; use the token field instead.")
    return problems


def csv_safe(value: object) -> object:
    """Neutralize spreadsheet formula injection in CSV exports."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value
