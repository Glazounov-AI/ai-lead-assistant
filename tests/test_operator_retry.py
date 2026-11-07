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

