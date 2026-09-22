import sys, pathlib
_HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(_HERE.parent / "src"))
sys.path.insert(0, str(_HERE))          # ★ 让 fake_backend 可导入
