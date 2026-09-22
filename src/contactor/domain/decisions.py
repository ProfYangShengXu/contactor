"""★ 「需要你拍板」的显式契约 —— 本桥自己定义的，不是 A2A/ACP 的一部分。

── 为什么需要这个东西 ──────────────────────────────────────────

ACP 的 `session/request_permission` 只覆盖【权限放行】（allow / reject）。
**它不覆盖「选 A 还是 B」。** 所以一个 agent 干到一半停下来问你要选哪个时：

    任务状态 = completed
    artifact = 一段问句
    调用方   = 以为干完了，把问句当成果往下传

**这个失败是静默的** —— 没有报错，比超时难查得多。
（和我跑 lec14 代码骨架时撞到的发现②同族：`input-required` 与「还在跑」
 在调用方眼里长得一样；这里是第三个变体：「在问你」与「完成了」也长得一样。）

── 为什么不靠文本猜测 ────────────────────────────────────────

不要写「看到 `1) 2) 3)` 就当成选择题」这种规则：代码注释、清单、验收项里
全是 `1) 2)`。**猜文本的误判率是常量级的。**

所以改成**显式契约**：桥把下面这段话追加到每个派出去的 prompt 末尾
（`config.append_decision_contract`，默认开），agent 需要拍板时按格式输出。
契约是【给出去的】，所以它有读者 —— 不是一条只写在文档里的注释。

── ⚠️ 但契约进了 prompt，就会带来【复述误判】 ──────────────────

契约文本跟着 prompt 一起进去了，所以 agent 只要**复述或引用**这段契约
（"我看到你要求我在末尾标 `[[NEEDS_DECISION]]`…"），标记就会出现 ——
**而它根本没在问任何东西。**

反制不能靠加正则。**反制是把契约里已经写明的规则变成解析规则**：

    契约说「end your reply with exactly this block」→ 解析就要求【块必须收尾】
    契约说「RECOMMEND: <copy one option above verbatim>」→ 解析就拒绝占位符

于是「复述契约」天然不成立：复述后面还跟着 Rules 那几行，块没有收尾。
**判据：解析的严格程度必须能从契约本身推出来，而不是靠调参试出来的。**
"""
from __future__ import annotations
import re

from .models import PendingDecision

#: 标记串。选得足够不可能自然出现（双括号 + 全大写 + 下划线）。
MARKER = "[[NEEDS_DECISION]]"

#: 追加到出站 prompt 末尾的契约。**纯 ASCII**，避免被当成正文翻译掉。
CONTRACT = """
---
[contactor bridge contract - output protocol, not part of the task]
If you need the requester to decide something before you can continue,
END your reply with exactly this block, and nothing after it:

""" + MARKER + """
- <option 1>
- <option 2>
RECOMMEND: <copy one option above verbatim>
REASON: <one short line>

Rules:
- Put everything you already finished BEFORE the marker.
- Only use it when you genuinely cannot proceed.
- If you do not need a decision, never emit the marker.
"""

_OPT = re.compile(r"^\s*(?:[-*]|\d+[.)]|[a-d][.)])\s+(.+)$")
_REC = re.compile(r"^\s*RECOMMEND\s*:\s*(.+)$", re.I)
_RSN = re.compile(r"^\s*REASON\s*:\s*(.+)$", re.I)

#: 契约模板里的占位符。复述契约时它们会原样出现 —— 那是引用，不是决策。
_PLACEHOLDER = re.compile(r"^<.*>$")


def _is_placeholder(s: str) -> bool:
    return bool(_PLACEHOLDER.match(s.strip()))


def split_decision(text: str) -> tuple[str, PendingDecision | None]:
    """把 agent 的回复切成 (去掉决策块的正文, 结构化决策)。

    没有标记 → (原文, None)。
    有标记但**不构成一个合法的收尾块** → 也算没有（那是复述，不是决策）。

    ★ 用【最后一次】出现的位置切：agent 可能在前面的推理里引用过这个串。
    """
    if not text or MARKER not in text:
        return text, None

    head, _, tail = text.rpartition(MARKER)
    question_lines: list[str] = []
    options: list[str] = []
    recommend: str | None = None
    reason: str | None = None
    closed = False          # 块已经结束了吗

    for line in tail.splitlines():
        if not line.strip():
            continue
        if closed:
            # ★ 契约说「END your reply with exactly this block, and nothing after it」
            #   → 块后面还有实义内容 = 它在复述契约，不是在做决策。
            return text, None
        if m := _REC.match(line):
            recommend = m.group(1).strip()
        elif m := _RSN.match(line):
            reason = m.group(1).strip()
        elif m := _OPT.match(line):
            options.append(m.group(1).strip())
        elif not options and not recommend:
            question_lines.append(line.strip())     # 标记刚下、还没列选项 = 题干
        else:
            closed = True                            # 出现不认识的行 → 块结束
            return text, None

    # ★ 契约说「copy one option above verbatim」→ 占位符 / 空选项一律不算
    options = [o for o in options if not _is_placeholder(o)]
    if recommend and _is_placeholder(recommend):
        recommend = None
    if not options:
        return text, None

    if recommend and recommend not in options:
        options = options + [recommend]              # 推荐项也当候选，别让它悬空

    question = " ".join(question_lines).strip()
    if not question:
        body = [l.strip() for l in head.strip().splitlines() if l.strip()]
        question = body[-1] if body else ""

    return head.rstrip(), PendingDecision(
        kind="decision", question=question,
        options=options, recommend=recommend, reason=reason)
