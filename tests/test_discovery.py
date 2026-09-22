"""发现链路：名片必须发得出去、读得到。

2026-09-22 实测补洞：CardRegistry.publish() 写好了但从来没被调用过，
注册表里只有桥自己 —— agent 声明的能力缺口委托方读不到，名片等于注释。
"""
from __future__ import annotations
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / "src"))

import pytest
from contactor.config import Config
from contactor.runtime.dispatcher import Dispatcher
from contactor.runtime.registry import CardRegistry
from contactor.runtime.shards import ShardedRunner
from contactor.stores.sqlite_store import SqliteTaskStore
from contactor.transport.http_jsonrpc import EventBus, build_app
from contactor.server import A2AServer
from contactor.domain.models import AgentCard

from fake_backend import FakeBackend


def build(tmp_path, scripts):
    cfg = Config(db_path=str(tmp_path / "t.db"), cards_dir=str(tmp_path / "cards"),
                 workspace_root=str(tmp_path))
    store = SqliteTaskStore(cfg.db_path)
    backends = {n: FakeBackend(n, s) for n, s in scripts.items()}
    reg = CardRegistry(cfg.cards_dir)
    d = Dispatcher(store=store, backends=backends, registry=reg,
                   shards=ShardedRunner(), sink=EventBus(), config=cfg)
    srv = A2AServer(dispatcher=d, registry=reg,
                    self_card=AgentCard(name=cfg.self_name), bus=EventBus(),
                    config=cfg, app_factory=lambda: None)
    return cfg, d, reg, srv


async def test_startup_publishes_every_agent_card(tmp_path):
    cfg, d, reg, srv = build(tmp_path, {"a": "ok", "b": "ok"})
    assert reg.discover() == {}, "起点：注册表是空的"
    await srv._publish_cards()
    got = reg.discover()
    assert set(got) >= {"a", "b"}, f"每个 agent 都该有名片，实得 {sorted(got)}"


async def test_capability_gaps_are_reachable(tmp_path):
    """★ 缺口的价值在于【能被告知】，不在于被写下来。"""
    from contactor.backends.subprocess_cli import SubprocessCliBackend
    cfg = Config(db_path=str(tmp_path / "t.db"), cards_dir=str(tmp_path / "cards"),
                 workspace_root=str(tmp_path))
    store = SqliteTaskStore(cfg.db_path)
    cli = SubprocessCliBackend("claude_fake", [sys.executable, "-c", "print(1)"])
    reg = CardRegistry(cfg.cards_dir)
    d = Dispatcher(store=store, backends={"cli": cli}, registry=reg,
                   shards=ShardedRunner(), sink=EventBus(), config=cfg)
    srv = A2AServer(dispatcher=d, registry=reg,
                    self_card=AgentCard(name=cfg.self_name), bus=EventBus(),
                    config=cfg, app_factory=lambda: None)
    await srv._publish_cards()

    card = reg.discover()["cli"]
    assert card.capabilities["inputRequired"] is False, \
        "委托方必须在【连接前】就能读到「这个 agent 不能中断」"


async def test_broken_card_does_not_block_startup(tmp_path):
    """一个 agent 的名片炸了，不该拖垮整个启动。"""
    cfg, d, reg, srv = build(tmp_path, {"a": "ok", "bad": "ok"})

    class Exploding:
        def __init__(self, inner): self._i = inner
        name = "bad"
        async def card(self): raise RuntimeError("名片炸了")
        def __getattr__(self, k): return getattr(self._i, k)
    d.backends["bad"] = Exploding(d.backends["bad"])

    await srv._publish_cards()          # 不许抛
    got = reg.discover()
    assert "a" in got and "bad" not in got


def test_agents_list_and_card_endpoints(tmp_path):
    """agent card 要能通过协议读到。"""
    from fastapi.testclient import TestClient
    import asyncio
    cfg, d, reg, srv = build(tmp_path, {"a": "ok"})
    reg.publish("a", AgentCard(name="a", description="测试用"))
    app = build_app(dispatcher=d, bus=EventBus(), self_card=AgentCard(name="bridge"),
                    agents=["a"], registry=reg)
    c = TestClient(app)

    r = c.post("/", json={"jsonrpc": "2.0", "id": 1, "method": "agents/list", "params": {}})
    j = r.json()["result"]
    assert j["agents"] == ["a"] and "a" in j["cards"]

    r = c.post("/", json={"jsonrpc": "2.0", "id": 2, "method": "agents/card",
                          "params": {"agent": "a"}})
    assert r.json()["result"]["card"]["name"] == "a"

    r = c.post("/", json={"jsonrpc": "2.0", "id": 3, "method": "agents/card",
                          "params": {"agent": "nope"}})
    assert r.json()["error"]["code"] == -32602
