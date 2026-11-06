import os
from uuid import UUID

from telegram import Update
from telegram.ext import ContextTypes

from services.storage.lead_repository import LeadRepository
from services.workflow import LeadWorkflow


def is_operator(user_id: int) -> bool:
    """Единая точка проверки прав оператора."""
    configured = os.getenv("TELEGRAM_OPERATOR_CHAT_ID", "")

    try:
        return user_id == int(configured)
    except ValueError:
        return False


async def authorize(update: Update) -> bool:
    user = update.effective_user
    chat = update.effective_chat

    if (
        user is None
        or chat is None
        or chat.type != "private"
        or not is_operator(user.id)
    ):
        if update.effective_message:
            await update.effective_message.reply_text(
                "Команда недоступна."
            )
        return False

    return True


def get_repository() -> LeadRepository:
    return LeadRepository(
        os.getenv("DATABASE_PATH", "data/sessions.db")
    )


def resolve_lead_id(
    repository: LeadRepository,
    identifier: str,
) -> str | None:
    """Разрешаем только полный UUID, без неоднозначных сокращений."""
    try:
        return str(UUID(identifier))
    except ValueError:
        return None


async def leads_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not await authorize(update):
        return

    leads = get_repository().list_recent(10)

    if not leads:
        await update.effective_message.reply_text(
            "Заявок пока нет."
        )
        return

    lines = ["Последние заявки:"]

    for session in leads:
        lines.append(
            f"\n{session.lead_id}\n"
            f"{session.lead.name or 'Без имени'} — "
            f"{session.delivery_status}"
        )

    await update.effective_message.reply_text(
        "\n".join(lines)
    )


async def lead_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not await authorize(update):
        return

    if len(context.args) != 1:
        await update.effective_message.reply_text(
            "Использование: /lead UUID"
        )
        return

    repository = get_repository()
    lead_id = resolve_lead_id(repository, context.args[0])

    if lead_id is None:
        await update.effective_message.reply_text(
            "Некорректный UUID."
        )
        return

    session = repository.get(lead_id)

    if session is None:
        await update.effective_message.reply_text(
            "Заявка не найдена."
        )
        return

    workflow = LeadWorkflow()

    await update.effective_message.reply_text(
        f"ID: {session.lead_id}\n"
        f"Статус: {session.delivery_status}\n\n"
        f"{workflow.format_summary(session.lead)}"
    )

