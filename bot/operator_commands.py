import os
import logging

from uuid import UUID

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import ContextTypes
from telegram.error import TelegramError

from services.storage.lead_repository import LeadRepository
from services.workflow import LeadWorkflow

logger = logging.getLogger(__name__)

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


async def pending_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not await authorize(update):
        return

    repository = get_repository()
    pending = repository.list_pending(limit=20)

    if not pending:
        await update.effective_message.reply_text(
            "Заявок с неподтверждённой доставкой нет."
        )
        return

    lines = ["Заявки без подтверждённой доставки:"]

    for session in pending:
        lines.append(
            f"\n{session.lead_id}\n"
            f"{session.lead.name or 'Без имени'} — "
            f"{session.delivery_status}"
        )

    lines.append(
        "\nДля повтора: /retry UUID\n"
        "Повтор разрешён для not_sent и uncertain. "
        "Для sending сначала проверьте доставку."
    )

    await update.effective_message.reply_text(
        "\n".join(lines)
    )


async def retry_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not await authorize(update):
        return

    if len(context.args or []) != 1:
        await update.effective_message.reply_text(
            "Использование: /retry UUID"
        )
        return

    lead_id = resolve_lead_id(
        get_repository(),
        context.args[0],
    )

    if lead_id is None:
        await update.effective_message.reply_text(
            "Некорректный UUID."
        )
        return

    repository = get_repository()
    session = repository.get(lead_id)

    if session is None:
        await update.effective_message.reply_text(
            "Заявка не найдена."
        )
        return

    if not session.submitted:
        await update.effective_message.reply_text(
            "Заявка ещё не принята."
        )
        return

    if session.delivery_status == "sent":
        await update.effective_message.reply_text(
            "Доставка уже подтверждена. "
            "Повтор не требуется."
        )
        return

    if session.delivery_status == "sending":
        await update.effective_message.reply_text(
            "Заявка находится в состоянии sending. "
            "Сначала проверьте, было ли получено "
            "уведомление. Автоматический повтор "
            "заблокирован."
        )
        return

    # Атомарно резервируем заявку.
    try:
        claimed = repository.claim_retry(lead_id)
    except Exception:
        logger.exception(
            "Ошибка резервирования заявки %s", lead_id
        )
        await update.effective_message.reply_text(
            "Не удалось подготовить повторную отправку."
        )
        return

    if claimed is None:
        await update.effective_message.reply_text(
            "Статус заявки изменился. "
            "Проверьте /lead UUID."
        )
        return

    workflow = LeadWorkflow()
    notification = (
        "Повторное уведомление о заявке\n"
        f"ID: {claimed.lead_id}\n\n"
        + workflow.format_summary(claimed.lead)
    )

    try:
        sent = await context.bot.send_message(
            chat_id=int(
                os.environ["TELEGRAM_OPERATOR_CHAT_ID"]
            ),
            text=notification,
        )
    except (TelegramError, ValueError, KeyError) as exc:
        logger.warning(
            "Не удалось повторно отправить заявку %s: %s",
            lead_id,
            exc,
        )
        try:
            repository.complete_retry(
                claimed, "uncertain"
            )
        except Exception:
            logger.exception(
                "Не удалось сохранить ошибку повтора %s",
                lead_id,
            )

        await update.effective_message.reply_text(
            "Доставка не подтверждена. "
            "Заявка сохранена в реестре."
        )
        return
    except Exception:
        logger.exception(
            "Неожиданная ошибка отправки %s", lead_id
        )
        try:
            repository.complete_retry(
                claimed, "uncertain"
            )
        except Exception:
            logger.exception(
                "Не удалось сохранить состояние %s",
                lead_id,
            )
        await update.effective_message.reply_text(
            "Повторная отправка завершилась ошибкой. "
            "Проверьте /pending."
        )
        return

    try:
        repository.complete_retry(
            claimed,
            "sent",
            telegram_message_id=sent.message_id,
        )
    except Exception:
        logger.exception(
            "Telegram принял повтор, но результат "
            "не записан в SQLite: %s",
            lead_id,
        )
        await update.effective_message.reply_text(
            "Telegram принял уведомление, но сохранить "
            "подтверждение не удалось. "
            "Проверьте заявку вручную."
        )
        return

    try:
        repository.record_action(
            lead_id,
            update.effective_user.id,
            "retry_sent",
        )
    except Exception:
        logger.exception(
            "Не удалось записать действие оператора %s",
            lead_id,
        )

    await update.effective_message.reply_text(
        "Уведомление успешно отправлено.\n"
        f"ID: {lead_id}"
    )


