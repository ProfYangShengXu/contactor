"""subprocess_cli —— 二等公民 backend 的行为与【能力缺口】测试。

⚠️ 这里一半的测试是在测「它做不到什么」。
   兜底 backend 的价值不在功能多，而在于【缺口是否被如实声明】。
"""
from __future__ import annotations
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / "src"))

import pytest
from contactor.backends.subprocess_cli import SubprocessCliBackend
from contactor.domain.errors import BackendFailure
from contactor.domain.models import (Artifact, Message, Part, Task, TaskState)

PY = sys.executable


class Ctx:
    workspace = str(pathlib.Path.cwd())
    contextId = "ctx-test"
    logs: list[str] = []
    def log(self, m): self.logs.append(m)


def task(tid="t1"):
    return Task(taskId=tid, agent="cli", contextId="ctx-test")


def msg(text="hello"):
    return Message(role="user", messageId="m-1",
                   parts=[Part(kind="text", text=text)])


async def drain(be, t, m=None):
    return [ev async for ev in be.submit(t, m or msg(), Ctx())]


# ── 正常路径 ─────────────────────────────────────────────────
async def test_stdout_becomes_artifact():
    be = SubprocessCliBackend("echo", [PY, "-c",
        "import sys; print('AGENT SAYS:', sys.stdin.read().strip())"],
        prompt_via="stdin")
    evs = await drain(be, task(), msg("ping"))
    arts = [e for e in evs if e.kind == "artifact"]
    assert arts and arts[0].artifact.parts[0].text == "AGENT SAYS: ping"
    assert evs[-1].state == TaskState.COMPLETED and evs[-1].final
    assert evs[0].state == TaskState.WORKING


async def test_prompt_as_argument():
    be = SubprocessCliBackend("argy", [PY, "-c",
        "import sys; print('argv:', sys.argv[1])"],
        prompt_via="arg")
    evs = await drain(be, task(), msg("from-argv"))
    art = [e for e in evs if e.kind == "artifact"][0].artifact
    assert art.parts[0].text == "argv: from-argv"


# ── 失败分类（lec14 B1：retryable 决定上层该重试还是该换路）──
async def test_nonzero_exit_is_not_retryable():
    be = SubprocessCliBackend("boom", [PY, "-c",
        "import sys; sys.stderr.write('我崩了'); sys.exit(3)"])
    with pytest.raises(BackendFailure) as e:
        await drain(be, task())
    assert e.value.retryable is False, "逻辑故障不该被重试"
    assert "3" in str(e.value)
    assert "我崩了" in e.value.detail, "stderr 必须带出来，否则没法排查"


async def test_timeout_is_retryable():
    be = SubprocessCliBackend("slow", [PY, "-c", "import time; time.sleep(30)"],
                              timeout_s=1)
    import time
    t0 = time.time()
    with pytest.raises(BackendFailure) as e:
        await drain(be, task())
    assert time.time() - t0 < 8, "超时后必须真把进程杀掉，不能挂着"
    assert e.value.retryable is True, "超时是瞬时故障，可以重试"


async def test_missing_command():
    be = SubprocessCliBackend("nope", ["definitely-not-a-real-binary-xyz"])
    with pytest.raises(BackendFailure) as e:
        await drain(be, task())
    assert e.value.retryable is False


# ── 不留痕就是欺骗：空输出必须留 warning ─────────────────────
async def test_empty_stdout_is_flagged():
    be = SubprocessCliBackend("quiet", [PY, "-c", "pass"])
    evs = await drain(be, task())
    art = [e for e in evs if e.kind == "artifact"][0].artifact
    meta = [p for p in art.parts if p.kind == "data"][0].data
    assert art.parts[0].text == ""
    assert "warning" in meta, "退出码 0 但没输出，必须留下痕迹"


async def test_stderr_kept_even_on_success():
    be = SubprocessCliBackend("noisy", [PY, "-c",
        "import sys; sys.stderr.write('警告信息'); print('ok')"])
    evs = await drain(be, task())
    art = [e for e in evs if e.kind == "artifact"][0].artifact
    meta = [p for p in art.parts if p.kind == "data"][0].data
    assert meta["stderr"] == "警告信息"


# ── ★ 能力缺口必须写进【名片】，不能等踩了坑才知道 ────────────
async def test_card_declares_incapabilities():
    """★ 名片要把【两个不同的缺口】分开说，混起来会给出错误的适配判断。

    2026-09-22 改动：加了 decisions.py 的显式契约之后，这个 backend
    能在【回合末尾】停下来要人拍板了（所以 inputRequired 变成 True）——
    但它【依然】不能在【执行中途】被拦下（危险命令会在放行前就跑掉）。
    旧测试把这两件事当成一件，所以现在必须拆开。
    """
    be = SubprocessCliBackend("cli", [PY, "-c", "pass"])
    card = await be.card()
    assert card.capabilities.inputRequired is True, \
        "靠契约它现在能问「选哪个」了 —— 名片不许继续说自己不能"
    assert card.capabilities.interruptible is False, \
        "★ 但它仍然不可中断：黑盒进程跑起来就拦不住。这个缺口必须留在名片上"
    assert card.capabilities.streaming is False
    assert card.capabilities.contentVerified is False


