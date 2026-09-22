# A2A-Local 架构设计

> 让本机多个 agent 进程（Hermes / dsh / grok-build / 未来的）能**互相发现、互相委托**。
>
> 版本：v1（2026-09-22）　状态：设计定稿，未实现

---

> 本文是 contactor 的架构设计文档（**为什么这么做**）。
> 实现代码见仓库根目录，使用说明见 `README.md`。

## 〇、这个项目要解决什么

**现状**：本机跑着好几个 agent，但它们互相看不见。

```
Hermes        常驻（QQ/飞书网关 + cron + 技能）
dsh           按需起（WSL，编码 agent）
grok-build    按需起（Rust 编码 agent）
```

想做的事：**Hermes 能把一个编码任务直接委托给 dsh，拿回结果；dsh 能反过来问 Hermes 要一条记忆。**

今天没有这个能力 —— 不是因为缺协议，是因为缺**发现**和**对等委托**。

---

## 一、⚠️ 先纠正一个前提：本机 agent 不是「没有通信能力」

设计前先做了探测（实测），结果和最初设想不同：

```
dsh            packages/acp/  →  automation-only ACP server
               启动：pnpm dsh --profile acp
               协议：标准 ACP v1，JSON-RPC over stdio
               已有方法：initialize / authenticate / session/new / session/list /
                        session/resume / session/close / session/set_config_option /
                        session/prompt / session/cancel / session/update /
                        session/request_permission
               另有：packages/sdk/（JSON-RPC protocol + TS client/server）
                     headless 模式、Web UI @ 127.0.0.1:3080

grok-build     crates/codegen/xai-grok-shell  →  leader / stdio / headless 入口
               README 原文："embedded in editors via the Agent Client Protocol (ACP)"

Hermes         acp_adapter/（server.py 106KB + session/permissions/tools/events/…）
               启动：hermes acp 或 python -m acp_adapter.entry
               协议：ACP，stdout 专供 JSON-RPC，日志走 stderr
               另有：gateway/platforms/api_server.py —— OpenAI 兼容 HTTP 服务
                     （/v1/chat/completions、/v1/runs/{id}/events SSE、
                       /v1/runs/{id}/approval、/api/sessions/…）
```

**结论：三家都已经会讲 ACP 了。**

所以缺的不是「通信协议」，是 ACP 没设计的三件事：

| 缺什么 | 为什么 ACP 给不了 |
|---|---|
| **发现** | ACP 没有目录/名片机制 —— 客户端必须**事先知道**要起哪个 agent 的可执行文件 |
| **对等委托** | ACP 是**父子进程**模型（agent 是客户端起的子进程）。agent A 想找 agent B，它得先知道怎么起 B |
| **跨进程互找** | ACP 的 session 是「我控制它」，不是「我委托它」 |

**这三条正好就是 A2A 的 Agent Card + Task + 「agent 找 agent」。**

> ### 这一条决定了整个项目的规模
>
> **不是「给 N 个 agent 写 N 个适配器」，是「写一个 ACP↔A2A 的桥」。**
>
> 因为三家都支持 ACP，一个 `AcpBackend` 就能覆盖它们（以及未来所有支持 ACP 的 agent）。
> 这就是 ACP 带来的杠杆 —— 把一个 N 的问题变成一个 1 的问题。

---

## 二、分层推导（不套模板）

按 `architecture-design` 的方法：**先列变化频率，再按频率切层。**

### 第 1 步：列变化频率

从最易变排到最稳定：

```
最易变
  ├─ 每个 agent 的接入方式（ACP / HTTP API / CLI / 未来新协议）
  │     ← 月级变，而且【一定会加新的 agent】
  ├─ 传输（HTTP 回环 / stdio / 命名管道）
  │     ← 半年级
  ├─ 持久化（SQLite / 内存 / 以后换 Postgres）
  │     ← 年级
  ├─ A2A 协议语义（Task 状态机 / Message / Artifact / Part 的形状）
  │     ← 只有 A2A 规范改版才变
  └─ 「一次委托 = 一个 Task，有生命周期」这个领域概念
        ← 基本不变
最稳定
```

