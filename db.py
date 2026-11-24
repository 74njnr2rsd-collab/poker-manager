import os
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

# По умолчанию база лежит рядом с кодом. На хостинге можно задать DB_PATH
# например /data/poker.sqlite3, если платформа предоставляет постоянный диск.
DB_PATH = Path(os.getenv("DB_PATH", str(Path(__file__).resolve().parent / "poker.sqlite3")))

def conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("PRAGMA journal_mode = WAL")
    return c

def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def rowdict(row):
    return dict(row) if row else None

async def init_db():
    with conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS players (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(chat_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_session_per_chat
        ON sessions(chat_id) WHERE ended_at IS NULL;
        CREATE TABLE IF NOT EXISTS operations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL REFERENCES sessions(id),
            player_id INTEGER NOT NULL REFERENCES players(id),
            kind TEXT NOT NULL CHECK(kind IN ('buy','cashout')),
            amount INTEGER NOT NULL CHECK(amount >= 0),
            created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            cancelled INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            user_id INTEGER,
            username TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('user','assistant')),
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_chat_messages_chat_id_id
            ON chat_messages(chat_id, id DESC);
        CREATE TABLE IF NOT EXISTS chat_memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            memory_type TEXT NOT NULL DEFAULT 'summary',
            content TEXT NOT NULL,
            source_message_id INTEGER,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_chat_memories_chat_id_id
            ON chat_memories(chat_id, id DESC);
        CREATE TABLE IF NOT EXISTS chat_memory_state (
            chat_id INTEGER PRIMARY KEY,
            last_summarized_message_id INTEGER NOT NULL DEFAULT 0
        );
        """)
        c.commit()

async def add_player(chat_id, user_id, name):
    with conn() as c:
        c.execute("INSERT OR IGNORE INTO players(chat_id,user_id,name,created_at) VALUES(?,?,?,?)",
                  (chat_id, user_id, name, now()))
        row = c.execute("SELECT * FROM players WHERE chat_id=? AND user_id=?", (chat_id,user_id)).fetchone()
        return rowdict(row)

async def find_player(chat_id, user_id):
    with conn() as c:
        return rowdict(c.execute("SELECT * FROM players WHERE chat_id=? AND user_id=?", (chat_id,user_id)).fetchone())

async def list_players(chat_id):
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM players WHERE chat_id=? ORDER BY name", (chat_id,)).fetchall()]

async def create_session(chat_id):
    with conn() as c:
        cur = c.execute("INSERT INTO sessions(chat_id,started_at) VALUES(?,?)", (chat_id,now()))
        return rowdict(c.execute("SELECT * FROM sessions WHERE id=?", (cur.lastrowid,)).fetchone())

async def get_active_session(chat_id):
    with conn() as c:
        return rowdict(c.execute("SELECT * FROM sessions WHERE chat_id=? AND ended_at IS NULL ORDER BY id DESC LIMIT 1", (chat_id,)).fetchone())

async def close_session(session_id):
    with conn() as c:
        c.execute("UPDATE sessions SET ended_at=? WHERE id=? AND ended_at IS NULL", (now(),session_id))
        return c.rowcount > 0

async def add_operation(session_id, player_id, kind, amount, created_by):
    with conn() as c:
        cur = c.execute(
            "INSERT INTO operations(session_id,player_id,kind,amount,created_by,created_at) VALUES(?,?,?,?,?,?)",
            (session_id,player_id,kind,amount,created_by,now()))
        return rowdict(c.execute("SELECT * FROM operations WHERE id=?", (cur.lastrowid,)).fetchone())

async def cancel_operation(operation_id, chat_id):
    with conn() as c:
        cur = c.execute("""UPDATE operations SET cancelled=1 WHERE id=? AND cancelled=0
            AND session_id IN (SELECT id FROM sessions WHERE chat_id=?)""", (operation_id,chat_id))
        return cur.rowcount > 0

async def update_operation(operation_id, chat_id, amount):
    if amount < 0:
        return False
    with conn() as c:
        cur = c.execute("""UPDATE operations SET amount=? WHERE id=? AND cancelled=0
            AND session_id IN (SELECT id FROM sessions WHERE chat_id=?)""", (amount,operation_id,chat_id))
        return cur.rowcount > 0

async def get_history(chat_id, limit=15):
    with conn() as c:
        rows = c.execute("""SELECT o.id,p.name,o.kind,o.amount,o.created_at
            FROM operations o JOIN players p ON p.id=o.player_id
            JOIN sessions s ON s.id=o.session_id
            WHERE s.chat_id=? AND o.cancelled=0 ORDER BY o.id DESC LIMIT ?""", (chat_id,limit)).fetchall()
        return [dict(r) for r in rows]

async def get_session_player_stats(session_id):
    with conn() as c:
        rows = c.execute("""SELECT p.id AS player_id,p.name,
            COALESCE(SUM(CASE WHEN o.kind='buy' THEN o.amount ELSE 0 END),0) AS buys,
            COALESCE(SUM(CASE WHEN o.kind='cashout' THEN o.amount ELSE 0 END),0) AS cashouts
            FROM operations o JOIN players p ON p.id=o.player_id
            WHERE o.session_id=? AND o.cancelled=0 GROUP BY p.id,p.name ORDER BY p.name""", (session_id,)).fetchall()
        return [dict(r) for r in rows]

async def get_session_players(session_id):
    return await get_session_player_stats(session_id)

async def get_player_stats(chat_id, player_id):
    with conn() as c:
        row = c.execute("""SELECT p.name,
            COALESCE(SUM(CASE WHEN o.kind='buy' THEN o.amount ELSE -o.amount END),0) AS net
            FROM players p LEFT JOIN operations o ON o.player_id=p.id AND o.cancelled=0
            LEFT JOIN sessions s ON s.id=o.session_id AND s.chat_id=p.chat_id
            WHERE p.chat_id=? AND p.id=? GROUP BY p.id,p.name""", (chat_id,player_id)).fetchone()
        return rowdict(row)

async def get_total_stats(chat_id):
    with conn() as c:
        rows = c.execute("""SELECT p.id AS player_id,p.name,
            COALESCE(SUM(CASE WHEN o.kind='cashout' THEN o.amount WHEN o.kind='buy' THEN -o.amount ELSE 0 END),0) AS net
            FROM players p LEFT JOIN operations o ON o.player_id=p.id AND o.cancelled=0
            LEFT JOIN sessions s ON s.id=o.session_id
            WHERE p.chat_id=? GROUP BY p.id,p.name ORDER BY net DESC""", (chat_id,)).fetchall()
        return [dict(r) for r in rows]


async def save_chat_message(chat_id, user_id, username, role, content):
    content = (content or "").strip()
    if not content:
        return None
    with conn() as c:
        cur = c.execute(
            "INSERT INTO chat_messages(chat_id,user_id,username,role,content,created_at) VALUES(?,?,?,?,?,?)",
            (chat_id, user_id, username or "Игрок", role, content[:4000], now())
        )
        return cur.lastrowid

async def get_recent_chat_messages(chat_id, limit=40):
    with conn() as c:
        rows = c.execute(
            "SELECT username,role,content,created_at FROM chat_messages WHERE chat_id=? ORDER BY id DESC LIMIT ?",
            (chat_id, limit)
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

async def get_chat_memories(chat_id, limit=20):
    with conn() as c:
        rows = c.execute(
            "SELECT id,memory_type,content,created_at FROM chat_memories WHERE chat_id=? ORDER BY id DESC LIMIT ?",
            (chat_id, limit)
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

async def get_unsummarized_chat_messages(chat_id, limit=30):
    with conn() as c:
        state = c.execute(
            "SELECT last_summarized_message_id FROM chat_memory_state WHERE chat_id=?", (chat_id,)
        ).fetchone()
        last_id = int(state[0]) if state else 0
        rows = c.execute(
            "SELECT id,username,role,content,created_at FROM chat_messages WHERE chat_id=? AND role='user' AND id>? ORDER BY id ASC LIMIT ?",
            (chat_id, last_id, limit)
        ).fetchall()
        return [dict(r) for r in rows]

async def mark_chat_messages_summarized(chat_id, last_message_id, summary):
    with conn() as c:
        c.execute(
            "INSERT INTO chat_memories(chat_id,memory_type,content,source_message_id,created_at) VALUES(?,?,?,?,?)",
            (chat_id, "summary", summary[:12000], last_message_id, now())
        )
        c.execute(
            "INSERT INTO chat_memory_state(chat_id,last_summarized_message_id) VALUES(?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET last_summarized_message_id=excluded.last_summarized_message_id",
            (chat_id, last_message_id)
        )
