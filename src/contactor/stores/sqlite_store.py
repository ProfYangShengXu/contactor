"""Task 元数据持久化。只存委托关系，不存 agent 内部。"""
from __future__ import annotations
import json, sqlite3, time
from pathlib import Path
from ..domain.models import (Message, Part, PendingDecision, Task, TaskError,
                             TaskState)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  taskId     TEXT PRIMARY KEY,
  contextId  TEXT NOT NULL,
  traceId    TEXT NOT NULL DEFAULT '',
  agent       TEXT NOT NULL,
  state       TEXT NOT NULL,
  pending    TEXT,
  error       TEXT,
  createdAt  REAL NOT NULL,
  updatedAt  REAL NOT NULL,
  artifacts   TEXT NOT NULL DEFAULT '[]',
  delegationDepth  INTEGER NOT NULL DEFAULT 0,
  visitedAgents    TEXT    NOT NULL DEFAULT '[]',
  requiresReview   INTEGER NOT NULL DEFAULT 1,
  originMessageId TEXT
);
CREATE INDEX IF NOT EXISTS idx_state   ON tasks(state);
CREATE INDEX IF NOT EXISTS idx_context ON tasks(contextId);
CREATE UNIQUE INDEX IF NOT EXISTS idx_origin_msg
  ON tasks(originMessageId) WHERE originMessageId IS NOT NULL;

CREATE TABLE IF NOT EXISTS messages (
  messageId TEXT PRIMARY KEY,
  taskId    TEXT NOT NULL,
  role       TEXT NOT NULL,
  parts      TEXT NOT NULL,
  ts         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_task ON messages(taskId);
"""

_COLS = ("taskId, contextId, traceId, agent, state, pending, error,"
         " createdAt, updatedAt, artifacts, delegationDepth, visitedAgents,"
         " requiresReview, originMessageId")


def _dump_error(e: TaskError | None) -> str | None:
    """★ error 是结构体，落库要序列化。

    为什么不留成人话字符串：调用方拿到的不该只是一句话 ——
    至少要知道「能不能重试」和「拿什么号去找对端问」（教案 2.4）。
    """
    return json.dumps(e.model_dump(), ensure_ascii=False) if e else None


def _dump_pending(d: PendingDecision | None) -> str | None:
    """★ 等拍板的状态要落库 —— 桥重启后委托方还得能 `answer`（server.py 的 recovery）。"""
    return json.dumps(d.model_dump(), ensure_ascii=False) if d else None


def _load_pending(raw) -> PendingDecision | None:
    if not raw:
        return None
    if isinstance(raw, str):
        try:
            return PendingDecision(**json.loads(raw))
        except Exception:
            # 兼容老库里的纯字符串（那时只存了权限请求的一句话）
            return PendingDecision(kind="permission", question=raw)
    return PendingDecision(**raw)


def _load_error(raw) -> TaskError | None:
    if not raw:
        return None
    if isinstance(raw, str):
        try:
            return TaskError(**json.loads(raw))
        except Exception:
            # 兼容老库里的纯字符串
            return TaskError(code="legacy", message=raw, retryable=False)
    return TaskError(**raw)


class SqliteTaskStore:
    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    async def create(self, task: Task) -> None:
        self.conn.execute(
            "INSERT INTO tasks (taskId, contextId, traceId, agent, state,"
            " createdAt, updatedAt, delegationDepth, visitedAgents,"
            " requiresReview, originMessageId) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (task.taskId, task.contextId, task.traceId, task.agent, task.state.value,
             task.createdAt, task.updatedAt, task.delegationDepth,
             json.dumps(task.visitedAgents, ensure_ascii=False),
             int(task.requiresReview), task.originMessageId))
        self.conn.commit()

    async def save(self, task: Task) -> None:
        self.conn.execute(
            "UPDATE tasks SET state=?, pending=?, error=?, updatedAt=?,"
            " artifacts=? WHERE taskId=?",
            (task.state.value, _dump_pending(task.pending), _dump_error(task.error),
             task.updatedAt,
             json.dumps([a.model_dump() for a in task.artifacts], ensure_ascii=False),
             task.taskId))
        self.conn.commit()

    async def append_message(self, taskId: str, msg: Message) -> None:
        # ★ INSERT OR IGNORE：重放/重试可能重复插同一条（messageId 是主键，天然去重）
        self.conn.execute(
            "INSERT OR IGNORE INTO messages (messageId, taskId, role, parts, ts)"
            " VALUES (?,?,?,?,?)",
            (msg.messageId, taskId, msg.role,
             json.dumps([p.model_dump() for p in msg.parts], ensure_ascii=False),
             time.time()))
        self.conn.commit()

    async def get(self, taskId: str) -> Task | None:
        row = self.conn.execute(
            f"SELECT {_COLS} FROM tasks WHERE taskId=?", (taskId,)).fetchone()
        return self._row_to_task(row) if row else None

    async def find_by_origin_message(self, messageId: str) -> Task | None:
        row = self.conn.execute(
            "SELECT taskId FROM tasks WHERE originMessageId=?", (messageId,)).fetchone()
        return await self.get(row[0]) if row else None

    async def list_by_state(self, state: TaskState) -> list[Task]:
        rows = self.conn.execute(
            f"SELECT {_COLS} FROM tasks WHERE state=? ORDER BY createdAt",
            (state.value,)).fetchall()
        return [self._row_to_task(r) for r in rows]

    def _row_to_task(self, row) -> Task:
        return Task(
            taskId=row[0], contextId=row[1], traceId=row[2], agent=row[3],
            state=TaskState(row[4]), pending=_load_pending(row[5]),
            error=_load_error(row[6]), createdAt=row[7], updatedAt=row[8],
            artifacts=json.loads(row[9]), delegationDepth=row[10],
            visitedAgents=json.loads(row[11]), requiresReview=bool(row[12]),
            originMessageId=row[13], history=self._history(row[0]))

    def _history(self, taskId: str) -> list[Message]:
        return [Message(messageId=r[0], taskId=taskId, role=r[1],
                        parts=[Part(**p) for p in json.loads(r[2])])
                for r in self.conn.execute(
                    "SELECT messageId, role, parts FROM messages"
                    " WHERE taskId=? ORDER BY ts", (taskId,))]
