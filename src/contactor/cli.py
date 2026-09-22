"""命令行客户端。

★ 这个文件是【dsh → 桥】的通道：
   dsh 在 WSL 里可以直接执行 Windows 的 exe（实测可行），
   所以 dsh 通过 /mnt/c/.../python.exe -m contactor.cli send ... 反调桥，
   不用碰防火墙、不用改绑定地址。

★ 本文件是【能力送达路径】的一道关：
   桥有的能力（幂等键 / 名片 / SSE），CLI 必须都能触发。
   桥实现了但 CLI 不暴露 = 那个能力对使用者不存在。

用法：
  contactor serve [-c config.yaml]
  contactor agents                        列出本机 agent + 每张名片
  contactor card <agent>                  看单个 agent 的名片
  contactor send <agent> "<text>"         派活
  contactor answer <taskId> "<text>"      回答 input-required
  contactor get <taskId>                  查任务
"""
from __future__ import annotations
import argparse, asyncio, io, json, os, subprocess, sys, time, uuid
import httpx


def _rpc(url: str, method: str, params: dict, timeout: float = 60):
    with httpx.Client(timeout=timeout) as c:
        r = c.post(url, json={"jsonrpc": "2.0", "id": 1,
                              "method": method, "params": params})
        r.raise_for_status()
        return r.json()


def _cfg(args):
    from .config import Config
    return Config.load(args.config) if args.config else Config()


