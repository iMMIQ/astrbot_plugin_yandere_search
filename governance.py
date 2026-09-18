# SPDX-License-Identifier: LGPL-3.0-or-later
"""会话治理与状态持久化：sqlite(WAL) 单文件，无第三方依赖。

承载：会话设置（分级上限/冷却/配额/转发/开关/搜图模式）、命令冷却与每日配额、
"下一张"查询游标、随机图去重（seen 表，按会话×标签组合记最近发过）、
外部 API 每日配额（SauceNAO）、订阅表、收藏夹。
事件循环内直用（查询都是微秒级），无需额外线程。
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any

RATING_ORDER = {"s": 0, "q": 1, "e": 2}

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats(
  umo TEXT PRIMARY KEY,
  rating_cap TEXT,
  cooldown_s INTEGER,
  quota INTEGER,
  forward INTEGER,
  enabled INTEGER,
  searchmode INTEGER,
  r18_ok INTEGER,
  last_cmd_ts REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS usage(
  umo TEXT NOT NULL,
  user_id TEXT NOT NULL,
  day TEXT NOT NULL,
  imgs INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(umo, user_id, day)
);
CREATE TABLE IF NOT EXISTS history(
  umo TEXT NOT NULL,
  user_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  data TEXT NOT NULL,
  ts REAL NOT NULL,
  PRIMARY KEY(umo, user_id)
);
CREATE TABLE IF NOT EXISTS api_usage(
  key TEXT NOT NULL,
  day TEXT NOT NULL,
  n INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(key, day)
);
CREATE TABLE IF NOT EXISTS subs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  umo TEXT NOT NULL,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL DEFAULT '{}',
  hh_mm TEXT NOT NULL,
  last_run_day TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS favorites(
  user_id TEXT NOT NULL,
  site TEXT NOT NULL,
  post_id INTEGER NOT NULL,
  tags TEXT NOT NULL DEFAULT '',
  ts REAL NOT NULL,
  PRIMARY KEY(user_id, site, post_id)
);
CREATE TABLE IF NOT EXISTS seen(
  umo TEXT NOT NULL,
  site TEXT NOT NULL,
  qkey TEXT NOT NULL,
  post_id INTEGER NOT NULL,
  ts REAL NOT NULL,
  PRIMARY KEY(umo, site, qkey, post_id)
);
CREATE TABLE IF NOT EXISTS tagmap(
  token TEXT PRIMARY KEY,
  tag TEXT NOT NULL,
  score REAL NOT NULL DEFAULT 0,
  ts REAL NOT NULL
);
"""

# 每个标签组合保留的“最近已发”数量：超过即按最旧淘汰
SEEN_KEEP = 240

CHAT_FIELDS = ("rating_cap", "cooldown_s", "quota", "forward", "enabled", "searchmode", "r18_ok")


