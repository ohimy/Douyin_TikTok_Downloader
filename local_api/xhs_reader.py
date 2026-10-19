"""Automatic selection of the HTTP and page-interaction XHS readers."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from local_api.xhs_runtime import XhsRiskControlError, XhsSessionExpiredError


class XhsAutoReader:
    """One fallback per read, with temporary per-account/operation preferences.

    The supplied runner applies the same service-wide pace and circuit breaker
    to each attempt. Risk challenges always propagate without trying another
    channel immediately; the next eligible read can use the browser instead.
    """

    def __init__(self, runner: Callable[..., Awaitable[Any]]) -> None:
        self.runner = runner
        self._preferences: dict[tuple[str, str], float] = {}
        self._last: dict[tuple[str, str], str] = {}

    def reset(self, account_id: str) -> None:
        for table in (self._preferences, self._last):
            for key in list(table):
                if key[0] == account_id:
                    table.pop(key, None)

    def status(self) -> dict[str, Any]:
        now = time.monotonic()
        return {
            "mode": "automatic",
            "accounts": [
                {
                    "account_id": account_id,
                    "operation": operation,
                    "last_transport": transport,
                    "preferred_transport": "browser" if self._preferences.get((account_id, operation), 0) > now else "http",
                }
                for (account_id, operation), transport in self._last.items()
            ],
        }

    async def read(
        self,
        account_id: str,
        operation: str,
        *,
        http: Callable[[], Awaitable[dict[str, Any]]],
        browser: Callable[[], Awaitable[dict[str, Any]]],
        browser_only: bool = False,
    ) -> dict[str, Any]:
        key = (account_id, operation)
        prefer_browser = browser_only or self._preferences.get(key, 0) > time.monotonic()
        order = [("browser", browser), ("http", http)] if prefer_browser else [("http", http), ("browser", browser)]
        if browser_only:
            order = order[:1]
        for index, (transport, loader) in enumerate(order):
            self._last[key] = transport
            try:
                return await self.runner(loader)
            except XhsSessionExpiredError:
                raise
            except Exception as exc:
                # The gate wraps risk replies in its own cooldown error. Walk
                # the cause chain so a challenge can never trigger a fallback.
                cause: BaseException | None = exc
                while cause is not None:
                    if isinstance(cause, XhsRiskControlError):
                        delay = float(getattr(exc, "retry_after_seconds", 0))
                        self._preferences[key] = time.monotonic() + delay + 900
                        raise exc
                    cause = cause.__cause__
                if hasattr(exc, "retry_after_seconds"):
                    raise
                if not isinstance(exc, (RuntimeError, ValueError)) or index + 1 == len(order):
                    raise
                if transport == "http":
                    self._preferences[key] = time.monotonic() + 900
                else:
                    self._preferences.pop(key, None)
        raise RuntimeError("小红书读取失败。")
