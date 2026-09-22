"""Task 元数据持久化。只存委托关系，不存 agent 内部。"""
from __future__ import annotations
import json, sqlite3, time
from pathlib import Path
from ..domain.models import Message, Part, Task, TaskState

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  task_id     TEXT PRIMARY KEY,
  context_id  TEXT NOT NULL,
  agent       TEXT NOT NULL,
  state       TEXT NOT NULL,
  pending_question TEXT,
  error       TEXT,
  created_at  REAL NOT NULL,
  updated_at  REAL NOT NULL,
  artifacts   TEXT NOT NULL DEFAULT '[]',
  delegation_depth  INTEGER NOT NULL DEFAULT 0,
  visited_agents    TEXT    NOT NULL DEFAULT '[]',
  requires_review   INTEGER NOT NULL DEFAULT 1,
  origin_message_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_state   ON tasks(state);
CREATE INDEX IF NOT EXISTS idx_context ON tasks(context_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_origin_msg
  ON tasks(origin_message_id) WHERE origin_message_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS messages (
  message_id TEXT PRIMARY KEY,
  task_id    TEXT NOT NULL,
  role       TEXT NOT NULL,
  parts      TEXT NOT NULL,
  ts         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_task ON messages(task_id);
"""

_COLS = ("task_id, context_id, agent, state, pending_question, error,"
         " created_at, updated_at, artifacts, delegation_depth, visited_agents,"
         " requires_review, origin_message_id")


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
            "INSERT INTO tasks (task_id, context_id, agent, state, created_at,"
            " updated_at, delegation_depth, visited_agents, requires_review,"
            " origin_message_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (task.task_id, task.context_id, task.agent, task.state.value,
             task.created_at, task.updated_at, task.delegation_depth,
             json.dumps(task.visited_agents, ensure_ascii=False),
             int(task.requires_review), task.origin_message_id))
        self.conn.commit()

    async def save(self, task: Task) -> None:
        self.conn.execute(
            "UPDATE tasks SET state=?, pending_question=?, error=?, updated_at=?,"
            " artifacts=? WHERE task_id=?",
            (task.state.value, task.pending_question, task.error, task.updated_at,
             json.dumps([a.model_dump() for a in task.artifacts], ensure_ascii=False),
             task.task_id))
        self.conn.commit()

    async def append_message(self, task_id: str, msg: Message) -> None:
        # ★ INSERT OR IGNORE：重放/重试可能重复插同一条（message_id 是主键，天然去重）
        self.conn.execute(
            "INSERT OR IGNORE INTO messages (message_id, task_id, role, parts, ts)"
            " VALUES (?,?,?,?,?)",
            (msg.message_id, task_id, msg.role,
             json.dumps([p.model_dump() for p in msg.parts], ensure_ascii=False),
             time.time()))
        self.conn.commit()

    async def get(self, task_id: str) -> Task | None:
        row = self.conn.execute(
            f"SELECT {_COLS} FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return self._row_to_task(row) if row else None

    async def find_by_origin_message(self, message_id: str) -> Task | None:
        row = self.conn.execute(
            "SELECT task_id FROM tasks WHERE origin_message_id=?", (message_id,)).fetchone()
        return await self.get(row[0]) if row else None

    async def list_by_state(self, state: TaskState) -> list[Task]:
        rows = self.conn.execute(
            f"SELECT {_COLS} FROM tasks WHERE state=? ORDER BY created_at",
            (state.value,)).fetchall()
        return [self._row_to_task(r) for r in rows]

    def _row_to_task(self, row) -> Task:
        return Task(
            task_id=row[0], context_id=row[1], agent=row[2], state=TaskState(row[3]),
            pending_question=row[4], error=row[5], created_at=row[6], updated_at=row[7],
            artifacts=json.loads(row[8]), delegation_depth=row[9],
            visited_agents=json.loads(row[10]), requires_review=bool(row[11]),
            origin_message_id=row[12], history=self._history(row[0]))

    def _history(self, task_id: str) -> list[Message]:
        return [Message(message_id=r[0], task_id=task_id, role=r[1],
                        parts=[Part(**p) for p in json.loads(r[2])])
                for r in self.conn.execute(
                    "SELECT message_id, role, parts FROM messages"
                    " WHERE task_id=? ORDER BY ts", (task_id,))]
