from core.schemas import AssistantTurn, LeadSession, ProjectLead


class LeadWorkflow:
    """Управление заполнением и подтверждением заявки."""


    def apply_turn(
        self,
        session: LeadSession,
        turn: AssistantTurn,
        ) -> LeadSession:
        if session.submitted:
            return session

        session.awaiting_confirmation = False

        updates = turn.extracted_lead.model_dump(
            exclude_none=True
        )

        # Согласие на передачу заявки не принимаем от LLM.
        # Его фиксируем отдельно при нажатии кнопки.
        updates.pop("consent_to_contact", None)

        for field, value in updates.items():
            if isinstance(value, str) and not value.strip():
                continue

            setattr(session.lead, field, value)

        # Для показа сводки достаточно имени, контакта
        # и описания задачи. Согласие будет получено
        # непосредственно перед отправкой.
        required = (
            session.lead.name,
            session.lead.contact,
            session.lead.business_task,
        )

        session.awaiting_confirmation = all(
            isinstance(value, str) and bool(value.strip())
            for value in required
        )

        return session

    def confirm(self, session: LeadSession) -> bool:
        """Подтверждение заявки пользователем."""
        if session.submitted:
            return False

        if not session.awaiting_confirmation:
            return False

        if not session.lead.is_complete:
            return False

        return True

    def mark_submitted(self, session: LeadSession) -> None:
        """Вызывается только после успешной отправки оператору."""
        if not self.confirm(session):
            raise ValueError(
                "Заявка не готова к отправке"
            )

        session.submitted = True
        session.awaiting_confirmation = False

    def format_summary(self, lead: ProjectLead) -> str:
        """Формирование заявки для подтверждения."""

        return (
            "Проверьте данные заявки:\n\n"
            f"Имя: {lead.name or '—'}\n"
            f"Контакт: {lead.contact or '—'}\n"
            f"Компания: {lead.company or '—'}\n"
            f"Задача: {lead.business_task or '—'}\n"
            f"Тип решения: {lead.solution_type or '—'}\n"
            f"Интеграции: {lead.integrations or '—'}\n"
            f"Сроки: {lead.timeline or '—'}\n"
            f"Бюджет: {lead.budget_range or '—'}"
        )

    def begin_delivery(self, session):
        if session.delivery_status != "not_sent":
            raise ValueError("Отправка уже начата")

        if not session.submitted or not session.lead.is_complete:
            raise ValueError("Заявка не принята")

        session.delivery_status = "sending"

    def delivery_uncertain(self, session: LeadSession) -> None:
        if session.delivery_status != "sending":
            raise ValueError("Некорректное состояние отправки")

        session.delivery_status = "uncertain"


    def delivery_succeeded(self, session):
        if session.delivery_status != "sending":
            raise ValueError("Некорректное состояние отправки")

        session.delivery_status = "sent"

