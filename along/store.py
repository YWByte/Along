import contextlib
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path


def uid(prefix: str) -> str:
    return prefix + "_" + uuid.uuid4().hex


def dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Conflict(ValueError):
    pass


class Store:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory = directory
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(directory / "along.sqlite3", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS tasks(
          id TEXT PRIMARY KEY, goal TEXT NOT NULL, status TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
          state TEXT NOT NULL, plan TEXT, created REAL NOT NULL, updated REAL NOT NULL);
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_task ON tasks((1))
          WHERE status IN ('draft','active','paused');
        CREATE TABLE IF NOT EXISTS ledger(
          id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
          kind TEXT NOT NULL, data TEXT NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS checkpoints(
          id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
          version INTEGER NOT NULL, state TEXT NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS sources(
          id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
          title TEXT NOT NULL, url TEXT NOT NULL, content TEXT NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS media(
          id TEXT PRIMARY KEY, task_id TEXT REFERENCES tasks(id) ON DELETE CASCADE,
          conversation_id TEXT, kind TEXT NOT NULL, path TEXT NOT NULL, captured REAL NOT NULL,
          received REAL NOT NULL, seq INTEGER NOT NULL, bytes INTEGER NOT NULL, digest TEXT NOT NULL,
          indexed INTEGER NOT NULL DEFAULT 0, expired INTEGER NOT NULL DEFAULT 0,
          UNIQUE(task_id,kind,seq));
        CREATE INDEX IF NOT EXISTS media_time ON media(task_id,kind,captured);
        CREATE TABLE IF NOT EXISTS events(
          id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
          start REAL NOT NULL, end REAL NOT NULL, data TEXT NOT NULL, frame_ids TEXT NOT NULL,
          revision INTEGER NOT NULL DEFAULT 1, embedding TEXT, embedding_model TEXT);
        CREATE INDEX IF NOT EXISTS event_time ON events(task_id,start);
        CREATE TABLE IF NOT EXISTS jobs(
          id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
          frame_ids TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
          available REAL NOT NULL, error TEXT);
        CREATE TABLE IF NOT EXISTS messages(
          id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
          task_id TEXT REFERENCES tasks(id) ON DELETE CASCADE, role TEXT NOT NULL,
          text TEXT NOT NULL, data TEXT, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS suggestions(
          id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
          text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'proposed', history TEXT NOT NULL,
          evidence_ids TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS requests(
          id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, status TEXT NOT NULL,
          result TEXT, created REAL NOT NULL);
        PRAGMA user_version=1;
        """)
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(media)")}
        if "origin" not in columns:
            self.conn.execute("ALTER TABLE media ADD COLUMN origin TEXT NOT NULL DEFAULT 'camera'")
            self.conn.commit()
        self.conn.execute("PRAGMA user_version=2")
        with self.tx() as db:
            db.execute("UPDATE jobs SET status='pending' WHERE status='running'")
            db.execute("UPDATE requests SET status='failed' WHERE status='running'")
            # A crashed plan draft can be retried explicitly, never silently marked ready.
            rows = db.execute("SELECT * FROM tasks WHERE status='draft'").fetchall()
            for row in rows:
                state = json.loads(row["state"])
                if state.get("preparing"):
                    state.update(preparing=False, error="准备过程被中断，可重新生成计划")
                    db.execute("UPDATE tasks SET state=? WHERE id=?", (dump(state), row["id"]))

    @contextlib.contextmanager
    def tx(self):
        with self.lock:
            try:
                yield self.conn
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise

    def rows(self, sql, args=()):
        with self.lock:
            return [dict(row) for row in self.conn.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        rows = self.rows(sql, args)
        return rows[0] if rows else None

    def task(self, task_id):
        row = self.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not row:
            raise KeyError("任务不存在")
        row["state"] = json.loads(row["state"])
        row["plan"] = json.loads(row["plan"]) if row["plan"] else None
        row["suggestions"] = self.rows("SELECT * FROM suggestions WHERE task_id=?", (task_id,))
        row["sources"] = self.rows("SELECT id,title,url,created FROM sources WHERE task_id=?", (task_id,))
        return row

    def current(self):
        row = self.one("SELECT id FROM tasks WHERE status IN ('draft','active','paused')")
        return self.task(row["id"]) if row else None

    def create_task(self, goal: str):
        goal = goal.strip()
        if len(goal) < 2:
            raise ValueError("请提供具体任务目标")
        task_id = uid("task")
        state = {"steps": {}, "notes": [], "open_issues": [], "preparing": False,
                 "error": None, "plan_revision": 0}
        try:
            with self.tx() as db:
                db.execute("INSERT INTO tasks(id,goal,status,state,created,updated) VALUES(?,?,'draft',?,?,?)",
                           (task_id, goal, dump(state), time.time(), time.time()))
                self._ledger(db, task_id, "task_started", {"goal": goal})
        except sqlite3.IntegrityError as exc:
            raise Conflict("已有未结束任务，请先结束或删除，再开始新任务") from exc
        return self.task(task_id)

    def _ledger(self, db, task_id, kind, data):
        db.execute("INSERT INTO ledger VALUES(?,?,?,?,?)", (uid("log"), task_id, kind, dump(data), time.time()))

    def log(self, task_id, kind, data):
        with self.tx() as db:
            self._ledger(db, task_id, kind, data)

    def change(self, task_id, version, callback):
        with self.tx() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise KeyError("任务不存在")
            if row["version"] != version:
                raise Conflict("任务已变化，请刷新后重试；旧操作没有覆盖新状态")
            state = json.loads(row["state"])
            plan = json.loads(row["plan"]) if row["plan"] else None
            status, state, plan, kind, detail = callback(dict(row), state, plan, db)
            db.execute("UPDATE tasks SET version=version+1,status=?,state=?,plan=?,updated=? WHERE id=?",
                       (status, dump(state), dump(plan) if plan else None, time.time(), task_id))
            self._ledger(db, task_id, kind, detail)
            db.execute("INSERT INTO checkpoints VALUES(?,?,?,?,?)", (
                uid("cp"), task_id, version + 1,
                dump({"status": status, "state": state, "plan": plan}), time.time()))
        return self.task(task_id)

    def messages(self, conversation_id, limit=8, task_id=None):
        if task_id:
            rows = self.rows("SELECT role,text FROM messages WHERE task_id=? ORDER BY created DESC LIMIT ?",
                             (task_id, limit))
        else:
            rows = self.rows("SELECT role,text FROM messages WHERE conversation_id=? AND task_id IS NULL "
                             "ORDER BY created DESC LIMIT ?", (conversation_id, limit))
        return rows[::-1]

    def message(self, conversation_id, task_id, role, text, data=None):
        with self.tx() as db:
            db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?)", (
                uid("msg"), conversation_id, task_id, role, text, dump(data) if data else None, time.time()))

    def begin_request(self, request_id, fingerprint):
        with self.tx() as db:
            row = db.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
            if row:
                if row["fingerprint"] != fingerprint:
                    raise Conflict("同一请求 ID 不能用于不同内容")
                if row["status"] == "done":
                    return json.loads(row["result"])
                if row["status"] == "running":
                    raise Conflict("该请求正在处理中")
                db.execute("UPDATE requests SET status='running' WHERE id=?", (request_id,))
            else:
                db.execute("INSERT INTO requests VALUES(?,?,'running',NULL,?)",
                           (request_id, fingerprint, time.time()))
        return None

    def complete_request(self, request_id, result=None):
        with self.tx() as db:
            db.execute("UPDATE requests SET status=?,result=? WHERE id=?",
                       ("done" if result is not None else "failed", dump(result) if result is not None else None,
                        request_id))

    def delete_task(self, task_id):
        self.task(task_id)
        paths = self.rows("SELECT path FROM media WHERE task_id=?", (task_id,))
        with self.tx() as db:
            db.execute("DELETE FROM tasks WHERE id=?", (task_id,))
            # Cached responses can include deleted evidence; clear all idempotency results.
            db.execute("DELETE FROM requests")
        for row in paths:
            (self.directory / row["path"]).unlink(missing_ok=True)

    def close(self):
        self.conn.close()
