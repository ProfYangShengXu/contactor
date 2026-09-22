"""「需要拍板」契约的解析规则。

★ 这些用例的严格程度**全部能从 decisions.CONTRACT 的措辞推出来**：
    契约说 "END your reply with exactly this block, and nothing after it" → 块必须收尾
    契约说 "RECOMMEND: <copy one option above verbatim>"                → 拒绝占位符
  不是调参调出来的。**解析规则和契约措辞对不上时，两边必有一个是错的。**
"""
from __future__ import annotations
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / "src"))

from contactor.domain.decisions import CONTRACT, MARKER, split_decision


def test_no_marker_is_passthrough():
    body, d = split_decision("干完了，改了 3 个文件。")
    assert d is None and body == "干完了，改了 3 个文件。"


def test_real_decision_is_parsed_and_body_is_clean():
    body, d = split_decision(
        "我已经把两种方案都试通了。\n"
        f"{MARKER}\n"
        "- 用 sqlite\n"
        "- 用 postgres\n"
        "RECOMMEND: 用 sqlite\n"
        "REASON: 本机场景不需要并发，少一个依赖\n")
    assert d is not None
    assert d.kind == "decision"
    assert d.options == ["用 sqlite", "用 postgres"]
    assert d.recommend == "用 sqlite"
    assert d.reason.startswith("本机场景")
    assert MARKER not in body, "产物里不该留协议标记"
    assert "我已经把两种方案都试通了" in body, "标记前的正文要保留"


def test_题干预设从标记上方取():
    _, d = split_decision(f"都查完了。\n存哪？\n{MARKER}\n- 本地\n- 云端\nRECOMMEND: 本地\n")
    assert d.question == "存哪？"


def test_echoing_the_contract_is_NOT_a_decision():
    """★ 头号误判：契约跟着 prompt 进去了，agent 复述它。

    复述后面还跟着 Rules 那几行 → 块没有收尾 → 不算决策。
    """
    echoed = f"我看到你要求在末尾标 {MARKER}，格式是：\n{CONTRACT}"
    body, d = split_decision(echoed)
    assert d is None, "复述契约不能被当成决策"
    assert body == echoed, "没识别成决策时不该改动原文"


def test_template_placeholders_are_not_options():
    """复述契约模板（<option 1> 这种占位符）不算列了选项。"""
    _, d = split_decision(f"{MARKER}\n- <option 1>\nRECOMMEND: <copy one option above verbatim>\n")
    assert d is None


def test_trailing_content_after_block_is_not_a_decision():
    _, d = split_decision(f"{MARKER}\n- A\n- B\nRECOMMEND: A\n\n顺便说一句，日志我放这了。\n")
    assert d is None, "块后面还有实义内容 = 没按契约收尾"


def test_bare_menu_without_recommend_is_still_surfaced():
    """没给推荐也要能上报 —— 缺推荐是【缺陷信号】，不是解析失败。

    （教案 5.3 那条：不许交裸选项。桥要做的是【让它可见】，不是替它编一个推荐。）
    """
    _, d = split_decision(f"{MARKER}\n- A\n- B\n- C\n")
    assert d is not None and d.options == ["A", "B", "C"]
    assert d.recommend is None


def test_last_marker_wins():
    """前面引用过、末尾真用了一次 → 认末尾那次。"""
    _, d = split_decision(
        f"（你让我用 {MARKER} 这个标记）\n活干完了。\n"
        f"{MARKER}\n- 保留\n- 删除\nRECOMMEND: 保留\n")
    assert d is not None and d.recommend == "保留"


def test_recommend_outside_options_gets_appended():
    _, d = split_decision(f"{MARKER}\n- A\n- B\nRECOMMEND: C\n")
    assert "C" in d.options, "推荐项不该悬在候选之外"
