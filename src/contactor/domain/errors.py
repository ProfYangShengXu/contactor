"""错误两分：协议错给程序，执行错给模型。"""
from __future__ import annotations
from typing import Any


class A2AError(Exception):
    """协议层错误 —— 走 JSON-RPC error 对象，给【程序】看。"""
    code: int = -32603

    def __init__(self, message: str, data: Any = None, code: int | None = None):
        super().__init__(message)
        self.message = message
        self.data = data
        if code is not None:
            self.code = code

    def to_jsonrpc(self, req_id: Any) -> dict:
        err: dict = {"code": self.code, "message": self.message}
        if self.data is not None:
            err["data"] = self.data
        return {"jsonrpc": "2.0", "id": req_id, "error": err}


class ParseError(A2AError):       code = -32700
class InvalidRequest(A2AError):   code = -32600
class MethodNotFound(A2AError):   code = -32601
class InvalidParams(A2AError):    code = -32602
class InternalError(A2AError):    code = -32603


class TaskNotFound(A2AError):
    code = -32001
    def __init__(self, task_id: str):
        super().__init__(f"task not found: {task_id}", {"taskId": task_id})


class BackendFailure(Exception):
    """执行层失败 —— 不抛给协议，转成 Task.state=failed + 一条【人话】Message。"""

    def __init__(self, reason: str, retryable: bool, detail: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable
        self.detail = detail

    def to_message_text(self) -> str:
        tail = f"（{self.detail}）" if self.detail else ""
        return f"任务失败：{self.reason}{tail}"
