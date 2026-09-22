"""命令行客户端。

★ 这个文件是【dsh → 桥】的通道：
   dsh 在 WSL 里可以直接执行 Windows 的 exe（实测可行），
   所以 dsh 通过 /mnt/c/.../python.exe -m contactor.cli send ... 反调桥，
   不用碰防火墙、不用改绑定地址。

用法：
  contactor serve [-c config.yaml]
  contactor agents                      列出本机 agent
  contactor send <agent> "<text>"       派活（阻塞到终态）
  contactor answer <taskId> "<text>"    回答 input-required
  contactor get <taskId>
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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="contactor")
    ap.add_argument("-c", "--config", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve")

    p = sub.add_parser("agents"); p.add_argument("--json", action="store_true")
    p = sub.add_parser("send")
    p.add_argument("agent"); p.add_argument("text")
    p.add_argument("--context-id", default=None)
    p.add_argument("--wait", type=float, default=1800)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("answer")
    p.add_argument("task_id"); p.add_argument("text")
    p.add_argument("--wait", type=float, default=1800)
    p = sub.add_parser("get"); p.add_argument("task_id")

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
        ags = (r.get("result") or {}).get("agents", [])
        if args.json:
            print(json.dumps(ags, ensure_ascii=False))
        else:
            print("本机 agent：" + ", ".join(ags) if ags else "（无）")
        return 0

    if args.cmd == "send":
        mid = uuid.uuid4().hex
        r = _rpc(url, "message/send",
                 {"agent": args.agent, "text": args.text,
                  "messageId": mid, "contextId": args.context_id})
        if "error" in r:
            print(json.dumps(r["error"], ensure_ascii=False)); return 2
        return _follow(url, r["result"]["task"], args.wait, getattr(args, "json", False))

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
