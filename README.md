# contactor

> 名字取自**接触器** —— 让两路电路接通的器件。这里是让两个 agent 接上话。

**让本机多个 agent 进程互相发现、互相委托。**

你在同一台机器上装了 Hermes、dsh、Claude Code、Codex……它们各自很能干，
**但它们之间不会说话**。contactor 是一个跑在 `127.0.0.1` 的 A2A 桥：
任何一个 agent 都能把活派给另一个，并拿回结果。

```
Hermes ──┐                              ┌── dsh
         ├──── contactor  127.0.0.1:8791 ┤
dsh    ──┘         A2A over JSON-RPC    └── Hermes
```

实测（本机 Win11 + WSL2，两个真实 agent）：

```
$ contactor send dsh "读 packages/acp/acp/README.md，三句话总结"
[working] → [completed]                                         7.5s

$ contactor answer <taskId> "yes"        # 它要放行 rm/写盘之类的操作
[working] → [completed]                                         继续跑完
```

---

## 它解决什么

本机 agent 之间的通信现状，通常是这样的：

| | 怎么通信 |
|---|---|
| Hermes ↔ 你 | 聊天 |
| dsh ↔ 你 | 终端 |
| Hermes ↔ dsh | **没办法** |

**但问题不在"它们不会讲同一种语言"。** 实测下来，三家的 ACP（Agent Client Protocol）都能跑：

```
dsh        pnpm dsh --profile acp
Hermes     python -m acp_adapter.entry
grok-build 也支持 ACP
```

**真正缺的是 ACP 没设计的三件事：**

```
发现          我怎么知道本机还有哪些 agent、它们各自能干什么？
对等委托      A 把一个任务交给 B —— 而不是"编辑器驱动 agent"这种主从关系
跨进程互找    agent 找 agent，不经过人的手
```

这三件事正好是 **A2A 协议的 Agent Card + Task + "agent 找 agent"**。
所以 contactor 不是给 N 个 agent 写 N 个适配器，而是**写一个 ACP ↔ A2A 的桥**。

---

## 快速开始

```bash
pip install -e ".[dev]"
cp config.example.yaml config.yaml     # 改成你本机的 agent 启动命令
python -m contactor.cli -c config.yaml serve
```

另开一个终端：

```bash
python -m contactor.cli -c config.yaml agents
python -m contactor.cli -c config.yaml send <agent名> "你的任务"
python -m contactor.cli -c config.yaml answer <taskId> "yes"   # 需要放行时
```

### 从 WSL 里反向调（WSL 里的 agent 调 Windows 侧的桥）

WSL 可以直接执行 Windows 的 exe，**不用碰防火墙、不用改绑定地址**：

```bash
/mnt/c/Users/<你>/.../python.exe -m contactor.cli \
  -c "C:\path\to\config.yaml" send <agent名> "你好"
```

> WSL2 是 NAT，Windows 侧绑 `127.0.0.1` 时 WSL 本来访问不到；
> 反过来走「WSL 执行 Windows 程序」这条路，双向都通了，且不引入任何网络暴露面。

---

## 为什么这样设计

四条不显然的判断。它们不是常识，是踩出来的。

### 1. `input-required` 是主角，不是边角料

其他 A2A 实现里，`input-required` 通常被当成罕见分支。
**但在本地场景它是最高频的分支** —— 本机 agent 天天在问"这条命令放行吗"。

所以桥把 ACP 的 `session/request_permission` 映射成 A2A 的 `INPUT_REQUIRED`，
并把它当成一等公民。

### 2. ACP 的权限请求是**同步阻塞**，A2A 的 `input-required` 是**异步状态机**

这是全项目最容易做错的一处：

```
ACP 侧：agent 子进程卡在那里，等一个响应，什么都干不了
A2A 侧：任务处于 INPUT_REQUIRED，桥必须【立刻释放】这个 agent 的队列，去干别的
```

桥的做法：收到放行请求 → **存一个 `asyncio.Future` + 落盘 + 立即 return**
（**绝不能 `await`，一等就占住分片队列**）。
委托方回话时再兑现那个 future。

代价是：**桥重启后这些 future 会失效**。所以停在 `input-required` 的任务重启后仍可查，
但回话时会拿到一个明确的错误，而不是静默吞掉。

### 3. `messageId` 幂等 —— 但这是**使用约定**，不是协议保证

```
同一个 messageId 重发 → 返回同一个 Task，不重跑
★ 委托方不传 messageId，就没有幂等
```

`AgentCard` 是**自我声明，不是担保**。桥只校验格式，不验证能力声明是否属实；
信任边界由**接收方**划定。同理，`TaskState.COMPLETED` 的语义是
**"执行完毕"不是"结果正确"** —— 所以 `Task.requires_review` 恒为 `True`。

