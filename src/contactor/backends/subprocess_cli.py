"""兜底 backend：什么协议都不会的 agent。

⚠️ 这是【二等公民】。能力缺口如实列在这里，也如实写进 README，不假装支持：

    ❌ 无流式      —— 进程跑完才有输出，中途看不到进展
    ❌ 不可中断    —— 没人能被问「这条命令放行吗」。它跑起来就是个黑盒，
                      agent 若等标准输入只会挂到 timeout（这是最大的坑）。
    ⚠️ 会停在【回合末尾】要人拍板 —— 靠 decisions.py 的显式契约（不靠猜文本），
                      所以它【能】产出 INPUT_REQUIRED，也【能】resume。
                      ⚠️ 但这两者不一样：它能在干完之后问你选哪个，
                         不能在干【之前】停下等你放行。
    ⚠️ 无会话复用  —— 每次委托起新进程。**续接靠【从历史重建 prompt】**，
                      不是真会话：上一轮进程里的中间状态（读过哪些文件、
                      试过什么）已经没了。是【有损重建】，不是恢复。
    ✅ 只能：喂 prompt 进 stdin（或当参数），收 stdout 当 Artifact，
             非零退出码当 FAILED

它存在的意义：让「不会讲 ACP 的 agent」也能挂到桥上，
而不是让它变得好用。
"""
from __future__ import annotations
import asyncio, os, uuid
from typing import AsyncIterator

from ..domain.errors import BackendFailure
from ..domain.models import (AgentCapabilities, AgentCard, Artifact, Message,
                             Part, Skill, Task, TaskEvent, TaskState)
from ..ports import BackendContext


class SubprocessCliBackend:
    """把一次性命令行程序当成 agent。

    ⚠️ 它能产出 INPUT_REQUIRED，但【只是】回合末尾那种（agent 主动标了契约）。
    它【不能】被中断：危险命令会在你放行之前就跑掉。名片里用 interruptible=False 说明。
    """

    def __init__(self, name: str, command: list[str] | None = None, *,
                 cwd: str | None = None, workspace: str | None = None,
                 timeout_s: int = 1800, prompt_via: str = "stdin",
                 prompt_flag: list[str] | None = None,
                 card_override: dict | None = None,
                 description: str = "", skills: list[Skill] | None = None):
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
        self._description = description
        self._skills = list(skills or [])       # ★ 业务能力由配置给，不自动生成
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
            description=self._description
                        or "命令行兜底 agent（无流式 / 无中断 / 无会话复用）",
            url=f"cli://{self._name}",
            version="0.1.0",
            capabilities=AgentCapabilities(
                streaming=False,            # ← 能力缺口也进名片
                pushNotifications=False,
                stateTransitionHistory=False,
                # ⚠️ 这两个要分开看，混起来会给出错误的适配判断：
                #    inputRequired  —— 会不会【在回合末尾】停下来等人（靠契约，现在能）
                #    interruptible  —— 能不能在【执行中途】被拦下（黑盒，不能）
                inputRequired=True,
                interruptible=False,
                contentVerified=False,
            ),
            skills=self._skills,
        )
        base.update(self._card_override)
        return AgentCard(**base)

    async def submit(self, task: Task, message: Message,
                     ctx: BackendContext) -> AsyncIterator[TaskEvent]:
        async for ev in self._run(task, self._text_of(message), ctx):
            yield ev

    async def _run(self, task: Task, text: str,
                   ctx: BackendContext | None = None) -> AsyncIterator[TaskEvent]:
        yield TaskEvent(kind="status", taskId=task.taskId, state=TaskState.WORKING)
        argv = list(self._command)
        stdin_data: bytes | None = text.encode("utf-8")
        if self._prompt_via == "arg":
            argv += self._prompt_flag + [text]
            stdin_data = None

        cwd = self._workspace or self._cwd or (ctx.workspace if ctx else None) or os.getcwd()
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

        self._procs[task.taskId] = proc
        if ctx is not None:
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
            self._procs.pop(task.taskId, None)

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
                    "streaming": False, "inputRequired": True,
                    "interruptible": False}}
        if not out_text:
            meta["warning"] = "进程正常退出但 stdout 为空"
            parts[0] = Part(kind="text", text="")
        if err_text:
            meta["stderr"] = err_text[-2000:]
        parts.append(Part(kind="data", data=meta))

        yield TaskEvent(
            kind="artifact", taskId=task.taskId,
            artifact=Artifact(artifactId=uuid.uuid4().hex[:12], name="stdout",
                              description=f"{self._name} 的 stdout（requiresReview=True）",
                              parts=parts))
        yield TaskEvent(kind="status", taskId=task.taskId,
                        state=TaskState.COMPLETED, final=True)

    async def resume(self, task: Task, answer: Message) -> AsyncIterator[TaskEvent]:
        """★ 续接 = 把历史重建成一段 prompt，再起一次进程。

        **这是有损重建，不是会话恢复** —— 上一轮进程内的状态已经随进程没了。
        所以名片上 streaming=False 依旧如实：你看不到它这一轮怎么想的。
        """
        prompt = self._rebuild(task, answer)
        async for ev in self._run(task, prompt):
            yield ev

    def _rebuild(self, task: Task, answer: Message) -> str:
        """把 task 的历史拼成一段自包含的 prompt。

        ⚠️ 命令行程序没有记忆，所以要把「原任务 + 上一轮产出 + 你上一轮问的 +
           委托方的回答」全写进去；否则它只会看见你最后那句话。
        """
        lines = ["（这是一次续接。你没有上一次进程的记忆，以下是你需要知道的全部。）"]
        for m in task.history:
            who = {"user": "委托方", "agent": self._name}.get(m.role, m.role)
            txt = self._text_of(m)
            if txt:
                lines.append(f"[{who}] {txt}")
            for p in m.parts:                      # ★ 上次问的是什么、选项有哪些
                if p.kind == "data" and isinstance(p.data, dict) and "pending" in p.data:
                    pd = p.data["pending"]
                    lines.append(f"[{who}（上一轮停下等你拍板）] {pd.get('question','')}")
                    for o in pd.get("options") or []:
                        lines.append(f"  - {o}")
                    if pd.get("recommend"):
                        lines.append(f"  它当时推荐：{pd['recommend']}")
                    if pd.get("reason"):
                        lines.append(f"  它当时的理由：{pd['reason']}")
        for a in task.artifacts:                   # ★ 它上一轮已经做完的部分
            for p in a.parts:
                if p.kind == "text" and p.text:
                    lines.append(f"[{self._name} 上一轮的产出] {p.text}")
        # 回答已经在 history 里了（dispatcher 先落盘再 resume）—— 别再写一遍，
        # 否则 prompt 里会出现两句一样的「委托方：用 TOML」。
        last = self._text_of(task.history[-1]) if task.history else ""
        if last.strip() != self._text_of(answer).strip():
            lines.append(f"[委托方] {self._text_of(answer)}")
        lines.append("（继续做完。不要再问同一件事 —— 决定已经给了。）")
        return "\n".join(lines)

    async def cancel(self, task: Task) -> None:
        proc = self._procs.get(task.taskId)
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
