"""Per-client rate limiting for the public sign-in / sign-up endpoints.

A sliding one-minute window per (client address, endpoint group), kept in memory.
RATE_LIMIT_AUTH_PER_MINUTE sets the limit (default 20; 0 turns limiting off).

The "signup" group is stricter: RATE_LIMIT_SIGNUP_PER_HOUR (default 10 per client per
hour; 0 turns it off).

Limits are per API process. With several workers or servers, put the same limit in
front of them as well (reverse proxy / API gateway).

Behind a reverse proxy every request arrives from the proxy's address, so set
TRUSTED_PROXY_COUNT to the number of proxies in front of the API (usually 1). The
client is then the address the outermost trusted proxy saw in X-Forwarded-For. Leave
it at 0 (default) when clients connect directly: the header is then ignored, because
a client could forge it.
"""

import os
import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request, status

_WINDOW_SECONDS = 60.0
_hits: dict[tuple[str, str], deque] = defaultdict(deque)
_lock = threading.Lock()


_GROUPS = {  # group -> (environment variable, default, window in seconds)
    "signup": ("RATE_LIMIT_SIGNUP_PER_HOUR", 10, 3600.0),
}


def _int_env(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def client_address(request: Request) -> str:
    """The real client: the socket peer, or the address a trusted proxy reported."""
    peer = request.client.host if request.client else "unknown"
    proxies = _int_env("TRUSTED_PROXY_COUNT", 0)
    if proxies:
        hops = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
        if len(hops) >= proxies:
            return hops[-proxies]
    return peer


def reset() -> None:
    with _lock:
        _hits.clear()


def limit(group: str):
    """FastAPI dependency: allow a group's calls per client per window (see module doc)."""
    env, default, window_seconds = _GROUPS.get(group, ("RATE_LIMIT_AUTH_PER_MINUTE", 20, _WINDOW_SECONDS))

    def dependency(request: Request) -> None:
        allowed = _int_env(env, default)
        if not allowed:
            return
        client = client_address(request)
        now = time.monotonic()
        with _lock:
            window = _hits[(client, group)]
            while window and now - window[0] > window_seconds:
                window.popleft()
            if len(window) >= allowed:
                retry = max(1, int(window_seconds - (now - window[0])) + 1)
                raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                                    "Too many attempts. Please wait and try again.",
                                    headers={"Retry-After": str(retry)})
            window.append(now)
            if len(_hits) > 50_000:  # drop idle clients so memory stays bounded
                for key in [k for k, v in _hits.items() if not v or now - v[-1] > 3600.0]:
                    del _hits[key]

    return dependency
