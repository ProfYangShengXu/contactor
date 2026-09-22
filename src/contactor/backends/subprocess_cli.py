"""兜底 backend：什么协议都不会的 agent。

⚠️ 这是【二等公民】。能力缺口如实列在这里，也如实写进 README，不假装支持：

    ❌ 无流式      —— 进程跑完才有输出，中途看不到进展
    ❌ 无权限征求  —— 没人能被问「这条命令放行吗」。agent 若等输入，
                      只会挂到 timeout（这是本 backend 最大的坑）
    ❌ 无会话复用  —— 每次委托起新进程，上下文攒不起来
    ✅ 只能：喂 prompt 进 stdin（或当参数），收 stdout 当 Artifact，
             非零退出码当 FAILED

它存在的意义：让「不会讲 ACP 的 agent」也能挂到桥上，
而不是让它变得好用。
"""
from __future__ import annotations
import asyncio, os, uuid
from typing import AsyncIterator

from ..domain.errors import BackendFailure
from ..domain.models import (AgentCard, Artifact, Message, Part, Skill, Task,
                             TaskEvent, TaskState)
from ..ports import BackendContext


class SubprocessCliBackend:
    """把一次性命令行程序当成 agent。

    它【不】产出 INPUT_REQUIRED —— 协议上它没地方说"我需要人裁决"。
    所以 resume() 永远不该被调到；真调到了说明上游状态机出了问题。
    """

    def __init__(self, name: str, command: list[str] | None = None, *,
                 cwd: str | None = None, workspace: str | None = None,
                 timeout_s: int = 1800, prompt_via: str = "stdin",
                 prompt_flag: list[str] | None = None,
                 card_override: dict | None = None):
        if not command:
            raise ValueError(f"subprocess_cli agent {name!r} 必须给 command")
        self._name = name
        self._command = list(command)
        self._cwd = cwd
        self._workspace = workspace
        self._timeout_s = timeout_s
        self._prompt_via = prompt_via
        self._prompt_flag = list(prompt_flag or [])
        self._card_override = card_override or {}
        self._procs: dict[str, asyncio.subprocess.Process] = {}

    # ── 端口实现 ────────────────────────────────────────────────
    @property
    def name(self) -> str:
        return self._name

    async def card(self) -> AgentCard:
        """★ 名片上如实写清能力缺口 —— 契约要说实话。

        委托方在看到「不支持中断」时就该知道：这个 agent 不能用来做
        需要逐步放行的任务。
        """
        base = dict(
            name=self._name,
            description=f"命令行兜底 agent（无流式 / 无中断 / 无会话复用）",
            url=f"cli://{self._name}",
            version="0.1.0",
            capabilities={
                "streaming": False,          # ← 能力缺口也进名片
                "inputRequired": False,      # ← 最重要的缺口
                "contentVerified": False,
            },
            skills=[Skill(id=self._name, name=self._name,
                          description="一次性命令行调用")],
        )
        base.update(self._card_override)
        return AgentCard(**base)

    async def submit(self, task: Task, message: Message,
                     ctx: BackendContext) -> AsyncIterator[TaskEvent]:
        yield TaskEvent(kind="status", task_id=task.task_id, state=TaskState.WORKING)

        text = self._text_of(message)
        argv = list(self._command)
        stdin_data: bytes | None = text.encode("utf-8")
        if self._prompt_via == "arg":
            argv += self._prompt_flag + [text]
            stdin_data = None

        cwd = self._workspace or self._cwd or ctx.workspace
        env = dict(os.environ)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=cwd, env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
        except FileNotFoundError as e:
            raise BackendFailure(f"命令不存在：{argv[0]}", retryable=False,
                                 detail=str(e)) from e

        self._procs[task.task_id] = proc
        ctx.log(f"subprocess_cli[{self._name}] 起进程 pid={proc.pid}")
        try:
            try:
                out, err = await asyncio.wait_for(
                    proc.communicate(stdin_data), timeout=self._timeout_s)
            except asyncio.TimeoutError as e:
                await self._kill(proc)
                # 超时是【瞬时故障】—— 上层可以重试（要带幂等键）
                raise BackendFailure(
                    f"命令行 agent 超时（{self._timeout_s}s）",
                    retryable=True, detail="可能是 agent 在等标准输入") from e
        finally:
            self._procs.pop(task.task_id, None)

        err_text = err.decode("utf-8", "replace").strip()
        if proc.returncode != 0:
            raise BackendFailure(
                f"命令行 agent 退出码 {proc.returncode}",
                retryable=False,          # 逻辑故障，重试是错的
                detail=(err_text or "<无 stderr>")[-500:])

        out_text = out.decode("utf-8", "replace").strip()

        # ★ 退出码 0 但没输出：桥不判断内容，但要在产出里【留痕】，
        #   不能悄悄返回一个空 Artifact 让委托方以为拿到了东西。
        parts = [Part(kind="text", text=out_text)]
        meta = {"stopReason": "exit_0", "returncode": 0,
                "backend": "subprocess_cli", "capabilities": {
                    "streaming": False, "inputRequired": False}}
        if not out_text:
            meta["warning"] = "进程正常退出但 stdout 为空"
            parts[0] = Part(kind="text", text="")
        if err_text:
            meta["stderr"] = err_text[-2000:]
        parts.append(Part(kind="data", data=meta))

        yield TaskEvent(
            kind="artifact", task_id=task.task_id,
            artifact=Artifact(artifact_id=uuid.uuid4().hex[:12], name="stdout",
                              description=f"{self._name} 的 stdout（requires_review=True）",
                              parts=parts))
        yield TaskEvent(kind="status", task_id=task.task_id,
                        state=TaskState.COMPLETED, is_final=True)

    async def resume(self, task: Task, answer: Message) -> AsyncIterator[TaskEvent]:
        raise BackendFailure(
            f"{self._name} 是命令行兜底 agent，不支持中断与续接"
            f"（协议上它没地方说「我需要人裁决」）",
            retryable=False)
        yield  # pragma: no cover  —— 让它仍是 async generator

    async def cancel(self, task: Task) -> None:
        proc = self._procs.get(task.task_id)
        if proc:
            await self._kill(proc)

    # ── 内部 ────────────────────────────────────────────────────
    @staticmethod
    def _text_of(message: Message) -> str:
        return "".join(p.text or "" for p in message.parts if p.kind == "text")

    @staticmethod
    async def _kill(proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        try:
            proc.kill()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass
