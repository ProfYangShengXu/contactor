"""按 agent 名分片：同一 agent 的委托串行，冲突在结构上不存在。"""
from __future__ import annotations
import asyncio
from typing import Awaitable, Callable


class ShardedRunner:
    def __init__(self, concurrency: dict[str, int] | None = None):
        self._sem: dict[str, asyncio.Semaphore] = {}
        self._conf = concurrency or {}
        self._default = 1                      # ★ 默认串行不是保守，是正确

    def _semaphore(self, agent: str) -> asyncio.Semaphore:
        if agent not in self._sem:
            self._sem[agent] = asyncio.Semaphore(
                max(1, self._conf.get(agent, self._default)))
        return self._sem[agent]

    async def run(self, agent: str, fn: Callable[[], Awaitable[None]]) -> None:
        async with self._semaphore(agent):
            await fn()