**判断依据（为什么这样排）**：
- 「接入方式」是**公司特有 + 工具特有**的 → 易变
- 「Task 有生命周期」是**行业通用 + 协议规定**的 → 稳定
- 验证法：如果改一个 agent 的接入方式，要不要同时改 Task 状态机？**不要** → 它们该在不同层

### 第 2 步：按频率切层

```
层                      变化频率        里面放什么
───────────────────────────────────────────────────────────────
domain                  最稳定          数据形状 + 状态机迁移规则 + 错误分类
ports                   稳定            ★ 三个接口：AgentBackend / TaskStore / EventSink
runtime                 稳定            编排逻辑：收委托 → 建 Task → 驱动 backend → 收事件
──────────────────────── 以上四项改动需要改协议，属"核心" ────────────────────────
backends                最易变          AcpBackend / HttpApiBackend / CliBackend
transport               易变            http_jsonrpc / stdio_jsonrpc
stores                  易变            sqlite_store
wiring                  唯一装配点      读配置 → new 出实现 → 注入核心
```

### 第 3 步：依赖方向（全部指向稳定侧）

```
                          ┌──────────────┐
                          │   domain     │  ← 零外部依赖
                          │  models.py   │
                          │  lifecycle.py│
                          │  errors.py   │
                          └──────▲───────┘
                                 │
                          ┌──────┴───────┐
                          │   ports.py   │  ★ 接口定义在稳定侧
                          │ AgentBackend │
                          │  TaskStore   │
                          │  EventSink   │
                          └──────▲───────┘
                                 │
        ┌────────────────────────┼────────────────────────┐
        │                        │                        │
  ┌─────┴──────┐          ┌──────┴──────┐          ┌──────┴──────┐
  │  runtime   │          │  backends/  │          │   stores/   │
  │ dispatcher │          │     acp     │          │   sqlite    │
  │  registry  │          │  http_api   │          └──────▲──────┘
  └─────▲──────┘          │     cli     │                 │
        │                 └──────▲──────┘                 │
  ┌─────┴──────┐                 │                        │
  │  server.py │                 │                        │
  │ + transport│                 │                        │
  └─────▲──────┘                 │                        │
        │                        │                        │
        └────────────────────────┴────────────────────────┘
                                 │
                          ┌──────┴───────┐
                          │  wiring.py   │  ★ 全项目唯一 new 对象的地方
                          └──────────────┘
```

**校验三条铁律**：

- **SDP**（依赖指向稳定）：`backends` 依赖 `ports` ✅；`ports` 不 import `backends` ✅
- **SAP**（稳定者抽象）：被两边依赖的 `ports.py` **全是接口/Protocol**，没有具体实现 ✅
- **ADP**（无环）：依赖图无环 ✅

### 第 4 步：唯一装配点

`wiring.py` 里的一个函数：

```python
def build(config: Config) -> A2AServer:
    store    = SqliteTaskStore(config.db_path)          # 实现
    backends = {name: make_backend(spec)                # 实现
                for name, spec in config.agents.items()}
    registry = CardRegistry(config.cards_dir)
    dispatcher = Dispatcher(store=store, backends=backends, registry=registry)
    transport  = HttpJsonRpcTransport(config.bind)      # 实现
    return A2AServer(dispatcher=dispatcher, transport=transport)
```

**全项目只有这一处 `new`。** 测试时换掉它 = 换掉整个运行环境。

---

## 三、★ 核心决策记录

### 决策 1：A2A Server 是「给 agent 套的外壳」，不改 agent 源码

**选择**：独立的 A2A Server 进程，通过 ACP（或 HTTP）驱动被包装的 agent。

**备选**（记录，不实现）：
- 给每个 agent 写原生 A2A 插件 → 要改三家源码、跟着人家升级维护，且 dsh/grok-build 是 TS/Rust，改动成本极高
- 用现成的 `a2a-sdk`（PyPI v1.1.5）→ 它解决的是「怎么对外暴露 A2A」，不解决「怎么驱动本机 agent」；而且本项目的学习目标是**自己实现协议语义**（面试/工业对齐优先）

