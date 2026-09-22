"""★ ACP 桥 —— 一个类覆盖所有支持 ACP 的 agent。

报文格式【全部来自 2026-09-22 实测】（dsh + Hermes），不是猜的：

  initialize      params {"protocolVersion":1,"clientCapabilities":{}}
                  result {"protocolVersion":1,"agentInfo":{...},
                          "agentCapabilities":{...},"authMethods":[]}
  session/new     params {"cwd":<agent 所在系统的绝对路径>,"mcpServers":[]}
                  result {"sessionId":"...","configOptions":[...]}
  session/prompt  params {"sessionId":sid,"prompt":[{"type":"text","text":"..."}]}
                  ★ 终态在这个【响应】里：result.stopReason = "end_turn"
  session/update  params {"sessionId":sid,"update":{...}}
                  update.sessionUpdate ∈ agent_thought_chunk /
                    agent_message_chunk / usage_update / availableCommands
                  update.content = {"type":"text","text":"..."}（chunk 类）
  session/cancel  params {"sessionId":sid}

纪律：stdout 只走协议，日志一律 stderr。
"""
from __future__ import annotations
import asyncio, json, sys, uuid
from typing import Any, AsyncIterator

from ..domain.models import (AgentCard, Artifact, Message, Part, Skill,
                             Task, TaskEvent, TaskState)
from ..domain.errors import BackendFailure

# ★ 日志只走 stderr —— stdout 是协议专用通道
_LOG = lambda m: print(m, file=sys.stderr, flush=True)

_TERMINAL_STOP = {"end_turn", "max_tokens", "max_turn_requests",
                  "refusal", "cancelled"}


