"""L5 ★ 全项目唯一 new 对象的地方。"""
from __future__ import annotations
from .config import Config, AgentSpec
from .backends.acp import AcpBackend
from .stores.sqlite_store import SqliteTaskStore
from .runtime.dispatcher import Dispatcher
from .runtime.registry import CardRegistry
from .runtime.shards import ShardedRunner
from .transport.http_jsonrpc import EventBus, build_app
from .ports import AgentBackend, TaskStore, EventSink
from .server import A2AServer
from .domain.models import AgentCard, Skill


def make_backend(name: str, spec: AgentSpec) -> AgentBackend:
    if spec.kind == "acp":
        return AcpBackend(name, spec.command, spec.cwd,
                          workspace=spec.workspace, card_override=spec.card_override)
    if spec.kind == "http_api":
        from .backends.http_api import HttpApiBackend
        return HttpApiBackend(name, spec.base_url, spec.card_override)
    if spec.kind == "subprocess_cli":
        from .backends.subprocess_cli import SubprocessCliBackend
        return SubprocessCliBackend(
            name, spec.command, cwd=spec.cwd, workspace=spec.workspace,
            timeout_s=spec.timeout_s or 1800,
            prompt_via=spec.prompt_via, prompt_flag=spec.prompt_flag,
            card_override=spec.card_override)
    raise ValueError(f"unknown backend kind: {spec.kind}")


def build(config: Config, logger=None):
    store: TaskStore = SqliteTaskStore(config.db_path)
    backends = {n: make_backend(n, s) for n, s in config.agents.items() if s.enabled}
    registry = CardRegistry(config.cards_dir)
    shards = ShardedRunner(config.agent_concurrency)
    bus = EventBus()
    dispatcher = Dispatcher(store=store, backends=backends, registry=registry,
                            shards=shards, sink=bus, config=config, logger=logger)

    # ★ 装配后立刻自检 —— 注错实现要在这里炸，不要等运行时
    assert isinstance(store, TaskStore), "store 没实现 TaskStore"
    for n, b in backends.items():
        assert isinstance(b, AgentBackend), f"{n} 没实现 AgentBackend"
    assert isinstance(bus, EventSink), "sink 没实现 EventSink"

    self_card = AgentCard(
        name=config.self_name,
        description="本机 agent 之间的 A2A 互操作桥",
        url=f"http://{config.bind_host}:{config.bind_port}",
        version="0.1.0",
        capabilities={"streaming": True, "inputRequired": True,
                      "contentVerified": False},
        skills=[Skill(id=n, name=n, description=f"委托给 {n}")
                for n in backends])

    def app_factory():
        return build_app(dispatcher=dispatcher, bus=bus, self_card=self_card,
                         agents=list(backends), registry=registry)

    return A2AServer(dispatcher=dispatcher, registry=registry, self_card=self_card,
                     bus=bus, config=config, app_factory=app_factory, logger=logger)