**理由**：agent 升级不影响桥；桥挂了 agent 照常用；三家都不用动。

---

### 决策 2：★ 不写 CLI 适配器，写 ACP 桥 —— 这是全项目最值钱的一条

**选择**：`AcpBackend` 一个类覆盖 dsh / grok-build / Hermes。

**理由**：三家都已实现 ACP server（见第一节实测）。

**杠杆**：

```
写 CLI 适配器： N 个 agent → N 个适配器，每个都要处理
               起进程 / 喂 prompt / 解析 stdout / 判失败 / 读退出码
               —— 而且每个 agent 的输出格式都不一样

写 ACP 桥：     1 个 AcpBackend → 覆盖全部，外加未来所有支持 ACP 的 agent
               （ACP 已经规定了 session/new、session/prompt、session/update、
                 session/request_permission —— 不用我猜）
```

**CLI 兜底**（`CliBackend`）保留，但**明确标为二等公民**：只给「什么协议都不会」的 agent 用，
且能力受限（无流式、无权限征求）。

---

### 决策 3：协议层不知道 ACP 的存在（端口定义在稳定侧）

**这是本项目的架构红线。**

`domain/` 和 `ports.py` 里**不许出现**这些词：`ACP`、`session`、`prompt`、`stdio`、`subprocess`、`进程`。

**判据**（Cockburn 强档自检）：

> 把 `ports.py` 拿给一个**不懂 coding agent** 的人看，他能看懂吗？

能 —— 因为里面只有 `Task` / `Message` / `Artifact` / `Backend` 这些通用词。

**反例（弱实现）**：
```python
class AgentBackend(Protocol):
    async def start_acp_session(self, cwd: str, mcp_servers: list) -> str: ...
    #                    ↑ 技术词汇漏进接口了 —— 这是「知道要用 ACP，绑死在 ACP 上」
```

**正例（强实现）**：
```python
class AgentBackend(Protocol):
    async def card(self) -> AgentCard: ...
    async def submit(self, task: Task, ctx: TaskContext) -> AsyncIterator[BackendEvent]: ...
    #                ↑ 只谈「委托」和「事件」，不谈怎么实现
```

将来加一个不用 ACP 的 agent，`ports.py` **一个字都不用改**。

---

### 决策 4：`input-required` 是本地场景的主角，不是边角料

**这是本地 A2A 和公网 A2A 最大的不同。**

本机 agent 天天在问「这条命令要不要放行」：

```
dsh      session/request_permission —— "A permission prompt with one-shot allow/reject"
Hermes   edit_approval.py / permissions.py / 终端审批
```

**映射**：

```
ACP 的 session/request_permission  →  A2A 的 Task.state = input-required + 一条 Message
```

所以本地 A2A 里 `input-required` **不是理论概念，是最常走的分支**。
设计上必须把它当一等公民：状态持久化要能停在 `input-required` 上，跨进程重启要能恢复。

**反面**：把 `input-required` 实现成「抛个异常等人」→ 桥一重启，等人放行的任务就全丢了。

---

### 决策 5：传输默认 HTTP 回环，不用 stdio

**理由**：stdio 天然是 **1:1**（父进程起子进程）。而本地 A2A 要的是 **N:N**（谁都能找谁，
且每个 server 要能被独立启动、独立存活）。

**选择**：A2A Server 是**常驻进程**，监听 `127.0.0.1:<port>`，走 JSON-RPC 2.0 over HTTP。

**保留 stdio transport**：A2A 规范里有，且「被别的进程拉起」的场景需要（比如一个临时 agent
只想被调一次）。

**传输与协议解耦**（MCP 那节 学的那条）：一份 handler，套两个 transport 适配器。

---

### 决策 6：发现 = 注册表文件 + Agent Card（两份，用途不同）

