import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from core.schemas import LeadSession, ProjectLead


def test_repeated_confirmation_sends_once(monkeypatch, tmp_path):
    from bot import main as bot_main
    from services.storage.session_repository import SessionRepository

    repository = SessionRepository(
        str(tmp_path / "sessions.db")
    )
    monkeypatch.setattr(bot_main, "repository", repository)
    monkeypatch.setenv("TELEGRAM_OPERATOR_CHAT_ID", "999999")

    bot_main.confirmation_locks.clear()

    session = LeadSession(
        user_id=123,
        chat_id=123,
        awaiting_confirmation=True,
        confirmation_message_id=456,
        lead=ProjectLead(
            name="Тестовый клиент",
            contact="@test_client",
            business_task="Автоматизация обработки заявок",
        ),
    )
    repository.save(session)

    message = SimpleNamespace(
        message_id=456,
        reply_text=AsyncMock(),
    )

    query = SimpleNamespace(
        data="lead_confirm",
        message=message,
        answer=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )

    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(type="private"),
    )

    bot = SimpleNamespace(
        send_message=AsyncMock()
    )
    context = SimpleNamespace(bot=bot)

    async def run_test():
        await bot_main.confirm_lead(update, context)
        await bot_main.confirm_lead(update, context)

    asyncio.run(run_test())

    assert bot.send_message.await_count == 1

    restored = repository.get(123)
    assert restored is not None
    assert restored.submitted is True
    assert restored.delivery_status == "sent"
    assert restored.awaiting_confirmation is False



def test_failed_delivery_keeps_lead_unsubmitted(
    monkeypatch, tmp_path
):
    from bot import main as bot_main
    from services.storage.session_repository import SessionRepository

    repository = SessionRepository(
        str(tmp_path / "sessions.db")
    )
    monkeypatch.setattr(bot_main, "repository", repository)
    monkeypatch.setenv("TELEGRAM_OPERATOR_CHAT_ID", "999999")

    bot_main.confirmation_locks.clear()

    session = LeadSession(
        user_id=123,
        chat_id=123,
        awaiting_confirmation=True,
        confirmation_message_id=456,
        lead=ProjectLead(
            name="Тестовый клиент",
            contact="@test_client",
            business_task="Автоматизация обработки заявок",
        ),
    )
    repository.save(session)

    message = SimpleNamespace(
        message_id=456,
        reply_text=AsyncMock(),
    )

    query = SimpleNamespace(
        data="lead_confirm",
        message=message,
        answer=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )

    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(type="private"),
    )

    bot = SimpleNamespace(
        send_message=AsyncMock(
            side_effect=RuntimeError("Telegram unavailable")
        )
    )
    context = SimpleNamespace(bot=bot)

    asyncio.run(
        bot_main.confirm_lead(update, context)
    )

    restored = repository.get(123)

    assert restored is not None
    assert restored.submitted is False
    assert restored.awaiting_confirmation is True
    assert restored.delivery_status == "uncertain"
    assert restored.lead.consent_to_contact is True

    assert bot.send_message.await_count == 1
    message.reply_text.assert_awaited_once_with(
        "Не удалось подтвердить доставку заявки. "
        "Повторная отправка заблокирована "
        "во избежание дубликатов. "
        "Свяжитесь с оператором."
    )



def test_delivery_state_transitions():
    from services.workflow import LeadWorkflow

    workflow = LeadWorkflow()

    session = LeadSession(
        user_id=123,
        chat_id=123,
        awaiting_confirmation=True,
        lead=ProjectLead(
            name="Клиент",
            contact="@client",
            business_task="Автоматизация заявок",
            consent_to_contact=True,
        ),
    )

    workflow.begin_delivery(session)
    assert session.delivery_status == "sending"

    workflow.delivery_uncertain(session)
    assert session.delivery_status == "uncertain"
    assert session.submitted is False


def test_delivery_success_state():
    from services.workflow import LeadWorkflow

    workflow = LeadWorkflow()

    session = LeadSession(
        user_id=123,
        chat_id=123,
        awaiting_confirmation=True,
        lead=ProjectLead(
            name="Клиент",
            contact="@client",
            business_task="Автоматизация заявок",
            consent_to_contact=True,
        ),
    )

    workflow.begin_delivery(session)
    workflow.delivery_succeeded(session)

    assert session.delivery_status == "sent"
    assert session.submitted is True
    assert session.awaiting_confirmation is False