### 4. 委托是图遍历，不是树 —— 必须有回环检测

A 委托 B，B 又委托 A，两个桥互相占着对方的队列 → **死锁到超时**。

两道防线：

```
① 精准命中：visitedAgents 里已有目标 agent → 拒绝
② 深度兜底：delegationDepth >= max_delegation_depth(默认 3) → 拒绝
```

★ 前提是**多跳委托时要把这两个字段透传下去**，否则只剩第二道防线。

---

## 接入方式

桥只认 `AgentBackend` 这一个端口。**加一种接法 = 加一个文件**，
`domain/`、`ports.py`、`dispatcher` 一个字都不用改（有测试断言这一点）。

| kind | 适用 | 状态 |
|---|---|---|
| `acp` | 会讲 ACP 的 agent（dsh / Hermes / grok-build） | ✅ |
| `subprocess_cli` | **兜底**：什么协议都不会的 agent | ✅ |
| `http_api` | OpenAI 兼容 HTTP 口 | 未实现 |

### `subprocess_cli` —— 二等公民，缺口写在名片上

```yaml
hermes_cli:
  kind: subprocess_cli
  command: ["C:\\path\\to\\hermes.exe"]
  prompt_via: arg          # stdin（默认）或 arg
  prompt_flag: ["-z"]      # arg 模式下插在 prompt 前的固定参数
  timeout_s: 300
```

```
❌ 无流式      进程跑完才有输出，中途看不到进展
❌ 无权限征求  没人能被问「这条命令放行吗」。agent 若等输入 → 只会挂到 timeout
❌ 无会话复用  每次委托起新进程，上下文攒不起来
✅ 喂 prompt 进 stdin（或当参数）→ 收 stdout 当 Artifact → 非零退出码当 FAILED
```

**关键不在它有什么，在它缺什么能被告知：**

```
$ contactor ... agents/card --agent hermes_cli
capabilities: {"streaming": false, "inputRequired": false, "contentVerified": false}
description : 命令行兜底 agent（无流式 / 无中断 / 无会话复用）
```

委托方在**连接前**读到 `inputRequired: false`，就该知道这个 agent
不能用来做需要逐步放行的任务 —— 而不是踩了坑才知道。

失败分类按"能不能重试"来分：

```
超时            → retryable=True    瞬时故障，可以重试（要带幂等键）
非零退出码      → retryable=False   逻辑故障，重试只会得到同样的错
退出 0 但空输出 → COMPLETED + meta.warning   桥不判断内容，但必须留痕
```

---

## 给 agent 用（不是给人用）

**这个项目是给 agent 当工具用的，所以它自带一份 `SKILL.md`。**

没有它，桥就是个「只有人知道怎么敲」的服务 —— agent 既不知道它在，
也不知道它能派活。装上这份 skill，agent 才知道先 `agents/list` 发现谁在、
再看名片上的 `capabilities` 决定能不能把活交给它。

```markdown
### Hermes Agent
cp SKILL.md ~/AppData/Local/hermes/skills/<category>/contactor/

### Claude Code
cp SKILL.md .claude/skills/contactor/

### dsh (DeepSeek Harness)
mkdir -p ~/.dsh/skills/contactor && cp SKILL.md ~/.dsh/skills/contactor/
> dsh 的 frontmatter 只认 name + description，本仓的 SKILL.md 已只带这两个
```

`SKILL.md` 里写了 agent 真正需要的四步：**先看桥在不在 → 发现谁在（读名片）→
派活（带幂等键）→ 需要放行时怎么答**，以及六个「别做的事」。

---

## 接口

JSON-RPC 2.0 over HTTP，单一端点 `POST /`。

| 方法 | 说明 |
|---|---|
| `message/send` | 派活。`agent` / `text` / `contextId?` / `messageId?` / `delegationDepth?` / `visitedAgents?` |
| `message/stream` | 同上，SSE 流式返回事件 |
| `tasks/get` | 查状态 + 历史 + 产出 |
| `tasks/cancel` | 取消 |
| **`tasks/answer`** | ⚠️ **本项目自定义**，标准 A2A 里没有 |
| `agents/list` | 列出本机 agent + 每张名片 |
| `agents/card` | 取单个名片（**连接前**先读，看它能不能干这活） |

另有两个 HTTP 端点：

```
GET /.well-known/agent-card.json    A2A 规范规定的名片路径
GET /health
```

### A2A 子集缺口（如实列出）

```
已实现   message/send · message/stream · tasks/get · tasks/cancel
未实现   agent/authenticatedExtendedCard
         tasks/pushNotificationConfig/get|delete
         tasks/resubscribe
         Artifact 的流式增量（append 标志）
```

