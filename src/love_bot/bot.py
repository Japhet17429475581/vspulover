import logging
from uuid import UUID, uuid4

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, ErrorEvent, LinkPreviewOptions, Message

from love_bot import ui
from love_bot.publication import PublicationService
from love_bot.safety import (
    ExpiringMemoryStorage,
    PrivateAccessMiddleware,
    PrivateChatRequests,
    UserEventIsolation,
)
from love_bot.service import SubmissionError, SubmissionService
from love_bot.text_normalizer import TextValidationError

logger = logging.getLogger(__name__)


class Form(StatesGroup):
    writing = State()
    confirming = State()
    deleting = State()


def make_bot(settings, session=None):
    bot = Bot(
        token=settings.bot_token.get_secret_value(),
        session=session,
        default=DefaultBotProperties(parse_mode=None, link_preview_is_disabled=True),
    )
    bot.session.middleware(PrivateChatRequests(settings))
    return bot


async def edit(message: Message, text: str, markup=None):
    try:
        await message.edit_text(
            text, reply_markup=markup, link_preview_options=LinkPreviewOptions(is_disabled=True)
        )
    except TelegramBadRequest as exc:
        if "message is not modified" not in exc.message.lower():
            raise


def make_dispatcher(service: SubmissionService) -> Dispatcher:
    storage = ExpiringMemoryStorage(service.settings.draft_ttl_seconds)
    dp = Dispatcher(storage=storage, events_isolation=UserEventIsolation())
    router = Router(name="love_bot")
    access = PrivateAccessMiddleware(service)
    dp["private_access"] = access
    router.message.outer_middleware(access)
    router.callback_query.outer_middleware(access)
    publication = PublicationService(service)

    async def show_submission(message, submission):
        job = await publication.get(message.chat.id, submission.id)
        await edit(
            message,
            ui.moderation_card(submission, service.settings.display_timezone, job),
            ui.review_buttons(submission, job, service.settings.publication_enabled),
        )

    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext):
        await state.clear()
        await service.touch_user(message.from_user.id)
        await message.answer(ui.WELCOME, reply_markup=ui.home())

    @router.message(Command("cancel"))
    async def cancel_command(message: Message, state: FSMContext):
        await state.clear()
        await message.answer("отменено ♡", reply_markup=ui.home())

    @router.message(Command("privacy"))
    async def privacy(message: Message):
        s = service.settings
        await message.answer(
            "для очереди, истории и ограничения частоты бот хранит Telegram ID и твои тексты. "
            "модераторские карточки не содержат ID автора. владелец базы может видеть связь автора с текстом.\n\n"
            f"предложения и связанный аудит удаляются через {s.retention_days} дней; исходный текст — "
            f"через {s.original_text_retention_days} дней; технические уведомления — через "
            f"{s.technical_retention_days} дней. очистка запускается каждый час. "
            f"неотправленный черновик живёт в памяти до {s.draft_ttl_seconds // 60} минут.\n\n"
            "/delete_my_data удаляет все твои предложения, включая одобренные, аудит и уведомления из базы. "
            "сообщения в истории Telegram и резервные копии владельца базы удаляются отдельно. "
            "после удаления старые карточки модерации перестают работать. "
            "уже опубликованные посты остаются в канале; для их удаления обратись к его администратору."
        )

    @router.message(Command("delete_my_data"))
    async def ask_delete(message: Message, state: FSMContext):
        await state.clear()
        token = uuid4().hex
        await state.set_state(Form.deleting)
        await state.set_data({"delete_token": token})
        await message.answer(
            "удалить все твои предложения и данные из базы? одобренные тоже будут удалены. "
            "это действие нельзя отменить. история сообщений Telegram и посты в канале останутся; "
            "удаление поста можно запросить у администратора канала.",
            reply_markup=ui.keyboard(
                (("удалить мои данные", f"u:erase:{token}"),), (("отмена", "u:cancel"),)
            ),
        )

    @router.message(Command("admin"))
    async def admin(message: Message):
        if message.from_user.id not in service.settings.admin_ids:
            await message.answer("эта команда доступна только модераторам")
            return
        stats = await service.statistics(message.from_user.id)
        await message.answer(admin_summary(stats), reply_markup=ui.admin_home())

    @router.message(Command("published"))
    async def reconcile_published(message: Message):
        if message.from_user.id not in service.settings.admin_ids:
            await message.answer("эта команда доступна только модераторам")
            return
        parts = message.text.split()
        if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
            await message.answer("после проверки канала: /published НОМЕР_ПРЕДЛОЖЕНИЯ MESSAGE_ID")
            return
        try:
            changed = await publication.mark_published(
                message.from_user.id, int(parts[1]), int(parts[2])
            )
        except SubmissionError as exc:
            await message.answer(str(exc))
            return
        await message.answer(
            "публикация подтверждена ♡"
            if changed
            else "нет публикации с неизвестным результатом для сверки"
        )

    @router.callback_query(F.data == "u:cancel")
    async def cancel_callback(callback: CallbackQuery, state: FSMContext):
        await state.clear()
        await callback.answer()
        await edit(callback.message, "отменено ♡", ui.home())

    @router.callback_query(F.data == "u:propose")
    async def propose(callback: CallbackQuery, state: FSMContext):
        await state.clear()
        await state.set_state(Form.writing)
        await callback.answer()
        await callback.message.answer(ui.PROMPT, reply_markup=ui.cancel())

    @router.callback_query(F.data == "u:mine")
    async def mine(callback: CallbackQuery):
        # Scope comes exclusively from the authenticated Telegram sender, never callback data.
        submissions = await service.mine(callback.from_user.id)
        await callback.answer()
        await callback.message.answer(
            ui.my_submissions(submissions, service.settings.display_timezone),
            reply_markup=ui.home(),
        )

    @router.callback_query(F.data.in_({"u:about", "u:notify"}))
    async def about(callback: CallbackQuery):
        enabled = (
            await service.toggle_notifications(callback.from_user.id)
            if callback.data == "u:notify"
            else await service.notifications_enabled(callback.from_user.id)
        )
        await callback.answer()
        markup = ui.keyboard(
            (("уведомления: " + ("включены" if enabled else "выключены"), "u:notify"),),
            (("♡ предложить", "u:propose"),),
        )
        if callback.data == "u:notify":
            await edit(callback.message, ui.ABOUT, markup)
        else:
            await callback.message.answer(ui.ABOUT, reply_markup=markup)

    @router.callback_query(F.data.startswith("u:erase:"))
    async def erase(callback: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        if await state.get_state() != Form.deleting.state or callback.data[8:] != data.get(
            "delete_token"
        ):
            await callback.answer("это подтверждение уже не действует", show_alert=True)
            return
        await service.delete_user_data(callback.from_user.id)
        await state.clear()
        await callback.answer()
        await edit(callback.message, "твои данные и предложения удалены из базы ♡", ui.home())

    @router.callback_query(F.data.startswith("u:edit:"))
    async def change(callback: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        if await state.get_state() != Form.confirming.state or callback.data[7:] != data.get(
            "token"
        ):
            await callback.answer("этот черновик уже не действует ♡", show_alert=True)
            return
        await state.set_data({})
        await state.set_state(Form.writing)
        await callback.answer()
        await edit(callback.message, ui.PROMPT, ui.cancel())

    @router.message(Form.writing, F.text, ~F.text.startswith("/"))
    async def receive_text(message: Message, state: FSMContext):
        try:
            preview = service.preview(message.text)
        except (TextValidationError, SubmissionError) as exc:
            await message.answer(str(exc), reply_markup=ui.cancel())
            return
        token = uuid4().hex
        await state.set_data({"original": message.text, "text": preview.text, "token": token})
        await state.set_state(Form.confirming)
        await message.answer(
            f"вот как это будет выглядеть:\n\n{preview.text}\n\nвсё правильно?",
            reply_markup=ui.confirmation(token),
        )

    @router.callback_query(F.data.startswith("u:send:"))
    async def send(callback: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        if await state.get_state() != Form.confirming.state or callback.data[7:] != data.get(
            "token"
        ):
            await callback.answer("этот черновик уже отправлен или устарел ♡", show_alert=True)
            return
        try:
            await service.submit(
                callback.from_user.id, data["original"], data["text"], UUID(data["token"])
            )
        except (SubmissionError, TextValidationError) as exc:
            await callback.answer(str(exc), show_alert=True)
            return
        await state.clear()
        await callback.answer()
        await edit(callback.message, ui.SENT, ui.home())

    @router.callback_query(F.data.startswith("a:"))
    async def admin_action(callback: CallbackQuery):
        admin_id = callback.from_user.id
        service.require_admin(admin_id)
        parts = callback.data.split(":")
        action = parts[1] if len(parts) > 1 else ""
        if action in {"home", "stats"} and len(parts) == 2:
            stats = await service.statistics(admin_id)
            text = admin_summary(stats) if action == "home" else stats_text(stats)
            await callback.answer()
            await edit(callback.message, text, ui.admin_home())
            return
        if action == "settings" and len(parts) == 2:
            s = service.settings
            await callback.answer()
            await edit(
                callback.message,
                f"автоподтверждение: {'включено, по фильтрам запрещённых слов' if s.auto_approve_enabled else 'выключено'}\n"
                f"лимит: {s.max_submissions_per_hour} в час\nдлина: до {s.max_text_length} символов\n"
                f"повторы: окно {s.duplicate_window_hours} ч\nхранение: {s.retention_days} дней\n\n"
                "настройки задаются переменными окружения и применяются после перезапуска.\n"
                f"публикация: {'включена' if s.publication_enabled else 'выключена'}\n"
                f"канал: {s.channel_id or 'не задан'}\nпауза между постами: {s.publication_interval_seconds} сек.",
                ui.admin_home(),
            )
            return
        if action == "publications" and len(parts) == 3 and parts[2].isdigit():
            row = await publication.next(admin_id, int(parts[2]))
            await callback.answer()
            if row is None:
                await edit(
                    callback.message, "в очереди публикаций больше нет записей", ui.admin_home()
                )
            else:
                submission, job = row
                markup = ui.review_buttons(submission, job, service.settings.publication_enabled)
                markup.inline_keyboard.extend(
                    ui.keyboard(
                        (("следующая публикация", f"a:publications:{submission.id}"),)
                    ).inline_keyboard
                )
                await edit(
                    callback.message,
                    ui.moderation_card(submission, service.settings.display_timezone, job),
                    markup,
                )
            return
        if action == "publish" and len(parts) == 3 and parts[2].isdigit():
            try:
                changed = await publication.enqueue(admin_id, int(parts[2]))
            except SubmissionError as exc:
                await callback.answer(str(exc), show_alert=True)
                return
            await callback.answer(
                "добавлено в очередь ♡" if changed else "уже в очереди, опубликовано или удалено"
            )
            submission = await service.get_for_admin(admin_id, int(parts[2]))
            if submission:
                await show_submission(callback.message, submission)
            return
        if (
            action in {"pub_retry", "pub_retry_confirm"}
            and len(parts) == 4
            and parts[2].isdigit()
            and parts[3].isdigit()
        ):
            submission_id, attempts = int(parts[2]), int(parts[3])
            if action == "pub_retry":
                job = await publication.get(admin_id, submission_id)
                if (
                    job is None
                    or job.status not in {"failed", "uncertain"}
                    or job.attempts != attempts
                ):
                    await callback.answer("эта кнопка больше не действует", show_alert=True)
                    return
                await callback.answer()
                await edit(
                    callback.message,
                    f"проверь канал: если предложение #{submission_id} уже появилось, повтор создаст дубль. "
                    f"если поста нет, отправить его в канал {service.settings.channel_id}?",
                    ui.keyboard(
                        (
                            (
                                "поста нет — повторить",
                                f"a:pub_retry_confirm:{submission_id}:{attempts}",
                            ),
                        ),
                        (("назад", f"a:card:{submission_id}"),),
                    ),
                )
                return
            try:
                changed = await publication.retry(admin_id, submission_id, attempts)
            except SubmissionError as exc:
                await callback.answer(str(exc), show_alert=True)
                return
            await callback.answer(
                "добавлено в очередь ♡" if changed else "уже обработано или удалено"
            )
            submission = await service.get_for_admin(admin_id, submission_id)
            if submission:
                await show_submission(callback.message, submission)
            return
        if (
            action == "q"
            and len(parts) == 4
            and parts[2] in {"pending", "approved"}
            and parts[3].isdigit()
        ):
            submission = await service.queue(admin_id, parts[2], int(parts[3]))
            await callback.answer()
            if submission is None:
                await edit(
                    callback.message, "в этой очереди больше нет предложений ♡", ui.admin_home()
                )
            else:
                await show_submission(callback.message, submission)
            return
        if action in {"ok", "no", "reject", "card"} and len(parts) >= 3 and parts[2].isdigit():
            submission_id = int(parts[2])
            if action == "no" and len(parts) == 3:
                submission = await service.get_for_admin(admin_id, submission_id)
                await callback.answer()
                if submission and submission.status == "pending":
                    await edit(
                        callback.message,
                        f"причина отклонения предложения #{submission_id}",
                        ui.reject_buttons(submission_id),
                    )
                else:
                    await edit(
                        callback.message, "предложение уже обработано или удалено", ui.admin_home()
                    )
                return
            if action in {"ok", "reject"}:
                if action == "reject" and (len(parts) != 4 or parts[3] not in ui.REJECTION_REASONS):
                    await callback.answer("неизвестная причина", show_alert=True)
                    return
                changed = await service.review(
                    admin_id,
                    submission_id,
                    "approved" if action == "ok" else "rejected",
                    parts[3] if action == "reject" else None,
                )
                await callback.answer(
                    "готово ♡" if changed else "предложение уже обработано или удалено"
                )
            else:
                await callback.answer()
            submission = await service.get_for_admin(admin_id, submission_id)
            if submission:
                await show_submission(callback.message, submission)
            else:
                await edit(callback.message, "предложение удалено", ui.admin_home())
            return
        await callback.answer("эта кнопка больше не действует", show_alert=True)

    @router.callback_query()
    async def unknown_callback(callback: CallbackQuery):
        await callback.answer("эта кнопка больше не действует ♡")

    @router.message()
    async def fallback(message: Message, state: FSMContext):
        current = await state.get_state()
        if current == Form.writing.state:
            await message.answer("пришли предложение обычным текстом ♡", reply_markup=ui.cancel())
        elif current == Form.confirming.state:
            await message.answer("проверь предпросмотр выше и нажми «отправить» или «изменить» ♡")
        else:
            await message.answer("хочешь поделиться чем-то тёплым? ♡", reply_markup=ui.home())

    @router.error()
    async def handle_error(event: ErrorEvent):
        logger.error("update_failed", extra={"error_type": type(event.exception).__name__})
        callback = event.update.callback_query
        message = event.update.message
        try:
            if callback:
                await callback.answer(
                    "не получилось выполнить действие. попробуй ещё раз ♡", show_alert=True
                )
            elif message and message.chat.type == "private":
                await message.answer("не получилось выполнить действие. попробуй ещё раз ♡")
        except Exception:
            logger.warning("error_reply_failed")
        return True

    dp.include_router(router)
    return dp


def admin_summary(stats):
    return (
        f"на проверке — {stats['pending']}\nодобрено — {stats['approved']}\n"
        f"отклонено — {stats['rejected']}\nза сегодня — {stats['today']}"
        f"\nопубликовано — {stats['published']}"
    )


def stats_text(stats):
    return (
        f"пользователей — {stats['users']}\nпредложений — {stats['total']}\n"
        f"за 24 часа — {stats['day']}\n{admin_summary(stats)}\n"
        f"автоматически обработано — {stats['automatic_share']:.1%}\n"
        f"авторов за 7 дней — {stats['authors_7']}\nавторов за 30 дней — {stats['authors_30']}\n\n"
        "показатели учитывают записи, оставшиеся после удаления данных и очистки."
    )
