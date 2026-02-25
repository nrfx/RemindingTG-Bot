"""
Telegram Lecture Reminder Bot
Отправляет напоминания за 15 минут до начала онлайн-лекций.
"""

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# ──────────────────────────── Config ────────────────────────────

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
TIMEZONE_NAME = os.getenv("TIMEZONE", "Asia/Irkutsk")
TZ = ZoneInfo(TIMEZONE_NAME)

# Использование resolve() гарантирует получение абсолютного пути к папке со скриптом.
# Это решает проблему сохранения файла в случайных директориях.
SCHEDULE_FILE = Path(__file__).resolve().parent / "schedule.json"
REMINDER_MINUTES = 15

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ──────────── Days mapping (RU ↔ weekday number) ────────────
# В планировщике python-telegram-bot неделя начинается с Воскресенья (0)
DAYS_RU = {
    "ВС": 0, "ПН": 1, "ВТ": 2, "СР": 3,
    "ЧТ": 4, "ПТ": 5, "СБ": 6,
}
DAYS_RU_FULL = {
    0: "Воскресенье", 1: "Понедельник", 2: "Вторник", 3: "Среда",
    4: "Четверг", 5: "Пятница", 6: "Суббота",
}
DAYS_RU_REV = {v: k for k, v in DAYS_RU.items()}

# ──────────────────── Schedule persistence ────────────────────

