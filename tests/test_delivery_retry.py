import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram.error import NetworkError

from core.schemas import LeadSession, ProjectLead
from services.storage.lead_repository import LeadRepository
from services.storage.session_repository import SessionRepository


def make_case(monkeypatch, tmp_path, side_effect):
    from bot import main as bot_main

    path = str(tmp_path / "sessions.db")
    repository = SessionRepository(path)
    leads = LeadRepository(path)

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
            business_task="Автоматизация заявок",
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
        send_message=AsyncMock(side_effect=side_effect)
    )

    context = SimpleNamespace(bot=bot)
    return bot_main, repository, leads, session, update, context


def test_retry_after_network_error(monkeypatch, tmp_path):
    sent = SimpleNamespace(message_id=98765)

    case = make_case(
        monkeypatch,
        tmp_path,
        [NetworkError("temporary"), sent],
    )

    bot_main, repository, leads, session, update, context = case

    asyncio.run(bot_main.confirm_lead(update, context))

    assert context.bot.send_message.await_count == 2

    saved = repository.get(123)
    assert saved.submitted is True
    assert saved.delivery_status == "sent"

    registered = leads.get(session.lead_id)
    assert registered is not None
    assert registered.delivery_status == "sent"


def test_failed_delivery_keeps_accepted_lead(monkeypatch, tmp_path):
    case = make_case(
        monkeypatch,
        tmp_path,
        NetworkError("temporary"),
    )

    bot_main, repository, leads, session, update, context = case

    asyncio.run(bot_main.confirm_lead(update, context))

    assert context.bot.send_message.await_count == 2

    saved = repository.get(123)
    assert saved.submitted is True
    assert saved.delivery_status == "uncertain"

    registered = leads.get(session.lead_id)
    assert registered is not None
    assert registered.submitted is True
    assert registered.delivery_status == "uncertain"


def test_repeated_confirmation_does_not_resend(monkeypatch, tmp_path):
    sent = SimpleNamespace(message_id=98765)

    case = make_case(
        monkeypatch,
        tmp_path,
        sent,
    )

    bot_main, repository, leads, session, update, context = case

    async def run():
        await bot_main.confirm_lead(update, context)
        await bot_main.confirm_lead(update, context)

    asyncio.run(run())

    assert context.bot.send_message.await_count == 1
    assert repository.get(123).submitted is True
