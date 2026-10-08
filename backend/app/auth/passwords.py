"""Password hashing. Only salted scrypt hashes are ever stored."""

import base64
import hashlib
import hmac
import secrets

# OWASP's scrypt minimum is N=2^17, r=8, p=1; that costs 128 MiB per attempt, which the
# API cannot spare per concurrent login. N=2^15 with p=3 is the equivalent-cost variant.
_N, _R, _P = 2**15, 8, 3
_MAXMEM = 64 * 1024 * 1024
MIN_PASSWORD_LENGTH = 8
# Bounds the work an unauthenticated caller can request.
MAX_PASSWORD_LENGTH = 1024


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    return "$".join(("scrypt", str(_N), str(_R), str(_P), _encode(salt),
                     _encode(_derive(password, salt, _N, _R, _P))))


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, expected = stored.split("$")
        if scheme != "scrypt":
            return False
        derived = _derive(password, base64.b64decode(salt), int(n), int(r), int(p))
        return hmac.compare_digest(derived, base64.b64decode(expected))
    except ValueError:
        return False


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
                          maxmem=_MAXMEM, dklen=32)


def _encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


# Verified against when the login is unknown, so both failures take the same time.
UNKNOWN_USER_HASH = hash_password(secrets.token_urlsafe(16))