class Governance:
    def __init__(self, db_path: Path, defaults: dict[str, Any] | None = None):
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(db_path))
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        # 老库迁移：r18 解锁标记列（新装库已含）
        try:
            self._db.execute("ALTER TABLE chats ADD COLUMN r18_ok INTEGER")
        except Exception:
            pass
        self._db.commit()
        self._defaults = dict(defaults or {})

    def close(self) -> None:
        try:
            self._db.commit()
            self._db.close()
        except Exception:
            pass

    # ---------- 会话设置 ----------

    def get_chat(self, umo: str) -> dict[str, Any]:
        row = self._db.execute("SELECT * FROM chats WHERE umo=?", (umo,)).fetchone()
        chat = {k: self._defaults.get(k) for k in CHAT_FIELDS}
        row_keys: set[str] = set()
        if row:
            for k in CHAT_FIELDS:
                if row[k] is not None:
                    chat[k] = row[k]
                    row_keys.add(k)
            chat["last_cmd_ts"] = row["last_cmd_ts"] or 0
        else:
            chat["last_cmd_ts"] = 0
        chat["rating_cap"] = chat.get("rating_cap") or "s"
        chat["cooldown_s"] = int(chat.get("cooldown_s") or 0)
        chat["quota"] = int(chat.get("quota") or 0)
        chat["forward"] = 1 if chat.get("forward") else 0
        chat["enabled"] = 0 if chat.get("enabled") == 0 else 1
        chat["searchmode"] = 1 if chat.get("searchmode") else 0
        chat["r18_ok"] = 1 if chat.get("r18_ok") else 0
        # 标记哪些字段是会话级显式覆盖（如私聊分级上限应回退 default_rating 而非群默认）
        chat["_row_keys"] = row_keys
        return chat

    def set_chat(self, umo: str, **kv) -> None:
        allowed = {k: v for k, v in kv.items() if k in CHAT_FIELDS}
        if not allowed:
            return
        cols = ",".join(allowed)
        ph = ",".join("?" for _ in allowed)
        self._db.execute(
            f"INSERT INTO chats(umo,{cols}) VALUES(?,{ph}) "
            f"ON CONFLICT(umo) DO UPDATE SET {', '.join(f'{c}=excluded.{c}' for c in allowed)}",
            (umo, *allowed.values()),
        )
        self._db.commit()

    # ---------- 命令门：开关 / 冷却 / 每日配额 ----------

    def check(self, umo: str, user_id: str, is_admin: bool) -> tuple[bool, str]:
        chat = self.get_chat(umo)
        if not chat["enabled"]:
            return False, "本会话的搜图功能已被管理员关闭"
        if is_admin:
            return True, ""
        now = time.time()
        if now - chat.get("last_cmd_ts", 0) < chat["cooldown_s"]:
            wait = int(chat["cooldown_s"] - (now - chat.get("last_cmd_ts", 0))) + 1
            return False, f"太快啦，本会话冷却中，约 {wait} 秒后再试"
        used = self.usage_today(umo, user_id)
        if chat["quota"] > 0 and used >= chat["quota"]:
            return False, f"今天的图片配额用完了（{used}/{chat['quota']} 张），明天再来吧"
        return True, ""

    def touch_command(self, umo: str) -> None:
        self._db.execute(
            "INSERT INTO chats(umo, last_cmd_ts) VALUES(?,?) "
            "ON CONFLICT(umo) DO UPDATE SET last_cmd_ts=excluded.last_cmd_ts",
            (umo, time.time()),
        )
        self._db.commit()

    def usage_today(self, umo: str, user_id: str) -> int:
        day = datetime.now().strftime("%F")
        row = self._db.execute(
            "SELECT imgs FROM usage WHERE umo=? AND user_id=? AND day=?",
            (umo, user_id, day),
        ).fetchone()
        return int(row["imgs"]) if row else 0

    def record_usage(self, umo: str, user_id: str, n: int) -> None:
        day = datetime.now().strftime("%F")
        self._db.execute(
            "INSERT INTO usage(umo,user_id,day,imgs) VALUES(?,?,?,?) "
            "ON CONFLICT(umo,user_id,day) DO UPDATE SET imgs=imgs+excluded.imgs",
            (umo, user_id, day, n),
        )
        self._db.commit()

    # ---------- “下一张”查询历史 ----------

    def set_history(self, umo: str, user_id: str, kind: str, data: dict) -> None:
        self._db.execute(
            "INSERT INTO history(umo,user_id,kind,data,ts) VALUES(?,?,?,?,?) "
            "ON CONFLICT(umo,user_id) DO UPDATE SET kind=excluded.kind, data=excluded.data, ts=excluded.ts",
            (umo, user_id, kind, json.dumps(data, ensure_ascii=False), time.time()),
        )
        self._db.commit()

    def get_history(self, umo: str, user_id: str, ttl: float = 600) -> dict | None:
        row = self._db.execute(
            "SELECT kind,data,ts FROM history WHERE umo=? AND user_id=?", (umo, user_id)
        ).fetchone()
        if not row or time.time() - row["ts"] > ttl:
            return None
        return {"kind": row["kind"], **json.loads(row["data"])}

    # ---------- 随机去重：按 (会话, 图源, 标签组合) 记住最近发过的图 ----------

    def add_seen(self, umo: str, site: str, qkey: str, ids: list[int], keep: int = SEEN_KEEP) -> None:
        if not ids:
            return
        now = time.time()
        self._db.executemany(
            "INSERT INTO seen(umo,site,qkey,post_id,ts) VALUES(?,?,?,?,?) "
            "ON CONFLICT(umo,site,qkey,post_id) DO UPDATE SET ts=excluded.ts",
            [(umo, site, qkey, int(i), now) for i in ids],
        )
        self._db.execute(
            "DELETE FROM seen WHERE umo=? AND site=? AND qkey=? AND post_id NOT IN ("
            "SELECT post_id FROM seen WHERE umo=? AND site=? AND qkey=? "
            "ORDER BY ts DESC, post_id DESC LIMIT ?)",
            (umo, site, qkey, umo, site, qkey, keep),
        )
        self._db.commit()

    def get_seen(self, umo: str, site: str, qkey: str, keep: int = SEEN_KEEP) -> set[int]:
        rows = self._db.execute(
            "SELECT post_id FROM seen WHERE umo=? AND site=? AND qkey=? "
            "ORDER BY ts DESC, post_id DESC LIMIT ?",
            (umo, site, qkey, keep),
        ).fetchall()
        return {int(r["post_id"]) for r in rows}

    # ---------- 语义标签缓存（词表未命中 → 嵌入召回的结果，tag='' 为负样本） ----------

    def get_tagmap(self, token: str) -> tuple[str, float, float] | None:
        row = self._db.execute(
            "SELECT tag,score,ts FROM tagmap WHERE token=?", (token,)
        ).fetchone()
        return (row["tag"], row["score"], row["ts"]) if row else None

    def set_tagmap(self, token: str, tag: str, score: float = 0.0) -> None:
        self._db.execute(
            "INSERT INTO tagmap(token,tag,score,ts) VALUES(?,?,?,?) "
            "ON CONFLICT(token) DO UPDATE SET tag=excluded.tag, score=excluded.score, ts=excluded.ts",
            (token, tag, score, time.time()),
        )
        self._db.commit()

    # ---------- 外部 API 每日配额（SauceNAO 等） ----------

    def api_count(self, key: str) -> int:
        day = datetime.now().strftime("%F")
        row = self._db.execute(
            "SELECT n FROM api_usage WHERE key=? AND day=?", (key, day)
        ).fetchone()
        return int(row["n"]) if row else 0

    def bump_api(self, key: str, n: int = 1) -> int:
        day = datetime.now().strftime("%F")
        cur = self._db.execute(
            "INSERT INTO api_usage(key,day,n) VALUES(?,?,?) "
            "ON CONFLICT(key,day) DO UPDATE SET n=n+excluded.n",
            (key, day, n),
        )
        self._db.commit()
        return self.api_count(key)

    # ---------- 订阅 ----------

    def add_sub(self, umo: str, kind: str, payload: dict, hh_mm: str) -> int:
        cur = self._db.execute(
            "INSERT INTO subs(umo,kind,payload,hh_mm) VALUES(?,?,?,?)",
            (umo, kind, json.dumps(payload, ensure_ascii=False), hh_mm),
        )
        self._db.commit()
        return int(cur.lastrowid)

    def list_subs(self, umo: str) -> list[dict]:
        rows = self._db.execute(
            "SELECT * FROM subs WHERE umo=? ORDER BY id", (umo,)
        ).fetchall()
        return [dict(r) for r in rows]

    def del_sub(self, umo: str, sub_id: int) -> bool:
        cur = self._db.execute("DELETE FROM subs WHERE id=? AND umo=?", (sub_id, umo))
        self._db.commit()
        return cur.rowcount > 0

    def due_subs(self, hh_mm: str, today: str) -> list[dict]:
        rows = self._db.execute(
            "SELECT * FROM subs WHERE hh_mm<=? AND (last_run_day IS NULL OR last_run_day!=?) ORDER BY id",
            (hh_mm, today),
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_sub_run(self, sub_id: int, today: str) -> None:
        self._db.execute("UPDATE subs SET last_run_day=? WHERE id=?", (today, sub_id))
        self._db.commit()

    def update_sub_payload(self, sub_id: int, payload: dict) -> None:
        self._db.execute(
            "UPDATE subs SET payload=? WHERE id=?",
            (json.dumps(payload, ensure_ascii=False), sub_id),
        )
        self._db.commit()

    # ---------- 收藏 ----------

    def add_fav(self, user_id: str, site: str, post_id: int, tags: str = "") -> bool:
        cur = self._db.execute(
            "INSERT OR IGNORE INTO favorites(user_id,site,post_id,tags,ts) VALUES(?,?,?,?,?)",
            (user_id, site, post_id, tags, time.time()),
        )
        self._db.commit()
        return cur.rowcount > 0

    def list_favs(self, user_id: str, limit: int = 5, offset: int = 0) -> list[dict]:
        rows = self._db.execute(
            "SELECT * FROM favorites WHERE user_id=? ORDER BY ts DESC LIMIT ? OFFSET ?",
            (user_id, limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]

    def count_favs(self, user_id: str) -> int:
        row = self._db.execute(
            "SELECT COUNT(*) AS c FROM favorites WHERE user_id=?", (user_id,)
        ).fetchone()
        return int(row["c"])
