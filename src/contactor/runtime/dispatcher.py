"""★★ 全项目核心：编排。

只认 ports.py 的三个 Protocol，不知道任何具体 backend。
"""
from __future__ import annotations
import asyncio, time, uuid
from dataclasses import dataclass

from ..domain.models import (Artifact, Message, Part, PendingDecision, Task,
                             TaskError, TaskEvent, TaskState)
from ..domain.lifecycle import assert_transition, is_terminal
from ..domain.errors import BackendFailure, TaskNotFound, InvalidParams
from ..domain.decisions import CONTRACT, split_decision


def _id() -> str:
    return uuid.uuid4().hex[:16]


def _perm_pending(text: str) -> PendingDecision:
    """把 ACP 的权限请求包成 PendingDecision（和 decision 走同一条路）。"""
    opts = [o for o in ("allow_once", "reject_once") if o in (text or "")]
    return PendingDecision(kind="permission", question=(text or "").strip(),
                           options=opts)



@dataclass
class _Ctx:
    """BackendContext 的实现。★ 只给必要的东西。"""
    workspace: str
    contextId: str
    _logger: object = None

    def log(self, msg: str) -> None:
        if self._logger:
            self._logger(msg)


class Dispatcher:
    def __init__(self, *, store, backends, registry, shards, sink, config, logger=None):
        self.store, self.backends, self.registry = store, backends, registry
        self.shards, self.sink, self.config = shards, sink, config
        self.log = logger or (lambda *a, **k: None)
        self._running: dict[str, asyncio.Task] = {}

    # ── 对外入口 ────────────────────────────────────────────────

    async def submit(self, agent: str, text: str, contextId: str | None = None,
                     *, messageId: str | None = None,
                     delegationDepth: int = 0,
                     visitedAgents: list[str] | None = None) -> Task:
        """message/send 入口。建 Task，入分片队列，【立即返回】。"""
        # ── 幂等去重 ──────────────────────────────────────────
        if messageId:
            existing = await self.store.find_by_origin_message(messageId)
            if existing is not None:
                self.log(f"duplicate messageId={messageId} -> task {existing.taskId}")
                return existing

        # ── 回环防护（双防线）────────────────────────────────
        visited = list(visitedAgents or [])
        if agent in visited:
            chain = " -> ".join(visited + [agent])
            raise InvalidParams(f"委托链上已出现过 {agent}，拒绝回环（链：{chain}）")
        if delegationDepth >= self.config.max_delegation_depth:
            raise InvalidParams(
                f"委托链过深（{delegationDepth} >= {self.config.max_delegation_depth}），疑似回环")

        backend = self._backend(agent)
        ctx_id = contextId or _id()
        task = Task(taskId=_id(), contextId=ctx_id, traceId=_id(), agent=agent,
                    delegationDepth=delegationDepth,
                    visitedAgents=visited + [agent],
                    originMessageId=messageId,
                    createdAt=time.time(), updatedAt=time.time())
        await self.store.create(task)

        # ★ 把「需要拍板时怎么标」的契约附在出站 prompt 末尾。
        #   不附的话，decisions.py 里那套标记就没有读者 —— 定义得再漂亮也是注释。
        #   （判据：「任何声明都必须有一条把它送到读者手里的路径。」）
        out_text = text
        if self.config.append_decision_contract:
            out_text = text + "\n" + CONTRACT

        msg = Message(role="user", parts=[Part(kind="text", text=out_text)],
                      messageId=_id(), taskId=task.taskId, contextId=ctx_id)
        await self.store.append_message(task.taskId, msg)

        self._spawn(task, backend, msg, resume=False)
        return task

    async def answer(self, taskId: str, text: str) -> Task:
        """委托方回答 input-required。★ 本地场景最常走的路。"""
        task = await self.store.get(taskId)
        if task is None:
            raise TaskNotFound(taskId)
        if task.state != TaskState.INPUT_REQUIRED:
            raise InvalidParams(
                f"task {taskId} 当前是 {task.state.value}，不是 input-required"
                + (f"（error: {task.error}）" if task.error else ""))

        # ★ 把「委托方在回答什么」一并挂进消息里。
        #   命令行 backend 没有会话，resume 只能靠历史重建 prompt ——
        #   不带上这个，它就不知道自己上一轮问的是什么、选项有哪些。
        parts = [Part(kind="text", text=text)]
        if task.pending is not None:
            parts.append(Part(kind="data", data={
                "pending": task.pending.model_dump(), "note": "委托方在回答这个"}))
        ans = Message(role="user", parts=parts,
                      messageId=_id(), taskId=taskId, contextId=task.contextId)
        await self.store.append_message(taskId, ans)
        task.pending = None
        await self.store.save(task)

        self._spawn(task, self._backend(task.agent), ans, resume=True)
        return task

    async def cancel(self, taskId: str) -> Task:
        task = await self.store.get(taskId)
        if task is None:
            raise TaskNotFound(taskId)
        if is_terminal(task.state):
            return task
        try:
            await self._backend(task.agent).cancel(task)
        except NotImplementedError:
            pass
        await self._transition(task, TaskState.CANCELED, final=True)
        return task

    # ── 核心循环 ─────────────────────────────────────────────────

    def _spawn(self, task: Task, backend, msg: Message, *, resume: bool) -> None:
        async def body() -> None:
            await self._drive(task, backend, msg, resume=resume)
        t = asyncio.create_task(self.shards.run(task.agent, body))
        self._running[task.taskId] = t
        t.add_done_callback(lambda _: self._running.pop(task.taskId, None))

    async def _drive(self, task: Task, backend, msg: Message, *, resume: bool) -> None:
        ctx = _Ctx(workspace=self.config.workspace_root,
                   contextId=task.contextId, _logger=self.log)
        await self._transition(task, TaskState.WORKING)

        try:
            stream = (backend.resume(task, msg) if resume
                      else backend.submit(task, msg, ctx))
            async for ev in stream:
                if ev.kind == "status" and ev.state is not None:
                    if ev.state == TaskState.INPUT_REQUIRED:
                        # ★★ 同步 → 异步的转换点：落盘 + 推事件 + 【返回】，不阻塞等
                        task.pending = _perm_pending(self._text_of(ev.message))
                        await self._transition(task, TaskState.INPUT_REQUIRED)
                        await self.store.save(task)
                        await self.sink.emit(task.taskId, ev)
                        return
                    if ev.state == TaskState.COMPLETED and ev.final:
                        # ★★ 「它在问我」与「它干完了」在协议上本来长得一样。
                        #     这一处是唯一把它们分开的地方：agent 按契约标了
                        #     [[NEEDS_DECISION]] → 就不是完成，是等人拍板。
                        d = self._decision_in(task)
                        if d is not None and task.pending is None:
                            task.pending = d
                            await self._transition(task, TaskState.INPUT_REQUIRED)
                            await self.store.save(task)
                            await self.sink.emit(task.taskId, TaskEvent(
                                kind="status", taskId=task.taskId,
                                state=TaskState.INPUT_REQUIRED, final=True))
                            return
                    await self._transition(task, ev.state, final=ev.final)
                elif ev.kind == "artifact" and ev.artifact is not None:
                    self._merge_artifact(task, ev.artifact)
                    # ★ 必须立刻落盘！
                    #   2026-09-23 实测踩到：artifact 只改内存，而 store.save() 原本只在
                    #   _transition / finally 里发生 —— 于是「增量推送」在 turn 结束前
                    #   **对 tasks/get 完全不可见**（读库读到的还是 artifacts=0）。
                    #   等于把刚修好的「声明没有读者」换了个形式：产出有了，但没人读得到。
                    #   教训：**产出侧修好只算一半，可见性要另外验一次。**
                    await self.store.save(task)

                if ev.message is not None:
                    await self.store.append_message(task.taskId, ev.message)
                await self.sink.emit(task.taskId, ev)
                if ev.final:
                    break
            else:
                self.log(f"backend 流结束但没给终态，兜底按完成处理: {task.taskId}")
                await self._transition(task, TaskState.COMPLETED, final=True)

        except BackendFailure as e:
            task.error = e.to_task_error(task.taskId, task.traceId)
            await self._transition(task, TaskState.FAILED, final=True)
            await self.sink.emit(task.taskId, TaskEvent(
                kind="status", taskId=task.taskId, state=TaskState.FAILED,
                final=True,
                message=Message(role="agent",
                                parts=[Part(kind="text", text=e.to_message_text())],
                                messageId=_id(), taskId=task.taskId)))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            task.error = TaskError(code="internal_error",
                                   message=f"内部错误：{type(e).__name__}: {e}",
                                   retryable=False, taskId=task.taskId,
                                   correlationId=task.traceId)
            self.log(f"dispatcher 内部错误 {task.taskId}: {e!r}")
            await self._transition(task, TaskState.FAILED, final=True)
        finally:
            await self.store.save(task)

    # ── 工具 ─────────────────────────────────────────────────────

    async def _transition(self, task: Task, to: TaskState, *, final: bool = False) -> None:
        """★ 唯一改 task.state 的地方 —— 所有迁移都过 lifecycle 校验。"""
        if task.state == to:
            return
        assert_transition(task.state, to)
        task.state = to
        task.updatedAt = time.time()
        await self.store.save(task)
        if final:
            await self.sink.emit(task.taskId, TaskEvent(
                kind="status", taskId=task.taskId, state=to, final=True))

    def _backend(self, agent: str):
        b = self.backends.get(agent)
        if b is None:
            raise InvalidParams(f"unknown agent: {agent}（已配：{list(self.backends)}）")
        return b

    @staticmethod
    def _merge_artifact(task: Task, art: Artifact) -> None:
        """★ append=True = 对【同名 artifact】的增量追加（A2A TaskArtifactUpdateEvent 语义）。

        没有这一层的话，一个 10 分钟以上的任务在结束前拿不到任何产出 ——
        而 `Artifact.append` 就会退化成一条【没人产出的声明】。
        （正是 bug 类 ⑪「声明没有读者」：字段在 schema 里，谁都不产它。
          2026-09-23 实跑一条 14 分钟的任务后才发现的。）
        """
        if art.append:
            for existing in task.artifacts:
                if existing.artifactId == art.artifactId:
                    existing.parts.extend(art.parts)
                    return
            # 没有同名 artifact 可追加 → 当成新的（别静默丢内容）
            art.append = False
        task.artifacts.append(art)

    @staticmethod
    def _decision_in(task: Task):
        """扫描已落盘的 artifact，看 agent 有没有按契约标出「需要拍板」。

        ★ 在【终态前】扫，不是在文本流里实时猜。
          切分用的是显式标记，误判率由契约（而不是正则的运气）决定。
        """
        for a in task.artifacts:
            for part in a.parts:
                if part.kind != "text" or not part.text:
                    continue
                body, d = split_decision(part.text)
                if d is not None:
                    part.text = body.strip()     # 产物里不留标记（它是协议，不是交付内容）
                    return d
        return None

    @staticmethod
    def _text_of(msg: Message | None) -> str:
        if msg is None:
            return ""
        return "".join(p.text or "" for p in msg.parts if p.kind == "text")
