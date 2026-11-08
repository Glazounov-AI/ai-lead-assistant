import asyncio
import sqlite3
import pytest

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


def test_manual_reset_delivery_success(tmp_path):
    path, leads, session = prepare_lead(
        tmp_path,
        status="sending",
    )

    operator_id = 999999

    result = leads.reset_delivery_manually(
        session.lead_id,
        operator_id,
    )

    assert result is True

    # Заявка перешла в uncertain.
    saved = leads.get(session.lead_id)

    assert saved is not None
    assert saved.submitted is True
    assert saved.delivery_status == "uncertain"

    # Проверяем журнал событий и действий оператора.
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

    assert attempt == ("manually_reset", None)
    assert action == (
        operator_id,
        "manual_reset_delivery",
    )

    # Проверяем возможность повторной отправки
    # только после проверки истории ручного сброса.
    assert leads.claim_retry(session.lead_id) is not None


@pytest.mark.parametrize(
    "status",
    ["sent", "uncertain", "not_sent"],
)
def test_manual_reset_delivery_rejects_invalid_status(
    tmp_path,
    status,
):
    path, leads, session = prepare_lead(
        tmp_path,
        status=status,
    )

    result = leads.reset_delivery_manually(
        session.lead_id,
        999999,
    )

    assert result is False

    # Исходный статус не изменился.
    saved = leads.get(session.lead_id)

    assert saved is not None
    assert saved.delivery_status == status

    # События ручного сброса не создавались.
    with sqlite3.connect(path) as db:
        attempts = db.execute(
            """
            SELECT COUNT(*)
            FROM delivery_attempts
            WHERE lead_id = ?
              AND status = 'manually_reset'
            """,
            (session.lead_id,),
        ).fetchone()[0]

        actions = db.execute(
            """
            SELECT COUNT(*)
            FROM operator_actions
            WHERE lead_id = ?
              AND action = 'manual_reset_delivery'
            """,
            (session.lead_id,),
        ).fetchone()[0]

    assert attempts == 0
    assert actions == 0


def test_manual_reset_delivery_rollback(tmp_path):
    path, leads, session = prepare_lead(
        tmp_path,
        status="sending",
    )

    # Имитируем ошибку записи аудита.
    with sqlite3.connect(path) as db:
        db.execute(
            """
            CREATE TRIGGER fail_manual_reset_audit
            BEFORE INSERT ON operator_actions
            WHEN NEW.action = 'manual_reset_delivery'
            BEGIN
                SELECT RAISE(ABORT, 'audit write failed');
            END;
            """
        )

    # Операция должна завершиться ошибкой SQLite.
    with pytest.raises(
        sqlite3.DatabaseError,
        match="audit write failed",
    ):
        leads.reset_delivery_manually(
            session.lead_id,
            999999,
        )

    # Статус заявки остался прежним.
    saved = leads.get(session.lead_id)

    assert saved is not None
    assert saved.delivery_status == "sending"

    # Активная сессия тоже не изменилась.
    with sqlite3.connect(path) as db:
        row = db.execute(
            """
            SELECT session_json
            FROM sessions
            WHERE user_id = ?
            """,
            (session.user_id,),
        ).fetchone()

        assert row is not None

        active = LeadSession.model_validate_json(row[0])
        assert active.delivery_status == "sending"

        # Запись о ручном сбросе также откатилась.
        count = db.execute(
            """
            SELECT COUNT(*)
            FROM delivery_attempts
            WHERE lead_id = ?
              AND status = 'manually_reset'
            """,
            (session.lead_id,),
        ).fetchone()[0]

        assert count == 0


def test_delivery_reset_confirmation_success(tmp_path):
    path, leads, session = prepare_lead(
        tmp_path,
        status="sending",
    )
    operator_id = 999999

    token = leads.create_delivery_reset_confirmation(
        session.lead_id,
        operator_id,
    )

    assert token is not None
    assert len(f"reset:yes:{token}".encode("utf-8")) <= 64

    result = leads.confirm_delivery_reset(token, operator_id)

    assert result == "ok"

    saved = leads.get(session.lead_id)

    assert saved is not None
    assert saved.delivery_status == "uncertain"

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

        confirmation = db.execute(
            """
            SELECT used_at, cancelled_at
            FROM delivery_reset_confirmations
            WHERE token = ?
            """,
            (token,),
        ).fetchone()

    assert attempt == ("manually_reset", None)
    assert action == (operator_id, "manual_reset_delivery")
    assert confirmation is not None
    assert confirmation[0] is not None
    assert confirmation[1] is None


