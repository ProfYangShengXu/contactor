"""★ 架构红线的机器化校验。

⚠️ 这个文件自己踩过两个坑（2026-09-22 实测），都写在这里防止再犯：

① 词汇检查必须【剥掉 docstring 和注释再查】——
   ports.py 的 docstring 里写着"本文件不许出现 ACP/session/..."，
   字面命中了自己的红线。规则解释自己的时候会违反规则。

② 裸 print 检查【不能一刀切】——
   cli.py 是命令行客户端，stdout 就是给人看的输出，不是协议通道。
   纪律管的是"stdout 承载协议流量"的那些文件：
   backends/（读别人的 stdout）和 transport/stdio*（自己的 stdout 就是协议流）。
"""
import ast, pathlib, re
import pytest

SRC = pathlib.Path(__file__).parents[1] / "src" / "contactor"

FORBIDDEN = re.compile(r"\b(ACP|session|prompt|stdio|subprocess|进程|子进程)\b")

# ★ ② 只有这些位置受"stdout 纪律"约束
STDOUT_DISCIPLINE_DIRS = ("backends", "transport")


def _code_text(path: pathlib.Path) -> str:
    """剥掉 docstring 和注释，只留可执行代码的文本。"""
    src = path.read_text("utf-8")
    tree = ast.parse(src)
    # 收集所有 docstring 节点
    doc_nodes = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and \
               isinstance(body[0].value, ast.Constant) and \
               isinstance(body[0].value.value, str):
                doc_nodes.add(id(body[0].value))
    # 用 tokenize 逐 token 过滤注释
    import tokenize, io as _io
    out = []
    for tok in tokenize.generate_tokens(_io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            continue
        if tok.type == tokenize.STRING and tok.string.startswith(('"""', "\'\'\'")):
            continue                       # 粗过滤三引号块（docstring 主体）
        out.append(tok.string)
    return "\n".join(out)


def _imports(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text("utf-8"))
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module:
            out.append(n.module)
        elif isinstance(n, ast.Import):
            out += [a.name for a in n.names]
    return out


# ① 依赖方向
def test_domain_has_no_internal_imports():
    for p in (SRC / "domain").glob("*.py"):
        for m in _imports(p):
            assert not m.startswith(("contactor", ".")), f"{p} 违反规则①"


def test_ports_only_imports_domain():
    for m in _imports(SRC / "ports.py"):
        assert not any(k in m for k in ("backends", "stores", "transport", "runtime")), \
            f"ports.py 违反规则②: {m}"


def test_runtime_does_not_import_implementations():
    for p in (SRC / "runtime").glob("*.py"):
        for m in _imports(p):
            assert not any(k in m for k in ("backends", "stores", "transport")), \
                f"{p.name} 违反规则③: {m}"


def test_backends_do_not_import_each_other():
    for p in (SRC / "backends").glob("*.py"):
        if p.name in ("acp.py", "__init__.py"):
            continue
        assert "from .acp" not in p.read_text("utf-8"), f"{p.name} 违反规则④"


# ⑥ ★ 架构红线（剥掉注释/docstring 再查）
def test_ports_has_no_tech_vocabulary():
    hits = FORBIDDEN.findall(_code_text(SRC / "ports.py"))
    assert not hits, f"★ 架构红线破了：ports.py 的【代码】里出现 {hits}"


def test_domain_has_no_tech_vocabulary():
    for p in (SRC / "domain").glob("*.py"):
        hits = FORBIDDEN.findall(_code_text(p))
        assert not hits, f"★ 架构红线破了：{p.name} 的【代码】里出现 {hits}"


# ⑦ stdout 纪律（★ 只查承载协议流的地方）
def test_no_bare_print_where_stdout_is_protocol():
    bad = []
    for d in STDOUT_DISCIPLINE_DIRS:
        for p in (SRC / d).rglob("*.py"):
            for i, line in enumerate(p.read_text("utf-8").splitlines(), 1):
                s = line.strip()
                if s.startswith("print(") and "stderr" not in s:
                    bad.append(f"{p.relative_to(SRC)}:{i}")
    assert not bad, f"裸 print()（这些位置 stdout 承载协议流量）: {bad}"
