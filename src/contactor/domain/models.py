"""数据形状。零外部依赖（只 pydantic + 标准库）。

★ 字段命名一律 **camelCase**，与 A2A 线上格式【逐字一致】。
  不用 alias 做转换是刻意的：少一层间接，读代码看到的就是报文里看到的。
  （见 lec14 教案 4.1 的 Agent Card 字段表。）
"""
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
    """A2A 的 Part 三类：text / file / data。"""
    kind: Literal["text", "data", "file"]
    text: str | None = None
    data: dict[str, Any] | None = None
    path: str | None = None
    mimeType: str | None = None


class Message(BaseModel):
    """过程。一个任务可能来回几十条 Message。"""
    role: Literal["user", "agent"]
    parts: list[Part]
    messageId: str
    taskId: str | None = None
    contextId: str | None = None


class Artifact(BaseModel):
    """产出。分开发的理由：混在一起调用方要自己分辨"哪条是聊天哪条是结果"，
    而这个判断很脆。分开之后 Artifact 可以直接喂给下游。"""
    artifactId: str
    name: str | None = None
    description: str | None = None
    parts: list[Part]
    # ★ 大产出可以一节一节推（教案 4.7）—— 增量追加
    append: bool = False


class Skill(BaseModel):
    """★ 业务能力，不是函数名。

    lec14 教案 5.3 点名的翻车点：按 tool 粒度设计 skills[] → 写成一个个函数名，
    编排器就无法判断"适不适合接这个活"（技能是业务语义，不是函数语义），
    而且暴露内部工具体系 —— 而"对方内部是黑盒"是【设计意图】不是缺陷。
    """
    id: str
    name: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)


class AgentCapabilities(BaseModel):
    """前三个是 A2A 规范键；后两个是本桥的扩展（规范外的读者忽略即可）。

    ⚠️ 扩展键必须【显式声明是本桥扩展】—— 别让读者以为它们是规范的一部分。
    """
    # ── A2A 规范 ──────────────────────────────────────────────
    streaming: bool = False                # 能不能边跑边看（SSE）
    pushNotifications: bool = False        # 跑完回调 webhook（本桥未实现，恒 false）
    stateTransitionHistory: bool = False   # 保不保留状态迁移历史
    # ── 本桥扩展（x- 语义，见 README「非规范键」）──────────────
    inputRequired: bool = False            # 会不会中途要放行 —— 本地场景最关键的一个
    contentVerified: bool = False          # 桥是否验证过内容（恒 false：桥不判断对错）


class AgentCard(BaseModel):
    """agent 的名片。

    ★ 它先天是【自我声明】不是【担保】—— 因为它由提供方自己生成。
      信任边界由【接收方】划定。桥只校验格式，不验证能力声明是否属实。
    """
    protocolVersion: str = "0.2.9"
    name: str
    description: str = ""
    url: str = ""
    preferredTransport: str = "JSONRPC"
    version: str = "0.0.0"
    provider: dict[str, Any] | None = None
    capabilities: AgentCapabilities = Field(default_factory=AgentCapabilities)
    defaultInputModes: list[str] = Field(default_factory=lambda: ["text/plain"])
    defaultOutputModes: list[str] = Field(default_factory=lambda: ["text/plain"])
    skills: list[Skill] = Field(default_factory=list)
    securitySchemes: dict[str, Any] = Field(default_factory=dict)


class TaskError(BaseModel):
    """★ 结构化 error + 关联 id（lec14 教案 2.4）。

    为什么要结构化：远程调用失败时，调用方拿到的不该只是一句话。
    教案原话 —— 读对方 Agent Card 时要看「失败时保不保证回传结构化 error 和关联 id」。
    本桥保证。
    """
    code: str                       # 机器可判：timeout / backend_error / invalid_params / ...
    message: str                    # 给人看的
    retryable: bool = False         # 瞬时（可重试，要带幂等键）还是逻辑（重试是错的）
    detail: str | None = None       # 原始信息（stderr 尾部等）
    taskId: str | None = None
    correlationId: str = ""         # ★ 跨进程出问题时，拿这个号去对端问


class Task(BaseModel):
    """一次委托。★ 桥只持有元数据，不持有 agent 的内部状态。

    lec14 A1 的判据（学员超出标准答案的那条）：
      只持有【路由与生命周期控制所需的标识】，不持有远程 agent 内部执行细节。
    """
    taskId: str
    contextId: str
    traceId: str = ""              # ★ 关联 id：跨进程出问题时拿这个号去对端问（教案 2.4）
    agent: str
    state: TaskState = TaskState.SUBMITTED
    history: list[Message] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)
    pendingQuestion: str | None = None
    error: TaskError | None = None
    createdAt: float = 0.0
    updatedAt: float = 0.0
    # 回环防护
    delegationDepth: int = 0
    visitedAgents: list[str] = Field(default_factory=list)
    # 桥无法判断内容对错 —— 永远 True，把"要不要采信"交给委托方
    requiresReview: bool = True
    # 幂等
    originMessageId: str | None = None


class TaskEvent(BaseModel):
    """SSE 的推流事件（桥的简化封装）。

    映射（见 README「事件类型映射」）：
      kind=status   ≙ A2A TaskStatusUpdateEvent
      kind=artifact ≙ A2A TaskArtifactUpdateEvent（append=true 时是增量追加）
      kind=message  ≙ A2A Message

    ⚠️ 这是【本桥的形状】，不是 A2A 规范的事件结构 —— 缺口如实列在 README。
    """
    kind: Literal["status", "artifact", "message"]
    taskId: str
    state: TaskState | None = None
    artifact: Artifact | None = None
    message: Message | None = None
    final: bool = False            # 规范里每个事件都带（原来叫 final）