def test_delivery_reset_confirmation_cancel(tmp_path):
    path, leads, session = prepare_lead(
        tmp_path,
        status="sending",
    )
    operator_id = 999999

    token = leads.create_delivery_reset_confirmation(
        session.lead_id,
        operator_id,
    )

    assert token is not None
    assert leads.cancel_delivery_reset_confirmation(
        token,
        operator_id,
    ) is True
    assert leads.cancel_delivery_reset_confirmation(
        token,
        operator_id,
    ) is False

    saved = leads.get(session.lead_id)

    assert saved is not None
    assert saved.delivery_status == "sending"
    assert leads.confirm_delivery_reset(token, operator_id) == (
        "consumed"
    )

    with sqlite3.connect(path) as db:
        attempts = db.execute(
            """
            SELECT COUNT(*)
            FROM delivery_attempts
            WHERE lead_id = ?
              AND status = 'manually_reset'
            """,
            (session.lead_id,),
        ).fetchone()[0]

        actions = db.execute(
            """
            SELECT COUNT(*)
            FROM operator_actions
            WHERE lead_id = ?
              AND action = 'manual_reset_delivery'
            """,
            (session.lead_id,),
        ).fetchone()[0]

        cancelled_at = db.execute(
            """
            SELECT cancelled_at
            FROM delivery_reset_confirmations
            WHERE token = ?
            """,
            (token,),
        ).fetchone()[0]

    assert attempts == 0
    assert actions == 0
    assert cancelled_at is not None


def test_delivery_reset_confirmation_expired(tmp_path):
    path, leads, session = prepare_lead(
        tmp_path,
        status="sending",
    )
    operator_id = 999999

    token = leads.create_delivery_reset_confirmation(
        session.lead_id,
        operator_id,
    )
    assert token is not None

    with sqlite3.connect(path) as db:
        db.execute(
            """
            UPDATE delivery_reset_confirmations
            SET expires_at = ?
            WHERE token = ?
            """,
            ("2000-01-01T00:00:00+00:00", token),
        )

    assert leads.confirm_delivery_reset(token, operator_id) == (
        "expired"
    )

    saved = leads.get(session.lead_id)

    assert saved is not None
    assert saved.delivery_status == "sending"

    with sqlite3.connect(path) as db:
        attempts = db.execute(
            """
            SELECT COUNT(*)
            FROM delivery_attempts
            WHERE lead_id = ?
              AND status = 'manually_reset'
            """,
            (session.lead_id,),
        ).fetchone()[0]

    assert attempts == 0


def test_delivery_reset_confirmation_double_confirm(tmp_path):
    _, leads, session = prepare_lead(
        tmp_path,
        status="sending",
    )
    operator_id = 999999

    token = leads.create_delivery_reset_confirmation(
        session.lead_id,
        operator_id,
    )
    assert token is not None

    assert leads.confirm_delivery_reset(token, operator_id) == "ok"
    assert leads.confirm_delivery_reset(token, operator_id) == (
        "consumed"
    )

    saved = leads.get(session.lead_id)

    assert saved is not None
    assert saved.delivery_status == "uncertain"


def test_delivery_reset_confirmation_rejects_stale_cycle(
    tmp_path,
):
    path, leads, session = prepare_lead(
        tmp_path,
        status="sending",
    )
    operator_id = 999999

    token = leads.create_delivery_reset_confirmation(
        session.lead_id,
        operator_id,
    )
    assert token is not None

    # Новый цикл без использования старого токена.
    assert leads.reset_delivery_manually(
        session.lead_id,
        operator_id,
    ) is True
    claimed = leads.claim_retry(session.lead_id)
    assert claimed is not None
    assert claimed.delivery_status == "sending"

    assert leads.confirm_delivery_reset(token, operator_id) == (
        "stale"
    )

    saved = leads.get(session.lead_id)

    assert saved is not None
    assert saved.delivery_status == "sending"

    with sqlite3.connect(path) as db:
        resets = db.execute(
            """
            SELECT COUNT(*)
            FROM delivery_attempts
            WHERE lead_id = ?
              AND status = 'manually_reset'
            """,
            (session.lead_id,),
        ).fetchone()[0]

        # Один сброс от reset_delivery_manually, без второго.
        assert resets == 1


