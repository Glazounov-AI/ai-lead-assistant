from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

from uuid import uuid4

class ProjectLead(BaseModel):
    """Заявка на разработку AI-решения."""

    name: str | None = None
    contact: str | None = None
    company: str | None = None
    business_task: str | None = None

    solution_type: Literal[
        "чат-бот",
        "AI-ассистент",
        "автоматизация",
        "не определено",
    ] | None = None

    integrations: str | None = None
    timeline: str | None = None
    budget_range: str | None = None
    consent_to_contact: bool | None = None

    @property
    def is_complete(self) -> bool:
        """Минимум данных для передачи заявки оператору."""
        return all(
            [
                self.name and self.name.strip(),
                self.contact and self.contact.strip(),
                self.business_task and self.business_task.strip(),
                self.consent_to_contact is True,
            ]
        )


class LeadSession(BaseModel):
    """Состояние диалога с пользователем Telegram."""

    user_id: int
    chat_id: int
    lead_id: str = Field(default_factory=lambda: str(uuid4()))
    username: str | None = None
    first_name: str | None = None

    started_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    submitted: bool = False
    delivery_status: Literal[
        "not_sent",
        "sending",
        "sent",
        "uncertain",
    ] = "not_sent"
    awaiting_confirmation: bool = False
    confirmation_message_id: int | None = None
    
    lead: ProjectLead = Field(default_factory=ProjectLead)
    history: list[dict[str, str]] = Field(default_factory=list)


class AssistantTurn(BaseModel):
    """Структурированный ответ LLM."""

    reply: str = Field(min_length=1)
    extracted_lead: ProjectLead = Field(default_factory=ProjectLead)
    ready_to_submit: bool = False
