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


    def list_pending(self, limit: int = 20) -> list[LeadSession]:
        """Принятые заявки без подтверждённой доставки."""
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT session_json
                FROM leads
                WHERE delivery_status IN (
                    'not_sent', 'sending', 'uncertain'
                )
                ORDER BY created_at ASC, rowid ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        return [
            LeadSession.model_validate_json(row["session_json"])
            for row in rows
        ]

    def claim_retry(self, lead_id: str) -> LeadSession | None:
        """Атомарно зарезервировать заявку для повтора."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")

            row = db.execute(
                """
                SELECT session_json, delivery_status
                FROM leads
                WHERE lead_id = ?
                """,
                (lead_id,),
            ).fetchone()

            if row is None:
                return None

            if row["delivery_status"] not in (
                "not_sent", "uncertain"
            ):
                return None

            session = LeadSession.model_validate_json(
                row["session_json"]
            )

            if not session.submitted:
                return None

            session.delivery_status = "sending"
            now = utc_now()

            db.execute(
                """
                UPDATE leads
                SET session_json = ?,
                    delivery_status = 'sending',
                    updated_at = ?
                WHERE lead_id = ?
                """,
                (
                    session.model_dump_json(),
                    now,
                    lead_id,
                ),
            )

            # Обновляем активную сессию только если она
            # всё ещё относится к этой заявке.
            current = db.execute(
                """
                SELECT session_json
                FROM sessions
                WHERE user_id = ?
                """,
                (session.user_id,),
            ).fetchone()

            if current is not None:
                active = LeadSession.model_validate_json(
                    current["session_json"]
                )

                if active.lead_id == lead_id:
                    db.execute(
                        """
                        UPDATE sessions
                        SET session_json = ?
                        WHERE user_id = ?
                        """,
                        (
                            session.model_dump_json(),
                            session.user_id,
                        ),
                    )

            db.execute(
                """
                INSERT INTO delivery_attempts (
                    lead_id, status, telegram_message_id,
                    created_at
                )
                VALUES (?, 'retrying', NULL, ?)
                """,
                (lead_id, now),
            )

            return session

    def complete_retry(
        self,
        session: LeadSession,
        status: str,
        telegram_message_id: int | None = None,
    ) -> None:
        """Сохранить результат ручной отправки."""
        if status not in ("sent", "uncertain"):
            raise ValueError("Недопустимый статус доставки")

        if (
            status == "sent"
            and telegram_message_id is None
        ):
            raise ValueError(
                "Для успешной доставки нужен message_id"
            )

        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")

            row = db.execute(
                """
                SELECT session_json, delivery_status
                FROM leads
                WHERE lead_id = ?
                """,
                (session.lead_id,),
            ).fetchone()

            if row is None:
                raise ValueError("Заявка не найдена")

            if row["delivery_status"] != "sending":
                raise ValueError(
                    "Заявка не находится в состоянии отправки"
                )

            stored = LeadSession.model_validate_json(
                row["session_json"]
            )
            stored.delivery_status = status
            now = utc_now()

            db.execute(
                """
                UPDATE leads
                SET session_json = ?,
                    delivery_status = ?,
                    updated_at = ?
                WHERE lead_id = ?
                """,
                (
                    stored.model_dump_json(),
                    status,
                    now,
                    session.lead_id,
                ),
            )

            current = db.execute(
                """
                SELECT session_json
                FROM sessions
                WHERE user_id = ?
                """,
                (stored.user_id,),
            ).fetchone()

            if current is not None:
                active = LeadSession.model_validate_json(
                    current["session_json"]
                )

                if active.lead_id == stored.lead_id:
                    db.execute(
                        """
                        UPDATE sessions
                        SET session_json = ?
                        WHERE user_id = ?
                        """,
                        (
                            stored.model_dump_json(),
                            stored.user_id,
                        ),
                    )

            db.execute(
                """
                INSERT INTO delivery_attempts (
                    lead_id, status,
                    telegram_message_id, created_at
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    stored.lead_id,
                    status,
                    telegram_message_id,
                    now,
                ),
            )

    def mark_sent_manually(
        self,
        lead_id: str,
        operator_id: int,
    ) -> bool:
        """Ручное подтверждение доставки заявки."""

        now = utc_now()

        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")

            row = db.execute(
                """
                SELECT session_json, delivery_status
                FROM leads
                WHERE lead_id = ?
                """,
                (lead_id,),
            ).fetchone()

            if row is None:
                return False

            if row["delivery_status"] != "sending":
                return False

            session = LeadSession.model_validate_json(
                row["session_json"]
            )

            if not session.submitted:
                return False

            session.delivery_status = "sent"
            session_json = session.model_dump_json()

            db.execute(
                """
                UPDATE leads
                SET session_json = ?,
                    delivery_status = 'sent',
                    updated_at = ?
                WHERE lead_id = ?
                """,
                (session_json, now, lead_id),
            )

            # Синхронизируем активную сессию,
            # только если это та же заявка.
            current = db.execute(
                """
                SELECT session_json
                FROM sessions
                WHERE user_id = ?
                """,
                (session.user_id,),
            ).fetchone()

            if current is not None:
                active = LeadSession.model_validate_json(
                    current["session_json"]
                )

                if active.lead_id == lead_id:
                    db.execute(
                        """
                        UPDATE sessions
                        SET session_json = ?
                        WHERE user_id = ?
                        """,
                        (
                            session_json,
                            session.user_id,
                        ),
                    )

            # Событие ручного подтверждения.
            # message_id не выдумываем.
            db.execute(
                """
                INSERT INTO delivery_attempts (
                    lead_id,
                    status,
                    telegram_message_id,
                    created_at
                )
                VALUES (?, 'manually_confirmed', NULL, ?)
                """,
                (lead_id, now),
            )

            # Аудит действия оператора.
            db.execute(
                """
                INSERT INTO operator_actions (
                    lead_id,
                    operator_id,
                    action,
                    created_at
                )
                VALUES (?, ?, 'manual_mark_sent', ?)
                """,
                (lead_id, operator_id, now),
            )

        return True