```
① 文件注册表   ~/.contactor/cards/<name>.json
               server 启动时写，退出时删
               委托方读整个目录 = "本机有哪些 agent 在跑"
               —— 本地场景用文件最自然，不需要服务发现中间件

② HTTP 端点    http://127.0.0.1:<port>/.well-known/agent-card.json
               协议一致性用（A2A 规范规定的路径）
```

**Agent Card 的内容**（本地裁剪版，见决策 7）：

```json
{
  "name": "dsh",
  "description": "DeepSeek Harness —— 编码 agent，能读写文件、跑命令",
  "url": "http://127.0.0.1:8791",
  "version": "0.1.5-rc.2",
  "capabilities": {
    "streaming": true,
    "pushNotifications": true,
    "stateTransitionHistory": true
  },
  "defaultInputModes": ["text/plain"],
  "defaultOutputModes": ["text/plain", "application/json"],
  "skills": [
    {"id": "code-edit", "name": "代码修改", "description": "...", "tags": ["code"]},
    {"id": "shell",     "name": "命令执行", "description": "...", "tags": ["shell"]}
  ]
}
```

> ⚠️ **Agent Card 是自我声明，不是担保**（A2A 那节 C2 的纪律）。
> 桥对 card 只做**格式校验**，不做**能力验证**。能力真伪由每次 Task 的实际产出说话。
> 这条要写进 README，免得以后有人把 card 当 SLA。

---

### 决策 7：本地只实现 A2A 的一个子集，缺的明确列出

**实现**：
```
message/send              发消息（返回 Task 或直接返回 Message）
message/stream            同上，SSE 流式
tasks/get                 查状态 + 历史
tasks/cancel              取消
tasks/pushNotificationConfig/set   配 webhook
```

**不做**（记录在案，不是遗漏）：
```
❌ 不做公网 / 跨机 —— 只 127.0.0.1，不出回环
❌ 不做鉴权 —— 同用户回环。⚠️ 但代码里预留 securitySchemes 字段，未来要加不用改协议
❌ 不做自治联邦 / 自由发现 / agent 市场 —— 那是自治联邦的前沿话题，本地不需要
❌ 不做多租户 / 配额 / 计费
❌ 不做 A2A 全量规范 —— 只做本地够用的部分，缺口逐条列在 README
❌ 不做内容校验 —— 桥不判断 agent 返回的内容对不对。
   但必须让这个风险【在名片上可见】：
   · Task.requires_review 恒为 True
   · TaskState.COMPLETED 的语义 = "执行完毕"，不是"结果正确"
   · AgentCard.capabilities 加 contentVerified: false
```

---

### 决策 8：Task 持久化到 SQLite，但**只存元数据**

**存什么**（= A2A 那节 A1 说的「编排器持有路由与生命周期标识」）：
```
task_id / context_id / 被委托的 agent / 状态 / 状态迁移历史
发出去的 Message / 收回来的 Artifact / 时间戳
```

**绝不存什么**（= A1 说的「绝不能持有」）：
```
❌ 被委托 agent 的内部状态、内部推理、内部记忆
```

**理由**：桥重启要能恢复（dsh 跑个重构可能几十分钟，桥不能一重启就丢）。
但**恢复的是「委托关系」，不是「agent 的内部」** —— 后者桥根本看不到（黑盒）。

---

### 决策 9：Task 分片 —— 同一 agent 的委托走单队列

**问题**：同一个 agent（比如 dsh）被两个委托方同时派活 → 工作目录冲突、资源抢占。

**选择**：**按 agent 名分片**（A2A 那节 B1 学的）：

```
hash(agent_name) % N → 一个 agent 的委托永远进同一个队列，串行执行
```

**为什么不用锁/仲裁**：分片让冲突**在结构上不存在**，不需要分布式锁
（免掉锁超时/脑裂/续期）。桥是本地单进程，分片就是一个 `dict[agent_name, asyncio.Queue]`。

**判据**（背这句）：**能不能在结构上让冲突不发生？能就分片，不能才回头用锁。**

---

### 决策 10：★ 委托链要防回环 —— 否则 A→B→A 会死锁

**问题**（架构对抗演练发现，2026-09-22）：

