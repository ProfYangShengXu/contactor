"""三个端口。接口定义在【稳定侧】，实现在外面。

🔴 架构红线：本文件不许出现 ACP / session / prompt / stdio / subprocess / 进程 这些词。
   由 tests/test_layering.py 机器化校验。
   判据：把这个文件拿给一个不懂 coding agent 的人看，他能看懂吗？
"""
from __future__ import annotations
from typing import AsyncIterator, Protocol, runtime_checkable
from .domain.models import AgentCard, Message, Task, TaskEvent, TaskState


class BackendContext(Protocol):
    """给 backend 的上下文。只给"这一步要做的判断所需要的东西"。"""
    @property
    def workspace(self) -> str: ...
    @property
    def context_id(self) -> str: ...
    def log(self, msg: str) -> None: ...


@runtime_checkable
class AgentBackend(Protocol):
    """驱动一个 agent 完成一次委托。实现者：backends/*"""

    @property
    def name(self) -> str: ...

    async def card(self) -> AgentCard: ...

    async def submit(self, task: Task, message: Message,
                     ctx: BackendContext) -> AsyncIterator[TaskEvent]:
        """提交一次委托，流式产出事件。

        约定：
          · 第一个事件应是 state=WORKING 的 status
          · 需要人裁决时：产出 state=INPUT_REQUIRED 的 status 事件（is_final=True）
            然后【立即结束迭代器返回】—— 不要阻塞等
          · 最后必须产出一个 is_final=True 的事件
        """
        ...

    async def resume(self, task: Task, answer: Message) -> AsyncIterator[TaskEvent]: ...

    async def cancel(self, task: Task) -> None: ...


@runtime_checkable
class TaskStore(Protocol):
    """Task 元数据持久化。实现者：stores/*"""
    async def create(self, task: Task) -> None: ...
    async def get(self, task_id: str) -> Task | None: ...
    async def save(self, task: Task) -> None: ...
    async def append_message(self, task_id: str, msg: Message) -> None: ...
    async def list_by_state(self, state: TaskState) -> list[Task]: ...
    async def find_by_origin_message(self, message_id: str) -> Task | None: ...


@runtime_checkable
class EventSink(Protocol):
    """把事件推给委托方。实现者：transport/*"""
    async def emit(self, task_id: str, event: TaskEvent) -> None: ...