def test_delivery_reset_confirmation_wrong_operator(tmp_path):
    _, leads, session = prepare_lead(
        tmp_path,
        status="sending",
    )

    token = leads.create_delivery_reset_confirmation(
        session.lead_id,
        999999,
    )
    assert token is not None

    assert leads.confirm_delivery_reset(token, 123456) == "invalid"
    assert leads.cancel_delivery_reset_confirmation(
        token,
        123456,
    ) is False

    saved = leads.get(session.lead_id)

    assert saved is not None
    assert saved.delivery_status == "sending"


def test_delivery_reset_confirmation_invalid_token(tmp_path):
    _, leads, session = prepare_lead(
        tmp_path,
        status="sending",
    )

    assert leads.confirm_delivery_reset(
        "not-a-real-token",
        999999,
    ) == "invalid"
    assert leads.cancel_delivery_reset_confirmation(
        "not-a-real-token",
        999999,
    ) is False

    saved = leads.get(session.lead_id)

    assert saved is not None
    assert saved.delivery_status == "sending"


def test_delivery_reset_confirmation_rollback(tmp_path):
    path, leads, session = prepare_lead(
        tmp_path,
        status="sending",
    )
    operator_id = 999999

    token = leads.create_delivery_reset_confirmation(
        session.lead_id,
        operator_id,
    )
    assert token is not None

    with sqlite3.connect(path) as db:
        db.execute(
            """
            CREATE TRIGGER fail_reset_confirm_audit
            BEFORE INSERT ON operator_actions
            WHEN NEW.action = 'manual_reset_delivery'
            BEGIN
                SELECT RAISE(ABORT, 'audit write failed');
            END;
            """
        )

    with pytest.raises(
        sqlite3.DatabaseError,
        match="audit write failed",
    ):
        leads.confirm_delivery_reset(token, operator_id)

    saved = leads.get(session.lead_id)

    assert saved is not None
    assert saved.delivery_status == "sending"

    with sqlite3.connect(path) as db:
        attempts = db.execute(
            """
            SELECT COUNT(*)
            FROM delivery_attempts
            WHERE lead_id = ?
              AND status = 'manually_reset'
            """,
            (session.lead_id,),
        ).fetchone()[0]

        used_at = db.execute(
            """
            SELECT used_at
            FROM delivery_reset_confirmations
            WHERE token = ?
            """,
            (token,),
        ).fetchone()[0]

        active = db.execute(
            """
            SELECT session_json
            FROM sessions
            WHERE user_id = ?
            """,
            (session.user_id,),
        ).fetchone()

    assert attempts == 0
    assert used_at is None
    assert active is not None
    assert (
        LeadSession.model_validate_json(active[0]).delivery_status
        == "sending"
    )


def test_reset_delivery_callback_cancel(
    tmp_path,
    monkeypatch,
):
    from unittest.mock import AsyncMock
    from types import SimpleNamespace
    import asyncio

    from bot import operator_commands as commands

    path, leads, session = prepare_lead(
        tmp_path,
        status="sending",
    )

    monkeypatch.setenv("DATABASE_PATH", path)
    monkeypatch.setenv(
        "TELEGRAM_OPERATOR_CHAT_ID",
        "999999",
    )

    token = leads.create_delivery_reset_confirmation(
        session.lead_id,
        999999,
    )
    assert token is not None

    query = SimpleNamespace(
        data=f"reset:no:{token}",
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )

    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=999999),
        effective_chat=SimpleNamespace(type="private"),
    )

    context = SimpleNamespace()

    asyncio.run(
        commands.reset_delivery_callback(
            update,
            context,
        )
    )

    query.answer.assert_awaited_once_with("Отменено.")
    query.edit_message_text.assert_awaited_once()

    # Статус заявки остался прежним.
    saved = leads.get(session.lead_id)

    assert saved is not None
    assert saved.delivery_status == "sending"

    # Ручной сброс не записывался в историю.
    with sqlite3.connect(path) as db:
        count = db.execute(
            """
            SELECT COUNT(*)
            FROM operator_actions
            WHERE lead_id = ?
              AND action = 'manual_reset_delivery'
            """,
            (session.lead_id,),
        ).fetchone()[0]

        cancelled_at = db.execute(
            """
            SELECT cancelled_at
            FROM delivery_reset_confirmations
            WHERE token = ?
            """,
            (token,),
        ).fetchone()[0]

    assert count == 0
    assert cancelled_at is not None


