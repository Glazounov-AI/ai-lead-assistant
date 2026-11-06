
from core.schemas import LeadSession
from services.storage.session_repository import SessionRepository


def test_session_persistence(tmp_path):
    db_path = tmp_path / "sessions.db"

    repository = SessionRepository(str(db_path))

    session = LeadSession(
        user_id=123,
        chat_id=456,
        first_name="Алексей",
    )
    session.lead.business_task = "Разработка FAQ-бота"

    repository.save(session)

    # Создаём новый объект, как после перезапуска приложения.
    reopened = SessionRepository(str(db_path))
    restored = reopened.get(123)

    assert restored is not None
    assert restored.first_name == "Алексей"
    assert restored.lead.business_task == "Разработка FAQ-бота"


def test_missing_session(tmp_path):
    repository = SessionRepository(
        str(tmp_path / "sessions.db")
    )

    assert repository.get(999) is None


def test_delete_session(tmp_path):
    repository = SessionRepository(
        str(tmp_path / "sessions.db")
    )

    session = LeadSession(user_id=123, chat_id=456)
    repository.save(session)
    repository.delete(123)

    assert repository.get(123) is None


def test_confirmation_message_id_persists(tmp_path):
    db_path = tmp_path / "sessions.db"
    repository = SessionRepository(str(db_path))

    session = LeadSession(user_id=123, chat_id=123)
    session.awaiting_confirmation = True
    session.confirmation_message_id = 456

    repository.save(session)

    restored = SessionRepository(str(db_path)).get(123)

    assert restored is not None
    assert restored.awaiting_confirmation is True
    assert restored.confirmation_message_id == 456

