import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class SettingsStoreConfig:
    """Configuration for SettingsStore.

    db_path: path to the sqlite file.
    """

    db_path: Path


class SettingsStore:
    """Simple SQLite-backed settings KV store with namespaces.

    Values are stored as JSON text so python types survive round-trips.
    Uses a single connection per process and enables WAL mode.
    """

    def __init__(self, cfg: SettingsStoreConfig):
        self.cfg = cfg
        self.cfg.db_path.parent.mkdir(parents=True, exist_ok=True)

        # Single connection per process. python-telegram-bot is asyncio-based,
        # but can still touch DB from different tasks.
        self._conn = sqlite3.connect(
            str(self.cfg.db_path),
            check_same_thread=False,
            isolation_level=None,  # autocommit
        )
        self._conn.row_factory = sqlite3.Row

        self._init_db()

    def _init_db(self) -> None:
        cur = self._conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL;")
        cur.execute("PRAGMA synchronous=NORMAL;")
        cur.execute("PRAGMA foreign_keys=ON;")
        cur.execute("PRAGMA busy_timeout=5000;")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS settings(
              namespace   TEXT NOT NULL,
              key         TEXT NOT NULL,
              value_json  TEXT NOT NULL,
              updated_at  TEXT NOT NULL,
              PRIMARY KEY(namespace, key)
            );
            """
        )
        cur.close()

    # --- public API ---
    def get(self, namespace: str, key: str, default: Any = None) -> Any:
        row = self._conn.execute(
            "SELECT value_json FROM settings WHERE namespace=? AND key=?",
            (namespace, key),
        ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value_json"])
        except Exception:
            # Corrupted JSON shouldn't crash the bot; fallback to default.
            return default

    def set(self, namespace: str, key: str, value: Any) -> None:
        value_json = json.dumps(value, ensure_ascii=False)
        self._conn.execute(
            """
            INSERT INTO settings(namespace, key, value_json, updated_at)
            VALUES(?,?,?,?)
            ON CONFLICT(namespace, key) DO UPDATE SET
              value_json=excluded.value_json,
              updated_at=excluded.updated_at
            """,
            (namespace, key, value_json, _utc_now_iso()),
        )

    def set_json(self, namespace: str, key: str, value: Any) -> None:
        """Compatibility wrapper for callers that explicitly store JSON values."""

        self.set(namespace, key, value)

    def delete(self, namespace: str, key: str) -> None:
        self._conn.execute(
            "DELETE FROM settings WHERE namespace=? AND key=?",
            (namespace, key),
        )

    def list(self, namespace: str) -> Dict[str, Any]:
        rows = self._conn.execute(
            "SELECT key, value_json FROM settings WHERE namespace=? ORDER BY key",
            (namespace,),
        ).fetchall()
        out: Dict[str, Any] = {}
        for r in rows:
            try:
                out[r["key"]] = json.loads(r["value_json"])
            except Exception:
                out[r["key"]] = r["value_json"]
        return out

    def exists(self, namespace: str, key: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM settings WHERE namespace=? AND key=?",
            (namespace, key),
        ).fetchone()
        return row is not None

    # --- one-time migration helpers ---
    def import_json_file_once(self, namespace: str, json_path: Path) -> bool:
        """Import a flat JSON dict file into namespace if that namespace is empty.

        Returns True if import happened.
        """

        if not json_path.exists():
            return False
        if self.list(namespace):
            return False

        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for k, v in data.items():
                    self.set(namespace, str(k), v)
                return True
        except Exception:
            pass
        return False

    def import_text_file_once(self, namespace: str, key: str, text_path: Path) -> bool:
        """Import plain text file into a key if that key doesn't exist yet."""

        if not text_path.exists():
            return False
        if self.exists(namespace, key):
            return False
        try:
            self.set(namespace, key, text_path.read_text(encoding="utf-8"))
            return True
        except Exception:
            return False


def default_store(db_path: Optional[str] = None) -> SettingsStore:
    """Factory with project defaults.

    Defaults to data/newsbot.sqlite unless overridden.
    """

    if db_path is None:
        db_path = os.getenv("NEWS_DB", "data/newsbot.sqlite")
    return SettingsStore(SettingsStoreConfig(db_path=Path(db_path)))
