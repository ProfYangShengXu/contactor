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
import argparse, asyncio, json, sys, time, uuid
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