def _fmt_cap(caps: dict) -> str:
    """把名片里的 capabilities 排成一行 —— 这是委托方最该先看的三个开关。"""
    keys = ["streaming", "inputRequired", "contentVerified"]
    return "  ".join(f"{k}={'true' if caps.get(k) else 'false'}" for k in keys)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="contactor")
    ap.add_argument("-c", "--config", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve")

    p = sub.add_parser("up", help="★ 确保桥在跑：已在跑就直接用，没跑就 detach 起一个")
    p.add_argument("--timeout", type=float, default=45, help="等健康检查通过的上限秒数")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("down", help="停掉由 up 起来的桥")
    p.add_argument("--timeout", type=float, default=10)

    p = sub.add_parser("agents", help="列出本机 agent + 每张名片")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("card", help="看单个 agent 的名片")
    p.add_argument("agent")

    p = sub.add_parser("send", help="派活给某个 agent")
    p.add_argument("agent"); p.add_argument("text")
    p.add_argument("--context-id", default=None)
    p.add_argument("--message-id", default=None,
                   help="★ 幂等键。同一个 id 重发 → 返回同一个 Task，不重跑。"
                        "有副作用的委托务必带上，否则重试会执行两遍")
    p.add_argument("--wait", type=float, default=1800)
    p.add_argument("--stream", action="store_true", help="SSE 边跑边看")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("answer", help="回答 input-required")
    p.add_argument("task_id"); p.add_argument("text")
    p.add_argument("--wait", type=float, default=1800)

    p = sub.add_parser("get", help="查任务状态与产出")
    p.add_argument("task_id")

    args = ap.parse_args(argv)
    cfg = _cfg(args)
    url = f"http://{cfg.bind_host}:{cfg.bind_port}/"

    if args.cmd == "serve":
        from .wiring import build
        srv = build(cfg, logger=lambda m: print(m, file=sys.stderr, flush=True))
        asyncio.run(srv.serve())
        return 0

    if args.cmd == "up":
        return _up(args, cfg, url)
    if args.cmd == "down":
        _down.cfg_path = args.config
        return _down(url, args.timeout)

    if args.cmd == "agents":
        r = _rpc(url, "agents/list", {})
        res = r.get("result") or {}
        ags, cards = res.get("agents", []), res.get("cards", {})
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2)); return 0
        if not ags:
            print("（本机没有配任何 agent）"); return 0
        print("本机 agent：")
        for a in ags:
            c = cards.get(a) or {}
            print(f"\n  {a}")
            if c.get("capabilities"):
                print(f"      {_fmt_cap(c['capabilities'])}")
            if c.get("description"):
                print(f"      {c['description']}")
        print("\n  ★ inputRequired=false 的 agent 不能用来做需要逐步放行的任务")
        print("  ★ contentVerified 恒为 false —— 桥不判断结果对不对，自己验")
        return 0

    if args.cmd == "card":
        r = _rpc(url, "agents/card", {"agent": args.agent})
        if "error" in r:
            print(json.dumps(r["error"], ensure_ascii=False)); return 2
        print(json.dumps(r["result"]["card"], ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "send":
        mid = args.message_id or uuid.uuid4().hex
        params = {"agent": args.agent, "text": args.text,
                  "messageId": mid, "contextId": args.context_id}
        if args.stream:
            return _stream(url, params, args.wait)
        r = _rpc(url, "message/send", params)
        if "error" in r:
            print(json.dumps(r["error"], ensure_ascii=False)); return 2
        return _follow(url, r["result"]["task"], args.wait, args.json)

    if args.cmd == "answer":
        r = _rpc(url, "tasks/answer", {"taskId": args.task_id, "text": args.text})
        if "error" in r:
            print(json.dumps(r["error"], ensure_ascii=False)); return 2
        return _follow(url, r["result"]["task"], args.wait, False)

    if args.cmd == "get":
        r = _rpc(url, "tasks/get", {"taskId": args.task_id})
        print(json.dumps(r.get("result") or r.get("error"), ensure_ascii=False, indent=2))
        return 0
    return 1


# ── 桥的启停 ────────────────────────────────────────────────
#  ★ 为什么必须 detach：
#    调用方的进程（agent 的一次工具调用 / shell）一结束，
#    它起的子进程会跟着被收走 —— 桥必须活过那次调用。
#  ★ 为什么先查健康再起：
#    一个桥就够了。多个 agent 各自起一个 = N 个桥抢同一个端口，
#    而且互相看不见。判据是「先发现，再补位」，不是「先起再说」。

def _state_dir(cfg) -> str:
    d = os.path.dirname(os.path.abspath(cfg.db_path)) or "."
    os.makedirs(d, exist_ok=True)
    return d


def _health(url: str, timeout: float = 3) -> dict | None:
    try:
        r = httpx.get(url + "health", timeout=timeout)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def _up(args, cfg, url) -> int:
    # 1) 已经在跑 → 直接用（这才是常态）
    h = _health(url)
    if h:
        if args.json:
            print(json.dumps({"started": False, "health": h}, ensure_ascii=False)); return 0
        print(f"桥已在运行 http://{cfg.bind_host}:{cfg.bind_port}")
        print("  agents: " + ", ".join(h.get("agents", []) or ["（无）"]))
        return 0

    # 2) 没跑 → detach 起一个
    sd = _state_dir(cfg)
    log_path = os.path.join(sd, "serve.log")
    pid_path = os.path.join(sd, "serve.pid")
    argv = [sys.executable, "-m", "contactor.cli"]
    if args.config:
        argv += ["-c", os.path.abspath(args.config)]
    argv += ["serve"]

    flags = 0
    if os.name == "nt":
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))
    log = open(log_path, "ab", buffering=0)
    try:
        proc = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            creationflags=flags, start_new_session=(os.name != "nt"), close_fds=True)
    except Exception as e:
        print(f"起桥失败：{e}", file=sys.stderr); return 2
    finally:
        log.close()
    with open(pid_path, "w", encoding="utf-8") as f:
        f.write(str(proc.pid) + "\n")

    # 3) 等健康检查通过
    end = time.time() + args.timeout
    while time.time() < end:
        time.sleep(0.4)
        h = _health(url)
        if h:
            if args.json:
                print(json.dumps({"started": True, "pid": proc.pid, "health": h},
                                 ensure_ascii=False)); return 0
            print(f"桥已起来 http://{cfg.bind_host}:{cfg.bind_port}  (pid {proc.pid})")
            print("  agents: " + ", ".join(h.get("agents", []) or ["（无）"]))
            print(f"  日志：{log_path}")
            return 0
        if proc.poll() is not None:
            break                                   # 子进程自己退了，别再等
    print(f"桥起来后 {args.timeout}s 内健康检查没通", file=sys.stderr)
    print(f"  看日志：{log_path}", file=sys.stderr)
    try:
        tail = io.open(log_path, encoding="utf-8", errors="replace").read()[-800:]
        if tail.strip(): print("  --- 日志尾部 ---\n" + tail, file=sys.stderr)
    except Exception:
        pass
    return 5


