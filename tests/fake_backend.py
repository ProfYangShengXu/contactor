"""假 backend：不接任何真 agent，把协议语义跑通。

三种剧本：正常完成 / 中途要放行 / 中途崩。
"""
from __future__ import annotations
import asyncio, uuid
from typing import AsyncIterator
from contactor.domain.models import (AgentCard, Artifact, Message, Part,
                                     Task, TaskEvent, TaskState)
from contactor.domain.errors import BackendFailure


class FakeBackend:
    def __init__(self, name="fake", script="ok", delay=0.0):
        self._name = name
        self.script = script
        self.delay = delay
        self.calls: list[tuple[str, str]] = []      # (taskId, text)
        self._pending: dict[str, str] = {}

    @property
    def name(self) -> str:
        return self._name

    async def card(self) -> AgentCard:
        return AgentCard(name=self._name, description="fake")

    async def submit(self, task: Task, message: Message, ctx) -> AsyncIterator[TaskEvent]:
        self.calls.append((task.taskId, self._text(message)))
        yield TaskEvent(kind="status", taskId=task.taskId, state=TaskState.WORKING)
        if self.delay:
            await asyncio.sleep(self.delay)

        if self.script == "ok":
            yield TaskEvent(kind="artifact", taskId=task.taskId,
                            artifact=Artifact(artifactId="a1", name="out",
                                              parts=[Part(kind="text", text="done: " + self._text(message))]))
            yield TaskEvent(kind="status", taskId=task.taskId,
                            state=TaskState.COMPLETED, final=True)

        elif self.script == "ask":
            self._pending[task.taskId] = self._text(message)
            yield TaskEvent(kind="status", taskId=task.taskId,
                            state=TaskState.INPUT_REQUIRED, final=True,
                            message=Message(role="agent", messageId="q",
                                            parts=[Part(kind="text", text="放行吗？")],
                                            taskId=task.taskId))

        elif self.script == "boom":
            raise BackendFailure("脚本要求的失败", retryable=True, detail="fake")

    async def resume(self, task: Task, answer: Message) -> AsyncIterator[TaskEvent]:
        self.calls.append((task.taskId, "ANSWER:" + self._text(answer)))
        yield TaskEvent(kind="status", taskId=task.taskId, state=TaskState.WORKING)
        yield TaskEvent(kind="artifact", taskId=task.taskId,
                        artifact=Artifact(artifactId="a2", name="out",
                                          parts=[Part(kind="text", text="answered: " + self._text(answer))]))
        yield TaskEvent(kind="status", taskId=task.taskId,
                        state=TaskState.COMPLETED, final=True)

    async def cancel(self, task: Task) -> None:
        raise NotImplementedError

    @staticmethod
    def _text(m: Message) -> str:
        return "".join(p.text or "" for p in m.parts if p.kind == "text")