```
A 的桥：分片队列里 agent=B 占着（等 B 返回）
B 的桥：分片队列里 agent=A 占着（等 A 返回）
→ 互相等 → 死锁到 task_timeout_s
```

**选择**：Task 上带委托链元数据，**建 Task 之前**就拒绝。

```python
delegation_depth: int          # 委托深度
visited_agents: list[str]      # 这条链上经过谁
```

**双防线（都要有，理由不同）**：

```
① visited_agents 命中 → 拒绝     精准，抓真正的环
② depth >= max_depth(默认 3) → 拒绝   兜底，抓「中间环节没透传 visited」的长链
```

**为什么不能只留一条**：只留 ② 会误杀 A→B→C→D 这种正常长链；
只留 ① 会在元数据透传漏了的时候放环过去。

**判据（面试口径）**：**agent 之间的委托是"图遍历"，不是"树"——
只要有环就必须要环检测，否则就是分布式死锁。**

---

### 决策 11：★ `message/send` 必须支持幂等键

**问题**（同一场演练发现）：委托方网络抖动重发 → 建两个 Task → **对会改文件的 agent 派两遍活 = 副作用执行两次**。

**选择**：`message/send` 接受可选的 `messageId`，桥按它去重。

```python
if message_id:
    existing = await store.find_by_origin_message(message_id)
    if existing: return existing          # ★ 不新建、不重跑
```

数据库层用**唯一索引**兜底（`origin_message_id`），代码层和存储层双保险。

**⚠️ 但这要写清楚是【使用约定】不是【协议保证】**：

```
A2A 标准里 message/send 【没有】幂等键的约定。
messageId 是委托方自己生成的，桥只负责按它去重 ——
委托方不传 messageId，就没有幂等。
```

**判据**：**任何"会被重试"的写操作，都要有幂等键。**
（任何会被重试的写操作都要幂等键 —— 在这里的形态是 messageId。）

---

## 四、★ ACP ↔ A2A 映射表（实现照这张表写）

```
ACP（三家都现成）                        A2A（本项目要实现的）
──────────────────────────────────────────────────────────────────────
initialize                            ～   （A2A 无握手；本地不做能力协商）
authenticate                          ～   ❌ 不做（回环）
session/new                           ～   建 Task（submitted）
session/prompt                        ～   message/send
session/update（流式语义更新）           ～   message/stream →
                                            TaskStatusUpdateEvent / TaskArtifactUpdateEvent
session/request_permission            ～   ★ Task.state = input-required + 一条 Message
                                            （委托方回一条 Message 继续）
session/cancel                        ～   tasks/cancel
session/list / session/resume         ～   tasks/get（+ SQLite 里的历史）
session/close                         ～   Task 终结（completed / canceled）
session/set_config_option             ～   ❌ 不在 A2A 范围（桥自己管，不暴露）
```

### 三个必须处理的映射难点

**难点 1：ACP 的 session ≠ A2A 的 Task**

```
ACP session   一次长连接，可以跑很多 prompt（多轮）
A2A Task      一次委托

映射：一个 A2A Task = 一个 ACP session 里的一个 prompt
      但【同一个 context_id 的多个 Task】应复用同一个 ACP session
      —— 否则每次委托都重开会话，对方攒不起上下文
      （这就是 A2A 那节 B1 说的「contextId 每轮新建 → 多轮退化成单轮」）
```

**难点 2：`session/request_permission` 是双向的，A2A 的 `input-required` 要把它转成消息**

```
ACP：agent →（请求）→ 客户端，客户端必须同步回答 allow/reject
A2A：Task 进 input-required，委托方【异步】回一条 Message

所以桥要：
  ① 收到 permission 请求 → 存成一个「待决问题」挂在 Task 上
  ② Task.state = input-required，推一条 Message 出去
  ③ 委托方回 Message → 桥翻译成 ACP 的 permission 应答 → 唤醒 agent
```

**这是全项目最容易做错的一处** —— 因为两边的「等待」语义不同：
ACP 是**同步阻塞等回答**，A2A 是**异步状态机**。桥必须把同步转成异步，
且要能跨进程重启恢复（所以待决问题必须落盘）。