def _down(url: str, timeout: float) -> int:
    from .config import Config
    cfg = Config.load(_down.cfg_path) if getattr(_down, "cfg_path", None) else None
    pid_path = None
    if cfg:
        pid_path = os.path.join(_state_dir(cfg), "serve.pid")
    if not pid_path or not os.path.exists(pid_path):
        print("找不到 pid 文件（桥可能是别的方式起的，自己停）", file=sys.stderr); return 1
    try:
        pid = int(io.open(pid_path, encoding="utf-8").read().strip())
    except Exception as e:
        print(f"pid 文件读不了：{e}", file=sys.stderr); return 1
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=timeout)
        else:
            os.kill(pid, 15)
    except Exception as e:
        print(f"停不了 pid {pid}：{e}", file=sys.stderr); return 1
    end = time.time() + timeout
    while time.time() < end:
        if not _health(url, 2):
            os.remove(pid_path)
            print(f"桥已停 (pid {pid})"); return 0
        time.sleep(0.3)
    print(f"pid {pid} 发出停止信号后仍能响应", file=sys.stderr); return 5


def _stream(url: str, params: dict, wait: float) -> int:
    """走 message/stream（SSE），边跑边打印事件。"""
    tid, final, printed, last_state = None, None, False, None
    try:
        with httpx.Client(timeout=httpx.Timeout(wait, connect=10)) as c:
            with c.stream("POST", url, json={"jsonrpc": "2.0", "id": 1,
                                             "method": "message/stream",
                                             "params": params}) as r:
                if r.status_code >= 400:
                    print(f"HTTP {r.status_code}", file=sys.stderr); return 2
                for line in r.iter_lines():
                    if not line.startswith("data: "):
                        continue
                    ev = json.loads(line[6:])
                    tid = ev.get("task_id") or tid
                    kind = ev.get("kind")
                    if kind == "status":
                        st = ev.get("state")
                        if st != last_state:            # 同一状态只打一次
                            print(f"[{st}]", file=sys.stderr, flush=True)
                            last_state = st
                        if ev.get("is_final"):
                            final = st
                    elif kind == "message":
                        # 流式吐答案：边到边打
                        for p in (ev.get("message") or {}).get("parts", []):
                            if p.get("kind") == "text" and p.get("text"):
                                print(p["text"], end="", flush=True)
                                printed = True
                    elif kind == "artifact" and not printed:
                        # ★ 只有前面没流过 message 才打 artifact ——
                        #   否则同一段答案会打两遍（实测踩到）
                        for p in (ev.get("artifact") or {}).get("parts", []):
                            if p.get("kind") == "text" and p.get("text"):
                                print(p["text"])
                                printed = True
                    if final:
                        break
    except httpx.HTTPError as e:
        print(f"连接失败：{e}", file=sys.stderr); return 2
    if printed:
        print()                                     # 流式输出后补一个换行
    if final == "input-required":
        print(f"\n→ 用 contactor answer {tid} \"<你的回答>\" 继续")
        return 4
    return 0 if final == "completed" else 3


def _follow(url: str, task: dict, wait: float, as_json: bool) -> int:
    """轮询到终态，打印结果。"""
    tid = task["task_id"]
    end = time.time() + wait
    last = None
    while time.time() < end:
        r = _rpc(url, "tasks/get", {"taskId": tid})
        t = (r.get("result") or {}).get("task") or {}
        st = t.get("state")
        if st != last:
            print(f"[{st}]", file=sys.stderr, flush=True)
            last = st
        if st in ("completed", "failed", "canceled", "rejected"):
            if as_json:
                print(json.dumps(t, ensure_ascii=False, indent=2))
            else:
                if st == "completed":
                    for a in t.get("artifacts", []):
                        for p in a.get("parts", []):
                            if p.get("kind") == "text":
                                print(p.get("text", ""))
                else:
                    print(t.get("error") or st)
            return 0 if st == "completed" else 3
        if st == "input-required":
            if as_json:
                print(json.dumps(t, ensure_ascii=False, indent=2))
            else:
                print(t.get("pending_question") or "（需要输入）")
                print(f"\n→ 用 contactor answer {tid} \"<你的回答>\" 继续")
            return 4                                   # 4 = 需要人介入
        time.sleep(0.5)
    print("超时", file=sys.stderr); return 5


if __name__ == "__main__":
    sys.exit(main())