def test_reset_delivery_callback_confirm(
    tmp_path,
    monkeypatch,
):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from bot import operator_commands as commands

    path, leads, session = prepare_lead(
        tmp_path,
        status="sending",
    )

    monkeypatch.setenv("DATABASE_PATH", path)
    monkeypatch.setenv(
        "TELEGRAM_OPERATOR_CHAT_ID",
        "999999",
    )

    token = leads.create_delivery_reset_confirmation(
        session.lead_id,
        999999,
    )
    assert token is not None

    query = SimpleNamespace(
        data=f"reset:yes:{token}",
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )

    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=999999),
        effective_chat=SimpleNamespace(type="private"),
    )

    asyncio.run(
        commands.reset_delivery_callback(
            update,
            SimpleNamespace(),
        )
    )

    # Telegram получил подтверждение нажатия.
    query.answer.assert_awaited_once_with(
        "Сброс выполнен."
    )
    query.edit_message_text.assert_awaited_once()

    # Статус заявки изменился.
    saved = leads.get(session.lead_id)

    assert saved is not None
    assert saved.delivery_status == "uncertain"

    # Действие оператора записано.
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

        attempt = db.execute(
            """
            SELECT status
            FROM delivery_attempts
            WHERE lead_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (session.lead_id,),
        ).fetchone()

    assert action == (
        999999,
        "manual_reset_delivery",
    )
    assert attempt == ("manually_reset",)


def test_reset_delivery_callback_double_confirm(
    tmp_path,
    monkeypatch,
):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from bot import operator_commands as commands

    path, leads, session = prepare_lead(
        tmp_path,
        status="sending",
    )

    monkeypatch.setenv("DATABASE_PATH", path)
    monkeypatch.setenv(
        "TELEGRAM_OPERATOR_CHAT_ID",
        "999999",
    )

    token = leads.create_delivery_reset_confirmation(
        session.lead_id,
        999999,
    )
    assert token is not None

    query = SimpleNamespace(
        data=f"reset:yes:{token}",
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )

    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=999999),
        effective_chat=SimpleNamespace(type="private"),
    )

    async def run_twice():
        await commands.reset_delivery_callback(
            update,
            SimpleNamespace(),
        )
        await commands.reset_delivery_callback(
            update,
            SimpleNamespace(),
        )

    asyncio.run(run_twice())

    saved = leads.get(session.lead_id)

    assert saved is not None
    assert saved.delivery_status == "uncertain"

    with sqlite3.connect(path) as db:
        attempts = db.execute(
            """
            SELECT COUNT(*)
            FROM delivery_attempts
            WHERE lead_id = ?
              AND status = 'manually_reset'
            """,
            (session.lead_id,),
        ).fetchone()[0]

        actions = db.execute(
            """
            SELECT COUNT(*)
            FROM operator_actions
            WHERE lead_id = ?
              AND action = 'manual_reset_delivery'
            """,
            (session.lead_id,),
        ).fetchone()[0]

    assert attempts == 1
    assert actions == 1

    assert query.answer.await_count == 2
    assert query.edit_message_text.await_count == 2


def test_reset_delivery_callback_unauthorized(
    tmp_path,
    monkeypatch,
):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from bot import operator_commands as commands

    path, leads, session = prepare_lead(
        tmp_path,
        status="sending",
    )

    monkeypatch.setenv("DATABASE_PATH", path)
    monkeypatch.setenv(
        "TELEGRAM_OPERATOR_CHAT_ID",
        "999999",
    )

    token = leads.create_delivery_reset_confirmation(
        session.lead_id,
        999999,
    )
    assert token is not None

    query = SimpleNamespace(
        data=f"reset:yes:{token}",
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )

    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=123456),
        effective_chat=SimpleNamespace(type="private"),
    )

    asyncio.run(
        commands.reset_delivery_callback(
            update,
            SimpleNamespace(),
        )
    )

    # Постороннему пользователю отказано.
    query.answer.assert_awaited_once_with(
        "Действие недоступно.",
        show_alert=True,
    )

    # Сообщение с кнопками не изменяется.
    query.edit_message_text.assert_not_awaited()

    # Заявка осталась в sending.
    saved = leads.get(session.lead_id)

    assert saved is not None
    assert saved.delivery_status == "sending"

    # Аудит ручного сброса не создавался.
    with sqlite3.connect(path) as db:
        count = db.execute(
            """
            SELECT COUNT(*)
            FROM operator_actions
            WHERE lead_id = ?
              AND action = 'manual_reset_delivery'
            """,
            (session.lead_id,),
        ).fetchone()[0]

    assert count == 0


def _create_legacy_sqlite_without_cycle_id(path: str) -> dict:
    """Старая производственная схема без delivery_cycle_id."""
    from core.schemas import LeadSession, ProjectLead

    session = LeadSession(
        user_id=123,
        chat_id=123,
        submitted=True,
        delivery_status="sending",
        lead=ProjectLead(
            name="Legacy Client",
            contact="@legacy",
            business_task="Сохранить заявку",
            consent_to_contact=True,
        ),
    )
    session_json = session.model_dump_json()
    created_at = "2024-01-01T00:00:00+00:00"

    with sqlite3.connect(path) as db:
        db.execute(
            """
            CREATE TABLE sessions (
                user_id INTEGER PRIMARY KEY,
                session_json TEXT NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE TABLE leads (
                lead_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                session_json TEXT NOT NULL,
                delivery_status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE TABLE delivery_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id TEXT NOT NULL,
                status TEXT NOT NULL,
                telegram_message_id INTEGER,
                created_at TEXT NOT NULL,
                FOREIGN KEY (lead_id) REFERENCES leads(lead_id)
            )
            """
        )
        db.execute(
            """
            CREATE TABLE operator_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id TEXT NOT NULL,
                operator_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (lead_id) REFERENCES leads(lead_id)
            )
            """
        )
        db.execute(
            """
            INSERT INTO sessions (user_id, session_json)
            VALUES (?, ?)
            """,
            (session.user_id, session_json),
        )
        db.execute(
            """
            INSERT INTO leads (
                lead_id, user_id, session_json,
                delivery_status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                session.lead_id,
                session.user_id,
                session_json,
                "sending",
                created_at,
                created_at,
            ),
        )
        db.execute(
            """
            INSERT INTO delivery_attempts (
                lead_id, status, telegram_message_id, created_at
            )
            VALUES (?, 'sending', NULL, ?)
            """,
            (session.lead_id, created_at),
        )
        db.execute(
            """
            INSERT INTO operator_actions (
                lead_id, operator_id, action, created_at
            )
            VALUES (?, 999999, 'reviewed', ?)
            """,
            (session.lead_id, created_at),
        )

    return {
        "lead_id": session.lead_id,
        "user_id": session.user_id,
        "session_json": session_json,
        "created_at": created_at,
    }


