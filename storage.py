import json
import sqlite3
import time
from pathlib import Path

DEFAULTS = {
    "enabled": True,
    "welcome": False,
    "antiflood": False,
    "antilink": False,
    "warn_limit": 3,
    "warn_mute": 3600,
    "flood_count": 6,
    "flood_window": 10,
    "flood_mute": 600,
}
TEMPLATES = {
    "welcome": "🐾 {user}, добро пожаловать в {chat}!\nПравила: /rules",
    "rules": "🐾 Уважайте участников. Не спамьте и не публикуйте рекламу.",
    "warn": "⚠️ {user}, предупреждение {count}/{limit}\nПричина: {reason}",
    "mute": "🔇 {user} получил мут на {duration}\nПричина: {reason}",
    "unmute": "🔊 Ограничения для {user} сняты",
    "ban": "⛔ {user} заблокирован\nПричина: {reason}",
    "unban": "✅ {user} снова может вступить в чат",
    "unwarn": "✅ Предупреждения {user} сброшены",
}


class Store:
    def __init__(self, path):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS chats(id INTEGER PRIMARY KEY,title TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS settings(chat INTEGER,key TEXT,value TEXT,PRIMARY KEY(chat,key));
        CREATE TABLE IF NOT EXISTS templates(chat INTEGER,key TEXT,body TEXT,PRIMARY KEY(chat,key));
        CREATE TABLE IF NOT EXISTS emojis(chat INTEGER,name TEXT,id TEXT,fallback TEXT,PRIMARY KEY(chat,name));
        CREATE TABLE IF NOT EXISTS warns(chat INTEGER,user INTEGER,count INTEGER,PRIMARY KEY(chat,user));
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT,ts INTEGER,chat INTEGER,actor INTEGER,target INTEGER,action TEXT,reason TEXT);
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
        """
        )
        self.db.commit()

    def execute(self, sql, args=()):
        cur = self.db.execute(sql, args)
        self.db.commit()
        return cur

    def chat(self, chat):
        self.execute(
            "INSERT OR REPLACE INTO chats VALUES(?,?)",
            (chat["id"], chat.get("title", str(chat["id"]))),
        )

    def chats(self):
        return self.db.execute("SELECT * FROM chats ORDER BY title").fetchall()

    def settings(self, chat):
        result = dict(DEFAULTS)
        result.update(
            {
                r["key"]: json.loads(r["value"])
                for r in self.db.execute(
                    "SELECT key,value FROM settings WHERE chat=?", (chat,)
                )
            }
        )
        return result

    def set(self, chat, key, value):
        if key not in DEFAULTS:
            raise ValueError("Неизвестная настройка")
        self.execute(
            "INSERT OR REPLACE INTO settings VALUES(?,?,?)",
            (chat, key, json.dumps(value)),
        )

    def template(self, chat, key):
        row = self.db.execute(
            "SELECT body FROM templates WHERE chat=? AND key=?", (chat, key)
        ).fetchone()
        return row["body"] if row else TEMPLATES[key]

    def text(self, chat, key, body):
        self.execute(
            "INSERT OR REPLACE INTO templates VALUES(?,?,?)", (chat, key, body)
        )

    def emojis(self, chat):
        return {
            r["name"]: (r["id"], r["fallback"])
            for r in self.db.execute("SELECT * FROM emojis WHERE chat=?", (chat,))
        }

    def emoji(self, chat, name, id, fallback):
        self.execute(
            "INSERT OR REPLACE INTO emojis VALUES(?,?,?,?)", (chat, name, id, fallback)
        )

    def warns(self, chat, user, delta=0, reset=False):
        row = self.db.execute(
            "SELECT count FROM warns WHERE chat=? AND user=?", (chat, user)
        ).fetchone()
        count = 0 if reset else (row["count"] if row else 0) + delta
        if delta or reset:
            self.execute(
                "INSERT OR REPLACE INTO warns VALUES(?,?,?)", (chat, user, count)
            )
        return count

    def log(self, chat, actor, target, action, reason=""):
        self.execute(
            "INSERT INTO audit(ts,chat,actor,target,action,reason) VALUES(?,?,?,?,?,?)",
            (int(time.time()), chat, actor, target, action, reason[:500]),
        )

    def logs(self, chat):
        return self.db.execute(
            "SELECT * FROM audit WHERE chat=? ORDER BY id DESC LIMIT 15", (chat,)
        ).fetchall()

    def meta(self, key, value=None):
        if value is not None:
            self.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, str(value)))
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None
