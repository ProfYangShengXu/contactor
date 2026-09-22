"""配置。★ 唯一允许出现 agent 名字字面量的地方。"""
from __future__ import annotations
import os
from typing import Any, Literal
import yaml
from pydantic import BaseModel, Field

HOME = os.path.expandvars(r"%LOCALAPPDATA%\contactor")


class SkillSpec(BaseModel):
    """★ 业务能力，不是函数名（lec14 教案 5.3）。

    反例（本桥原来就是这么干的）：
        skills: [{id: "dsh", name: "dsh", description: "委托给 dsh"}]
    —— 这是【名字粒度】不是【业务能力粒度】。后果：编排器无法判断"适不适合接这个活"，
       而且暴露了内部实现（对方内部是黑盒是设计意图，不是缺陷）。

    正例：
        skills: [{id: "code-refactor", name: "代码重构",
                  description: "在仓库内做多文件重构并跑测试",
                  examples: ["把 X 模块从 A 框架迁到 B"]}]
    """
    id: str
    name: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)


class AgentSpec(BaseModel):
    kind: Literal["acp", "http_api", "subprocess_cli"]
    description: str = ""                 # 名片上的 description（人看的）
    skills: list[SkillSpec] = Field(default_factory=list)   # ★ 业务能力清单
    command: list[str] | None = None
    cwd: str | None = None
    workspace: str | None = None          # 传给 agent 的工作目录（可能和 cwd 不同）
    base_url: str | None = None
    card_override: dict[str, Any] | None = None
    enabled: bool = True
    # ── 只对 kind=subprocess_cli 有意义 ──────────────────────
    timeout_s: int | None = None              # 不填用 Config.task_timeout_s
    prompt_via: Literal["stdin", "arg"] = "stdin"
    #   stdin : 把 prompt 喂进标准输入（规格默认）
    #   arg   : 把 prompt 当命令行参数追加（codex exec / claude -p 这类）
    prompt_flag: list[str] = Field(default_factory=list)
    #   arg 模式下插在 prompt 前面的固定参数，如 ["exec"] 或 ["-p"]


class Config(BaseModel):
    bind_host: str = "127.0.0.1"
    bind_port: int = 8791
    self_name: str = "contactor"
    cards_dir: str = os.path.join(HOME, "cards")
    db_path: str = os.path.join(HOME, "tasks.db")
    workspace_root: str = os.path.join(HOME, "workspace")
    agents: dict[str, AgentSpec] = Field(default_factory=dict)
    agent_concurrency: dict[str, int] = Field(default_factory=dict)
    task_timeout_s: int = 1800
    max_delegation_depth: int = 3
    acp_noise_limit: int = 50

    @classmethod
    def load(cls, path: str) -> "Config":
        raw = yaml.safe_load(io_open(path)) or {}
        return cls(**raw)


def io_open(path: str):
    import io
    return io.open(path, encoding="utf-8")
