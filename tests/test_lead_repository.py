import sqlite3

from core.schemas import LeadSession
from services.storage.lead_repository import LeadRepository


def test_lead_persists(tmp_path):
    path = str(tmp_path / "leads.db")
    repository = LeadRepository(path)

    session = LeadSession(
        user_id=123,
        chat_id=123,
    )
    session.lead.name = "Тестовый клиент"

    repository.save(session)

    restored = LeadRepository(path).get(session.lead_id)

    assert restored is not None
    assert restored.lead_id == session.lead_id
    assert restored.lead.name == "Тестовый клиент"


def test_multiple_leads_per_user(tmp_path):
    repository = LeadRepository(str(tmp_path / "leads.db"))

    first = LeadSession(user_id=123, chat_id=123)
    second = LeadSession(user_id=123, chat_id=123)

    repository.save(first)
    repository.save(second)

    assert first.lead_id != second.lead_id
    assert len(repository.list_recent()) == 2


def test_delivery_attempt_and_action_logged(tmp_path):
    path = str(tmp_path / "leads.db")
    repository = LeadRepository(path)

    session = LeadSession(user_id=123, chat_id=123)
    repository.save(session)

    repository.record_attempt(
        session.lead_id,
        "sending",
    )
    repository.record_action(
        session.lead_id,
        999,
        "reviewed",
    )

    with sqlite3.connect(path) as db:
        attempts = db.execute(
            "SELECT COUNT(*) FROM delivery_attempts"
        ).fetchone()[0]

        actions = db.execute(
            "SELECT COUNT(*) FROM operator_actions"
        ).fetchone()[0]

    assert attempts == 1
    assert actions == 1


def test_unauthorized_operator(monkeypatch):
    from bot.operator_commands import is_operator

    monkeypatch.setenv("TELEGRAM_OPERATOR_CHAT_ID", "999")

    assert is_operator(999)
    assert not is_operator(123)


