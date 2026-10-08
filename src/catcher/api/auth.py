"""API keys: the `API_KEYS` setting and the dependency that checks a request's Bearer key and scope.

No message, log line or repr here ever holds a key or a part of one."""

import hashlib
import hmac
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

SCOPES = frozenset({"read", "run"})
MIN_KEY_LENGTH = 24
_NAME = re.compile(r"[a-z0-9_-]{1,32}")
_KEY = re.compile(r"[\x21-\x7e]+")  # printable ASCII without spaces: safe in a header and in the setting
FORMAT = "name:scope[,scope]:key"


@dataclass(frozen=True)
class ApiKey:
    name: str
    scopes: frozenset[str]
    secret: str = field(repr=False)


def parse_api_keys(raw: str) -> list[ApiKey]:
    """Parse `API_KEYS`. Raises ValueError naming the problem and the entry number, never the key."""
    entries = raw.split()
    if not entries:
        raise ValueError(f"API_KEYS has no keys: set at least one {FORMAT} entry")
    keys: list[ApiKey] = []
    names: set[str] = set()
    secrets: set[str] = set()
    for number, entry in enumerate(entries, start=1):
        where = f"API_KEYS entry {number}"
        parts = entry.split(":", 2)
        if len(parts) != 3:
            raise ValueError(f"{where} is not {FORMAT}")
        name, scope_text, secret = parts
        if not _NAME.fullmatch(name):
            raise ValueError(f"{where}: the name must be 1 to 32 of a-z, 0-9, _ and -")
        scopes = scope_text.split(",")
        if not all(scope in SCOPES for scope in scopes):
            raise ValueError(f"{where} ({name}): every scope must be one of {', '.join(sorted(SCOPES))}")
        if len(secret) < MIN_KEY_LENGTH:
            raise ValueError(f"{where} ({name}): the key must be at least {MIN_KEY_LENGTH} characters")
        if not _KEY.fullmatch(secret):
            raise ValueError(f"{where} ({name}): the key must be printable ASCII without spaces")
        if name in names:
            raise ValueError(f"{where}: duplicate name {name}")
        if secret in secrets:
            raise ValueError(f"{where} ({name}): duplicate key (the same key is configured twice)")
        names.add(name)
        secrets.add(secret)
        keys.append(ApiKey(name=name, scopes=frozenset(scopes), secret=secret))
    return keys


def _digest(value: bytes) -> bytes:
    return hashlib.sha256(value).digest()


def match_key(keys: Sequence[ApiKey], supplied: str | None) -> ApiKey | None:
    """The configured key equal to `supplied`, or None. Compares fixed-length digests in constant time
    against every key, without stopping at a match, so the time taken tells nothing about which key or
    how much of one matched."""
    # surrogatepass: any text encodes (never a 500), and two different texts never give the same bytes
    supplied_digest = _digest((supplied or "").encode("utf-8", errors="surrogatepass"))
    found: ApiKey | None = None
    for key in keys:
        if hmac.compare_digest(supplied_digest, _digest(key.secret.encode("ascii"))) and found is None:
            found = key
    return found


# auto_error=False: a missing or malformed header comes back as None and gets our own 401 below.
bearer = HTTPBearer(scheme_name="bearer", auto_error=False)

NOT_AUTHENTICATED = "not authenticated"


def require(scope: str) -> Callable[..., ApiKey]:
    """A FastAPI dependency: the request's key when it is configured and has `scope`; 401 for a missing,
    malformed or unknown key, 403 for a key without the scope."""
    if scope not in SCOPES:
        raise ValueError(f"unknown scope {scope!r}")

    def dependency(
        request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]
    ) -> ApiKey:
        keys: Sequence[ApiKey] = request.app.state.api_keys
        supplied = credentials.credentials if credentials is not None else None
        if len(request.headers.getlist("authorization")) > 1:  # ambiguous: a proxy may read the other one
            supplied = None
        key = match_key(keys, supplied)
        if key is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=NOT_AUTHENTICATED,
                headers={"WWW-Authenticate": "Bearer"},
            )
        if scope not in key.scopes:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail=f"this key does not have the {scope} scope"
            )
        return key

    return dependency