**难点 3：错误两分**

```
协议错误（方法不存在 / 参数不合法 / task_id 不存在）
   → JSON-RPC error 对象（给程序看）

任务执行错误（agent 崩了 / 超时 / 产出格式不合格）
   → Task.state = failed + 一条说明性 Message（给模型看，让它自纠）
```

（这是 MCP 那节 学过的 MCP 错误两分，A2A 同样两分。）

---

## 五、职责表

| 模块 | 干什么 | 什么情况归它 | 边界（不归它） |
|---|---|---|---|
| `domain/models.py` | Task/TaskState/Message/Part/Artifact/AgentCard/Skill 的数据形状 | 定义"长什么样" | 不管怎么存、怎么传 |
| `domain/lifecycle.py` | 状态机：哪些迁移合法 | 判断 `submitted → working` 行不行 | 不管谁触发迁移 |
| `domain/errors.py` | 错误两分（协议错 vs 执行错） | 分类 | 不管怎么上报 |
| `ports.py` ★ | 三个 Protocol：`AgentBackend` / `TaskStore` / `EventSink` | 定义"要什么能力" | **不含任何实现，不含任何技术词汇** |
| `runtime/dispatcher.py` | 收委托 → 建 Task → 驱动 backend → 收事件 → 迁移状态 → 落盘 | 编排 | 不知道 backend 是 ACP 还是别的 |
| `runtime/registry.py` | 扫 Agent Card 目录 | 发现 | 不验证能力真伪 |
| `backends/acp.py` ★ | 用 ACP 驱动 agent（覆盖三家） | 接入支持 ACP 的 agent | 不做协议语义（那是 domain） |
| `backends/http_api.py` | 用 Hermes 的 OpenAI 兼容 HTTP 口驱动 | 接入 Hermes HTTP | 同上 |
| `backends/subprocess_cli.py` | 兜底：起进程喂 prompt | 什么协议都不会的 agent（**二等公民**） | 同上 |
| `stores/sqlite_store.py` | Task 元数据持久化 | 存储 | 不存 agent 内部状态 |
| `transport/http_jsonrpc.py` | JSON-RPC over HTTP | 默认传输 | 不含协议语义 |
| `transport/stdio_jsonrpc.py` | JSON-RPC over stdio | 被拉起场景 | 同上 |
| `server.py` | 把 transport 收到的请求接给 dispatcher | 外壳 | 不做编排 |
| `wiring.py` ★ | 读配置 → new 实现 → 注入核心 | **全项目唯一 new 对象处** | 不含业务逻辑 |

### 接口归属清单

| 接口 | 定义在哪 | 由谁实现 |
|---|---|---|
| `AgentBackend` | `ports.py`（稳定侧） | `backends/*` |
| `TaskStore` | `ports.py`（稳定侧） | `stores/sqlite_store.py`（未来可换 PG） |
| `EventSink` | `ports.py`（稳定侧） | `transport/*` |

> **接口定义在稳定侧，不在实现侧。** 如果 `AcpBackend` 自己定义了「什么叫 backend」，
> 那就是端口位置错了 —— 依赖方向反了。

---

## 六、目录结构

```
contactor/
├── README.md                  # 含「不做什么」和 A2A 子集缺口清单
├── pyproject.toml
├── config.example.yaml
├── src/contactor/
│   ├── __init__.py
│   ├── domain/
│   │   ├── models.py          # 数据形状
│   │   ├── lifecycle.py       # 状态机
│   │   └── errors.py          # 错误两分
│   ├── ports.py               # ★ 三个 Protocol
│   ├── runtime/
│   │   ├── dispatcher.py
│   │   └── registry.py
│   ├── backends/
│   │   ├── acp.py             # ★ 覆盖 dsh / grok-build / Hermes
│   │   ├── http_api.py
│   │   └── subprocess_cli.py
│   ├── stores/
│   │   └── sqlite_store.py
│   ├── transport/
│   │   ├── http_jsonrpc.py
│   │   └── stdio_jsonrpc.py
│   ├── server.py
│   └── wiring.py              # ★ 唯一装配点
└── tests/
```