async def mark_sent_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not await authorize(update):
        return

    if len(context.args or []) != 1:
        await update.effective_message.reply_text(
            "Использование: /mark_sent UUID"
        )
        return

    lead_id = resolve_lead_id(
        get_repository(),
        context.args[0],
    )

    if lead_id is None:
        await update.effective_message.reply_text(
            "Некорректный UUID."
        )
        return

    try:
        repository = get_repository()
        result = repository.mark_sent_manually(
            lead_id,
            update.effective_user.id,
        )
    except Exception:
        logger.exception(
            "Ошибка ручного подтверждения заявки %s",
            lead_id,
        )
        await update.effective_message.reply_text(
            "Не удалось сохранить подтверждение доставки."
        )
        return

    if not result:
        await update.effective_message.reply_text(
            "Заявка не найдена или её статус "
            "не позволяет ручное подтверждение. "
            "Проверьте /lead UUID."
        )
        return

    await update.effective_message.reply_text(
        "Доставка подтверждена оператором.\n"
        f"ID: {lead_id}"
    )


async def reset_delivery_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not await authorize(update):
        return

    if len(context.args or []) != 1:
        await update.effective_message.reply_text(
            "Использование: /reset_delivery UUID"
        )
        return

    repository = get_repository()
    lead_id = resolve_lead_id(
        repository,
        context.args[0],
    )

    if lead_id is None:
        await update.effective_message.reply_text(
            "Некорректный UUID."
        )
        return

    try:
        session = repository.get(lead_id)
    except Exception:
        logger.exception(
            "Ошибка чтения заявки для сброса %s",
            lead_id,
        )
        await update.effective_message.reply_text(
            "Не удалось получить заявку из базы данных."
        )
        return

    if (
        session is None
        or not session.submitted
        or session.delivery_status != "sending"
    ):
        await update.effective_message.reply_text(
            "Заявка не найдена или её статус "
            "не позволяет выполнить сброс.\n"
            "Допустимый статус: sending.\n"
            "Проверьте /lead UUID."
        )
        return

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Подтвердить сброс",
                    callback_data=f"reset:yes:{lead_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "Отмена",
                    callback_data=f"reset:no:{lead_id}",
                ),
            ],
        ]
    )

    await update.effective_message.reply_text(
        "⚠️ Подтверждение сброса доставки\n\n"
        f"ID: {lead_id}\n"
        "Текущий статус: sending\n\n"
        "Telegram мог уже доставить уведомление. "
        "Повторная отправка может создать дубликат.\n\n"
        "Подтвердить перевод заявки в uncertain?\n\n"
        "Повторная отправка выполняется отдельно "
        "командой /retry.",
        reply_markup=keyboard,
    )


async def reset_delivery_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    if query is None:
        return

    # Проверяем права при каждом нажатии кнопки.
    user = update.effective_user
    chat = update.effective_chat

    if (
        user is None
        or chat is None
        or chat.type != "private"
        or not is_operator(user.id)
    ):
        await query.answer(
            "Действие недоступно.",
            show_alert=True,
        )
        return

    data = query.data or ""
    parts = data.split(":")

    if len(parts) != 3 or parts[0] != "reset":
        await query.answer("Некорректная команда.")
        return

    action = parts[1]
    lead_id = resolve_lead_id(
        get_repository(),
        parts[2],
    )

    if action not in ("yes", "no") or lead_id is None:
        await query.answer("Некорректные данные.")
        return

    # Отмена не изменяет базу данных.
    if action == "no":
        await query.answer("Отменено.")
        await query.edit_message_text(
            "Сброс доставки отменён.\n"
            f"ID: {lead_id}"
        )
        return

    try:
        repository = get_repository()
        result = repository.reset_delivery_manually(
            lead_id,
            user.id,
        )
    except Exception:
        logger.exception(
            "Ошибка ручного сброса доставки %s",
            lead_id,
        )
        await query.answer(
            "Ошибка сохранения.",
            show_alert=True,
        )
        return

    if not result:
        await query.answer(
            "Статус заявки уже изменился.",
            show_alert=True,
        )
        await query.edit_message_text(
            "Сброс не выполнен.\n"
            "Заявка отсутствует или её статус "
            "уже не sending.\n"
            f"ID: {lead_id}"
        )
        return

    await query.answer("Сброс выполнен.")
    await query.edit_message_text(
        "Доставка переведена в uncertain.\n"
        f"ID: {lead_id}\n\n"
        "Проверьте возможную предыдущую доставку "
        "перед выполнением /retry."
    )


