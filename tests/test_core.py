"""核心协议语义测试。全部用 fake_backend，不接真 agent。"""
from __future__ import annotations
import asyncio, os, sys, pathlib, tempfile
sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / "src"))

import pytest
from contactor.config import Config, AgentSpec
from contactor.runtime.dispatcher import Dispatcher
from contactor.runtime.shards import ShardedRunner
from contactor.runtime.registry import CardRegistry
from contactor.stores.sqlite_store import SqliteTaskStore
from contactor.transport.http_jsonrpc import EventBus
from contactor.domain.models import TaskState
from contactor.domain.errors import InvalidParams

from fake_backend import FakeBackend


def make(tmp_path, scripts: dict[str, str], concurrency=None):
    cfg = Config(db_path=str(tmp_path / "t.db"),
                 cards_dir=str(tmp_path / "cards"),
                 workspace_root=str(tmp_path),
                 agent_concurrency=concurrency or {})
    store = SqliteTaskStore(cfg.db_path)
    backends = {n: FakeBackend(n, s) for n, s in scripts.items()}
    bus = EventBus()
    d = Dispatcher(store=store, backends=backends, registry=CardRegistry(cfg.cards_dir),
                   shards=ShardedRunner(concurrency), sink=bus, config=cfg)
    return d, store, backends, bus


async def wait_state(store, tid, state, timeout=5):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        t = await store.get(tid)
        if t and t.state == state:
            return t
        await asyncio.sleep(0.02)
    raise AssertionError(f"timeout waiting {state}, now={t.state if t else None}")


# ── A9 基本委托 ──────────────────────────────────────────────
@pytest.mark.asyncio
async def test_normal_task(tmp_path):
    d, store, _, _ = make(tmp_path, {"a": "ok"})
    t = await d.submit("a", "hello")
    await wait_state(store, t.task_id, TaskState.COMPLETED)
    got = await store.get(t.task_id)
    assert got.artifacts[0].parts[0].text == "done: hello"


# ── A6 幂等 ──────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_idempotent_send(tmp_path):
    d, store, backends, _ = make(tmp_path, {"a": "ok"})
    t1 = await d.submit("a", "x", message_id="m-1")
    t2 = await d.submit("a", "x", message_id="m-1")
    assert t1.task_id == t2.task_id, "同一个 messageId 应返回同一个 Task"
    await wait_state(store, t1.task_id, TaskState.COMPLETED)
    assert len(backends["a"].calls) == 1, "agent 只应被驱动一次"


# ── A5 回环防护 ──────────────────────────────────────────────
@pytest.mark.asyncio
async def test_loop_guard_visited(tmp_path):
    d, *_ = make(tmp_path, {"a": "ok", "b": "ok"})
    with pytest.raises(InvalidParams) as e:
        await d.submit("a", "x", visited_agents=["a"])
    assert "回环" in str(e.value)


@pytest.mark.asyncio
async def test_loop_guard_depth(tmp_path):
    d, *_ = make(tmp_path, {"a": "ok"})
    with pytest.raises(InvalidParams):
        await d.submit("a", "x", delegation_depth=3)


# ── A3 input-required 全流程 ─────────────────────────────────
@pytest.mark.asyncio
async def test_input_required_roundtrip(tmp_path):
    d, store, _, _ = make(tmp_path, {"a": "ask"})
    t = await d.submit("a", "危险操作")
    await wait_state(store, t.task_id, TaskState.INPUT_REQUIRED)
    got = await store.get(t.task_id)
    assert got.pending_question == "放行吗？"

    await d.answer(t.task_id, "yes")
    await wait_state(store, t.task_id, TaskState.COMPLETED)
    final = await store.get(t.task_id)
    assert "answered: yes" in final.artifacts[-1].parts[0].text


@pytest.mark.asyncio
async def test_answer_wrong_state_rejected(tmp_path):
    d, store, _, _ = make(tmp_path, {"a": "ok"})
    t = await d.submit("a", "x")
    await wait_state(store, t.task_id, TaskState.COMPLETED)
    with pytest.raises(InvalidParams):
        await d.answer(t.task_id, "yes")


# ── input-required 不堵分片队列 ──────────────────────────────
@pytest.mark.asyncio
async def test_input_required_does_not_block_shard(tmp_path):
    """第一路卡在等放行，第二路必须能跑完。"""
    d, store, _, _ = make(tmp_path, {"a": "ask"})
    t1 = await d.submit("a", "first")
    await wait_state(store, t1.task_id, TaskState.INPUT_REQUIRED)

    # 换脚本让第二路正常完成
    d.backends["a"].script = "ok"
    t2 = await d.submit("a", "second")
    await wait_state(store, t2.task_id, TaskState.COMPLETED, timeout=5)


# ── 执行错误 → FAILED（不是 JSON-RPC error）──────────────────
@pytest.mark.asyncio
async def test_backend_failure_becomes_failed_state(tmp_path):
    d, store, _, _ = make(tmp_path, {"a": "boom"})
    t = await d.submit("a", "x")
    await wait_state(store, t.task_id, TaskState.FAILED)
    got = await store.get(t.task_id)
    assert "脚本要求的失败" in (got.error or "")


# ── A8 桥重启恢复：working → FAILED，不留僵尸 ────────────────
@pytest.mark.asyncio
async def test_recover_marks_working_failed(tmp_path):
    d, store, _, _ = make(tmp_path, {"a": "ok"})
    t = await d.submit("a", "x")
    await wait_state(store, t.task_id, TaskState.COMPLETED)

    # 手工造一个"停在 working"的任务，模拟上次跑一半就死了
    t2 = await d.submit("a", "y")
    await wait_state(store, t2.task_id, TaskState.COMPLETED)
    cur = await store.get(t2.task_id)
    cur.state = TaskState.WORKING
    await store.save(cur)

    # 起一个新 store（模拟桥重启）然后跑恢复
    from contactor.server import A2AServer
    store2 = SqliteTaskStore(str(tmp_path / "t.db"))
    d2 = Dispatcher(store=store2, backends={}, registry=CardRegistry(str(tmp_path/"cards")),
                    shards=ShardedRunner(), sink=EventBus(), config=d.config)
    srv = A2AServer(dispatcher=d2, registry=CardRegistry(str(tmp_path/"cards")),
                    self_card=None, bus=EventBus(), config=d.config,
                    app_factory=lambda: None)
    await srv._recover()
    after = await store2.get(t2.task_id)
    assert after.state == TaskState.FAILED, "working 必须被标 FAILED，不能留僵尸"
    assert "桥重启" in (after.error or "")


# ── A7 SSE 快照：终态之后连上也能拿到状态 ────────────────────
@pytest.mark.asyncio
async def test_sse_snapshot_after_terminal(tmp_path):
    from contactor.transport.http_jsonrpc import _sse
    d, store, _, bus = make(tmp_path, {"a": "ok"})
    t = await d.submit("a", "x")
    await wait_state(store, t.task_id, TaskState.COMPLETED)

    got = []
    async for chunk in _sse(bus, d, t.task_id):
        got.append(chunk)
    assert got, "终态之后订阅也必须有输出（快照）"
    assert '"state":"completed"' in got[0].replace(" ", "")


# ── 状态机：非法迁移被拒 ─────────────────────────────────────
def test_illegal_transition():
    from contactor.domain.lifecycle import assert_transition, IllegalTransition
    with pytest.raises(IllegalTransition):
        assert_transition(TaskState.COMPLETED, TaskState.WORKING)
    assert_transition(TaskState.INPUT_REQUIRED, TaskState.WORKING)   # 这条必须合法
