import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "poker.sqlite3"


def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


async def init_db():
    with conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS players (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            name TEXT NOT NULL COLLATE NOCASE,
            telegram_user_id INTEGER,
            created_at TEXT NOT NULL,
            UNIQUE(chat_id, name)
        );
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            started_by INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            is_active INTEGER NOT NULL DEFAULT 1
        );
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_session_per_chat
            ON sessions(chat_id) WHERE is_active = 1;
        CREATE TABLE IF NOT EXISTS operations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL REFERENCES sessions(id),
            chat_id INTEGER NOT NULL,
            player_id INTEGER NOT NULL REFERENCES players(id),
            kind TEXT NOT NULL CHECK(kind IN ('buy','exit')),
            amount INTEGER NOT NULL,
            author_user_id INTEGER NOT NULL,
            telegram_message_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT
        );
        """)
        c.commit()


def now():
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


def rowdict(row):
    return dict(row) if row else None


def add_player(chat_id, name, telegram_user_id=None, full_name=None):
    clean = " ".join(name.strip().split())
    with conn() as c:
        row = c.execute("SELECT * FROM players WHERE chat_id=? AND name=?", (chat_id, clean)).fetchone()
        if row:
            if telegram_user_id:
                c.execute("UPDATE players SET telegram_user_id=? WHERE id=?", (telegram_user_id, row["id"]))
                row = c.execute("SELECT * FROM players WHERE id=?", (row["id"],)).fetchone()
            return rowdict(row)
        cur = c.execute(
            "INSERT INTO players(chat_id,name,telegram_user_id,created_at) VALUES(?,?,?,?)",
            (chat_id, clean, telegram_user_id, now())
        )
        return rowdict(c.execute("SELECT * FROM players WHERE id=?", (cur.lastrowid,)).fetchone())


def find_player(chat_id, name):
    with conn() as c:
        row = c.execute("SELECT * FROM players WHERE chat_id=? AND name=?", (chat_id, " ".join(name.strip().split()))).fetchone()
        return rowdict(row)


def list_players(chat_id):
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM players WHERE chat_id=? ORDER BY name", (chat_id,))]


def get_session(chat_id):
    with conn() as c:
        return rowdict(c.execute("SELECT * FROM sessions WHERE chat_id=? AND is_active=1", (chat_id,)).fetchone())


def create_session(chat_id, user_id):
    with conn() as c:
        cur = c.execute(
            "INSERT INTO sessions(chat_id,started_by,started_at) VALUES(?,?,?)",
            (chat_id, user_id, now())
        )
        return cur.lastrowid


def close_session(session_id):
    with conn() as c:
        c.execute("UPDATE sessions SET is_active=0, ended_at=? WHERE id=?", (now(), session_id))


def add_operation(session_id, chat_id, player_id, kind, amount, author_id, message_id):
    with conn() as c:
        cur = c.execute(
            """INSERT INTO operations(session_id,chat_id,player_id,kind,amount,author_user_id,telegram_message_id,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (session_id, chat_id, player_id, kind, amount, author_id, message_id, now())
        )
        return cur.lastrowid


def get_session_players(session_id):
    with conn() as c:
        rows = c.execute("""
            SELECT p.id AS player_id, p.name,
              COALESCE(SUM(CASE WHEN o.kind='buy' AND o.is_active=1 THEN o.amount ELSE 0 END),0) AS buys,
              COALESCE(SUM(CASE WHEN o.kind='exit' AND o.is_active=1 THEN o.amount ELSE 0 END),0) AS exits,
              COALESCE(SUM(CASE WHEN o.kind='buy' AND o.is_active=1 THEN 1 ELSE 0 END),0) AS buy_count
            FROM players p JOIN operations o ON o.player_id=p.id
            WHERE o.session_id=? GROUP BY p.id,p.name ORDER BY p.name
        """, (session_id,)).fetchall()
        return [dict(r) for r in rows]


def get_session_player_stats(session_id, player_id):
    with conn() as c:
        row = c.execute("""
          SELECT COALESCE(SUM(CASE WHEN kind='buy' AND is_active=1 THEN amount ELSE 0 END),0) buys,
                 COALESCE(SUM(CASE WHEN kind='exit' AND is_active=1 THEN amount ELSE 0 END),0) exits
          FROM operations WHERE session_id=? AND player_id=?
        """, (session_id, player_id)).fetchone()
        return dict(row)


def cancel_operation(chat_id, op_id, author_id):
    with conn() as c:
        row = c.execute("SELECT * FROM operations WHERE id=? AND chat_id=? AND is_active=1", (op_id, chat_id)).fetchone()
        if not row:
            return False, "Активная операция с таким ID не найдена."
        if row["author_user_id"] != author_id:
            return False, "Можно отменять только собственные операции. Администратор может отменить любую."
        c.execute("UPDATE operations SET is_active=0, updated_at=? WHERE id=?", (now(), op_id))
        return True, ""


def update_operation(chat_id, op_id, amount, author_id):
    with conn() as c:
        row = c.execute("SELECT * FROM operations WHERE id=? AND chat_id=? AND is_active=1", (op_id, chat_id)).fetchone()
        if not row:
            return False, "Активная операция с таким ID не найдена."
        if row["author_user_id"] != author_id:
            return False, "Можно исправлять только собственные операции. Администратор может исправить любую."
        c.execute("UPDATE operations SET amount=?, updated_at=? WHERE id=?", (amount, now(), op_id))
        return True, ""


def get_history(chat_id, limit=10):
    with conn() as c:
        rows = c.execute("""
          SELECT s.id,s.started_at,s.ended_at,
            COALESCE(SUM(CASE WHEN o.kind='buy' AND o.is_active=1 THEN o.amount ELSE 0 END),0) buys,
            COALESCE(SUM(CASE WHEN o.kind='exit' AND o.is_active=1 THEN o.amount ELSE 0 END),0) exits
          FROM sessions s LEFT JOIN operations o ON o.session_id=s.id
          WHERE s.chat_id=? AND s.is_active=0
          GROUP BY s.id ORDER BY s.id DESC LIMIT ?
        """, (chat_id, limit)).fetchall()
        return [dict(r) for r in rows]


def get_player_stats(chat_id, player_id):
    with conn() as c:
        row = c.execute("""
          SELECT COUNT(DISTINCT s.id) games,
            COALESCE(SUM(CASE WHEN o.kind='buy' AND o.is_active=1 THEN o.amount ELSE 0 END),0) buys,
            COALESCE(SUM(CASE WHEN o.kind='exit' AND o.is_active=1 THEN o.amount ELSE 0 END),0) exits
          FROM sessions s LEFT JOIN operations o ON o.session_id=s.id AND o.player_id=?
          WHERE s.chat_id=? AND s.is_active=0
        """, (player_id, chat_id)).fetchone()
        return dict(row)


def get_total_stats(chat_id):
    with conn() as c:
        rows = c.execute("""
          SELECT p.id,p.name,COUNT(DISTINCT s.id) games,
            COALESCE(SUM(CASE WHEN o.kind='buy' AND o.is_active=1 THEN o.amount ELSE 0 END),0) buys,
            COALESCE(SUM(CASE WHEN o.kind='exit' AND o.is_active=1 THEN o.amount ELSE 0 END),0) exits
          FROM players p
          LEFT JOIN operations o ON o.player_id=p.id AND o.is_active=1
          LEFT JOIN sessions s ON s.id=o.session_id AND s.is_active=0
          WHERE p.chat_id=?
          GROUP BY p.id,p.name
          HAVING games > 0
        """, (chat_id,)).fetchall()
        return [dict(r) for r in rows]