**`tasks/answer` 是扩展，不是规范。** 标准 A2A 里续接 `input-required` 靠再发一条
带 `taskId` 的 `message/send`；本桥简化成了独立方法。

---

## 不做什么

```
❌ 不做鉴权 —— 只绑 127.0.0.1，同用户回环。
   ⚠️ 不要暴露到网络。AgentCard 里留了 securitySchemes 字段，要加不用改协议。
❌ 不做公网 / 跨机
❌ 不做自治联邦 / 自由发现 / agent 市场
❌ 不做内容校验 —— 桥不判断 agent 返回的内容对不对。
   但会让这个风险在名片上可见（见上）。
❌ 不做多租户 / 配额 / 计费
❌ 不改任何 agent 的源码
❌ 不依赖官方 a2a-sdk —— 协议语义自己实现
```

---

## 架构

```
domain/       数据形状 + 状态机 + 错误两分      ← 最稳定，零外部依赖
ports.py      ★ 三个 Protocol：AgentBackend / TaskStore / EventSink
config.py     ★ 唯一允许出现 agent 名字字面量的地方
runtime/      dispatcher（编排）· shards（分片）· registry（发现）
backends/     ★ acp.py 一个类覆盖所有 ACP agent；subprocess_cli.py 兜底
stores/       sqlite_store
transport/    http_jsonrpc（默认）· stdio_jsonrpc
server.py     外壳 + 重启恢复 + 名片发布
wiring.py     ★ 全项目唯一 new 对象的地方
```

### 🔴 一条架构红线

> **`ports.py` 和 `domain/` 的【代码】里不许出现
> `ACP|session|prompt|stdio|subprocess|进程`。**

由 `tests/test_layering.py` 机器化校验。判据：
**把这个文件拿给一个不懂 coding agent 的人看，他能看懂吗？**

将来加一个不用 ACP 的 agent，`ports.py` 一个字都不用改。

> ⚠️ 校验时必须**剥掉 docstring 和注释再查** —— 否则"本文件不许出现
> ACP/session/…"这句话本身会命中断言。**规则解释自己的时候会违反规则。**

---

## 常见故障

| 症状 | 原因 | 处置 |
|---|---|---|
| `session/new` 返回 `-32602 cwd must be an absolute path` | workspace 给了错的 OS 的路径 | dsh 在 WSL → Linux 路径；Hermes 在 Windows → Windows 路径 |
| agent 跑着跑着不动，无报错 | 子进程 stderr 写满 pipe buffer | 桥已接 stderr 管道 |
| 日志刷屏说 stdout 被污染 | agent 把日志打到了 stdout | 桥会在 50 行后熔断该任务；根治要改 agent |
| 任务永远停在 `working` | 桥启动时会把它标 FAILED | 若没标，查 `_recover()` |
| `放行请求已失效` | 桥在 `input-required` 期间重启过 | `asyncio.Future` 不能持久化，重新发起任务 |
| agent 起来了但没反应 | 用了没装 `acp` 包的那份 Python | 找有 venv 的那份 |

---

## 测试

```bash
pytest -q          # 33 passed
```

全部用 `tests/fake_backend.py`，**不接真 agent**。

> ★ 这一点是分水岭：**在没有任何真 agent 的情况下把协议语义跑通**，
> 说明协议层和 agent 层真的解耦了。
> 如果 dispatcher 里被迫 import 了 ACP 的东西，说明架构红线已经破了。

---

## 设计出处

本项目每一处设计都能在一个「A2A 与多 Agent 互操作」的课程里找到出处：

| 设计 | 出处 |
|---|---|
| Agent Card + 文件注册表 | 连接前的名片 |
| `input-required` 当一等公民 | 中断态，任务没死 |
| ACP permission → A2A input-required | 同步 → 异步的转换点 |
| Task 只存元数据 | 编排器持有路由与生命周期标识，不持有内部执行细节 |
| Agent Card 只校验格式不验证能力 | 名片是自我介绍，不是担保 |
| 委托回环双防线 | agent 委托是图遍历不是树，有环必须检测 |
| messageId 幂等 | 任何会被重试的写操作都要幂等键 |
| `requires_review` 恒 True | 桥不判断内容，但要让风险在名片上可见 |
| 兜底 backend 的缺口写进名片 | 能力边界要**连接前**可见 |
| 按 agent 名分片 | 能不能在结构上让冲突不发生？能就分片 |

**术语出处**：Agent2Agent (A2A) 协议由 Google 提出；
Agent Client Protocol (ACP) 由 Zed Industries 提出。
本仓库是独立实现，与二者均无关联，不代表其官方实现。

---

## 许可证

MIT —— 见 [LICENSE](LICENSE)。

仅供本机同用户环境使用。**本项目不提供鉴权，请勿暴露到网络。**
