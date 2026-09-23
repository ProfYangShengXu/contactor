"""★ acp backend 的增量推送 / 收尾去重 —— **行为测试**，不是源码字符串断言。

由来（2026-09-23）：上一版对这段逻辑的全部覆盖是一句
    assert "append=not first_flush" in src
**那只证明源码里有这段字符串，证明不了它行为对。** 而当时的 bug
（"收尾把全文当新 artifact 又发一遍"→ 同一个 artifactId 出现两次）
**恰好就藏在那个没被测的分支里**，是真跑抓到的。

判据：**断言"代码里有这段"不算验收；要断言"跑出来是什么"。**
"""
from __future__ import annotations
import asyncio, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / "src"))

import pytest
from contactor.backends.acp import FLUSH_CHARS, AcpBackend
from contactor.domain.models import Message, Part, Task


class FakeSession:
    """只实现 _consume 真正用到的那点面：一个 update 队列。"""
    def __init__(self, items):
        self._updates = asyncio.Queue()
        for it in items:
            self._updates.put_nowait(it)
    async def deny_permission(self, key):        # 权限分支才会用到
        return None


def msg_chunk(text):
    return {"update": {"sessionUpdate": "agent_message_chunk",
                       "content": {"type": "text", "text": text}}}


def thought_chunk(text):
    return {"update": {"sessionUpdate": "agent_thought_chunk",
                       "content": {"type": "text", "text": text}}}


async def _drain(be, t, chunks, delay=0.4):
    """把队列喂给 `_consume`，并在【队列消费完之后】把 inflight future 置为完成。

    ⚠️ 不能用一开始就 `set_result` 的 future —— `_consume` 的循环第一件事就是
       `if fut.done(): break`，那样会直接跳出、一条 update 都不消费（这里踩过）。
    """
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    be._inflight[t.taskId] = fut
    def _finish():
        if not fut.done():
            fut.set_result({"result": {"stopReason": "end_turn"}})
    loop.call_later(delay, _finish)

    evs = []
    async for ev in be._consume(t, FakeSession(chunks)):
        evs.append(ev)
    return evs


@pytest.mark.asyncio
async def test_long_turn_flushes_incrementally():
    """超阈值 → 中途就该有 artifact 事件，且都带 append 续写关系。"""
    be = AcpBackend("t", ["true"])
    t = Task(taskId="t1", contextId="c1", agent="t")
    chunks = [msg_chunk("a" * 300), msg_chunk("b" * 300), msg_chunk("c" * 200)]
    evs = await _drain(be, t, chunks)

    arts = [e for e in evs if e.kind == "artifact"]
    assert len(arts) >= 1, "超过 FLUSH_CHARS 却一个增量都没推"
    ids = {a.artifact.artifactId for a in arts}
    assert len(ids) == 1, f"同一次对话用了 {len(ids)} 个 artifactId，应当只有一个"
    # 第一段 append=False，后续全是 append=True
    assert arts[0].artifact.append is False
    assert all(a.artifact.append is True for a in arts[1:]), "后续增量必须标记为追加"

    # ★ 关键：拼接结果 == 全文，且**不允许出现重复**
    full = ""
    for e in [x for x in evs if x.kind == "artifact"]:
        for p in e.artifact.parts:
            if p.kind == "text":
                full += p.text or ""
    assert full == "a" * 300 + "b" * 300 + "c" * 200, (
        f"拼接结果不对（长度 {len(full)}，期望 800）—— 多半是收尾把全文重发了一遍")


@pytest.mark.asyncio
async def test_short_turn_still_emits_one_complete_artifact():
    """★ 回归：短回复（< FLUSH_CHARS）必须还是老行为 —— 一个完整 artifact。

    没有这条的话，"为长任务加速"很容易把短任务的常见路径改坏。
    """
    be = AcpBackend("t", ["true"])
    t = Task(taskId="t2", contextId="c2", agent="t")
    evs = await _drain(be, t, [msg_chunk("很短"), msg_chunk("的回答")])

    arts = [e for e in evs if e.kind == "artifact"]
    assert len(arts) == 1, f"短回复应当只产 1 个 artifact，实际 {len(arts)}"
    assert arts[0].artifact.append is False
    txt = "".join(p.text or "" for p in arts[0].artifact.parts if p.kind == "text")
    assert txt == "很短的回答"


@pytest.mark.asyncio
async def test_thoughts_land_in_data_part_not_duplicated():
    """思路进 data part，且不在 text 里重复出现。"""
    be = AcpBackend("t", ["true"])
    t = Task(taskId="t3", contextId="c3", agent="t")
    evs = await _drain(be, t, [msg_chunk("答案"), thought_chunk("我在想")])

    txt = "".join(p.text or "" for e in evs if e.kind == "artifact"
                  for p in e.artifact.parts if p.kind == "text")
    data = [p for e in evs if e.kind == "artifact" for p in e.artifact.parts if p.kind == "data"]
    assert txt == "答案"
    assert data and "我在想" in data[0].data.get("thoughts", "")


@pytest.mark.asyncio
async def test_never_emits_two_artifacts_with_same_id_and_append_false():
    """★ 直接钉住那条真实 bug：同一个 artifactId **不能**出现两次 append=False。

    （真实事故：收尾分支在 tail 为空时掉进 elif answer，把全文当新 artifact 又发一遍。）
    """
    for n in (1, 2, 5, 12):
        be = AcpBackend("t", ["true"])
        t = Task(taskId=f"t{n}", contextId="c", agent="t")
        chunks = [msg_chunk("x" * 400) for _ in range(n)]
        evs = await _drain(be, t, chunks)
        fresh = [e for e in evs if e.kind == "artifact" and not e.artifact.append]
        assert len(fresh) == 1, f"{n} 段时出现了 {len(fresh)} 个 append=False 的 artifact（只该有 1 个）"
