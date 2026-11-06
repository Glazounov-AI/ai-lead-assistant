
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from core.schemas import LeadSession


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LeadRepository:
    """Постоянный реестр заявок и журнал доставки."""

    def __init__(self, db_path: str = "data/sessions.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS leads (
                    lead_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    session_json TEXT NOT NULL,
                    delivery_status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)

            db.execute("""
                CREATE TABLE IF NOT EXISTS delivery_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lead_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    telegram_message_id INTEGER,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (lead_id) REFERENCES leads(lead_id)
                )
            """)

            db.execute("""
                CREATE TABLE IF NOT EXISTS operator_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lead_id TEXT NOT NULL,
                    operator_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (lead_id) REFERENCES leads(lead_id)
                )
            """)

            db.execute("""
                CREATE INDEX IF NOT EXISTS idx_leads_user
                ON leads(user_id)
            """)

    def save(self, session: LeadSession) -> None:
        now = utc_now()

        with self._connect() as db:
            db.execute("""
                INSERT INTO leads (
                    lead_id, user_id, session_json,
                    delivery_status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(lead_id) DO UPDATE SET
                    session_json = excluded.session_json,
                    delivery_status = excluded.delivery_status,
                    updated_at = excluded.updated_at
            """, (
                session.lead_id,
                session.user_id,
                session.model_dump_json(),
                session.delivery_status,
                now,
                now,
            ))

    def get(self, lead_id: str) -> LeadSession | None:
        with self._connect() as db:
            row = db.execute("""
                SELECT session_json
                FROM leads
                WHERE lead_id = ?
            """, (lead_id,)).fetchone()

        if row is None:
            return None

        return LeadSession.model_validate_json(row["session_json"])

    def list_recent(self, limit: int = 10) -> list[LeadSession]:
        with self._connect() as db:
            rows = db.execute("""
                SELECT session_json
                FROM leads
                ORDER BY created_at DESC, rowid DESC
                LIMIT ?
            """, (limit,)).fetchall()

        return [
            LeadSession.model_validate_json(row["session_json"])
            for row in rows
        ]

    def record_attempt(
        self,
        lead_id: str,
        status: str,
        telegram_message_id: int | None = None,
    ) -> None:
        with self._connect() as db:
            db.execute("""
                INSERT INTO delivery_attempts (
                    lead_id, status, telegram_message_id, created_at
                )
                VALUES (?, ?, ?, ?)
            """, (
                lead_id,
                status,
                telegram_message_id,
                utc_now(),
            ))

    def record_action(
        self,
        lead_id: str,
        operator_id: int,
        action: str,
    ) -> None:
        with self._connect() as db:
            db.execute("""
                INSERT INTO operator_actions (
                    lead_id, operator_id, action, created_at
                )
                VALUES (?, ?, ?, ?)
            """, (
                lead_id,
                operator_id,
                action,
                utc_now(),
            ))
