"""数据形状。零外部依赖（只 pydantic + 标准库）。"""
from __future__ import annotations
from enum import Enum
from typing import Any, Literal
from pydantic import BaseModel, Field


class TaskState(str, Enum):
    SUBMITTED      = "submitted"
    WORKING        = "working"
    INPUT_REQUIRED = "input-required"      # ★ 本地场景最常走的分支
    COMPLETED      = "completed"
    FAILED         = "failed"
    CANCELED       = "canceled"
    REJECTED       = "rejected"


class Part(BaseModel):
    kind: Literal["text", "data", "file"]
    text: str | None = None
    data: dict[str, Any] | None = None
    path: str | None = None
    mime_type: str | None = None


class Message(BaseModel):
    role: Literal["user", "agent"]
    parts: list[Part]
    message_id: str
    task_id: str | None = None
    context_id: str | None = None


class Artifact(BaseModel):
    artifact_id: str
    name: str | None = None
    description: str | None = None
    parts: list[Part]


class Skill(BaseModel):
    id: str
    name: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)


class AgentCard(BaseModel):
    """agent 的名片。⚠️ 这是【自我声明】，不是担保 —— 桥只校验格式，不验证能力。"""
    name: str
    description: str = ""
    url: str = ""
    version: str = "0.0.0"
    capabilities: dict[str, bool] = Field(default_factory=dict)
    default_input_modes: list[str] = Field(default_factory=lambda: ["text/plain"])
    default_output_modes: list[str] = Field(default_factory=lambda: ["text/plain"])
    skills: list[Skill] = Field(default_factory=list)
    security_schemes: dict[str, Any] = Field(default_factory=dict)


class Task(BaseModel):
    """一次委托。★ 桥只持有元数据，不持有 agent 的内部状态。"""
    task_id: str
    context_id: str
    agent: str
    state: TaskState = TaskState.SUBMITTED
    history: list[Message] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)
    pending_question: str | None = None
    error: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0
    # 回环防护
    delegation_depth: int = 0
    visited_agents: list[str] = Field(default_factory=list)
    # 桥无法判断内容对错 —— 永远 True，把"要不要采信"交给委托方
    requires_review: bool = True
    # 幂等
    origin_message_id: str | None = None


class TaskEvent(BaseModel):
    kind: Literal["status", "artifact", "message"]
    task_id: str
    state: TaskState | None = None
    artifact: Artifact | None = None
    message: Message | None = None
    is_final: bool = False