class AcpSession:
    """一条 ACP 连接 = 一个子进程 + 一个读循环 + 一批在途请求。"""

    _NOISE_LIMIT = 50

    def __init__(self, name: str, command: list[str], cwd: str | None = None):
        self.name, self.command, self.cwd = name, command, cwd
        self.proc: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future] = {}
        self._permissions: dict[str, asyncio.Future] = {}
        self._updates: asyncio.Queue = asyncio.Queue()
        self._reader: asyncio.Task | None = None
        self._init_result: dict | None = None

    # ── 生命周期 ───────────────────────────────────────────────

    async def start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            *self.command, cwd=self.cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)          # ★ 必须接，否则 stderr 写满会卡死
        self._reader = asyncio.create_task(self._read_loop())
        self._init_result = await self.request("initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {"fs": {}, "terminal": {}},
        }, timeout=60)

    async def close(self) -> None:
        if self._reader:
            self._reader.cancel()
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.proc.kill()

    # ── JSON-RPC 收发 ──────────────────────────────────────────

    async def request(self, method: str, params: dict,
                      *, timeout: float | None = 120) -> dict:
        rid = self._next_id
        self._next_id += 1
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        await self._send({"jsonrpc": "2.0", "id": rid, "method": method,
                          "params": params})
        try:
            return await (asyncio.wait_for(fut, timeout=timeout) if timeout else fut)
        finally:
            self._pending.pop(rid, None)

    async def notify(self, method: str, params: dict) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def respond(self, rid: Any, result: dict) -> None:
        await self._send({"jsonrpc": "2.0", "id": rid, "result": result})

    async def _send(self, obj: dict) -> None:
        assert self.proc and self.proc.stdin
        self.proc.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode())
        await self.proc.stdin.drain()

    async def _read_loop(self) -> None:
        """★ 必须一直转 —— 否则收不到后续 update，也答不了反向请求。"""
        assert self.proc and self.proc.stdout
        noise = 0
        try:
            async for raw in self.proc.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    # ★ 熔断：不能无限 continue，否则读循环被空转占死
                    noise += 1
                    if noise == 1:
                        _LOG(f"[acp:{self.name}] stdout 被污染（日志没走 stderr？）: {line[:200]}")
                    elif noise > self._NOISE_LIMIT:
                        raise BackendFailure(
                            f"{self.name} 的 stdout 持续被非协议内容污染（{noise} 行）",
                            retryable=True, detail="检查该 agent 是否把日志打到了 stdout")
                    continue
                noise = 0

                # ① 我们发出去、对方回答的
                if "id" in msg and ("result" in msg or "error" in msg):
                    fut = self._pending.get(msg["id"])
                    if fut is None:
                        _LOG(f"[acp:{self.name}] 收到未知 id 的响应: {msg['id']}")
                    elif not fut.done():
                        fut.set_result(msg)
                    continue

                # ② 对方发来的【请求】—— 必须回
                if "id" in msg and "method" in msg:
                    await self._handle_incoming_request(msg)
                    continue

                # ③ 通知 / 流式更新
                if msg.get("method") == "session/update":
                    await self._updates.put(msg.get("params", {}))
                    continue

                _LOG(f"[acp:{self.name}] 未处理: {line[:200]}")
        except asyncio.CancelledError:
            raise
        except BackendFailure as e:
            if self.proc and self.proc.returncode is None:
                self.proc.kill()
            await self._updates.put({"_fatal": e})

    async def _handle_incoming_request(self, msg: dict) -> None:
        method = msg.get("method")
        if method == "session/request_permission":
            # ★★ 反向请求：存 future 立刻返回，【不在这里等委托方】
            sid = (msg.get("params") or {}).get("sessionId")
            if not sid:
                _LOG(f"[acp:{self.name}] permission 请求没带 sessionId，拒绝")
                await self._send({"jsonrpc": "2.0", "id": msg.get("id"),
                                  "error": {"code": -32602,
                                            "message": "缺 sessionId"}})
                return
            key = f"{sid}:{msg.get('id')}:{uuid.uuid4().hex[:8]}"
            fut = asyncio.get_running_loop().create_future()
            fut._acp_rid = msg.get("id")
            fut._acp_sid = sid
            fut._acp_options = (msg.get("params") or {}).get("options", [])
            self._permissions[key] = fut
            await self._updates.put({"_permission_request": {
                "key": key, "sid": sid, "rid": msg.get("id"),
                "params": msg.get("params", {})}})
            return
        # 其余反向请求本子集不支持 —— 明确拒绝
        await self._send({"jsonrpc": "2.0", "id": msg.get("id"), "error": {
            "code": -32601, "message": f"unsupported client method: {method}"}})

    async def answer_permission(self, key: str, allow: bool) -> bool:
        fut = self._permissions.pop(key, None)
        if fut is None:
            return False
        opts = fut._acp_options or []
        want = "allow_once" if allow else "reject_once"
        chosen = next((o.get("optionId") for o in opts
                       if (o.get("kind") or "").startswith("allow" if allow else "reject")), None)
        if chosen is None and opts:
            chosen = opts[0].get("optionId")
        await self.respond(fut._acp_rid,
                           {"outcome": {"outcome": "selected", "optionId": chosen}})
        return True

    async def deny_permission(self, key: str) -> None:
        fut = self._permissions.pop(key, None)
        if fut is not None:
            await self.respond(fut._acp_rid, {"outcome": {"outcome": "cancelled"}})


