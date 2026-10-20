"""Request-local execution metadata, shared safely with async child tasks."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RequestExecution:
    transports: list[str] = field(default_factory=list)
    cache_status: str | None = None

    def metadata(self) -> dict[str, Any]:
        return {
            "transport": self.transports[-1] if self.transports else "none",
            "transport_attempts": list(self.transports),
            "cache_status": self.cache_status,
        }


request_execution: ContextVar[RequestExecution | None] = ContextVar("request_execution", default=None)


def mark_transport(transport: str) -> None:
    execution = request_execution.get()
    if execution is not None and (not execution.transports or execution.transports[-1] != transport):
        execution.transports.append(transport)


def mark_cache_source(transports: tuple[str, ...], cache_status: str) -> None:
    execution = request_execution.get()
    if execution is not None:
        for transport in transports:
            mark_transport(transport)
        execution.cache_status = cache_status