def load_schedule() -> list[dict]:
    """Load lecture schedule from JSON file."""
    if SCHEDULE_FILE.exists():
        try:
            with open(SCHEDULE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Ошибка при чтении файла {SCHEDULE_FILE}: {e}")
    else:
        logger.info(f"Файл расписания не найден. Будет создан новый: {SCHEDULE_FILE}")
    return []

def save_schedule(schedule: list[dict]) -> None:
    """Save lecture schedule to JSON file."""
    try:
        with open(SCHEDULE_FILE, "w", encoding="utf-8") as f:
            json.dump(schedule, f, ensure_ascii=False, indent=2)
        logger.info(f"Расписание успешно сохранено в {SCHEDULE_FILE}")
    except Exception as e:
        logger.error(f"Ошибка при сохранении расписания: {e}")

# ──────────────────────── Reminder job ────────────────────────

async def send_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Job callback — sends a reminder message to the chat."""
    data = context.job.data
    parity = data.get("parity", "all")

    # Проверка четности недели (isocalendar возвращает номер недели в году)
    if parity != "all":
        current_week = datetime.now(TZ).isocalendar()[1]
        is_even_week = (current_week % 2 == 0)
        
        # Пропускаем, если четность не совпадает
        if parity == "even" and not is_even_week:
            return
        if parity == "odd" and is_even_week:
            return

    text = (
        f"⏰ <b>Напоминание!</b>\n\n"
        f"Через {REMINDER_MINUTES} минут начнётся лекция:\n"
        f"📚 <b>{data['name']}</b>\n"
        f"🕐 Начало в {data['time']}"
    )
    await context.bot.send_message(
        chat_id=data["chat_id"],
        text=text,
        parse_mode="HTML",
    )

def schedule_lecture_job(
    app: Application,
    chat_id: int,
    lecture: dict,
    lecture_id: int,
) -> None:
    """Register a recurring weekly job for a single lecture."""
    hour, minute = map(int, lecture["time"].split(":"))

    reminder_dt = datetime.now(TZ).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    ) - timedelta(minutes=REMINDER_MINUTES)
    
    job_name = f"lecture_{chat_id}_{lecture_id}"

    # Удаляем старую задачу с таким же именем, если она есть
    current_jobs = app.job_queue.get_jobs_by_name(job_name)
    for job in current_jobs:
        job.schedule_removal()

    app.job_queue.run_daily(
        send_reminder,
        time=datetime.now(TZ).replace(
            hour=reminder_dt.hour,
            minute=reminder_dt.minute,
            second=0,
            microsecond=0,
        ).timetz(),
        days=(lecture["day"],),
        chat_id=chat_id,
        name=job_name,
        data={
            "chat_id": chat_id,
            "name": lecture["name"],
            "time": lecture["time"],
            "parity": lecture.get("parity", "all"),
        },
    )
    logger.info(
        "Запланировано напоминание: '%s' [%s] в %02d:%02d (День %d, Чат %d)",
        lecture["name"], lecture.get("parity", "all"), reminder_dt.hour, reminder_dt.minute,
        lecture["day"], chat_id,
    )

def schedule_all_jobs(app: Application) -> None:
    """Load schedule and register all jobs."""
    schedule = load_schedule()
    count = 0
    for entry in schedule:
        chat_id = entry["chat_id"]
        for idx, lecture in enumerate(entry["lectures"]):
            schedule_lecture_job(app, chat_id, lecture, idx)
            count += 1
    logger.info(f"Успешно загружено и запланировано задач: {count}")

# ──────────────── Helper: get/create chat entry ───────────────

def get_chat_entry(schedule: list[dict], chat_id: int) -> dict:
    """Find or create a schedule entry for a given chat."""
    for entry in schedule:
        if entry["chat_id"] == chat_id:
            return entry
    entry = {"chat_id": chat_id, "lectures": []}
    schedule.append(entry)
    return entry

# ──────────────────────── Bot handlers ────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command."""
    await update.message.reply_text(
        "👋 Привет! Я бот-напоминалка о лекциях.\n\n"
        "Добавь лекцию командой:\n"
        "<code>/add ПН 09:00 [ЧЕТ/НЕЧЕТ] Название лекции</code>\n\n"
        "Я напомню за 15 минут до начала! 🔔\n"
        "Используй /help для списка команд.",
        parse_mode="HTML",
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help command."""
    text = (
        "📖 <b>Команды бота:</b>\n\n"
        "<code>/add ДЕНЬ ЧЧ:ММ [ЧЕТ/НЕЧЕТ/ВСЕ] Название</code>\n"
        "  — Добавить лекцию (параметр недели не обязателен)\n"
        "  Дни: ПН, ВТ, СР, ЧТ, ПТ, СБ, ВС\n\n"
        "<code>/remove НОМЕР</code>\n"
        "  — Удалить лекцию по номеру из /schedule\n\n"
        "<code>/schedule</code>\n"
        "  — Показать расписание\n\n"
        "<code>/help</code>\n"
        "  — Эта справка\n\n"
        f"⏰ Напоминания приходят за <b>{REMINDER_MINUTES} минут</b> до лекции.\n"
        f"🌍 Часовой пояс: <b>{TIMEZONE_NAME}</b>"
    )
    await update.message.reply_text(text, parse_mode="HTML")

async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /add command."""
    if not context.args or len(context.args) < 3:
        await update.message.reply_text(
            "❌ Формат: <code>/add ДЕНЬ ЧЧ:ММ [ЧЕТ/НЕЧЕТ] Название</code>\n"
            "Примеры:\n"
            "<code>/add ПН 09:00 Математика</code>\n"
            "<code>/add ВТ 10:30 ЧЕТ Физика</code>",
            parse_mode="HTML",
        )
        return

    day_str = context.args[0].upper()
    time_str = context.args[1]

    if day_str not in DAYS_RU:
        await update.message.reply_text(
            f"❌ Неизвестный день: <b>{day_str}</b>\n"
            f"Допустимые: {', '.join(DAYS_RU.keys())}",
            parse_mode="HTML",
        )
        return

    try:
        parts = time_str.split(":")
        hour, minute = int(parts[0]), int(parts[1])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except (ValueError, IndexError):
        await update.message.reply_text(
            "❌ Неверный формат времени. Используйте <code>ЧЧ:ММ</code>, например <code>09:00</code>",
            parse_mode="HTML",
        )
        return

    parity = "all"
    name_start_idx = 2
    potential_parity = context.args[2].upper()
    
    if potential_parity in ["ЧЕТ", "НЕЧЕТ", "ВСЕ"]:
        if potential_parity == "ЧЕТ":
            parity = "even"
        elif potential_parity == "НЕЧЕТ":
            parity = "odd"
        name_start_idx = 3

    name = " ".join(context.args[name_start_idx:])
    
    if not name:
        await update.message.reply_text("❌ Укажите название лекции.", parse_mode="HTML")
        return

    chat_id = update.effective_chat.id
    lecture = {
        "day": DAYS_RU[day_str],
        "time": f"{hour:02d}:{minute:02d}",
        "parity": parity,
        "name": name,
    }

    schedule = load_schedule()
    chat_entry = get_chat_entry(schedule, chat_id)
    chat_entry["lectures"].append(lecture)
    save_schedule(schedule)

    lecture_idx = len(chat_entry["lectures"]) - 1
    schedule_lecture_job(context.application, chat_id, lecture, lecture_idx)

    parity_text = ""
    if parity == "even": parity_text = " (Чётная неделя)"
    elif parity == "odd": parity_text = " (Нечётная неделя)"

    await update.message.reply_text(
        f"✅ Лекция добавлена!\n\n"
        f"📚 <b>{name}</b>{parity_text}\n"
        f"📅 {DAYS_RU_FULL[DAYS_RU[day_str]]}\n"
        f"🕐 {hour:02d}:{minute:02d}\n"
        f"🔔 Напоминание в {(datetime(2000,1,1,hour,minute) - timedelta(minutes=REMINDER_MINUTES)).strftime('%H:%M')}",
        parse_mode="HTML",
    )

async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /remove command."""
    if not context.args or len(context.args) != 1:
        await update.message.reply_text(
            "❌ Формат: <code>/remove НОМЕР</code>\n"
            "Посмотри номера через /schedule",
            parse_mode="HTML",
        )
        return

    try:
        idx = int(context.args[0]) - 1
    except ValueError:
        await update.message.reply_text("❌ Номер должен быть числом.")
        return

    chat_id = update.effective_chat.id
    schedule = load_schedule()
    chat_entry = get_chat_entry(schedule, chat_id)

    if idx < 0 or idx >= len(chat_entry["lectures"]):
        await update.message.reply_text(
            f"❌ Лекции с номером <b>{idx + 1}</b> нет.\n"
            "Проверь /schedule",
            parse_mode="HTML",
        )
        return

    removed = chat_entry["lectures"].pop(idx)
    save_schedule(schedule)

    job_name = f"lecture_{chat_id}_{idx}"
    current_jobs = context.application.job_queue.get_jobs_by_name(job_name)
    for job in current_jobs:
        job.schedule_removal()

    for i, lecture in enumerate(chat_entry["lectures"]):
        schedule_lecture_job(context.application, chat_id, lecture, i)

    parity_str = ""
    if removed.get("parity") == "even": parity_str = " [чётная]"
    elif removed.get("parity") == "odd": parity_str = " [нечётная]"

    await update.message.reply_text(
        f"🗑️ Лекция удалена: <b>{removed['name']}</b>{parity_str} "
        f"({DAYS_RU_FULL[removed['day']]} {removed['time']})",
        parse_mode="HTML",
    )

async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /schedule command."""
    chat_id = update.effective_chat.id
    schedule = load_schedule()
    chat_entry = get_chat_entry(schedule, chat_id)

    if not chat_entry["lectures"]:
        await update.message.reply_text(
            "📭 Расписание пусто.\n"
            "Добавь лекцию: <code>/add ПН 09:00 Математика</code>",
            parse_mode="HTML",
        )
        return

    indexed = list(enumerate(chat_entry["lectures"]))
    indexed.sort(key=lambda x: (x[1]["day"], x[1]["time"]))

    lines = ["📅 <b>Расписание лекций:</b>\n"]
    current_day = -1
    for orig_idx, lecture in indexed:
        if lecture["day"] != current_day:
            current_day = lecture["day"]
            lines.append(f"\n<b>{DAYS_RU_FULL[current_day]}:</b>")
        reminder_time = (
            datetime(2000, 1, 1, *map(int, lecture["time"].split(":")))
            - timedelta(minutes=REMINDER_MINUTES)
        ).strftime("%H:%M")
        
        p = lecture.get("parity", "all")
        parity_str = ""
        if p == "even":
            parity_str = " <i>[чётная]</i>"
        elif p == "odd":
            parity_str = " <i>[нечётная]</i>"

        lines.append(
            f"  {orig_idx + 1}. 🕐 {lecture['time']}{parity_str} — {lecture['name']}  "
            f"<i>(🔔 {reminder_time})</i>"
        )

    lines.append(f"\n🌍 Часовой пояс: {TIMEZONE_NAME}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

# ────────────────────────── Main ──────────────────────────────

def main() -> None:
    """Start the bot."""
    if not BOT_TOKEN or BOT_TOKEN == "your-telegram-bot-token-here":
        print(
            "❌ BOT_TOKEN не задан!\n"
            "1. Получите токен у @BotFather в Telegram\n"
            "2. Скопируйте .env.example в .env\n"
            "3. Вставьте токен в .env"
        )
        return

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("schedule", cmd_schedule))

    schedule_all_jobs(app)

    logger.info("🤖 Bot started! Timezone: %s", TIMEZONE_NAME)
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
