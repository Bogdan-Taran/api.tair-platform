"""Single-process login throttle; use an edge limiter when adding API replicas."""

import time
from collections import defaultdict, deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


class AuthThrottle(BaseHTTPMiddleware):
    def __init__(self, app):
        super().__init__(app)
        self.attempts = defaultdict(deque)

    async def dispatch(self, request, call_next):
        if request.url.path == "/api/v1/auth/login" and request.method == "POST":
            now = time.monotonic()
            # Keep the map bounded even when many source addresses are used.
            if len(self.attempts) > 10000:
                self.attempts = defaultdict(
                    deque, {k: v for k, v in self.attempts.items() if v and now - v[-1] < 60}
                )
                if len(self.attempts) > 10000:
                    return JSONResponse(
                        {"detail": "Try again later"}, status_code=429, headers={"Retry-After": "60"}
                    )
            key = request.client.host if request.client else "unknown"
            window = self.attempts[key]
            while window and now - window[0] >= 60:
                window.popleft()
            if len(window) >= 20:
                return JSONResponse(
                    {"detail": "Too many login attempts"}, status_code=429, headers={"Retry-After": "60"}
                )
            window.append(now)
        return await call_next(request)
