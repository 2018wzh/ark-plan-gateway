"""Small SQLite store; secrets are encrypted before persistence."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from cryptography.fernet import Fernet


class Store:
    def __init__(self, path: str, master_key: str):
        self.lock = threading.RLock()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.cipher = Fernet(master_key.encode())
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS accounts (
          id TEXT PRIMARY KEY, plan TEXT NOT NULL, label TEXT NOT NULL,
          key_hash TEXT NOT NULL UNIQUE, api_key TEXT NOT NULL,
          access_key TEXT, secret_key TEXT, quota_group TEXT NOT NULL,
          models TEXT NOT NULL, model_mapping TEXT NOT NULL DEFAULT '{}', enabled INTEGER NOT NULL DEFAULT 1,
          auth_failed INTEGER NOT NULL DEFAULT 0, expired INTEGER NOT NULL DEFAULT 0,
          cooldown_until REAL, cooldown_kind TEXT, quota_checked_at REAL,
          quota_error TEXT, usage_json TEXT NOT NULL DEFAULT '{}',
          active INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS response_bindings (
          response_id TEXT PRIMARY KEY, account_id TEXT NOT NULL,
          created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS quota_snapshots (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          observed_at REAL NOT NULL, quota_group TEXT NOT NULL, plan TEXT NOT NULL,
          window TEXT NOT NULL, quota REAL NOT NULL, used REAL NOT NULL, reset_time REAL
        );
        CREATE INDEX IF NOT EXISTS quota_snapshots_scope_time
          ON quota_snapshots (quota_group, window, observed_at DESC);
        CREATE TABLE IF NOT EXISTS request_daily (
          day TEXT NOT NULL, account_id TEXT NOT NULL, outcome TEXT NOT NULL,
          requests INTEGER NOT NULL DEFAULT 0, input_tokens INTEGER NOT NULL DEFAULT 0,
          output_tokens INTEGER NOT NULL DEFAULT 0, latency_ms INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY (day, account_id, outcome)
        );
        """)
        if "model_mapping" not in {r[1] for r in self.db.execute("PRAGMA table_info(accounts)")}:
            self.db.execute("ALTER TABLE accounts ADD COLUMN model_mapping TEXT NOT NULL DEFAULT '{}'")
        if "model" not in {r[1] for r in self.db.execute("PRAGMA table_info(request_daily)")}:
            self.db.executescript("""
            CREATE TABLE request_daily_new (
              day TEXT NOT NULL, account_id TEXT NOT NULL, model TEXT NOT NULL,
              outcome TEXT NOT NULL, requests INTEGER NOT NULL DEFAULT 0,
              input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
              latency_ms INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY (day, account_id, model, outcome)
            );
            INSERT INTO request_daily_new SELECT day,account_id,'',outcome,requests,input_tokens,output_tokens,latency_ms FROM request_daily;
            DROP TABLE request_daily;
            ALTER TABLE request_daily_new RENAME TO request_daily;
            """)
        self._last_pruned_day = ""
        self._prune_locked(time.time())
        self.db.commit()

    def _prune_locked(self, now: float) -> None:
        today = time.strftime("%Y-%m-%d", time.gmtime(now))
        if today == self._last_pruned_day:
            return
        self.db.execute("DELETE FROM quota_snapshots WHERE observed_at < ?", (now - 90 * 86400,))
        self.db.execute("DELETE FROM request_daily WHERE day < ?",
                        (time.strftime("%Y-%m-%d", time.gmtime(now - 90 * 86400)),))
        self._last_pruned_day = today

    def _enc(self, value: str | None) -> str | None:
        return self.cipher.encrypt(value.encode()).decode() if value else None

    def _dec(self, value: str | None) -> str | None:
        return self.cipher.decrypt(value.encode()).decode() if value else None

    def _row(self, row: sqlite3.Row, private: bool = False) -> dict:
        d = dict(row)
        d["models"] = json.loads(d["models"])
        d["model_mapping"] = json.loads(d["model_mapping"])
        d["usage"] = json.loads(d.pop("usage_json"))
        d["api_key_mask"] = "••••" + self._dec(d["api_key"])[-4:]
        d["has_ak_sk"] = bool(d["access_key"] and d["secret_key"])
        if private:
            for k in ("api_key", "access_key", "secret_key"):
                d[k] = self._dec(d[k])
        else:
            for k in ("api_key", "access_key", "secret_key", "key_hash"):
                d.pop(k, None)
        return d

    def accounts(self, private: bool = False) -> list[dict]:
        with self.lock:
            return [self._row(r, private) for r in self.db.execute("SELECT * FROM accounts ORDER BY id")]

    def account(self, account_id: str, private: bool = False) -> dict | None:
        with self.lock:
            r = self.db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
            return self._row(r, private) if r else None

    def add_account(self, plan: str, key: str, label: str | None = None, models: list[str] | None = None) -> str:
        digest = hashlib.sha256(key.encode()).hexdigest()
        with self.lock:
            prior = self.db.execute("SELECT id FROM accounts WHERE key_hash=?", (digest,)).fetchone()
            if prior:
                return prior["id"]
            account_id = str(uuid.uuid4())
            self.db.execute("INSERT INTO accounts (id,plan,label,key_hash,api_key,quota_group,models) VALUES (?,?,?,?,?,?,?)",
                            (account_id, plan, label or f"{plan} {digest[:6]}", digest, self._enc(key), account_id,
                             json.dumps(models or ["ark-code-latest"])))
            self.db.commit()
            return account_id

    def update(self, account_id: str, **fields) -> None:
        allowed = {"plan", "label", "quota_group", "models", "model_mapping", "enabled", "auth_failed", "expired",
                   "cooldown_until", "cooldown_kind", "quota_checked_at", "quota_error", "usage_json", "active"}
        data = {k: v for k, v in fields.items() if k in allowed}
        if "models" in data:
            data["models"] = json.dumps(data["models"])
        if "model_mapping" in data:
            data["model_mapping"] = json.dumps(data["model_mapping"])
        with self.lock:
            if data:
                self.db.execute("UPDATE accounts SET " + ", ".join(f"{k}=?" for k in data) + " WHERE id=?",
                                (*data.values(), account_id))
            for k in ("api_key", "access_key", "secret_key"):
                if fields.get(k):
                    value = str(fields[k])
                    if k == "api_key":
                        digest = hashlib.sha256(value.encode()).hexdigest()
                        self.db.execute("UPDATE accounts SET api_key=?,key_hash=? WHERE id=?", (self._enc(value), digest, account_id))
                    else:
                        self.db.execute(f"UPDATE accounts SET {k}=? WHERE id=?", (self._enc(value), account_id))
            self.db.commit()

    def set_quota_group(self, account_id: str, group_id: str) -> None:
        self.update(account_id, quota_group=group_id)

    def bind(self, response_id: str, account_id: str) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO response_bindings VALUES (?,?,?)", (response_id, account_id, time.time()))
            self.db.commit()

    def record_request(self, account_id: str, outcome: str, latency_ms: int,
                       model: str = "",
                       input_tokens: int = 0, output_tokens: int = 0) -> None:
        now = time.time()
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        with self.lock:
            self._prune_locked(now)
            self.db.execute("""INSERT INTO request_daily
                (day,account_id,model,outcome,requests,input_tokens,output_tokens,latency_ms)
                VALUES (?,?,?,?,1,?,?,?) ON CONFLICT(day,account_id,model,outcome) DO UPDATE SET
                requests=requests+1, input_tokens=input_tokens+excluded.input_tokens,
                output_tokens=output_tokens+excluded.output_tokens,
                latency_ms=latency_ms+excluded.latency_ms""",
                (day, account_id, model, outcome, max(0, input_tokens), max(0, output_tokens), max(0, latency_ms)))
            self.db.commit()

    def record_quota(self, quota_group: str, plan: str, usage: dict, observed_at: float) -> None:
        with self.lock:
            self._prune_locked(observed_at)
            for window, values in usage.items():
                previous = self.db.execute("""SELECT observed_at,quota,used,reset_time FROM quota_snapshots
                    WHERE quota_group=? AND window=? ORDER BY observed_at DESC LIMIT 1""",
                    (quota_group, window)).fetchone()
                current = (float(values["quota"]), float(values["used"]), values.get("reset_time"))
                if previous and tuple(previous[k] for k in ("quota", "used", "reset_time")) == current and observed_at - previous["observed_at"] < 300:
                    continue
                self.db.execute("""INSERT INTO quota_snapshots
                    (observed_at,quota_group,plan,window,quota,used,reset_time)
                    VALUES (?,?,?,?,?,?,?)""", (observed_at, quota_group, plan, window, *current))
            self.db.commit()

    def statistics(self, account_id: str | None, days: int) -> dict:
        since_day = time.strftime("%Y-%m-%d", time.gmtime(time.time() - (days - 1) * 86400))
        with self.lock:
            account = self.account(account_id) if account_id else None
            if account_id and account is None:
                raise KeyError(account_id)
            condition = " AND account_id=?" if account_id else ""
            args = (since_day, account_id) if account_id else (since_day,)
            pricing = self.pricing()
            daily_groups: dict[tuple[str, str], dict] = {}
            for raw in self.db.execute(f"""SELECT day,model,outcome,requests,input_tokens,output_tokens,latency_ms
                FROM request_daily WHERE day>=?{condition} ORDER BY day DESC,outcome""", args):
                row = dict(raw)
                key = (row["day"], row["outcome"])
                item = daily_groups.setdefault(key, {"day": key[0], "outcome": key[1], "requests": 0,
                    "input_tokens": 0, "output_tokens": 0, "latency_ms": 0,
                    "equivalent_cny": 0.0, "unpriced_input_tokens": 0, "unpriced_output_tokens": 0})
                for field in ("requests", "input_tokens", "output_tokens", "latency_ms"):
                    item[field] += row[field]
                rates = pricing["models"].get(row["model"], {})
                for side in ("input", "output"):
                    tokens = row[f"{side}_tokens"]
                    rate = rates.get(side, pricing["default"].get(side))
                    if rate is None:
                        item[f"unpriced_{side}_tokens"] += tokens
                    else:
                        item["equivalent_cny"] += tokens * rate / 1_000_000
            daily = list(daily_groups.values())
            group_condition = "AND quota_group=?" if account else ""
            group_args = (time.time() - days * 86400, account["quota_group"]) if account else (time.time() - days * 86400,)
            history = [dict(row) for row in self.db.execute(f"""SELECT observed_at,quota_group,plan,window,quota,used,reset_time
                FROM quota_snapshots WHERE observed_at>=? {group_condition} ORDER BY observed_at DESC LIMIT 200""", group_args)]
            current = []
            representatives = {}
            for row in self.accounts():
                if account and row["quota_group"] != account["quota_group"]:
                    continue
                previous = representatives.get(row["quota_group"])
                if row["usage"] and (previous is None or (row["quota_checked_at"] or 0) > (previous["quota_checked_at"] or 0)):
                    representatives[row["quota_group"]] = row
            for row in representatives.values():
                for window, values in row["usage"].items():
                    current.append({"quota_group": row["quota_group"], "plan": row["plan"],
                                    "window": window, "quota": values["quota"], "used": values["used"],
                                    "reset_time": values.get("reset_time"), "observed_at": row["quota_checked_at"],
                                    "quota_error": row["quota_error"]})
            return {"daily": daily, "quota_history": history, "quota_current": current}

    def pricing(self) -> dict:
        return json.loads(self.setting("pricing", '{"default":{},"models":{}}'))

    def set_pricing(self, value: dict) -> None:
        self.set_setting("pricing", json.dumps(value, separators=(",", ":")))

    def lookup_binding(self, response_id: str) -> str | None:
        with self.lock:
            row = self.db.execute("SELECT account_id FROM response_bindings WHERE response_id=?", (response_id,)).fetchone()
            return row[0] if row else None

    def setting(self, key: str, default: str = "") -> str:
        with self.lock:
            row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return row[0] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.lock:
            self.db.execute("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
            self.db.commit()

    def close(self):
        self.db.close()
