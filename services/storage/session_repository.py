import sqlite3
from pathlib import Path

from core.schemas import LeadSession


class SessionRepository:
    """Хранение Telegram-сессий в SQLite."""

    def __init__(self, db_path: str = "data/sessions.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        self._initialize()

    def _connect(self):
        return sqlite3.connect(self.db_path)

    def _initialize(self):
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    user_id INTEGER PRIMARY KEY,
                    session_json TEXT NOT NULL
                )
                """
            )

            # Производственная база могла быть создана до
            # появления delivery_cycle_id.
            tables = {
                row[0]
                for row in connection.execute(
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE type = 'table'
                    """
                )
            }
            if "leads" in tables:
                columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(leads)"
                    )
                }
                if "delivery_cycle_id" not in columns:
                    connection.execute(
                        """
                        ALTER TABLE leads
                        ADD COLUMN delivery_cycle_id TEXT
                        """
                    )

    def get(self, user_id: int) -> LeadSession | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT session_json
                FROM sessions
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()

        if row is None:
            return None

        return LeadSession.model_validate_json(row[0])

    def save(self, session: LeadSession) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO sessions (user_id, session_json)
                VALUES (?, ?)
                ON CONFLICT(user_id)
                DO UPDATE SET session_json = excluded.session_json
                """,
                (
                    session.user_id,
                    session.model_dump_json(),
                ),
            )

    def delete(self, user_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM sessions WHERE user_id = ?",
                (user_id,),
            )


    def save_delivery(
        self,
        session: LeadSession,
        event_status: str,
        telegram_message_id: int | None = None,
    ) -> None:
        """Сохранить заявку, сессию и событие одной транзакцией."""
        from datetime import datetime, timezone

        from services.storage.lead_repository import (
            new_delivery_cycle_id,
        )

        expected_status = {
            "accepted": "not_sent",
            "sending": "sending",
            "retrying": "sending",
            "sent": "sent",
            "uncertain": "uncertain",
        }

        if event_status not in expected_status:
            raise ValueError("Недопустимое событие доставки")

        if session.delivery_status != expected_status[event_status]:
            raise ValueError(
                "Событие не соответствует состоянию заявки"
            )

        if not session.submitted:
            raise ValueError("Заявка ещё не принята")

        now = datetime.now(timezone.utc).isoformat()
        session_json = session.model_dump_json()
        # Новый цикл только при входе в sending, не при retrying.
        cycle_id = (
            new_delivery_cycle_id()
            if event_status == "sending"
            else None
        )

        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")

            db.execute("""
                CREATE TABLE IF NOT EXISTS leads (
                    lead_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    session_json TEXT NOT NULL,
                    delivery_status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    delivery_cycle_id TEXT
                )
            """)

            columns = {
                row[1]
                for row in db.execute("PRAGMA table_info(leads)")
            }
            if "delivery_cycle_id" not in columns:
                db.execute(
                    """
                    ALTER TABLE leads
                    ADD COLUMN delivery_cycle_id TEXT
                    """
                )

            db.execute("""
                CREATE TABLE IF NOT EXISTS delivery_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lead_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    telegram_message_id INTEGER,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (lead_id)
                        REFERENCES leads(lead_id)
                )
            """)

            db.execute("""
                INSERT INTO sessions (user_id, session_json)
                VALUES (?, ?)
                ON CONFLICT(user_id)
                DO UPDATE SET
                    session_json = excluded.session_json
            """, (
                session.user_id,
                session_json,
            ))

            if cycle_id is not None:
                db.execute("""
                    INSERT INTO leads (
                        lead_id,
                        user_id,
                        session_json,
                        delivery_status,
                        created_at,
                        updated_at,
                        delivery_cycle_id
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(lead_id)
                    DO UPDATE SET
                        session_json = excluded.session_json,
                        delivery_status = excluded.delivery_status,
                        updated_at = excluded.updated_at,
                        delivery_cycle_id = excluded.delivery_cycle_id
                """, (
                    session.lead_id,
                    session.user_id,
                    session_json,
                    session.delivery_status,
                    now,
                    now,
                    cycle_id,
                ))
            else:
                db.execute("""
                    INSERT INTO leads (
                        lead_id,
                        user_id,
                        session_json,
                        delivery_status,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(lead_id)
                    DO UPDATE SET
                        session_json = excluded.session_json,
                        delivery_status = excluded.delivery_status,
                        updated_at = excluded.updated_at
                """, (
                    session.lead_id,
                    session.user_id,
                    session_json,
                    session.delivery_status,
                    now,
                    now,
                ))

            db.execute("""
                INSERT INTO delivery_attempts (
                    lead_id,
                    status,
                    telegram_message_id,
                    created_at
                )
                VALUES (?, ?, ?, ?)
            """, (
                session.lead_id,
                event_status,
                telegram_message_id,
                now,
            ))


