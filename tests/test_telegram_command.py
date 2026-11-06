
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from core.schemas import LeadSession
from services.storage.session_repository import SessionRepository


def make_update():
    message = SimpleNamespace(reply_text=AsyncMock())

    update = SimpleNamespace(
        effective_user=SimpleNamespace(
            id=123,
            username="test_user",
            first_name="Test",
        ),
        effective_chat=SimpleNamespace(
            id=123,
            type="private",
        ),
        effective_message=message,
    )

    return update, message


def test_start_blocked_during_uncertain_delivery(
    monkeypatch, tmp_path
):
    from bot import main as bot_main

    repository = SessionRepository(
        str(tmp_path / "sessions.db")
    )
    monkeypatch.setattr(bot_main, "repository", repository)

    session = LeadSession(
        user_id=123,
        chat_id=123,
        delivery_status="uncertain",
    )
    repository.save(session)

    update, message = make_update()

    asyncio.run(bot_main.start(update, SimpleNamespace()))

    restored = repository.get(123)

    assert restored is not None
    assert restored.delivery_status == "uncertain"
    message.reply_text.assert_awaited_once()


def test_cancel_blocked_during_sending(
    monkeypatch, tmp_path
):
    from bot import main as bot_main

    repository = SessionRepository(
        str(tmp_path / "sessions.db")
    )
    monkeypatch.setattr(bot_main, "repository", repository)

    session = LeadSession(
        user_id=123,
        chat_id=123,
        delivery_status="sending",
    )
    repository.save(session)

    update, message = make_update()

    asyncio.run(bot_main.cancel(update, SimpleNamespace()))

    assert repository.get(123) is not None
    message.reply_text.assert_awaited_once()


