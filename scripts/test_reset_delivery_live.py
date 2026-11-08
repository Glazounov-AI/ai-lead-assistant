"""Диагностика /reset_delivery на рабочей SQLite-базе.

Команды:
  create  — создать искусственную заявку в статусе sending
  verify  — проверить результат ручного сброса
  cleanup — удалить только тестовые записи

Скрипт не отправляет Telegram-уведомления и не изменяет
существующие методы репозиториев.

Пример:
  python scripts/test_reset_delivery_live.py create
  # в Telegram: /reset_delivery <UUID>
  python scripts/test_reset_delivery_live.py verify
  python scripts/test_reset_delivery_live.py cleanup
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.schemas import LeadSession, ProjectLead
from services.storage.lead_repository import LeadRepository
from services.storage.session_repository import SessionRepository

TEST_USER_ID = -910001
TEST_LEAD_NAME = "TEST RESET DELIVERY"
ID_FILE = ROOT / "data" / "reset_delivery_test_id.txt"


def database_path() -> str:
    return os.getenv("DATABASE_PATH", str(ROOT / "data" / "sessions.db"))


def fail(message: str, code: int = 1) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(code)


def read_test_lead_id() -> str:
    if not ID_FILE.is_file():
        fail(f"Файл с UUID не найден: {ID_FILE}")

    raw = ID_FILE.read_text(encoding="utf-8").strip()
    if not raw:
        fail(f"Файл UUID пуст: {ID_FILE}")

    try:
        return str(UUID(raw))
    except ValueError:
        fail(f"Некорректный UUID в файле: {raw!r}")


def write_test_lead_id(lead_id: str) -> None:
    ID_FILE.parent.mkdir(parents=True, exist_ok=True)
    ID_FILE.write_text(f"{lead_id}\n", encoding="utf-8")


def is_test_lead(session: LeadSession) -> bool:
    return (
        session.user_id == TEST_USER_ID
        and (session.lead.name or "") == TEST_LEAD_NAME
    )


def cmd_create() -> None:
    if ID_FILE.is_file():
        existing = ID_FILE.read_text(encoding="utf-8").strip()
        fail(
            "Тестовая заявка уже подготовлена "
            f"(файл {ID_FILE.name}: {existing!r}). "
            "Сначала выполните cleanup."
        )

    db_path = database_path()
    sessions = SessionRepository(db_path)
    leads = LeadRepository(db_path)

    existing_session = sessions.get(TEST_USER_ID)
    if existing_session is not None:
        fail(
            f"Сессия тестового user_id={TEST_USER_ID} уже существует "
            f"(lead_id={existing_session.lead_id}). "
            "Сначала выполните cleanup."
        )

    with sqlite3.connect(db_path) as db:
        row = db.execute(
            """
            SELECT lead_id
            FROM leads
            WHERE user_id = ?
            LIMIT 1
            """,
            (TEST_USER_ID,),
        ).fetchone()

    if row is not None:
        fail(
            f"В leads уже есть заявка user_id={TEST_USER_ID} "
            f"(lead_id={row[0]}). Сначала выполните cleanup."
        )

    session = LeadSession(
        user_id=TEST_USER_ID,
        chat_id=TEST_USER_ID,
        submitted=True,
        lead=ProjectLead(
            name=TEST_LEAD_NAME,
            contact="@test_reset_delivery",
            business_task="Диагностика команды /reset_delivery",
            consent_to_contact=True,
        ),
    )

    # Штатный путь: accepted -> sending. Telegram не вызывается.
    sessions.save_delivery(session, "accepted")
    session.delivery_status = "sending"
    sessions.save_delivery(session, "sending")

    saved = leads.get(session.lead_id)
    if saved is None or saved.delivery_status != "sending":
        fail("Не удалось сохранить тестовую заявку в статусе sending.")

    write_test_lead_id(session.lead_id)

    print("Тестовая заявка создана.")
    print(f"lead_id: {session.lead_id}")
    print(f"user_id: {TEST_USER_ID}")
    print(f"name: {TEST_LEAD_NAME}")
    print(f"delivery_status: {saved.delivery_status}")
    print(f"uuid_file: {ID_FILE}")
    print()
    print("Дальше в личном чате с ботом:")
    print(f"  /reset_delivery {session.lead_id}")
    print("После подтверждения кнопкой выполните:")
    print("  python scripts/test_reset_delivery_live.py verify")


def cmd_verify() -> None:
    lead_id = read_test_lead_id()
    db_path = database_path()
    sessions = SessionRepository(db_path)
    leads = LeadRepository(db_path)

    checks: list[tuple[str, bool, str]] = []

    session = leads.get(lead_id)
    if session is None:
        fail(f"Заявка {lead_id} не найдена.")

    if not is_test_lead(session):
        fail(
            "UUID указывает не на тестовую заявку "
            f"(user_id={session.user_id}, name={session.lead.name!r})."
        )

    status_ok = session.delivery_status == "uncertain"
    checks.append(
        (
            "leads.delivery_status == uncertain",
            status_ok,
            session.delivery_status,
        )
    )

    with sqlite3.connect(db_path) as db:
        reset_attempts = db.execute(
            """
            SELECT COUNT(*)
            FROM delivery_attempts
            WHERE lead_id = ?
              AND status = 'manually_reset'
            """,
            (lead_id,),
        ).fetchone()[0]

        reset_actions = db.execute(
            """
            SELECT COUNT(*)
            FROM operator_actions
            WHERE lead_id = ?
              AND action = 'manual_reset_delivery'
            """,
            (lead_id,),
        ).fetchone()[0]

        lead_status = db.execute(
            """
            SELECT delivery_status
            FROM leads
            WHERE lead_id = ?
            """,
            (lead_id,),
        ).fetchone()

    checks.append(
        (
            "ровно одна запись manually_reset",
            reset_attempts == 1,
            str(reset_attempts),
        )
    )
    checks.append(
        (
            "ровно одна запись manual_reset_delivery",
            reset_actions == 1,
            str(reset_actions),
        )
    )

    column_ok = (
        lead_status is not None
        and lead_status[0] == "uncertain"
    )
    checks.append(
        (
            "колонка leads.delivery_status == uncertain",
            column_ok,
            "missing" if lead_status is None else lead_status[0],
        )
    )

    active = sessions.get(TEST_USER_ID)
    if active is None:
        checks.append(
            ("активная сессия существует", False, "missing"),
        )
    else:
        same_lead = active.lead_id == lead_id
        checks.append(
            (
                "активная сессия относится к тестовой заявке",
                same_lead,
                active.lead_id,
            )
        )
        checks.append(
            (
                "активная сессия.delivery_status == uncertain",
                active.delivery_status == "uncertain",
                active.delivery_status,
            )
        )

    print(f"Проверка заявки {lead_id}")
    failed = False
    for title, ok, detail in checks:
        mark = "OK" if ok else "FAIL"
        print(f"  [{mark}] {title} ({detail})")
        if not ok:
            failed = True

    if failed:
        fail("Проверка /reset_delivery не пройдена.")

    print("Все проверки успешны.")


def cmd_cleanup() -> None:
    lead_id = read_test_lead_id()
    db_path = database_path()
    leads = LeadRepository(db_path)

    session = leads.get(lead_id)
    if session is None:
        fail(
            f"Заявка {lead_id} не найдена. "
            "Файл UUID не удалён — проверьте базу вручную."
        )

    if not is_test_lead(session):
        fail(
            "Отказ в удалении: заявка не совпадает с тестовыми "
            f"метками (user_id={session.user_id}, "
            f"name={session.lead.name!r})."
        )

    with sqlite3.connect(db_path) as db:
        db.execute("BEGIN IMMEDIATE")

        row = db.execute(
            """
            SELECT user_id, session_json
            FROM leads
            WHERE lead_id = ?
            """,
            (lead_id,),
        ).fetchone()

        if row is None:
            fail(f"Заявка {lead_id} исчезла до очистки.")

        user_id, session_json = row
        stored = LeadSession.model_validate_json(session_json)

        if (
            user_id != TEST_USER_ID
            or stored.user_id != TEST_USER_ID
            or (stored.lead.name or "") != TEST_LEAD_NAME
        ):
            fail(
                "Отказ в удалении внутри транзакции: "
                "тестовые метки не подтверждены."
            )

        db.execute(
            """
            DELETE FROM delivery_reset_confirmations
            WHERE lead_id = ?
            """,
            (lead_id,),
        )
        db.execute(
            """
            DELETE FROM delivery_attempts
            WHERE lead_id = ?
            """,
            (lead_id,),
        )
        db.execute(
            """
            DELETE FROM operator_actions
            WHERE lead_id = ?
            """,
            (lead_id,),
        )
        db.execute(
            """
            DELETE FROM leads
            WHERE lead_id = ?
              AND user_id = ?
            """,
            (lead_id, TEST_USER_ID),
        )
        if db.execute("SELECT changes()").fetchone()[0] != 1:
            raise RuntimeError(
                "Не удалось удалить тестовую заявку из leads"
            )

        db.execute(
            """
            DELETE FROM sessions
            WHERE user_id = ?
            """,
            (TEST_USER_ID,),
        )

        remaining = db.execute(
            """
            SELECT COUNT(*)
            FROM leads
            WHERE user_id = ?
            """,
            (TEST_USER_ID,),
        ).fetchone()[0]
        if remaining != 0:
            raise RuntimeError(
                "После очистки остались заявки тестового user_id"
            )

    if ID_FILE.is_file():
        ID_FILE.unlink()

    print("Тестовая заявка и связанные записи удалены.")
    print(f"lead_id: {lead_id}")
    print(f"uuid_file удалён: {ID_FILE}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Диагностика /reset_delivery на рабочей базе "
            "без отправки Telegram-уведомлений."
        )
    )
    parser.add_argument(
        "command",
        choices=("create", "verify", "cleanup"),
        help="create | verify | cleanup",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "create":
        cmd_create()
    elif args.command == "verify":
        cmd_verify()
    elif args.command == "cleanup":
        cmd_cleanup()
    else:
        fail(f"Неизвестная команда: {args.command}")


if __name__ == "__main__":
    main()
