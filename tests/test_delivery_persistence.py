import sqlite3

from core.schemas import LeadSession
from services.storage.lead_repository import LeadRepository
from services.storage.session_repository import SessionRepository


def test_delivery_state_persists_in_both_repositories(tmp_path):
    path = str(tmp_path / "sessions.db")

    sessions = SessionRepository(path)
    leads = LeadRepository(path)

    session = LeadSession(
        user_id=123,
        chat_id=123,
        submitted=True,
    )

    # Сначала заявка принимается и сохраняется.
    sessions.save_delivery(session, "accepted")

    assert sessions.get(123).submitted is True
    assert leads.get(session.lead_id).submitted is True

    # Начинается отправка уведомления.
    session.delivery_status = "sending"
    sessions.save_delivery(session, "sending")

    assert sessions.get(123).delivery_status == "sending"
    assert leads.get(session.lead_id).delivery_status == "sending"

    # Telegram не подтвердил доставку.
    session.delivery_status = "uncertain"
    sessions.save_delivery(session, "uncertain")

    restored = sessions.get(123)
    registered = leads.get(session.lead_id)

    assert restored.submitted is True
    assert registered.submitted is True
    assert restored.delivery_status == "uncertain"
    assert registered.delivery_status == "uncertain"

    with sqlite3.connect(path) as db:
        rows = db.execute("""
            SELECT status
            FROM delivery_attempts
            WHERE lead_id = ?
            ORDER BY id
        """, (session.lead_id,)).fetchall()

    assert rows == [
        ("accepted",),
        ("sending",),
        ("uncertain",),
    ]


def test_successful_delivery_stores_message_id(tmp_path):
    path = str(tmp_path / "sessions.db")

    sessions = SessionRepository(path)
    leads = LeadRepository(path)

    session = LeadSession(
        user_id=456,
        chat_id=456,
        submitted=True,
    )

    sessions.save_delivery(session, "accepted")

    session.delivery_status = "sending"
    sessions.save_delivery(session, "sending")

    session.delivery_status = "sent"
    sessions.save_delivery(
        session,
        "sent",
        telegram_message_id=98765,
    )

    registered = leads.get(session.lead_id)

    assert registered.submitted is True
    assert registered.delivery_status == "sent"

    with sqlite3.connect(path) as db:
        row = db.execute("""
            SELECT telegram_message_id
            FROM delivery_attempts
            WHERE lead_id = ?
              AND status = 'sent'
        """, (session.lead_id,)).fetchone()

    assert row == (98765,)

