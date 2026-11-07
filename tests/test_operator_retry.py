import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

from core.schemas import LeadSession, ProjectLead
from services.storage.lead_repository import LeadRepository
from services.storage.session_repository import SessionRepository


def prepare_lead(tmp_path, status="uncertain"):
    path = str(tmp_path / "sessions.db")
    sessions = SessionRepository(path)
    leads = LeadRepository(path)

    session = LeadSession(
        user_id=123,
        chat_id=123,
        submitted=True,
        lead=ProjectLead(
            name="Клиент",
            contact="@client",
            business_task="Автоматизация заявок",
            consent_to_contact=True,
        ),
    )

    sessions.save_delivery(session, "accepted")

    if status != "not_sent":
        session.delivery_status = "sending"
        sessions.save_delivery(session, "sending")

    if status == "uncertain":
        session.delivery_status = "uncertain"
        sessions.save_delivery(session, "uncertain")

    if status == "sent":
        session.delivery_status = "sent"
        sessions.save_delivery(
            session, "sent", telegram_message_id=111
        )

    return path, leads, session


def make_command(monkeypatch, path, lead_id, side_effect=None):
    from bot import operator_commands as commands

    monkeypatch.setenv("DATABASE_PATH", path)
    monkeypatch.setenv("TELEGRAM_OPERATOR_CHAT_ID", "999999")

    message = SimpleNamespace(reply_text=AsyncMock())

    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=999999),
        effective_chat=SimpleNamespace(type="private"),
        effective_message=message,
    )

    bot = SimpleNamespace(
        send_message=AsyncMock(side_effect=side_effect)
    )
    if side_effect is None:
        bot.send_message.return_value = SimpleNamespace(
            message_id=98765
        )

    context = SimpleNamespace(
        args=[lead_id],
        bot=bot,
    )
    return commands, update, context


def test_pending_lists_uncertain(tmp_path):
    _, leads, session = prepare_lead(tmp_path)

    pending = leads.list_pending()

    assert len(pending) == 1
    assert pending[0].lead_id == session.lead_id


def test_manual_retry_succeeds(monkeypatch, tmp_path):
    path, leads, session = prepare_lead(tmp_path)
    commands, update, context = make_command(
        monkeypatch, path, session.lead_id
    )

    asyncio.run(commands.retry_command(update, context))

    assert context.bot.send_message.await_count == 1
    assert leads.get(session.lead_id).delivery_status == "sent"
    assert leads.list_pending() == []

    with sqlite3.connect(path) as db:
        rows = db.execute(
            """
            SELECT status
            FROM delivery_attempts
            WHERE lead_id = ?
            ORDER BY id
            """,
            (session.lead_id,),
        ).fetchall()

    assert rows[-2:] == [
        ("retrying",),
        ("sent",),
    ]


def test_manual_retry_rejected_when_sent(monkeypatch, tmp_path):
    path, leads, session = prepare_lead(
        tmp_path, status="sent"
    )
    commands, update, context = make_command(
        monkeypatch, path, session.lead_id
    )

    asyncio.run(commands.retry_command(update, context))

    context.bot.send_message.assert_not_awaited()
    assert leads.get(session.lead_id).delivery_status == "sent"


def test_claim_retry_is_atomic(tmp_path):
    _, leads, session = prepare_lead(tmp_path)

    first = leads.claim_retry(session.lead_id)
    second = leads.claim_retry(session.lead_id)

    assert first is not None
    assert second is None
    assert leads.get(session.lead_id).delivery_status == "sending"


def test_manual_retry_telegram_failure(monkeypatch, tmp_path):
    from telegram.error import NetworkError

    path, leads, session = prepare_lead(tmp_path)

    commands, update, context = make_command(
        monkeypatch,
        path,
        session.lead_id,
        side_effect=NetworkError("temporary failure"),
    )

    asyncio.run(
        commands.retry_command(update, context)
    )

    # Попытка отправки была ровно одна.
    assert context.bot.send_message.await_count == 1

    # Заявка не потеряна и остаётся доступной для повтора.
    saved = leads.get(session.lead_id)

    assert saved is not None
    assert saved.submitted is True
    assert saved.delivery_status == "uncertain"

    # Заявка присутствует в списке неподтверждённых.
    pending_ids = {
        item.lead_id for item in leads.list_pending()
    }
    assert session.lead_id in pending_ids

    # Проверяем журнал доставки.
    with sqlite3.connect(path) as db:
        statuses = [
            row[0]
            for row in db.execute(
                """
                SELECT status
                FROM delivery_attempts
                WHERE lead_id = ?
                ORDER BY id
                """,
                (session.lead_id,),
            )
        ]

    assert statuses[-2:] == ["retrying", "uncertain"]

    update.effective_message.reply_text.assert_awaited_once_with(
        "Доставка не подтверждена. "
        "Заявка сохранена в реестре."
    )


