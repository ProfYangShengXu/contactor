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
from contactor.domain.models import Message, Part, Task, TaskState

PY = sys.executable


class Ctx:
    workspace = str(pathlib.Path.cwd())
    context_id = "ctx-test"
    logs: list[str] = []
    def log(self, m): self.logs.append(m)


def task(tid="t1"):
    return Task(task_id=tid, agent="cli", context_id="ctx-test")


def msg(text="hello"):
    return Message(role="user", message_id="m-1",
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
    assert evs[-1].state == TaskState.COMPLETED and evs[-1].is_final
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
    be = SubprocessCliBackend("cli", [PY, "-c", "pass"])
    card = await be.card()
    assert card.capabilities["inputRequired"] is False, \
        "不支持中断这件事必须出现在名片上"
    assert card.capabilities["streaming"] is False
    assert card.capabilities["contentVerified"] is False


async def test_resume_is_rejected_loudly():
    be = SubprocessCliBackend("cli", [PY, "-c", "pass"])
    with pytest.raises(BackendFailure) as e:
        async for _ in be.resume(task(), msg("yes")):
            pass
    assert "不支持中断" in str(e.value)


async def test_requires_command():
    with pytest.raises(ValueError):
        SubprocessCliBackend("bad", None)


# ── ★ 架构红线的实战验证：新 backend 不改任何稳定侧文件 ──────
def test_adding_backend_touched_no_stable_layer():
    """加一个 backend，domain/ 和 ports.py 必须【一个字节都没动】。

    这是架构红线的实战判据 —— 比 grep 词汇表更有说服力：
    真加了个实现，稳定侧却毫无感知。
    """
    import subprocess
    root = pathlib.Path(__file__).parents[1]
    r = subprocess.run(["git", "status", "--porcelain", "src/contactor/domain",
                        "src/contactor/ports.py"],
                       cwd=root, capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip("不是 git 仓库，跳过")
    assert r.stdout.strip() == "", f"稳定侧被改动了：\n{r.stdout}"