def _leads_columns(path: str) -> set[str]:
    with sqlite3.connect(path) as db:
        return {
            row[1]
            for row in db.execute("PRAGMA table_info(leads)")
        }


def test_reset_delivery_handlers_registered(
    monkeypatch,
    tmp_path,
):
    import importlib
    from types import SimpleNamespace

    from telegram.ext import (
        CallbackQueryHandler,
        CommandHandler,
    )

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:TEST")
    monkeypatch.setenv(
        "DATABASE_PATH",
        str(tmp_path / "handlers.db"),
    )

    import bot.main as bot_main

    bot_main = importlib.reload(bot_main)

    handlers = []
    application = SimpleNamespace(
        add_handler=lambda handler: handlers.append(handler),
        run_polling=lambda: None,
    )
    builder = SimpleNamespace(
        token=lambda *args, **kwargs: builder,
        concurrent_updates=lambda *args, **kwargs: builder,
        build=lambda: application,
    )
    monkeypatch.setattr(
        bot_main.Application,
        "builder",
        classmethod(lambda cls: builder),
    )

    bot_main.main()

    reset_commands = [
        handler
        for handler in handlers
        if isinstance(handler, CommandHandler)
        and "reset_delivery" in handler.commands
    ]
    reset_callbacks = [
        handler
        for handler in handlers
        if isinstance(handler, CallbackQueryHandler)
        and handler.pattern is not None
        and handler.pattern.pattern == r"^reset:"
    ]

    assert len(reset_commands) == 1
    assert (
        reset_commands[0].callback
        is bot_main.reset_delivery_command
    )
    assert len(reset_callbacks) == 1
    assert (
        reset_callbacks[0].callback
        is bot_main.reset_delivery_callback
    )