def test_retry_sqlite_failure_after_telegram_success(
    monkeypatch, tmp_path
):
    path, leads, session = prepare_lead(tmp_path)

    commands, update, context = make_command(
        monkeypatch,
        path,
        session.lead_id,
    )

    # Telegram отправит сообщение успешно,
    # но запись результата в SQLite завершится ошибкой.
    def failed_complete_retry(*args, **kwargs):
        raise sqlite3.OperationalError(
            "database is locked"
        )

    monkeypatch.setattr(
        LeadRepository,
        "complete_retry",
        failed_complete_retry,
    )

    asyncio.run(
        commands.retry_command(update, context)
    )

    # Telegram получил только одно уведомление.
    context.bot.send_message.assert_awaited_once()

    # Успешная доставка не зафиксирована в SQLite.
    saved = leads.get(session.lead_id)

    assert saved is not None
    assert saved.submitted is True
    assert saved.delivery_status == "sending"

    # Оператор получает предупреждение.
    update.effective_message.reply_text.assert_awaited_once_with(
        "Telegram принял уведомление, но сохранить "
        "подтверждение не удалось. "
        "Проверьте заявку вручную."
    )

    # Повторная отправка заблокирована.
    assert leads.claim_retry(session.lead_id) is None

    # Успешная доставка не записана в журнал.
    with sqlite3.connect(path) as db:
        statuses = [
            row[0]
            for row in db.execute(
                """
                SELECT status
                FROM delivery_attempts
                WHERE lead_id = ?
                ORDER BY id
                """,
                (session.lead_id,),
            )
        ]

    assert statuses[-1] == "retrying"
    assert "sent" not in statuses


def test_manual_mark_sent(monkeypatch, tmp_path):
    path, leads, session = prepare_lead(
        tmp_path,
        status="sending",
    )

    operator_id = 999999

    result = leads.mark_sent_manually(
        session.lead_id,
        operator_id,
    )

    assert result is True

    # Статус заявки изменился.
    saved = leads.get(session.lead_id)

    assert saved is not None
    assert saved.submitted is True
    assert saved.delivery_status == "sent"

    # Повторная отправка запрещена.
    assert leads.claim_retry(session.lead_id) is None

    # Проверяем журнал доставки и действий оператора.
    with sqlite3.connect(path) as db:
        attempt = db.execute(
            """
            SELECT status, telegram_message_id
            FROM delivery_attempts
            WHERE lead_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (session.lead_id,),
        ).fetchone()

        action = db.execute(
            """
            SELECT operator_id, action
            FROM operator_actions
            WHERE lead_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (session.lead_id,),
        ).fetchone()

    assert attempt == ("manually_confirmed", None)
    assert action == (
        operator_id,
        "manual_mark_sent",
    )


def test_manual_mark_sent_rollback_on_sqlite_failure(tmp_path):
    path, leads, session = prepare_lead(
        tmp_path,
        status="sending",
    )

    # Искусственно запрещаем запись в журнал оператора.
    with sqlite3.connect(path) as db:
        db.execute(
            """
            CREATE TRIGGER fail_operator_action
            BEFORE INSERT ON operator_actions
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'simulated SQLite failure'
                );
            END
            """
        )

    # Метод должен завершиться ошибкой SQLite.
    import pytest

    with pytest.raises(
        sqlite3.IntegrityError,
        match="simulated SQLite failure",
    ):
        leads.mark_sent_manually(
            session.lead_id,
            999999,
        )

    # Проверяем, что статус не изменился.
    saved = leads.get(session.lead_id)

    assert saved is not None
    assert saved.delivery_status == "sending"

    # Проверяем отсутствие частично записанных событий.
    with sqlite3.connect(path) as db:
        attempts = db.execute(
            """
            SELECT COUNT(*)
            FROM delivery_attempts
            WHERE lead_id = ?
              AND status = 'manually_confirmed'
            """,
            (session.lead_id,),
        ).fetchone()[0]

        actions = db.execute(
            """
            SELECT COUNT(*)
            FROM operator_actions
            WHERE lead_id = ?
              AND action = 'manual_mark_sent'
            """,
            (session.lead_id,),
        ).fetchone()[0]

    assert attempts == 0
    assert actions == 0


