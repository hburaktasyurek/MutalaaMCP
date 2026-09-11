"""In-process async singleflight keyed by cache key."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Hashable


class SingleFlight[T]:
    """Coalesce concurrent awaitables that share a key into one in-flight call."""

    def __init__(self) -> None:
        self._guard = asyncio.Lock()
        self._inflight: dict[Hashable, asyncio.Task[T]] = {}

    async def do(self, key: Hashable, factory: Callable[[], Awaitable[T]]) -> T:
        async with self._guard:
            task = self._inflight.get(key)
            if task is None:
                task = asyncio.create_task(self._run(key, factory))
                self._inflight[key] = task
        return await asyncio.shield(task)

    async def _run(self, key: Hashable, factory: Callable[[], Awaitable[T]]) -> T:
        try:
            return await factory()
        finally:
            async with self._guard:
                current = self._inflight.get(key)
                if current is not None and current is asyncio.current_task():
                    del self._inflight[key]
