import pytest

from core.schemas import AssistantTurn, LeadSession, ProjectLead
from services.workflow import LeadWorkflow


def test_partial_lead_not_ready():
    workflow = LeadWorkflow()
    session = LeadSession(user_id=1, chat_id=1)

    turn = AssistantTurn(
        reply="Уточните контакт.",
        extracted_lead=ProjectLead(
            name="Алексей",
            business_task="Нужен FAQ-бот",
        ),
        ready_to_submit=True,
    )

    workflow.apply_turn(session, turn)

    assert session.lead.name == "Алексей"
    assert session.awaiting_confirmation is False
    assert workflow.confirm(session) is False


def test_complete_lead_requires_confirmation():
    workflow = LeadWorkflow()
    session = LeadSession(user_id=1, chat_id=1)

    turn = AssistantTurn(
        reply="Проверьте заявку.",
        extracted_lead=ProjectLead(
            name="Алексей",
            contact="@alexey",
            business_task="Нужен FAQ-бот",
            consent_to_contact=True,
        ),
    )

    workflow.apply_turn(session, turn)

    assert session.awaiting_confirmation is True
    assert session.submitted is False
    assert session.lead.consent_to_contact is None
    assert workflow.confirm(session) is False


def test_mark_submitted():
    workflow = LeadWorkflow()
    session = LeadSession(user_id=1, chat_id=1)

    session.lead = ProjectLead(
        name="Алексей",
        contact="@alexey",
        business_task="Нужен FAQ-бот",
        consent_to_contact=True,
    )
    session.awaiting_confirmation = True

    workflow.mark_submitted(session)

    assert session.submitted is True
    assert session.awaiting_confirmation is False


def test_cannot_submit_incomplete_lead():
    workflow = LeadWorkflow()
    session = LeadSession(user_id=1, chat_id=1)

    with pytest.raises(ValueError):
        workflow.mark_submitted(session)


def test_empty_fields_do_not_overwrite():
    workflow = LeadWorkflow()
    session = LeadSession(user_id=1, chat_id=1)
    session.lead.name = "Алексей"

    turn = AssistantTurn(
        reply="Продолжаем.",
        extracted_lead=ProjectLead(name=""),
    )

    workflow.apply_turn(session, turn)

    assert session.lead.name == "Алексей"


def test_llm_cannot_grant_consent():
    workflow = LeadWorkflow()
    session = LeadSession(user_id=1, chat_id=1)

    turn = AssistantTurn(
        reply="Заявка подготовлена.",
        extracted_lead=ProjectLead(
            name="Алексей",
            contact="@alexey",
            business_task="Разработка FAQ-бота",
            consent_to_contact=True,
        ),
        ready_to_submit=True,
    )

    workflow.apply_turn(session, turn)

    # Данные собраны, сводку можно показать.
    assert session.awaiting_confirmation is True

    # Но согласие от LLM не принимается.
    assert session.lead.consent_to_contact is None

    # Отправка без подтверждения запрещена.
    assert workflow.confirm(session) is False