def test_mark_sent_command_success(monkeypatch, tmp_path):
    path, leads, session = prepare_lead(
        tmp_path,
        status="sending",
    )

    commands, update, context = make_command(
        monkeypatch,
        path,
        session.lead_id,
    )

    asyncio.run(
        commands.mark_sent_command(update, context)
    )

    # Заявка подтверждена вручную.
    saved = leads.get(session.lead_id)

    assert saved is not None
    assert saved.delivery_status == "sent"

    # Telegram не должен отправлять новое уведомление.
    context.bot.send_message.assert_not_awaited()

    # Оператор получает подтверждение.
    update.effective_message.reply_text.assert_awaited_once_with(
        "Доставка подтверждена оператором.\n"
        f"ID: {session.lead_id}"
    )

    # Действие оператора записано в журнал.
    with sqlite3.connect(path) as db:
        action = db.execute(
            """
            SELECT operator_id, action
            FROM operator_actions
            WHERE lead_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (session.lead_id,),
        ).fetchone()

    assert action == (999999, "manual_mark_sent")


def test_mark_sent_command_rejects_unauthorized_user(
    monkeypatch, tmp_path
):
    path, leads, session = prepare_lead(
        tmp_path,
        status="sending",
    )

    commands, update, context = make_command(
        monkeypatch,
        path,
        session.lead_id,
    )

    # Команду пытается выполнить другой пользователь.
    update.effective_user.id = 123456

    asyncio.run(
        commands.mark_sent_command(update, context)
    )

    # Статус заявки не изменился.
    saved = leads.get(session.lead_id)

    assert saved is not None
    assert saved.delivery_status == "sending"

    # Уведомление оператору не отправлялось.
    context.bot.send_message.assert_not_awaited()

    # Постороннему пользователю отказано.
    update.effective_message.reply_text.assert_awaited_once_with(
        "Команда недоступна."
    )

    # Действие оператора не зарегистрировано.
    with sqlite3.connect(path) as db:
        count = db.execute(
            """
            SELECT COUNT(*)
            FROM operator_actions
            WHERE lead_id = ?
              AND action = 'manual_mark_sent'
            """,
            (session.lead_id,),
        ).fetchone()[0]

    assert count == 0


def test_mark_sent_command_already_sent(monkeypatch, tmp_path):
    path, leads, session = prepare_lead(
        tmp_path,
        status="sent",
    )

    commands, update, context = make_command(
        monkeypatch,
        path,
        session.lead_id,
    )

    asyncio.run(
        commands.mark_sent_command(update, context)
    )

    saved = leads.get(session.lead_id)

    assert saved is not None
    assert saved.delivery_status == "sent"

    # Повторной отправки сообщения нет.
    context.bot.send_message.assert_not_awaited()

    # Оператор получает отказ.
    update.effective_message.reply_text.assert_awaited_once_with(
        "Заявка не найдена или её статус "
        "не позволяет ручное подтверждение. "
        "Проверьте /lead UUID."
    )

    # Ручное подтверждение не записано.
    with sqlite3.connect(path) as db:
        attempts = db.execute(
            """
            SELECT COUNT(*)
            FROM delivery_attempts
            WHERE lead_id = ?
              AND status = 'manually_confirmed'
            """,
            (session.lead_id,),
        ).fetchone()[0]

        actions = db.execute(
            """
            SELECT COUNT(*)
            FROM operator_actions
            WHERE lead_id = ?
              AND action = 'manual_mark_sent'
            """,
            (session.lead_id,),
        ).fetchone()[0]

    assert attempts == 0
    assert actions == 0


def test_mark_sent_command_sqlite_failure(monkeypatch, tmp_path):
    path, leads, session = prepare_lead(
        tmp_path,
        status="sending",
    )

    commands, update, context = make_command(
        monkeypatch,
        path,
        session.lead_id,
    )

    # Имитируем ошибку SQLite при записи действия оператора.
    with sqlite3.connect(path) as db:
        db.execute(
            """
            CREATE TRIGGER fail_manual_confirmation
            BEFORE INSERT ON operator_actions
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'simulated SQLite failure'
                );
            END
            """
        )

    asyncio.run(
        commands.mark_sent_command(update, context)
    )

    # Статус должен остаться прежним.
    saved = leads.get(session.lead_id)

    assert saved is not None
    assert saved.delivery_status == "sending"

    # Оператор получает сообщение об ошибке.
    update.effective_message.reply_text.assert_awaited_once_with(
        "Не удалось сохранить подтверждение доставки."
    )

    # Новое Telegram-уведомление не отправлялось.
    context.bot.send_message.assert_not_awaited()

    # Частично записанных событий быть не должно.
    with sqlite3.connect(path) as db:
        attempts = db.execute(
            """
            SELECT COUNT(*)
            FROM delivery_attempts
            WHERE lead_id = ?
              AND status = 'manually_confirmed'
            """,
            (session.lead_id,),
        ).fetchone()[0]

        actions = db.execute(
            """
            SELECT COUNT(*)
            FROM operator_actions
            WHERE lead_id = ?
              AND action = 'manual_mark_sent'
            """,
            (session.lead_id,),
        ).fetchone()[0]

    assert attempts == 0
    assert actions == 0


