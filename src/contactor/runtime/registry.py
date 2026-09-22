"""Agent Card 注册表：文件注册表（本地发现）+ HTTP 端点（协议一致性）。"""
from __future__ import annotations
import os, tempfile
from pathlib import Path
from ..domain.models import AgentCard


class CardRegistry:
    def __init__(self, cards_dir: str):
        self.dir = Path(cards_dir)

    def publish(self, name: str, card: AgentCard) -> None:
        """★ 原子写：先写临时文件再 rename，避免别的进程读到半个文件。"""
        self.dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.dir), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(card.model_dump_json(indent=2))
        os.replace(tmp, self.dir / f"{name}.json")

    def unpublish(self, name: str) -> None:
        try:
            (self.dir / f"{name}.json").unlink()
        except FileNotFoundError:
            pass

    def discover(self) -> dict[str, AgentCard]:
        """读整个目录 = 本机有哪些 agent。★ 坏 card 跳过，不阻断发现。"""
        out: dict[str, AgentCard] = {}
        if not self.dir.is_dir():
            return out
        for p in sorted(self.dir.glob("*.json")):
            try:
                out[p.stem] = AgentCard.model_validate_json(p.read_text("utf-8"))
            except Exception:
                continue
        return out