class AcpBackend:
    """name → 一个 AcpSession（懒启动 + 复用）。"""

    def __init__(self, name: str, command: list[str], cwd: str | None = None,
                 workspace: str | None = None, card_override: dict | None = None):
        self.name, self.command, self.cwd = name, command, cwd
        self.workspace = workspace or cwd or "."
        self._card_override = card_override
        self._sess: AcpSession | None = None
        self._session_by_ctx: dict[str, str] = {}
        self._inflight: dict[str, asyncio.Task] = {}     # task_id → 挂着的 prompt 请求
        self._pending_perm: dict[str, str] = {}          # task_id → permission key
        self._session_by_ctx_hint: str | None = None     # 最近一次会话 id（排查用）

    # ── 基础设施 ───────────────────────────────────────────────

    async def _ensure(self) -> AcpSession:
        if self._sess is None or (self._sess.proc and self._sess.proc.returncode is not None):
            self._sess = AcpSession(self.name, self.command, self.cwd)
            await self._sess.start()
        return self._sess

    async def card(self) -> AgentCard:
        if self._card_override:
            return AgentCard(**self._card_override)
        sess = await self._ensure()
        info = (sess._init_result or {}).get("result", {})
        ai = info.get("agentInfo", {})
        return AgentCard(name=self.name,
                         description=f"ACP agent: {ai.get('name', self.name)}",
                         version=str(ai.get("version", "0.0.0")),
                         capabilities={"streaming": True, "inputRequired": True,
                                       "contentVerified": False})

    # ── 委托 ──────────────────────────────────────────────────

    async def _session_for(self, ctx) -> str:
        sess = await self._ensure()
        if ctx.context_id not in self._session_by_ctx:
            r = await sess.request("session/new",
                                   {"cwd": self.workspace, "mcpServers": []}, timeout=120)
            if "error" in r:
                raise BackendFailure(f"session/new 失败: {r['error'].get('message')}",
                                     retryable=False,
                                     detail=f"cwd={self.workspace}")
            self._session_by_ctx[ctx.context_id] = r["result"]["sessionId"]
            self._session_by_ctx_hint = r["result"]["sessionId"]
        return self._session_by_ctx[ctx.context_id]

    async def submit(self, task: Task, message: Message,
                     ctx) -> AsyncIterator[TaskEvent]:
        sid = await self._session_for(ctx)
        yield TaskEvent(kind="status", task_id=task.task_id, state=TaskState.WORKING)
        async for ev in self._turn(task, sid, self._text_of(message)):
            yield ev

    async def resume(self, task: Task, answer: Message) -> AsyncIterator[TaskEvent]:
        sess = await self._ensure()
        key = self._pending_perm.pop(task.task_id, None)
        if key:
            allow = self._is_allow(answer)
            ok = await sess.answer_permission(key, allow)
            if not ok:
                # ★ 桥重启过，future 已经没了 —— 不能静默当成功
                raise BackendFailure(
                    "放行请求已失效（桥可能重启过），请重新发起这个任务",
                    retryable=True)
        else:
            sid = self._session_by_ctx.get(task.context_id)
            if sid is None:
                raise BackendFailure("会话已丢失（桥重启过），请重新发起",
                                     retryable=True)
            yield TaskEvent(kind="status", task_id=task.task_id, state=TaskState.WORKING)
            async for ev in self._turn(task, sid, self._text_of(answer)):
                yield ev
            return
        yield TaskEvent(kind="status", task_id=task.task_id, state=TaskState.WORKING)
        async for ev in self._continue(task):
            yield ev

    # ── 一轮对话 ──────────────────────────────────────────────

    async def _turn(self, task: Task, sid: str, text: str) -> AsyncIterator[TaskEvent]:
        """发 prompt + 并发消费 update，直到 prompt 响应回来（★ 终态在响应里）。"""
        sess = await self._ensure()
        fut = asyncio.create_task(sess.request(
            "session/prompt",
            {"sessionId": sid, "prompt": [{"type": "text", "text": text}]},
            timeout=None))                      # 长任务不设超时
        self._inflight[task.task_id] = fut
        try:
            async for ev in self._consume(task, sess):
                yield ev
        finally:
            if fut.done():
                self._inflight.pop(task.task_id, None)

    async def _continue(self, task: Task) -> AsyncIterator[TaskEvent]:
        """permission 兑现后，继续消费（prompt 请求还挂着）。"""
        sess = await self._ensure()
        async for ev in self._consume(task, sess):
            yield ev

    async def _consume(self, task: Task, sess: AcpSession) -> AsyncIterator[TaskEvent]:
        fut = self._inflight.get(task.task_id)
        answer_parts: list[str] = []      # ★ 累积答案 → Artifact
        thoughts: list[str] = []          # ★ 累积思路 → Artifact 的 data part
        while True:
            if fut is not None and fut.done():
                break
            try:
                params = await asyncio.wait_for(sess._updates.get(), timeout=0.25)
            except asyncio.TimeoutError:
                continue

            if "_fatal" in params:
                raise params["_fatal"]

            if "_permission_request" in params:
                pr = params["_permission_request"]
                self._pending_perm[task.task_id] = pr["key"]
                # ★ 清空队列里已堆积的 update —— 否则 resume 时会拿到上一轮的残留
                dropped = 0
                while not sess._updates.empty():
                    leftover = sess._updates.get_nowait()
                    if "_permission_request" in leftover:
                        await sess.deny_permission(leftover["_permission_request"]["key"])
                    dropped += 1
                if dropped:
                    _LOG(f"[acp:{self.name}] input-required 前丢弃 {dropped} 条残留 update")
                yield TaskEvent(kind="status", task_id=task.task_id,
                                state=TaskState.INPUT_REQUIRED, is_final=True,
                                message=Message(role="agent", message_id="perm",
                                                parts=[Part(kind="text",
                                                            text=self._describe(pr["params"]))],
                                                task_id=task.task_id))
                return                                   # ★ 让 dispatcher return，不阻塞

            update = params.get("update") or params
            kind = update.get("sessionUpdate")

            # ★ ACP 没有 Artifact 概念 —— 全走 session/update。
            #   A2A 里 Message 是过程、Artifact 是交付，所以要分开处理：
            #     agent_message_chunk  → 累积成【答案】→ turn 结束做成 Artifact
            #     agent_thought_chunk  → 累积成【思路】→ 作为 Artifact 的 data part
            #   （2026-09-22 实测踩过：两者都当 message 存进 history，
            #     结果 artifacts=0，CLI 打印不出任何东西。）
            if kind == "agent_message_chunk":
                t = self._text_of_content(update.get("content"))
                if t:
                    answer_parts.append(t)
                    yield TaskEvent(kind="message", task_id=task.task_id,
                                    message=Message(role="agent",
                                                    message_id=uuid.uuid4().hex[:12],
                                                    parts=[Part(kind="text", text=t)],
                                                    task_id=task.task_id))
            elif kind == "agent_thought_chunk":
                t = self._text_of_content(update.get("content"))
                if t:
                    thoughts.append(t)

        # prompt 响应回来了 = 这一轮结束
        resp = fut.result() if fut else {}
        if "error" in resp:
            raise BackendFailure(f"agent 返回错误: {resp['error'].get('message')}",
                                 retryable=False, detail=str(resp["error"])[:300])
        stop = (resp.get("result") or {}).get("stopReason", "end_turn")

        answer = "".join(answer_parts).strip()
        if answer:
            parts = [Part(kind="text", text=answer)]
            if thoughts:
                parts.append(Part(kind="data", data={
                    "thoughts": "".join(thoughts).strip(),
                    "stopReason": stop,
                    "acpSessionId": self._session_by_ctx_hint,
                }))
            yield TaskEvent(kind="artifact", task_id=task.task_id,
                            artifact=Artifact(artifact_id=uuid.uuid4().hex[:12],
                                              name="agent-output",
                                              description=f"{self.name} 的输出（requires_review=True）",
                                              parts=parts))

        if stop == "cancelled":
            yield TaskEvent(kind="status", task_id=task.task_id,
                            state=TaskState.CANCELED, is_final=True)
        else:
            yield TaskEvent(kind="status", task_id=task.task_id,
                            state=TaskState.COMPLETED, is_final=True)

    async def cancel(self, task: Task) -> None:
        sess = await self._ensure()
        sid = self._session_by_ctx.get(task.context_id)
        if sid:
            await sess.notify("session/cancel", {"sessionId": sid})

    # ── 小工具 ────────────────────────────────────────────────

    @staticmethod
    def _text_of(msg: Message) -> str:
        return "".join(p.text or "" for p in msg.parts if p.kind == "text")

    @staticmethod
    def _text_of_content(content: Any) -> str:
        if isinstance(content, dict):
            return content.get("text") or ""
        if isinstance(content, list):
            return "".join(c.get("text", "") for c in content if isinstance(c, dict))
        return ""

    @staticmethod
    def _is_allow(answer: Message) -> bool:
        t = "".join(p.text or "" for p in answer.parts).strip().lower()
        return t in {"y", "yes", "allow", "ok", "放行", "允许", "同意", "是", "1"}

    @staticmethod
    def _describe(params: dict) -> str:
        tool = params.get("toolCall") or {}
        title = tool.get("title") or params.get("description") or "（未提供描述）"
        opts = ", ".join(f"{o.get('kind')}" for o in (params.get("options") or []))
        return (f"[需要放行] {title}\n可选：{opts}\n"
                f"回复 yes/allow 或 no/reject")