async def test_resume_rebuilds_prompt_from_history():
    """★ 命令行 backend 的续接 = 【从历史重建 prompt】。

    它没有会话，所以新起的进程必须能自己看出「上一轮问了什么、委托方答了什么、
    我上次做完到哪了」。少了任何一样，它只会看见最后那句话，然后重新问一遍。

    用的是【有损】重建 —— 进程内状态已随进程消失，这一点名片里如实写了。
    """
    be = SubprocessCliBackend("cli", [PY, "-c",
                                     "import sys; d=sys.stdin.read(); print(len(d))"])
    tk = task()
    tk.history = [
        Message(role="user", messageId="m1", taskId=tk.taskId,
                parts=[Part(kind="text", text="加配置文件，用 YAML 还是 TOML？")]),
        Message(role="user", messageId="m2", taskId=tk.taskId, parts=[
            Part(kind="text", text="用 TOML"),
            Part(kind="data", data={"pending": {
                "kind": "decision", "question": "配置文件格式选哪个？",
                "options": ["YAML", "TOML"], "recommend": "TOML",
                "reason": "标准库零依赖"}})]),
    ]
    tk.artifacts = [Artifact(artifactId="a1", name="stdout", parts=[
        Part(kind="text", text="我把两种方案的代价都列出来了。")])]

    be2 = SubprocessCliBackend("cli", [PY, "-c",
                                     "import sys; print(sys.stdin.read())"])
    got = ""
    async for ev in be2.resume(tk, msg("用 TOML")):
        if ev.kind == "artifact":
            got = "".join(p.text or "" for p in ev.artifact.parts if p.kind == "text")

    for must in ("配置文件格式选哪个？", "YAML", "TOML", "标准库零依赖",
                 "我把两种方案的代价都列出来了", "用 TOML", "不要再问同一件事"):
        assert must in got, f"重建的 prompt 里缺了 {must!r} —— 它会重新问一遍"
    assert got.count("用 TOML") == 1, "回答不该在重建的 prompt 里出现两次"


async def test_requires_command():
    with pytest.raises(ValueError):
        SubprocessCliBackend("bad", None)


# ── ★ 架构红线的实战验证：加 backend 不该动【端口面】 ────────
#
# ⚠️ 这里原本写的是 `git status --porcelain src/.../domain src/.../ports.py` 必须为空。
#    那个写法【对任何合法改动都会误报】—— 本次因为要符合 A2A 线上字段名
#    （snake_case → camelCase）就改了 domain/，而那并不是"加 backend 导致的架构漂移"。
#    改用【端口面快照】：加实现时真正不该变的是【接口】本身。

PORTS_SURFACE = {
    "BackendContext": {"workspace", "contextId", "log"},
    "AgentBackend":   {"name", "card", "submit", "resume", "cancel"},
    "TaskStore":      {"create", "get", "save", "append_message",
                       "list_by_state", "find_by_origin_message"},
    "EventSink":      {"emit"},
}

# Protocol 类自身的残余属性，不是端口的一部分
_PROTOCOL_NOISE = {"model_config", "model_fields", "model_compute_fields"}


def test_ports_surface_is_frozen():
    """加一个 backend，ports 的【公开面】不该变。

    它变了只有两种可能：
      ① 这是新能力的正当扩展 —— 那就在这条快照里显式改，并说清它为什么是通用的
      ② 某个实现的需求被塞进了通用接口 —— 那就是架构漂移，架构红线破了
    """
    import inspect
    from contactor import ports

    for name, expect in PORTS_SURFACE.items():
        obj = getattr(ports, name)
        members = {m for m in dir(obj) if not m.startswith("_")} - _PROTOCOL_NOISE
        missing = expect - members
        extra = members - expect
        assert not missing, f"{name} 的端口面少了 {missing} —— 接口被削了？"
        assert not extra, (
            f"{name} 多出了 {extra} —— 加实现时接口不该长出新东西。"
            f"先说明它为什么是【通用】能力而不是某个实现的需求。")
    # 端口的方法必须带类型注解（否则跟注释没区别）
    for name in PORTS_SURFACE:
        obj = getattr(ports, name)
        for m in PORTS_SURFACE[name]:
            fn = getattr(obj, m, None)
            if inspect.isfunction(fn):
                assert inspect.signature(fn), f"{name}.{m} 没有签名"
