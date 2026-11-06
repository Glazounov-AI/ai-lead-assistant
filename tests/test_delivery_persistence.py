
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
    )

    session.delivery_status = "sending"
    sessions.save_delivery(session, "sending")

    assert sessions.get(123).delivery_status == "sending"
    assert leads.get(session.lead_id).delivery_status == "sending"

    session.delivery_status = "uncertain"
    sessions.save_delivery(session, "uncertain")

    assert sessions.get(123).delivery_status == "uncertain"
    assert leads.get(session.lead_id).delivery_status == "uncertain"

    with sqlite3.connect(path) as db:
        rows = db.execute("""
            SELECT status
            FROM delivery_attempts
            WHERE lead_id = ?
            ORDER BY id
        """, (session.lead_id,)).fetchall()

    assert rows == [
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
    )

    session.delivery_status = "sending"
    sessions.save_delivery(session, "sending")

    session.delivery_status = "sent"
    sessions.save_delivery(
        session,
        "sent",
        telegram_message_id=98765,
    )

    assert leads.get(session.lead_id).delivery_status == "sent"

    with sqlite3.connect(path) as db:
        row = db.execute("""
            SELECT telegram_message_id
            FROM delivery_attempts
            WHERE lead_id = ?
              AND status = 'sent'
        """, (session.lead_id,)).fetchone()

    assert row == (98765,)


