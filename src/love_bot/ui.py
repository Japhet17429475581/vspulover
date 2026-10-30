from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from love_bot.service import REJECTION_REASONS

WELCOME = (
    "привет! ♡\n\n"
    "это предложка канала „люблю | ВГСПУ“.\n\n"
    "здесь можно анонимно поделиться тем, что тебе дорого: человеком, моментом, "
    "воспоминанием или просто маленькой радостью.\n\n"
    "напиши продолжение фразы „люблю…“ — и оно попадёт на проверку.\n\n"
    "например:\n„тебя“\n„когда отменяют пары“\n„нашу группу“\n\n"
    "авторство в канале не раскрывается."
)
PROMPT = "что ты любишь? ♡\n\nпросто напиши продолжение фразы. слово „люблю“ добавлять не нужно."
SENT = (
    "готово ♡\n\nтвоё предложение отправлено на проверку. если его одобрят, "
    "оно сможет появиться в канале.\n\nспасибо, что поделился."
)
APPROVED = "твоё предложение одобрено ♡"
REJECTED = "к сожалению, это предложение не прошло проверку. можешь попробовать отправить другое ♡"
ABOUT = (
    "напиши, проверь текст и отправь — дальше его посмотрят модераторы ♡\n\n"
    "после одобрения предложение может попасть в очередь публикации. точных сроков мы не обещаем.\n\n"
    "авторство в канале не раскрывается. для работы бота в базе хранится твой Telegram ID, "
    "связанный с предложениями. имя, username и телефон профиля мы не собираем. "
    "доступ к базе есть у её владельца. не отправляй чужие контакты и чувствительные сведения.\n\n"
    "предложения хранятся до 180 дней (срок может быть изменён владельцем), "
    "исходный текст — до 7 дней. /privacy — точные сроки этого бота. "
    "/delete_my_data — удалить все свои предложения, включая одобренные. "
    "сообщения в истории Telegram и уже опубликованные посты эта команда не удаляет; "
    "для удаления поста обратись к администратору канала."
)
STATUS = {
    "pending": "на проверке",
    "approved": "одобрено",
    "rejected": "отклонено",
    "published": "опубликовано",
}


def keyboard(*rows: tuple[tuple[str, str], ...]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=data) for label, data in row]
            for row in rows
        ]
    )


def home():
    return keyboard(
        (("♡ предложить", "u:propose"),),
        (("мои предложения", "u:mine"),),
        (("как это работает", "u:about"),),
    )


def cancel():
    return keyboard((("отмена", "u:cancel"),))


def confirmation(token: str):
    return keyboard(
        (("♡ отправить", f"u:send:{token}"),),
        (("изменить", f"u:edit:{token}"), ("отмена", "u:cancel")),
    )


def admin_home():
    return keyboard(
        (("📥 очередь", "a:q:pending:0"), ("✅ одобренные", "a:q:approved:0")),
        (("📊 статистика", "a:stats"), ("⚙️ настройки", "a:settings")),
        (("📤 публикации", "a:publications:0"),),
    )


def review_buttons(submission, job=None, publication_enabled=False):
    rows = []
    if submission.status == "pending":
        rows.append(
            (("✅ одобрить", f"a:ok:{submission.id}"), ("❌ отклонить", f"a:no:{submission.id}"))
        )
    if submission.status == "approved" and publication_enabled and job is None:
        rows.append((("📤 опубликовать", f"a:publish:{submission.id}"),))
    if job and job.status in {"failed", "uncertain"}:
        rows.append((("повторить публикацию", f"a:pub_retry:{submission.id}:{job.attempts}"),))
    queue_status = "approved" if submission.status == "approved" else "pending"
    rows.append((("⏭ следующее", f"a:q:{queue_status}:{submission.id}"),))
    rows.append((("📥 очередь", "a:q:pending:0"), ("панель", "a:home")))
    return keyboard(*rows)


def reject_buttons(submission_id: int):
    return keyboard(
        *(
            ((label, f"a:reject:{submission_id}:{key}"),)
            for key, label in REJECTION_REASONS.items()
        ),
        (("назад", f"a:card:{submission_id}"),),
    )


def moderation_card(submission, timezone: str, job=None) -> str:
    reasons = "; ".join(submission.moderation_reasons)
    date = submission.created_at.astimezone(ZoneInfo(timezone)).strftime("%d.%m.%Y %H:%M")
    text = (
        f"♡ предложение #{submission.id}\n\n{submission.normalized_text}\n\n"
        f"Статус: {STATUS[submission.status]}\nПричины: {reasons}\nДата: {date}"
    )
    if submission.rejection_reason:
        text += f"\nПричина отклонения: {submission.rejection_reason}"
    if job:
        labels = {
            "queued": "в очереди",
            "sending": "отправляется",
            "published": "опубликовано",
            "failed": "ошибка, нужен повтор",
            "uncertain": "результат неизвестен, проверь канал",
        }
        text += f"\nПубликация: {labels[job.status]}\nКанал: {job.channel_id}"
        if job.error_code:
            text += f"\nКод ошибки: {job.error_code}"
        if job.status == "uncertain":
            text += f"\nЕсли пост есть: /published {submission.id} MESSAGE_ID. Если поста нет — повтори публикацию."
    if submission.channel_message_id:
        text += f"\nMessage ID: {submission.channel_message_id}"
    return text


def my_submissions(submissions, timezone: str) -> str:
    if not submissions:
        return "здесь пока пусто ♡"
    rows = ["твои последние предложения ♡"]
    for submission in submissions:
        date = submission.created_at.astimezone(ZoneInfo(timezone)).strftime("%d.%m.%Y %H:%M")
        short = submission.normalized_text.replace("\n", " ")
        if len(short) > 100:
            short = short[:97] + "…"
        rows.append(f"{short}\n{date} · {STATUS[submission.status]}")
    return "\n\n".join(rows)