**为什么这么拆（对"别过度设计"的自我辩护）**：

`architecture-design` 说「项目活不过三个月 / 只有一个人用 → 别拆」。
本项目两条都不满足：

1. **agent 数量一定会加** —— 今天是 3 家，明天可能有 AstrBot、openclaw、或新装的
2. **是要给面试官看的** —— 用户明确"技术选型：面试/工业对齐优先于少运维"

所以 `backends/` 这个拆分不是过度设计，是**真实的变更轴**。

**但以下不做**（避免抽空接口）：
- 不为 `transport/` 抽抽象基类 —— 就两个实现，写成两个类够了
- 不做 plugin 动态加载 —— 配置里列出来，wiring 里 if/else 造，够了
- 不做依赖注入框架 —— 一个 `build()` 函数就是装配点

---

## 七、技术栈

```
Python 3.12
├── 标准库 asyncio        —— 全部异步
├── pydantic 2.13.4       —— 数据模型（本机已装）
├── fastapi 0.136.1       —— HTTP transport（本机已装）
├── uvicorn 0.47.0        —— ASGI server（本机已装）
├── httpx 0.28.1          —— 驱动 agent 用的 HTTP 客户端（本机已装）
├── httpx-sse 0.4.3       —— SSE 流式（本机已装）
└── sqlite3               —— 标准库，Task 持久化
```

**全部本机已装，零新增依赖**（唯一可能加的是 `pytest-asyncio` 做异步测试）。

**不用 `a2a-sdk`（PyPI v1.1.5）的理由**：
- 它解决「怎么对外暴露标准 A2A」，不解决「怎么驱动本机 agent」—— 后者是本项目的核心
- 本项目的学习目标是**自己实现协议语义**（今天刚学完 A2A 那节，正是练手时机）
- **但 README 里要写一句**：如果将来要对外暴露，可以套 `a2a-sdk` 做传输层，我们的 `domain/` 不用动
  —— 这正是端口定义在稳定侧的好处

---

## 八、验收标准与实测结果

设计时定的三档标准。**全部已实测**（2026-09-22，Win11 + WSL2，两个真实 agent）：

```
A 级（必须过）                                                   状态
  A1  domain/ 和 ports.py 里 grep 不到 ACP|session|prompt|...    ✅ 机器化校验，7 passed
  A2  一个 AcpBackend 能同时驱动 dsh 和 Hermes（不写两个类）      ✅ 同一个类，两条真实链路
  A3  Task 状态机能走完 submitted → working → completed          ✅
  A4  input-required 能跨桥重启恢复                              ✅ 重启后仍可查
  A5  error 两分：协议错走 JSON-RPC error，执行错走 failed        ✅
  A6  同一 agent 的并发委托被串行化（分片生效）                   ✅

B 级（应该有）                                                   状态
  B1  message/stream 的 SSE 能推状态事件和 artifact 事件          ✅ 并修了"订阅前事件丢失"
  B2  Agent Card 目录扫描 + /.well-known/agent-card.json          ✅ 并修了"名片从没被发布过"的洞
  B3  tasks/cancel 能真的中断 ACP 会话                            ✅
  B4  一个 fake backend（不接真 agent）跑通全链路测试             ✅ 33 passed 全走 fake

C 级（加分）                                                     状态
  C1  CliBackend 兜底能跑通一个纯命令行 agent                     ✅ subprocess_cli
  C2  pushNotificationConfig + webhook 能收到状态回调             ❌ 未做（明确不做）
  C3  ★ 两个 A2A Server 单向委托能跑通（A → B），
      并且【回环 A → B → A 被拒绝】                              ✅ 双防线，A5/A5b 实测拒绝
      —— 不是"回环能跑通"（那是死锁路径），是"回环被挡住"
```

**最大的一条标准其实是 B4/A1 合起来的意思**：
**在没有任何真 agent 的情况下把全部协议语义跑通。**
如果 dispatcher 里被迫 import 了 ACP 的东西，说明架构红线已经破了。

