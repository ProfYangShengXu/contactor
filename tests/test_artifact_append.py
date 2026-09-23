"""★ `Artifact.append` 必须有产出者。

这个测试的由来：2026-09-23 实跑一条 14 分钟的任务，调用方在结束前 **artifacts=0**，
最后一次性收到 9,224 字符。查下来 `Artifact.append` 字段在 schema 里躺着，
**全项目零处产出它** —— 正是 bug 类 ⑪「声明没有读者」。

判据（写进 skill 的那条）：**任何"声明/发布/注册"行为，都要配一条【从外部读得到】的验收。**
所以这里断言的不是"字段存在"，而是"发一段就真能看到一段"。
"""
from __future__ import annotations
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / "src"))

from contactor.domain.models import Artifact, Part, Task
from contactor.runtime.dispatcher import Dispatcher


def _task():
    return Task(taskId="t1", contextId="c1", agent="a")


def test_append_merges_into_same_artifact():
    """append=True 的片段要续进【同一个】artifact，而不是变成 N 个 artifact。"""
    t = _task()
    Dispatcher._merge_artifact(t, Artifact(artifactId="A", name="out",
                                           parts=[Part(kind="text", text="前半")]))
    Dispatcher._merge_artifact(t, Artifact(artifactId="A", name="out", append=True,
                                           parts=[Part(kind="text", text="，后半")]))
    Dispatcher._merge_artifact(t, Artifact(artifactId="A", name="out", append=True,
                                           parts=[Part(kind="data", data={"thoughts": "想"})]))
    assert len(t.artifacts) == 1, f"被拆成了 {len(t.artifacts)} 个 artifact"
    a = t.artifacts[0]
    assert "".join(p.text or "" for p in a.parts if p.kind == "text") == "前半，后半"
    assert any(p.kind == "data" for p in a.parts)


def test_incremental_chunks_are_visible_before_the_turn_ends():
    """★ 核心断言：中途就能读到内容，不必等终点。

    修之前的行为：turn 结束前 task.artifacts 恒为空（一次全给）。
    """
    t = _task()
    mid = _task()
    for chunk in ("第一段" * 40, "第二段" * 40, "第三段" * 40):
        Dispatcher._merge_artifact(mid, Artifact(artifactId="A", append=len(mid.artifacts) > 0,
                                                 parts=[Part(kind="text", text=chunk)]))
        assert mid.artifacts, "每追加一段之后都应该已经能看到产出"
    seen_mid = "".join(p.text or "" for p in mid.artifacts[0].parts if p.kind == "text")
    assert len(seen_mid) > 200, "中途就该有内容，不是空的"


def test_orphan_append_is_not_silently_dropped():
    """对不上 artifactId 的增量 → 当成新 artifact，【不能静默丢】。"""
    t = _task()
    Dispatcher._merge_artifact(t, Artifact(artifactId="A", parts=[Part(kind="text", text="x")]))
    Dispatcher._merge_artifact(t, Artifact(artifactId="B", append=True,
                                           parts=[Part(kind="text", text="y")]))
    assert len(t.artifacts) == 2
    assert t.artifacts[1].append is False, "找不到可追加的目标时要把 append 归位，别留个撒谎的标志"


def test_acp_backend_actually_produces_append():
    """产出侧也要钉：acp.py 里得有 append= 的赋值，否则字段又变回死声明。"""
    src = (pathlib.Path(__file__).parents[1] / "src" / "contactor" / "backends" / "acp.py").read_text("utf-8")
    assert "append=not first_flush" in src or "append=True" in src, \
        "acp backend 没有产出 append 事件 —— Artifact.append 又变成没人产出的声明了"


# ── 集成层：走真 Dispatcher + 真 sqlite，复现那条"同一个 artifactId 出现两次" ──
#
# ⚠️ 为什么单测挡不住：重复不是 `_merge_artifact` 的错，是 **backend 收尾时把全文重发了一遍**。
#    只有让事件流真穿过 dispatcher + store 才看得见。

import asyncio
import pytest

from contactor.config import Config
from contactor.domain.models import Artifact, Part, Task, TaskEvent, TaskState
from contactor.runtime.dispatcher import Dispatcher
from contactor.runtime.registry import CardRegistry
from contactor.runtime.shards import ShardedRunner
from contactor.stores.sqlite_store import SqliteTaskStore
from contactor.transport.http_jsonrpc import EventBus


