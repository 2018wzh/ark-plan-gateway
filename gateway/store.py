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
          models TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
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
        """)
        self.db.commit()

    def _enc(self, value: str | None) -> str | None:
        return self.cipher.encrypt(value.encode()).decode() if value else None

    def _dec(self, value: str | None) -> str | None:
        return self.cipher.decrypt(value.encode()).decode() if value else None

    def _row(self, row: sqlite3.Row, private: bool = False) -> dict:
        d = dict(row)
        d["models"] = json.loads(d["models"])
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
        allowed = {"plan", "label", "quota_group", "models", "enabled", "auth_failed", "expired",
                   "cooldown_until", "cooldown_kind", "quota_checked_at", "quota_error", "usage_json", "active"}
        data = {k: v for k, v in fields.items() if k in allowed}
        if "models" in data:
            data["models"] = json.dumps(data["models"])
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
