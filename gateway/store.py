"""Small SQLite store; secrets are encrypted before persistence."""
from __future__ import annotations

import hashlib
import base64
import json
import sqlite3
import threading
import time
import uuid
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .pricing import price_usage, price_period
from .private_files import create_private, private_directory, restrict

MAX_BINDINGS = 100_000
BINDING_TTL = 30 * 86400
MAX_AUDIT_ROWS = 10_000
AUDIT_TTL = 30 * 86400


class Store:
    def __init__(self, path: str, master_key: str):
        self.lock = threading.RLock()
        self.cipher = Fernet(master_key.encode())
        self.session_key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
            info=b"ark-plan-gateway/admin-session/v1").derive(base64.urlsafe_b64decode(master_key))
        if path != ":memory:":
            target = Path(path).absolute()
            protected = {Path.cwd().resolve(), Path.home().resolve(), Path(tempfile.gettempdir()).resolve(), target.parent.parent.resolve()}
            allowed = {target.name, target.name + "-wal", target.name + "-shm", target.name + "-journal"}
            if target.parent.resolve() in protected or (target.parent.exists() and any(p.name not in allowed for p in target.parent.iterdir())):
                raise ValueError("database requires a dedicated private directory")
            private_directory(target.parent)
            if not target.exists():
                with create_private(target):
                    pass
            for sibling in (target, Path(str(target) + "-wal"), Path(str(target) + "-shm"), Path(str(target) + "-journal")):
                if sibling.exists():
                    restrict(sibling)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
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
        CREATE INDEX IF NOT EXISTS response_bindings_age ON response_bindings(created_at);
        CREATE TABLE IF NOT EXISTS model_blocks (
          account_id TEXT NOT NULL, model TEXT NOT NULL, kind TEXT NOT NULL,
          retry_at REAL, code TEXT NOT NULL, failures INTEGER NOT NULL,
          PRIMARY KEY (account_id, model)
        );
        CREATE TABLE IF NOT EXISTS request_daily (
          day TEXT NOT NULL, account_id TEXT NOT NULL, outcome TEXT NOT NULL,
          requests INTEGER NOT NULL DEFAULT 0, input_tokens INTEGER NOT NULL DEFAULT 0,
          output_tokens INTEGER NOT NULL DEFAULT 0, latency_ms INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY (day, account_id, outcome)
        );
        CREATE TABLE IF NOT EXISTS request_audit (
          id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL,
          request_id TEXT NOT NULL, transport TEXT NOT NULL, method TEXT NOT NULL,
          path TEXT NOT NULL, model TEXT NOT NULL, http_status INTEGER,
          outcome TEXT NOT NULL, source TEXT NOT NULL, error_code TEXT NOT NULL,
          error_type TEXT NOT NULL, error_param TEXT NOT NULL, account_id TEXT NOT NULL,
          plan TEXT NOT NULL, upstream_status INTEGER, upstream_request_id TEXT NOT NULL,
          duration_ms INTEGER NOT NULL, attempt_count INTEGER NOT NULL,
          had_errors INTEGER NOT NULL, attempts_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS request_audit_age ON request_audit(created_at);
        CREATE INDEX IF NOT EXISTS request_audit_request ON request_audit(request_id);
        """)
        if "model_mapping" not in {r[1] for r in self.db.execute("PRAGMA table_info(accounts)")}:
            self.db.execute("ALTER TABLE accounts ADD COLUMN model_mapping TEXT NOT NULL DEFAULT '{}'")
        account_columns = {r[1] for r in self.db.execute("PRAGMA table_info(accounts)")}
        for name, sql_type in (("cooldown_failures", "INTEGER NOT NULL DEFAULT 0"), ("cooldown_code", "TEXT")):
            if name not in account_columns:
                self.db.execute(f"ALTER TABLE accounts ADD COLUMN {name} {sql_type}")
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(request_daily)")}
        if "context_tokens" not in columns:
            model_column = "model" if "model" in columns else "''"
            self.db.executescript(f"""
            CREATE TABLE request_daily_new (
              day TEXT NOT NULL, account_id TEXT NOT NULL, model TEXT NOT NULL,
              outcome TEXT NOT NULL, context_tokens INTEGER NOT NULL, price_period INTEGER NOT NULL,
              requests INTEGER NOT NULL DEFAULT 0, input_tokens INTEGER NOT NULL DEFAULT 0,
              output_tokens INTEGER NOT NULL DEFAULT 0, latency_ms INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY (day, account_id, model, outcome, context_tokens, price_period)
            );
            INSERT INTO request_daily_new
              SELECT day,account_id,{model_column},outcome,-1,-1,requests,input_tokens,output_tokens,latency_ms FROM request_daily;
            DROP TABLE request_daily;
            ALTER TABLE request_daily_new RENAME TO request_daily;
            """)
        self.db.execute("""CREATE TABLE IF NOT EXISTS request_hourly (
            hour TEXT NOT NULL, account_id TEXT NOT NULL, model TEXT NOT NULL,
            outcome TEXT NOT NULL, context_tokens INTEGER NOT NULL, price_period INTEGER NOT NULL,
            requests INTEGER NOT NULL DEFAULT 0, input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0, latency_ms INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (hour, account_id, model, outcome, context_tokens, price_period)
        )""")
        self.db.execute("INSERT OR IGNORE INTO settings VALUES ('hourly_started_at', ?)", (str(time.time()),))
        self._last_pruned_day = ""
        self._prune_locked(time.time())
        self.db.execute("DELETE FROM settings WHERE key='session_secret'")
        self._binding_count = self.db.execute("SELECT COUNT(*) FROM response_bindings").fetchone()[0]
        self._prune_bindings_locked(time.time())
        self.audit_write_failures = 0
        self.audit_capacity = threading.BoundedSemaphore(32)
        self.audit_metrics_lock = threading.Lock()
        self.audit_warning_at = float('-inf')
        self._audit_pruned_at = 0
        self._audit_count = self.db.execute("SELECT COUNT(*) FROM request_audit").fetchone()[0]
        self._prune_audit_locked(time.time())
        self.db.commit()

    def _prune_audit_locked(self, now):
        if now - self._audit_pruned_at >= 60:
            self._audit_count -= self.db.execute("DELETE FROM request_audit WHERE created_at < ?", (now - AUDIT_TTL,)).rowcount
            self._audit_pruned_at = now
        if self._audit_count > MAX_AUDIT_ROWS:
            self._audit_count -= self.db.execute("DELETE FROM request_audit WHERE id IN "
                "(SELECT id FROM request_audit ORDER BY id LIMIT ?)", (self._audit_count - MAX_AUDIT_ROWS,)).rowcount

    def record_audit(self, data: dict):
        fields = ("created_at", "request_id", "transport", "method", "path", "model", "http_status",
            "outcome", "source", "error_code", "error_type", "error_param", "account_id", "plan",
            "upstream_status", "upstream_request_id", "duration_ms", "attempt_count")
        had_errors = data["outcome"] != "success" or any(a["outcome"] != "success" for a in data["attempts"])
        with self.lock, self.db:
            self.db.execute("INSERT INTO request_audit (" + ",".join(fields) + ",had_errors,attempts_json) VALUES (" +
                ",".join("?" for _ in range(len(fields) + 2)) + ")",
                (*[data[k] for k in fields], int(had_errors), json.dumps(data["attempts"], separators=(",", ":"))))
            self._audit_count += 1
            self._prune_audit_locked(time.time())

    def audit_history(self, days=7, limit=50, before=None, errors_only=True, source=None,
                      error_code=None, model=None, http_status=None, request_id=None):
        conditions, args = ["created_at>=?"], [time.time() - min(days * 86400, AUDIT_TTL)]
        if errors_only:
            conditions.append("had_errors=1")
        for key, value in (("source", source), ("error_code", error_code)):
            if value:
                conditions.append(f"({key}=? OR EXISTS(SELECT 1 FROM json_each(attempts_json) "
                    f"WHERE json_extract(value,'$.{key}')=?))")
                args.extend((value, value))
        for key, value in (("model", model), ("http_status", http_status), ("request_id", request_id)):
            if value is not None:
                conditions.append(f"{key}=?")
                args.append(value)
        where = " AND ".join(conditions)
        with self.lock, self.db:
            self._prune_audit_locked(time.time())
            total = self.db.execute("SELECT COUNT(*) FROM request_audit WHERE " + where, args).fetchone()[0]
            summary = [dict(r) for r in self.db.execute("SELECT source,error_code,COUNT(*) AS count,MAX(created_at) AS last_seen "
                "FROM request_audit WHERE " + where + " AND outcome='error' GROUP BY source,error_code ORDER BY count DESC LIMIT 20", args)]
            recovered = self.db.execute("SELECT COUNT(*) FROM request_audit WHERE " + where + " AND outcome='success' AND had_errors=1", args).fetchone()[0]
            page_where = where + (" AND id<?" if before is not None else "")
            page_args = [*args, *([before] if before is not None else []), min(200, max(1, limit)) + 1]
            rows = [dict(r) for r in self.db.execute("SELECT * FROM request_audit WHERE " + page_where + " ORDER BY id DESC LIMIT ?", page_args)]
            more = len(rows) > limit
            rows = rows[:limit]
            for row in rows:
                row["attempts"] = json.loads(row.pop("attempts_json"))
            return dict(items=rows, next_cursor=rows[-1]["id"] if more and rows else None, total=total,
                summary=summary, recovered=recovered, retention_days=30, max_rows=MAX_AUDIT_ROWS,
                write_failures=self.audit_write_failures)

    def _prune_bindings_locked(self, now: float) -> None:
        deleted = self.db.execute("DELETE FROM response_bindings WHERE created_at < ?", (now - BINDING_TTL,)).rowcount
        self._binding_count -= deleted
        if self._binding_count > MAX_BINDINGS:
            deleted = self.db.execute("DELETE FROM response_bindings WHERE response_id IN "
                "(SELECT response_id FROM response_bindings ORDER BY created_at,response_id LIMIT ?)",
                (self._binding_count - MAX_BINDINGS,)).rowcount
            self._binding_count -= deleted

    def _prune_locked(self, now: float) -> None:
        today = time.strftime("%Y-%m-%d", time.gmtime(now))
        if today == self._last_pruned_day:
            return
        self.db.execute("DELETE FROM request_daily WHERE day < ?",
                        (time.strftime("%Y-%m-%d", time.gmtime(now - 90 * 86400)),))
        self.db.execute("DELETE FROM request_hourly WHERE hour < ?",
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
        d["model_blocks"] = [dict(r) for r in self.db.execute(
            "SELECT model,kind,retry_at,code,failures FROM model_blocks WHERE account_id=? ORDER BY model", (d["id"],))]
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
                   "cooldown_until", "cooldown_kind", "cooldown_failures", "cooldown_code", "quota_checked_at", "quota_error", "usage_json", "active"}
        data = {k: v for k, v in fields.items() if k in allowed}
        if "models" in data:
            data["models"] = json.dumps(data["models"])
        if "model_mapping" in data:
            data["model_mapping"] = json.dumps(data["model_mapping"])
        with self.lock, self.db:
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

    def set_quota_group(self, account_id: str, group_id: str) -> None:
        self.update(account_id, quota_group=group_id)

    def block_model(self, account_id: str, model: str, kind: str, retry_at: float | None, code: str, failures: int):
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO model_blocks VALUES (?,?,?,?,?,?)",
                            (account_id, model, kind, retry_at, code, failures))

    def clear_model_blocks(self, account_id: str, model: str | None = None):
        with self.lock, self.db:
            if model is None:
                self.db.execute("DELETE FROM model_blocks WHERE account_id=?", (account_id,))
            else:
                self.db.execute("DELETE FROM model_blocks WHERE account_id=? AND model=?", (account_id, model))

    def bind(self, response_id: str, account_id: str) -> None:
        if not isinstance(response_id, str) or not 0 < len(response_id) <= 256:
            raise ValueError("invalid upstream response ID")
        with self.lock:
            now = time.time()
            self._binding_count += self.db.execute("INSERT OR IGNORE INTO response_bindings VALUES (?,?,?)", (response_id, account_id, now)).rowcount
            self._prune_bindings_locked(now)
            self.db.commit()

    def unbind(self, response_id: str) -> None:
        with self.lock, self.db:
            self._binding_count -= self.db.execute("DELETE FROM response_bindings WHERE response_id=?", (response_id,)).rowcount

    def record_request(self, account_id: str, outcome: str, latency_ms: int,
                       model: str = "",
                       input_tokens: int = 0, output_tokens: int = 0, requested_at: float | None = None,
                       context_tokens: int | None = None) -> None:
        now = time.time()
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        with self.lock:
            self._prune_locked(now)
            # Both rollups count the completion hour; pricing still uses request start time.
            for table, column, bucket in (("request_daily", "day", day),
                                           ("request_hourly", "hour", time.strftime("%Y-%m-%dT%H:00:00Z", time.gmtime(now)))):
                self.db.execute(f"""INSERT INTO {table}
                    ({column},account_id,model,outcome,context_tokens,price_period,requests,input_tokens,output_tokens,latency_ms)
                    VALUES (?,?,?,?,?,?,1,?,?,?) ON CONFLICT({column},account_id,model,outcome,context_tokens,price_period) DO UPDATE SET
                    requests=requests+1, input_tokens=input_tokens+excluded.input_tokens,
                    output_tokens=output_tokens+excluded.output_tokens,
                    latency_ms=latency_ms+excluded.latency_ms""",
                    (bucket, account_id, model, outcome, max(0, input_tokens) if context_tokens is None else context_tokens,
                     price_period(requested_at if requested_at is not None else now), max(0, input_tokens), max(0, output_tokens), max(0, latency_ms)))
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
            result = {"hourly_started_at": float(self.setting("hourly_started_at", "0"))}
            for table, column in (("request_daily", "day"), ("request_hourly", "hour")):
                outcome_groups: dict[tuple[str, str], dict] = {}
                model_groups: dict[tuple[str, str], dict] = {}
                for raw in self.db.execute(f"""SELECT {column},model,outcome,context_tokens,price_period,requests,input_tokens,output_tokens,latency_ms
                    FROM {table} WHERE {column}>=?{condition} ORDER BY {column} DESC,outcome""", args):
                    row = dict(raw)
                    key = (row[column], row["outcome"])
                    empty = {"requests": 0, "input_tokens": 0, "output_tokens": 0, "latency_ms": 0,
                             "equivalent_cny": 0.0, "unpriced_input_tokens": 0, "unpriced_output_tokens": 0}
                    priced = {**empty, **row}
                    priced.update(price_usage(pricing, row["model"], row["context_tokens"], row["input_tokens"], row["output_tokens"], row["price_period"]))
                    outcome_item = outcome_groups.setdefault(key, {**empty, column: key[0], "outcome": key[1]})
                    model_item = model_groups.setdefault((row[column], row["model"]),
                        {**empty, column: row[column], "model": row["model"]})
                    for item in (outcome_item, model_item):
                        for field in empty:
                            item[field] += priced[field]
                suffix = "daily" if column == "day" else "hourly"
                result[suffix] = list(outcome_groups.values())
                result[f"model_{suffix}"] = list(model_groups.values())
            return result

    def pricing(self) -> dict:
        return json.loads(self.setting("pricing", '{"default":{},"models":{}}'))

    def set_pricing(self, value: dict) -> None:
        self.set_setting("pricing", json.dumps(value, separators=(",", ":")))

    def lookup_binding(self, response_id: str) -> str | None:
        with self.lock:
            row = self.db.execute("SELECT account_id FROM response_bindings WHERE response_id=? AND created_at >= ?",
                                  (response_id, time.time() - BINDING_TTL)).fetchone()
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
