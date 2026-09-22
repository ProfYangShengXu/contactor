---
name: contactor
description: 要把活派给本机另一个 agent、或想让别的 agent 帮你看一眼时用。先查 agents/list 发现谁在，再 send 委托。
metadata:
  trigger: 派活给别的 agent、委托、让 dsh 做、让别的 agent 看、本机 agent、agent 互通、contactor、a2a、多 agent
---

# contactor：把活派给本机另一个 agent

**何时用**：
- 手上这个任务**更适合另一个 agent 干**（例：Hermes 想用 dsh 的沙箱跑代码；反过来 dsh 想用 Hermes 的工具）
- 想要**第二双眼睛**（让另一个 agent 独立看一遍）
- 有长活要甩出去，自己继续干别的

**不适用**：
- 自己就能干 → 直接干，别为派活而派活
- 要写桥/改桥本身、或判断该用哪个协议 → `agent-interop-protocols`
- 只是想要一个子任务拆解 → 那是 planner 的事，不是跨 agent

---

## 第 0 步：桥活着吗

```bash
curl -s --max-time 5 http://127.0.0.1:8791/health
# → {"ok":true,"agents":["dsh","hermes","hermes_cli"]}
```

**不通** → 桥没起。问用户，或（如果知道配置在哪）`contactor -c <config.yaml> serve`。
**不要**自己猜别的端口、也不要以为能直接调某个 agent。

---

## 第 1 步：发现 —— 谁在？它能干什么？

**★ 不要假设哪个 agent 在，也不要假设它能干什么。先查。**

```bash
contactor agents          # 所有人 + 每张名片（含 capabilities）
```

或直接走协议：

```bash
curl -s -X POST http://127.0.0.1:8791/ -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"agents/card","params":{"agent":"dsh"}}'
```

名片里**最要紧的是 `capabilities`**：

| 字段 | 意思 | 怎么用它 |
|---|---|---|
| `inputRequired: true` | 它会中途要放行 | 你得准备回答（见第 3 步） |
| `inputRequired: false` | **不能中断** —— 它要是等输入，只会挂到超时 | **别把需要逐步放行的活派给它** |
| `streaming: true` | 可以边跑边看 | 长任务用 `message/stream` |
| `contentVerified: false` | **桥不保证结果正确** | 结果自己验，别直接采信 |

> 这几条是**连接前**就能读到的。踩了坑才知道是没看名片。

---

## 第 2 步：派活

```bash
contactor send <agent> "任务描述" --wait 280
```

**写任务描述时记住三件事**：

1. **对方没有你的上下文** —— 要自包含。路径给绝对路径，指代写清楚（"那个文件" 它会不知道是哪个）
2. **给它理由，不只是指令** —— 为什么做、要验证什么假设，决定它遇到取舍时怎么选
3. **给入口不给穷举** —— 告诉它该读哪个文件/目录，别把内容抄一遍

想边跑边看：

```bash
contactor send <agent> "..." --stream
```

---

## 第 3 步：它要放行怎么办（`input-required`）

**这是本地 agent 的常态，不是异常。** 常见于：要写工作区外的文件、要跑危险命令。

```
[working]
[input-required]
[需要放行] 可选：allow_once, reject_once
→ 用 contactor answer <taskId> "<你的回答>" 继续
```

```bash
contactor answer <taskId> "yes"      # 或 "no"
```

**判断该不该放行**：看它到底要干什么、影响面多大。
**⚠️ 不要闭眼放行** —— 这是唯一一道人（或上游 agent）还能拦住它的关口。
**⚠️ 别原地等** —— 任务处于 `input-required` 时桥会释放该 agent 的队列，
你可以先去干别的，回头再答。

**如果收到「放行请求已失效」**：桥在这期间重启过（待决请求存在内存里，不能持久化）。
→ 重新发起这个任务。

---

## 第 4 步：收结果

```bash
contactor get <taskId>       # 状态 + 历史 + 产出
```

产出在 `artifacts` 里。**注意三个坑**：

1. **`state: completed` 只表示「执行完毕」，不表示「结果正确」** —— 桥不判断内容。
   结果要自己验（跑一遍、看 diff、对照事实）。
2. **思路（thoughts）也在产出里**，在 data part 的 `thoughts` 字段。
   想知道它**为什么**这么做时看这里，比只看答案有用。
3. 失败分两种，处理方式不同：
   - `retryable: true`（超时/网络抖）→ **可以重发**，但**带上同一个 `messageId`**，否则副作用会执行两遍
   - `retryable: false`（参数错/能力不够）→ **重试是错的**，同样的输入只会得到同样的错。换个 agent，或改任务描述

---

## 幂等：重发一定要带 `messageId`

```bash
contactor send dsh "..." --message-id my-job-001
```

**同一个 `messageId` 重发 → 返回同一个 Task，不重跑。**
**不传 `messageId` 就没有幂等** —— 网络抖一下重试，副作用就执行两遍。

凡是有副作用的活（写文件、发请求、改状态），**都带上**。

---

## 省事的写法：直接走 HTTP

不想 shell 出去的话，桥就是一个 JSON-RPC 端点：

```
POST http://127.0.0.1:8791/
```

```
message/send          {agent, text, messageId?, contextId?}   → Task
message/stream        同上，SSE 流式
tasks/get             {taskId}
tasks/answer          {taskId, answer}      ⚠️ 本项目自定义，非 A2A 规范
tasks/cancel          {taskId}
agents/list           {}   → {agents: [...], cards: {...}}
agents/card           {agent} → {card}      ← 连接前先读这个
```

`GET /.well-known/agent-card.json` 是桥自己的名片。

---

## 别做的事

- ❌ **别假设某个 agent 在** —— 先 `agents/list`
- ❌ **别把需要放行的活派给 `inputRequired: false` 的 agent** —— 它会挂到超时
- ❌ **别把 `completed` 当成「结果对」** —— `contentVerified` 恒为 false
- ❌ **别在有副作用的委托上省 `messageId`**
- ❌ **别闭眼放行** —— 放行是最后一道关口
- ❌ **别为了派活而派活** —— 自己两步能干完的就自己干

---

## 排错

| 现象 | 处置 |
|---|---|
| `/health` 不通 | 桥没起。问用户，别猜 |
| `没有这个 agent：xxx（现有：[...]）` | 用返回的现有列表挑一个 |
| 任务停在 `input-required` 很久 | 你在等它，它在等你 —— `contactor answer` |
| 一直不出结果 | 查 `contactor get <id>` 看状态；可能已经在 `input-required` 了 |
| `放行请求已失效` | 桥重启过，重新发起任务 |
| 超时后重发 | ✅ 可以，但**必须带同一个 `messageId`** |

---

## 相关的

- `agent-interop-protocols` —— 协议本身（FC / MCP / ACP / A2A 的边界、桥接设计规则）
- 项目主页与源码：<https://github.com/ProfYangShengXu/contactor>