class StreamingBackend:
    """仿 acp backend 的收尾姿势：先推几段增量，最后补尾巴。

    ★ 关键：**收尾只补没发过的部分**。写错成"重发全文"就会产出重复 artifact。
    """
    name = "streamer"

    def __init__(self):
        self.gate = asyncio.Event()
        self.dup_full_text = False       # 打开 = 故意犯那个 bug

    async def card(self):
        from contactor.domain.models import AgentCard
        return AgentCard(name=self.name)

    async def submit(self, task: Task, message, ctx):
        yield TaskEvent(kind="status", taskId=task.taskId, state=TaskState.WORKING)
        aid = "AAA"
        for i, chunk in enumerate(("A" * 300, "B" * 300)):
            yield TaskEvent(kind="artifact", taskId=task.taskId,
                            artifact=Artifact(artifactId=aid, name="out", append=i > 0,
                                              parts=[Part(kind="text", text=chunk)]))
            if i == 0:
                await self.gate.wait()          # 卡在第一段之后 → 测"中途可见"
        if self.dup_full_text:
            yield TaskEvent(kind="artifact", taskId=task.taskId,
                            artifact=Artifact(artifactId=aid, name="out",
                                              parts=[Part(kind="text", text="A" * 300 + "B" * 300)]))
        yield TaskEvent(kind="status", taskId=task.taskId,
                        state=TaskState.COMPLETED, final=True)

    async def resume(self, task, answer):
        raise NotImplementedError

    async def cancel(self, task):
        return None


def _wire(tmp_path, be):
    cfg = Config(db_path=str(tmp_path / "t.db"), cards_dir=str(tmp_path / "cards"),
                 workspace_root=str(tmp_path))
    store = SqliteTaskStore(cfg.db_path)
    d = Dispatcher(store=store, backends={be.name: be},
                   registry=CardRegistry(cfg.cards_dir),
                   shards=ShardedRunner(None), sink=EventBus(), config=cfg)
    return d, store


@pytest.mark.asyncio
async def test_artifacts_are_visible_mid_turn(tmp_path):
    """★ 核心：turn 还没结束，`tasks/get` 就该看得到已经产出的部分。

    修之前：artifact 只改内存，store.save() 要到 turn 结束才发生 → 读库恒为 0。
    """
    be = StreamingBackend()
    d, store = _wire(tmp_path, be)
    t = await d.submit("streamer", "干活")
    for _ in range(200):                       # 等第一段落地（backend 卡在 gate 上）
        await asyncio.sleep(0.02)
        got = await store.get(t.taskId)
        if got.artifacts:
            break
    mid = await store.get(t.taskId)
    assert mid.state == TaskState.WORKING, "这时还应该没跑完"
    assert mid.artifacts, "★ turn 中途就必须能看到产出，不能等到终点"
    assert len("".join(p.text or "" for p in mid.artifacts[0].parts if p.kind == "text")) == 300

    be.gate.set()
    for _ in range(300):
        await asyncio.sleep(0.02)
        if (await store.get(t.taskId)).state == TaskState.COMPLETED:
            break
    done = await store.get(t.taskId)
    assert len(done.artifacts) == 1, f"被拆成了 {len(done.artifacts)} 个 artifact"
    txt = "".join(p.text or "" for p in done.artifacts[0].parts if p.kind == "text")
    assert txt == "A" * 300 + "B" * 300, "增量拼接的结果不对"


@pytest.mark.asyncio
async def test_full_text_resend_is_caught(tmp_path):
    """把「收尾重发全文」这个 bug 打开 → 必须被这条测试抓住。

    （这条是测试的测试：如果它没有失败，说明断言形同虚设。）
    """
    be = StreamingBackend()
    be.dup_full_text = True
    be.gate.set()
    d, store = _wire(tmp_path, be)
    t = await d.submit("streamer", "干活")
    for _ in range(300):
        await asyncio.sleep(0.02)
        if (await store.get(t.taskId)).state == TaskState.COMPLETED:
            break
    done = await store.get(t.taskId)
    txt = "".join(p.text or "" for p in done.artifacts[0].parts if p.kind == "text")
    assert len(done.artifacts) != 1 or txt != "A" * 300 + "B" * 300, \
        "重发全文居然没被抓到 —— 检查 _merge_artifact 或收尾逻辑"
