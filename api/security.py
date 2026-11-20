"""API-key auth (constant-time compare), per-key rate limiting and request size limits."""

from __future__ import annotations

import hashlib
import hmac
import threading
import time
from collections import defaultdict, deque

from fastapi import Depends, HTTPException, Request, Security
from fastapi.security import APIKeyHeader

from api.settings import ApiSettings, get_api_settings

_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def _match(candidate: str, keys: list[str]) -> bool:
    ok = False
    for k in keys:  # no early exit: timing does not reveal which key matched
        ok |= hmac.compare_digest(candidate.encode(), k.encode())
    return ok


class RateLimiter:
    """Sliding one-minute window per caller. In-memory: fine for one process; use Redis behind many."""

    def __init__(self, clock=time.monotonic):
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()
        self._clock = clock

    def check(self, caller: str, limit: int) -> None:
        now = self._clock()
        with self._lock:
            q = self._hits[caller]
            while q and now - q[0] >= 60.0:
                q.popleft()
            if len(q) >= limit:
                retry = max(1, int(60 - (now - q[0])))
                raise HTTPException(
                    status_code=429,
                    detail=f"Too many requests. Try again in about {retry} seconds.",
                    headers={"Retry-After": str(retry)},
                )
            q.append(now)


def get_rate_limiter(request: Request) -> RateLimiter:
    return request.app.state.rate_limiter


def require_key(
    request: Request,
    key: str | None = Security(_header),
    cfg: ApiSettings = Depends(get_api_settings),
    limiter: RateLimiter = Depends(get_rate_limiter),
) -> str:
    """Returns the caller id (a short key fingerprint, never the key itself)."""
    if cfg.api_auth_disabled:
        caller = "anonymous"
    else:
        if not cfg.keys:
            raise HTTPException(
                status_code=503,
                detail="The API has no keys configured. Set API_KEYS in .env (comma-separated) and restart.",
            )
        if not key or not _match(key, cfg.keys):
            raise HTTPException(status_code=401, detail="Missing or invalid API key (X-API-Key header).")
        caller = "key-" + hashlib.sha256(key.encode()).hexdigest()[:8]
    limiter.check(caller, cfg.api_rate_limit_per_minute)
    request.state.caller = caller
    return caller


def require_admin(
    key: str | None = Security(_header),
    cfg: ApiSettings = Depends(get_api_settings),
    _caller: str = Depends(require_key),
) -> str:
    if not cfg.admin_keys:
        raise HTTPException(status_code=403, detail="Feedback review is disabled (no API_ADMIN_KEYS set).")
    if not key or not _match(key, cfg.admin_keys):
        raise HTTPException(status_code=403, detail="This action needs an admin API key.")
    return _caller
