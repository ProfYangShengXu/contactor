"""L4 外壳：把 transport 收到的请求接给 dispatcher。不做编排。"""
from __future__ import annotations
import asyncio, sys
import uvicorn
from .domain.models import TaskError, TaskState


class A2AServer:
    def __init__(self, *, dispatcher, registry, self_card, bus, config, app_factory,
                 logger=None):
        self.dispatcher, self.registry = dispatcher, registry
        self.self_card, self.bus, self.config = self_card, bus, config
        self.app_factory = app_factory
        self.log = logger or (lambda *a, **k: print(*a, file=sys.stderr, flush=True))
        self._server: uvicorn.Server | None = None

    async def startup(self) -> None:
        # ① 恢复
        await self._recover()
        # ② 发布名片
        await self._publish_cards()

    async def _publish_cards(self) -> None:
        """★ 把【每个 agent 自己的】名片发到文件注册表。

        ⚠️ 2026-09-22 实测补的洞：CardRegistry.publish() 写好了但从来没被调用过，
           注册表里只有桥自己。后果是 agent 在 card() 里声明的能力缺口
           （subprocess_cli 的 inputRequired=False）委托方【根本读不到】——
           名片写了等于没写。

        纪律：**任何"声明"都必须有一条把它送到读者手里的路径。**
             没有读者的声明只是注释。
        """
        self.registry.publish(self.config.self_name, self.self_card)
        for name, be in self.dispatcher.backends.items():
            try:
                card = await be.card()
            except Exception as e:                     # 一个坏名片不该拖垮启动
                self.log(f"[cards] {name} 的名片取不到：{e!r}")
                continue
            self.registry.publish(name, card)
        self.log(f"[cards] 已发布 {len(self.dispatcher.backends) + 1} 张名片")

    async def _recover(self) -> None:
        """★ 桥重启后的恢复 —— 最关键的一段。

        input-required：重新可查（委托方还能 answer），但 pending 的放行 future 已失效，
                        answer 时 backend 会明确报错（不静默吞）
        working       ：★ 上次跑一半就死了的 —— 一律标 FAILED，
                        绝不留在 working 变僵尸（working 不是终态）
        """
        ir = await self.dispatcher.store.list_by_state(TaskState.INPUT_REQUIRED)
        if ir:
            self.log(f"[recover] {len(ir)} 个任务停在 input-required（放行请求已失效）")
        wk = await self.dispatcher.store.list_by_state(TaskState.WORKING)
        for t in wk:
            t.error = TaskError(code="bridge_restarted",
                                message="桥重启，任务中断，请重新发起",
                                retryable=True, taskId=t.taskId,
                                correlationId=t.traceId)
            await self.dispatcher._transition(t, TaskState.FAILED, final=True)
        if wk:
            self.log(f"[recover] {len(wk)} 个 working 任务被标为 FAILED（避免僵尸）")

    async def serve(self) -> None:
        await self.startup()
        app = self.app_factory()
        cfg = uvicorn.Config(app, host=self.config.bind_host,
                             port=self.config.bind_port, log_level="warning")
        self._server = uvicorn.Server(cfg)
        try:
            await self._server.serve()
        finally:
            self.registry.unpublish(self.config.self_name)
