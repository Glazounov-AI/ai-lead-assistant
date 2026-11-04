from pydantic import ValidationError
import pytest

from core.schemas import AssistantTurn, LeadSession, ProjectLead


def test_empty_lead_is_incomplete():
    lead = ProjectLead()
    assert lead.is_complete is False


def test_required_fields_without_consent():
    lead = ProjectLead(
        name="Алексей",
        contact="@alexey",
        business_task="Автоматизировать ответы клиентам",
        consent_to_contact=False,
    )
    assert lead.is_complete is False


def test_complete_lead():
    lead = ProjectLead(
        name="Алексей",
        contact="@alexey",
        business_task="Автоматизировать ответы клиентам",
        consent_to_contact=True,
    )
    assert lead.is_complete is True


def test_invalid_solution_type():
    with pytest.raises(ValidationError):
        ProjectLead(solution_type="неизвестный тип")


def test_session_defaults():
    session = LeadSession(user_id=123, chat_id=456)

    assert session.submitted is False
    assert session.awaiting_confirmation is False
    assert session.history == []
    assert session.lead.is_complete is False


def test_assistant_turn():
    turn = AssistantTurn(
        reply="Расскажите, какую задачу нужно решить."
    )

    assert turn.ready_to_submit is False
    assert turn.extracted_lead.is_complete is False
