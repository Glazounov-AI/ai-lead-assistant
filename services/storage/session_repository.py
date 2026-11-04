
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
