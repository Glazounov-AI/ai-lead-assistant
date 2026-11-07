import asyncio
import logging
import os

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from core.schemas import LeadSession
from services.assistant.openai_assistant import (
    AssistantResponseError,
    OpenAIAssistant,
)
from services.storage.session_repository import SessionRepository
from services.workflow import LeadWorkflow

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler
from telegram.error import NetworkError, RetryAfter

from bot.operator_commands import (
    leads_command,
    lead_command,
    pending_command,
    retry_command,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

logging.getLogger("httpx").setLevel(logging.WARNING)

repository = SessionRepository(
    os.getenv("DATABASE_PATH", "data/sessions.db")
)
workflow = LeadWorkflow()

confirmation_locks: dict[int, asyncio.Lock] = {}

assistant = OpenAIAssistant(
    api_key=os.environ["OPENAI_API_KEY"],
    model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
    base_url=os.getenv(
        "OPENAI_BASE_URL",
        "https://api.openai.com/v1",
    ),
)


async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.effective_user
    chat = update.effective_chat

    if user is None or chat is None or chat.type != "private":
        return

    existing = repository.get(user.id)

    if existing is not None and not existing.submitted:
        if existing.delivery_status != "not_sent":
            await update.effective_message.reply_text(
                "Предыдущая заявка ещё обрабатывается. "
                "Попробуйте немного позже."
            )
            return

    session = LeadSession(
        user_id=user.id,
        chat_id=chat.id,
        username=user.username,
        first_name=user.first_name,
    )
    repository.save(session)

    await update.effective_message.reply_text(
        "Здравствуйте! Я помогу оформить заявку "
        "на разработку AI-решения.\n\n"
        "Расскажите, какую задачу вы хотите решить."
    )


async def cancel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.effective_user
    chat = update.effective_chat

    if user is None or chat is None or chat.type != "private":
        return

    existing = repository.get(user.id)

    if (
        existing is not None
        and existing.delivery_status != "not_sent"
        and not existing.submitted
    ):
        await update.effective_message.reply_text(
            "Заявка уже обрабатывается. "
            "Отменить её сейчас нельзя."
        )
        return
    
    repository.delete(user.id)

    await update.effective_message.reply_text(
        "Текущая заявка отменена. "
        "Чтобы начать заново, отправьте /start."
    )



async def confirm_lead(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    user = update.effective_user
    chat = update.effective_chat

    if (
        query is None
        or user is None
        or chat is None
        or chat.type != "private"
    ):
        return

    await query.answer()

    lock = confirmation_locks.setdefault(
        user.id, asyncio.Lock()
    )

    async with lock:
        session = repository.get(user.id)

        if session is None:
            await query.message.reply_text(
                "Заявка не найдена. Отправьте /start."
            )
            return

        if (
            query.message is None
            or session.confirmation_message_id is None
            or query.message.message_id
            != session.confirmation_message_id
        ):
            if query.message is not None:
                await query.message.reply_text(
                    "Это подтверждение устарело."
                )
            return

        if session.submitted:
            await query.message.reply_text(
                "Заявка уже принята."
            )
            return

        if session.delivery_status != "not_sent":
            await query.message.reply_text(
                "Заявка уже обрабатывается. "
                "Повторное подтверждение недоступно."
            )
            return

        if not session.awaiting_confirmation:
            await query.message.reply_text(
                "Подтверждение сейчас недоступно."
            )
            return

        if query.data == "lead_edit":
            session.awaiting_confirmation = False
            session.confirmation_message_id = None
            repository.save(session)

            await query.edit_message_reply_markup(
                reply_markup=None
            )
            await query.message.reply_text(
                "Напишите, какие сведения нужно исправить."
            )
            return

        if query.data != "lead_confirm":
            return

        session.lead.consent_to_contact = True

        if not workflow.confirm(session):
            session.lead.consent_to_contact = None
            repository.save(session)
            await query.message.reply_text(
                "Заявка заполнена не полностью."
            )
            return

        # Сначала сохраняем принятую заявку.
        session.submitted = True
        session.awaiting_confirmation = False

        try:
            repository.save_delivery(
                session, "accepted"
            )
        except Exception:
            logger.exception(
                "Не удалось сохранить принятую заявку"
            )
            await query.message.reply_text(
                "Не удалось сохранить заявку. "
                "Попробуйте подтвердить её ещё раз."
            )
            return

        # После успешного сохранения заявка принята.
        try:
            await query.edit_message_reply_markup(
                reply_markup=None
            )
        except Exception:
            logger.exception(
                "Не удалось убрать кнопки"
            )

        summary = workflow.format_summary(session.lead)
        operator_chat_id = int(
            os.environ["TELEGRAM_OPERATOR_CHAT_ID"]
        )

        notification = (
            "Новая заявка на AI-решение\n"
            f"ID: {session.lead_id}\n\n"
            + summary
        )

        # Подготовка доставки.
        session.delivery_status = "sending"

        try:
            repository.save_delivery(
                session, "sending"
            )
        except Exception:
            logger.exception(
                "Не удалось зарегистрировать отправку"
            )
            await query.message.reply_text(
                "Заявка сохранена и принята. "
                "Уведомление оператору пока не отправлено."
            )
            return

        sent_message = None
        last_error = None

        for attempt in range(2):
            if attempt == 1:
                try:
                    repository.save_delivery(
                        session, "retrying"
                    )
                except Exception:
                    logger.exception(
                        "Не удалось зарегистрировать повтор"
                    )
                    break

                await asyncio.sleep(1)

            try:
                sent_message = await context.bot.send_message(
                    chat_id=operator_chat_id,
                    text=notification,
                )
                break

            except RetryAfter as exc:
                last_error = exc
                logger.warning(
                    "Telegram ограничил частоту отправки: %s",
                    exc,
                )
                break

            except NetworkError as exc:
                last_error = exc
                logger.warning(
                    "Временная ошибка Telegram, попытка %s",
                    attempt + 1,
                    exc_info=True,
                )

            except Exception as exc:
                last_error = exc
                logger.exception(
                    "Ошибка отправки уведомления"
                )
                break

        if sent_message is not None:
            session.delivery_status = "sent"

            try:
                repository.save_delivery(
                    session,
                    "sent",
                    telegram_message_id=sent_message.message_id,
                )
            except Exception:
                logger.exception(
                    "Telegram принял сообщение, "
                    "но не удалось сохранить результат"
                )
                await query.message.reply_text(
                    "Заявка принята. "
                    "Результат уведомления оператора "
                    "пока не удалось зафиксировать."
                )
                return

            await query.message.reply_text(
                "Спасибо! Заявка принята и "
                "передана оператору. "
                "С вами свяжутся по указанному контакту."
            )
            return

        session.delivery_status = "uncertain"

        try:
            repository.save_delivery(
                session, "uncertain"
            )
        except Exception:
            logger.exception(
                "Не удалось сохранить ошибку доставки"
            )

        logger.warning(
            "Уведомление не подтверждено для заявки %s: %s",
            session.lead_id,
            type(last_error).__name__
            if last_error else "ошибка регистрации повтора",
        )

        await query.message.reply_text(
            "Ваша заявка принята и сохранена. "
            "Уведомление оператору пока не подтверждено. "
            "Повторно оформлять заявку не нужно."
        )


async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.effective_user
    chat = update.effective_chat
    message = update.effective_message

    if (
        user is None
        or chat is None
        or message is None
        or chat.type != "private"
        or not message.text
    ):
        return

    session = repository.get(user.id)

    if session is None:
        await message.reply_text(
            "Для начала работы отправьте /start."
        )
        return

    if session.submitted:
        await message.reply_text(
            "Ваша заявка уже принята. "
            "Для новой заявки отправьте /start."
        )
        return

    if session.awaiting_confirmation:
        await message.reply_text(
            "Заявка подготовлена, но пока не отправлена.\n"
            "Механизм подтверждения подключим "
            "на следующем этапе.\n\n"
            "Для новой заявки отправьте /start."
        )
        return

    try:
        turn = await asyncio.to_thread(
            assistant.generate,
            session,
            message.text,
        )
    except AssistantResponseError:
        logger.exception("Ошибка обработки запроса LLM")
        await message.reply_text(
            "Не удалось обработать сообщение. "
            "Попробуйте ещё раз немного позже."
        )
        return

    workflow.apply_turn(session, turn)

    session.history.extend(
        [
            {"role": "user", "content": message.text},
            {"role": "assistant", "content": turn.reply},
        ]
    )

    session.history = session.history[-20:]
    repository.save(session)

    await message.reply_text(turn.reply)

    if session.awaiting_confirmation:
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Отправить заявку",
                        callback_data="lead_confirm",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "Исправить данные",
                        callback_data="lead_edit",
                    )
                ],
            ]
        )

        confirmation_message = await message.reply_text(
            workflow.format_summary(session.lead)
            + "\n\nПроверьте сведения. "
            "Нажимая «Отправить заявку», "
            "вы соглашаетесь передать указанные данные "
            "для обратной связи.",
            reply_markup=keyboard,
        )

        session.confirmation_message_id = (
            confirmation_message.message_id
        )

        repository.save(session)



def main() -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]

    application = (
        Application.builder()
        .token(token)
        .concurrent_updates(False)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("cancel", cancel)
    )

    application.add_handler(
        CommandHandler("leads", leads_command)
    )

    application.add_handler(
        CommandHandler("lead", lead_command)
    )

    application.add_handler(
        CommandHandler("pending", pending_command)
    )

    application.add_handler(
        CommandHandler("retry", retry_command)
    )

    application.add_handler(
        CallbackQueryHandler(
            confirm_lead,
            pattern=r"^lead_(confirm|edit)$",
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_message,
        )
    )

    logger.info("Telegram-бот запускается")
    application.run_polling()


if __name__ == "__main__":
    try:
        main()
    finally:
        assistant.close()
