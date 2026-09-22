"""JSON-RPC over HTTP（默认传输）。协议 × 传输解耦：一份 handler 套两个 transport。"""
from __future__ import annotations
import asyncio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..domain.errors import A2AError
from ..domain.models import TaskEvent, TaskState

TERMINAL_STATES = {TaskState.COMPLETED, TaskState.FAILED,
                   TaskState.CANCELED, TaskState.REJECTED}


class EventBus:
    """进程内 pub/sub。★ 队列满时丢事件，不阻塞 —— 慢订阅者不能拖垮桥。"""

    def __init__(self):
        self._subs: dict[str, list[asyncio.Queue]] = {}

    def subscribe(self, taskId: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._subs.setdefault(taskId, []).append(q)
        return q

    def unsubscribe(self, taskId: str, q: asyncio.Queue) -> None:
        subs = self._subs.get(taskId)
        if subs and q in subs:
            subs.remove(q)
            if not subs:
                self._subs.pop(taskId, None)

    async def emit(self, taskId: str, event: TaskEvent) -> None:
        for q in list(self._subs.get(taskId, [])):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass


def build_app(*, dispatcher, bus: EventBus, self_card, agents: list[str],
              registry=None) -> FastAPI:
    app = FastAPI(title="contactor")

    @app.get("/.well-known/agent-card.json")
    async def card():
        return JSONResponse(self_card.model_dump())

    @app.get("/health")
    async def health():
        return {"ok": True, "agents": agents}

    @app.post("/")
    async def rpc(req: Request):
        try:
            body = await req.json()
        except Exception:
            return JSONResponse({"jsonrpc": "2.0", "id": None,
                                 "error": {"code": -32700, "message": "parse error"}})
        rid = body.get("id")
        method = body.get("method")
        params = body.get("params") or {}
        try:
            if method == "message/send":
                task = await dispatcher.submit(
                    agent=params["agent"], text=params["text"],
                    contextId=params.get("contextId"),
                    messageId=params.get("messageId"),
                    delegationDepth=params.get("delegationDepth", 0),
                    visitedAgents=params.get("visitedAgents"))
                return {"jsonrpc": "2.0", "id": rid,
                        "result": {"task": task.model_dump(mode="json")}}

            if method == "message/stream":
                task = await dispatcher.submit(
                    agent=params["agent"], text=params["text"],
                    contextId=params.get("contextId"),
                    messageId=params.get("messageId"),
                    delegationDepth=params.get("delegationDepth", 0),
                    visitedAgents=params.get("visitedAgents"))
                return StreamingResponse(_sse(bus, dispatcher, task.taskId),
                                         media_type="text/event-stream")

            if method == "tasks/get":
                t = await dispatcher.store.get(params["taskId"])
                if t is None:
                    raise A2AError("task not found", {"taskId": params["taskId"]},
                                   code=-32001)
                return {"jsonrpc": "2.0", "id": rid,
                        "result": {"task": t.model_dump(mode="json")}}

            if method == "tasks/answer":
                t = await dispatcher.answer(params["taskId"], params["text"])
                return {"jsonrpc": "2.0", "id": rid,
                        "result": {"task": t.model_dump(mode="json")}}

            if method == "tasks/cancel":
                t = await dispatcher.cancel(params["taskId"])
                return {"jsonrpc": "2.0", "id": rid,
                        "result": {"task": t.model_dump(mode="json")}}

            if method == "agents/card":
                name = params.get("agent")
                cards = registry.discover() if registry is not None else {}
                if name not in cards:
                    return JSONResponse({"jsonrpc": "2.0", "id": rid,
                        "error": {"code": -32602,
                                  "message": f"没有这个 agent：{name}（现有：{sorted(cards)}）"}})
                return JSONResponse({"jsonrpc": "2.0", "id": rid,
                                     "result": {"card": cards[name].model_dump()}})

            if method == "agents/list":
                return {"jsonrpc": "2.0", "id": rid,
                        "result": {"agents": agents,
                                   "cards": {n: c.model_dump()
                                             for n, c in (registry.discover()
                                                          if registry is not None else {}).items()}}}

            raise A2AError(f"method not found: {method}", code=-32601)

        except A2AError as e:
            return JSONResponse(e.to_jsonrpc(rid))     # ★ 协议错误也是 HTTP 200
        except KeyError as e:
            return JSONResponse(A2AError(f"missing param: {e}", code=-32602).to_jsonrpc(rid))

    return app


async def _sse(bus: EventBus, dispatcher, taskId: str):
    """★★ 顺序：【先订阅 → 再取快照 → 再推后续】。反了会丢事件。"""
    q = bus.subscribe(taskId)
    try:
        snap = await dispatcher.store.get(taskId)
        if snap is not None:
            yield f"data: {_snapshot(snap).model_dump_json()}\n\n"
            if snap.state in TERMINAL_STATES:
                return
        while True:
            ev: TaskEvent = await q.get()
            yield f"data: {ev.model_dump_json()}\n\n"
            if ev.final:
                return
    finally:
        bus.unsubscribe(taskId, q)


def _snapshot(task) -> TaskEvent:
    return TaskEvent(kind="status", taskId=task.taskId, state=task.state,
                     final=False)