---

## 九、风险与实测结论

| 风险 | 影响 | 结论 |
|---|---|---|
| **ACP 三家实现有差异** | `AcpBackend` 可能要每家打补丁 | ✅ **未发生**。dsh 与 Hermes 用同一个类跑通，差异只在 workspace 路径格式 |
| **Hermes 的 ACP 是否完整暴露** | 可能只实现了子集 | ✅ 完整。`initialize` / `session/new` / `session/prompt` / `session/update` 全通 |
| **dsh 在 WSL 里** | 跨 Windows/WSL 边界的 stdio 和端口 | ✅ `wsl.exe` 拉起 + stdio 即可。**反向（WSL 调桥）用「WSL 执行 Windows exe」，不碰防火墙** |
| **`input-required` 的同步→异步转换** | 全项目最容易做错 | ✅ 实测跑通。收到放行请求 → 存 future + 落盘 + **立即 return**，绝不 await |
| **长任务的桥重启** | 委托丢失 | ✅ 停在 working 的一律标 FAILED，不留僵尸 |

### 实现前标为「未决」的三条 —— 实测答案

```
① hermes acp 实际暴露哪些方法
   → initialize / session/new / session/prompt / session/cancel
   字段名：session/new 返回 result.sessionId；
          session/update 带 params.sessionId + params.update.sessionUpdate
   取值：agent_thought_chunk · agent_message_chunk · usage_update · availableCommands

② dsh 在 WSL 里怎么被 Windows 侧拉起最稳
   → wsl -d Ubuntu -u root -- bash -lc "cd <项目> && exec pnpm dsh --profile acp"
   ★ 坑：workspace 必须是【agent 所在操作系统】的路径（dsh 要 Linux 路径）

③ grok-build 的 ACP 入口
   → 未验证（作者无 key，明确跳过）
```

### ⚠️ 三个实现前猜错、实测才发现的地方

```
① 终态不在 update 事件里
   猜的是 turn_complete / prompt_complete 之类的 update。
   真实是：session/prompt 这个【请求的响应】带回 {"stopReason": "end_turn"}

② ACP 根本没有 Artifact 概念
   全部走 session/update。A2A 里 Message 是过程、Artifact 是交付，必须自己拆：
     agent_message_chunk → 累积成答案 → turn 结束做成 Artifact 的 text part
     agent_thought_chunk → 累积成思路 → Artifact 的 data part
   第一版两者都当 message 存进 history → artifacts=0，CLI 什么都打不出来（但不报错）

③ sessionId 确实在 params 里
   补丁 1 猜对了 —— 但猜对和验证过是两回事
```

---

## 十、一句话总结

> **本机 agent 已经会讲 ACP（编辑器驱动 agent），缺的是 A2A（agent 委托 agent）。
> 所以这个项目不是「给 N 个 agent 写 N 个适配器」，是「写一个 ACP↔A2A 的桥」——
> 里面那层三家都现成，外面那层才是要写的。**

---

## 附：设计出处

本项目的每一处设计都能在一个「A2A 与多 Agent 互操作」的课程里找到出处。
下表把设计决策和它对应的原理对上：

| 设计 | 对应原理 |
|---|---|
| Agent Card + 文件注册表 | C2「Agent Card 是连接前的名片」 |
| `input-required` 当一等公民 | C2「input-required 是中断态，任务没死」 |
| ACP 的 permission → A2A 的 input-required | C2 + 难点 2 |
| Task 持久化只存元数据 | A1「编排器持有路由与生命周期标识，不持有内部执行细节」 |
| contextId 复用同一个 ACP session | A1「contextId 映射业务会话边界，不是请求边界」 |
| 错误两分 | 协议错给程序，执行错给模型 |
| 按 agent 名分片 | B1「能不能在结构上让冲突不发生？能就分片」 |
| Agent Card 只校验格式不验证能力 | C2「Agent Card 是自我介绍，不是担保」 |
| 端口不含技术词汇 | C1「A2A 边界之外不可见」+ architecture-design 强实现判据 |