def test_legacy_db_migration_adds_delivery_cycle_id(tmp_path):
    path = str(tmp_path / "legacy.db")
    legacy = _create_legacy_sqlite_without_cycle_id(path)

    assert "delivery_cycle_id" not in _leads_columns(path)

    sessions = SessionRepository(path)
    leads = LeadRepository(path)

    assert "delivery_cycle_id" in _leads_columns(path)

    # Повторная инициализация безопасна.
    SessionRepository(path)
    LeadRepository(path)

    assert "delivery_cycle_id" in _leads_columns(path)

    # Существующие записи сохранены.
    with sqlite3.connect(path) as db:
        lead_row = db.execute(
            """
            SELECT lead_id, user_id, session_json,
                   delivery_status, created_at, delivery_cycle_id
            FROM leads
            WHERE lead_id = ?
            """,
            (legacy["lead_id"],),
        ).fetchone()
        session_row = db.execute(
            """
            SELECT session_json
            FROM sessions
            WHERE user_id = ?
            """,
            (legacy["user_id"],),
        ).fetchone()
        attempts = db.execute(
            """
            SELECT COUNT(*)
            FROM delivery_attempts
            WHERE lead_id = ?
            """,
            (legacy["lead_id"],),
        ).fetchone()[0]
        actions = db.execute(
            """
            SELECT COUNT(*)
            FROM operator_actions
            WHERE lead_id = ?
            """,
            (legacy["lead_id"],),
        ).fetchone()[0]

    assert lead_row is not None
    assert lead_row[0] == legacy["lead_id"]
    assert lead_row[1] == legacy["user_id"]
    assert lead_row[2] == legacy["session_json"]
    assert lead_row[3] == "sending"
    assert lead_row[4] == legacy["created_at"]
    assert lead_row[5] is None
    assert session_row is not None
    assert session_row[0] == legacy["session_json"]
    assert attempts == 1
    assert actions == 1

    restored = leads.get(legacy["lead_id"])
    active = sessions.get(legacy["user_id"])

    assert restored is not None
    assert restored.delivery_status == "sending"
    assert active is not None
    assert active.lead_id == legacy["lead_id"]


def test_legacy_db_confirmation_works_after_migration(tmp_path):
    path = str(tmp_path / "legacy_confirm.db")
    legacy = _create_legacy_sqlite_without_cycle_id(path)

    leads = LeadRepository(path)
    operator_id = 999999

    token = leads.create_delivery_reset_confirmation(
        legacy["lead_id"],
        operator_id,
    )
    assert token is not None

    with sqlite3.connect(path) as db:
        cycle_id = db.execute(
            """
            SELECT delivery_cycle_id
            FROM leads
            WHERE lead_id = ?
            """,
            (legacy["lead_id"],),
        ).fetchone()[0]

    assert cycle_id is not None

    assert leads.confirm_delivery_reset(token, operator_id) == "ok"

    saved = leads.get(legacy["lead_id"])
    assert saved is not None
    assert saved.delivery_status == "uncertain"

    with sqlite3.connect(path) as db:
        attempt = db.execute(
            """
            SELECT status
            FROM delivery_attempts
            WHERE lead_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (legacy["lead_id"],),
        ).fetchone()
        action = db.execute(
            """
            SELECT action
            FROM operator_actions
            WHERE lead_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (legacy["lead_id"],),
        ).fetchone()

    assert attempt == ("manually_reset",)
    assert action == ("manual_reset_delivery",)


