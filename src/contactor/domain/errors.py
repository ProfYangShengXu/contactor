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
    def __init__(self, taskId: str):
        super().__init__(f"task not found: {taskId}", {"taskId": taskId})


class BackendFailure(Exception):
    """执行层失败 —— 不抛给协议，转成 Task.state=failed + 一条【人话】Message。"""

    code: str = "backend_error"

    def __init__(self, reason: str, retryable: bool, detail: str = "",
                 code: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable
        self.detail = detail
        if code:
            self.code = code

    def to_message_text(self) -> str:
        tail = f"（{self.detail}）" if self.detail else ""
        return f"任务失败：{self.reason}{tail}"

    def to_task_error(self, taskId: str = "", traceId: str = "") -> "TaskError":
        """★ 转成【结构化】error（教案 2.4）。

        为什么要结构化，而不是一句话：
        远程调用失败时，调用方拿到的不该只是一句人话 —— 它至少要能判
        「这是瞬时故障（可以重发）还是逻辑故障（重发是错的）」，以及
        「拿什么号去找对端的人问」。
        """
        from .models import TaskError
        return TaskError(code=self.code, message=self.reason,
                         retryable=self.retryable, detail=self.detail or None,
                         taskId=taskId or None, correlationId=traceId)
