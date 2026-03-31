"""
Telegram Lecture Reminder Bot (Ultimate Async Edition)
Максимально оптимизированная версия без блокировок и багов.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, time
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
SCHEDULE_FILE = Path(__file__).resolve().parent / "schedule.json"
REMINDER_MINUTES = int(os.getenv("REMINDER_MINUTES", "15"))

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

DAYS_RU = {"ВС": 0, "ПН": 1, "ВТ": 2, "СР": 3, "ЧТ": 4, "ПТ": 5, "СБ": 6}
DAYS_RU_FULL = {
    0: "Воскресенье", 1: "Понедельник", 2: "Вторник", 3: "Среда",
    4: "Четверг", 5: "Пятница", 6: "Суббота",
}

# ────────────────── Async State & I/O ─────────────────────────

def load_schedule_sync() -> list[dict]:
    """Синхронное чтение при старте (O(1) I/O)."""
    if SCHEDULE_FILE.exists():
        try:
            with open(SCHEDULE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Ошибка чтения {SCHEDULE_FILE}: {e}")
    else:
        logger.info(f"Файл не найден. Будет создан: {SCHEDULE_FILE}")
    return []

async def save_schedule_async(app: Application) -> None:
    """Асинхронное сохранение на диск без блокировки Event Loop."""
    schedule = app.bot_data.get("schedule", [])
    
    def _write():
        try:
            with open(SCHEDULE_FILE, "w", encoding="utf-8") as f:
                json.dump(schedule, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Ошибка сохранения: {e}")

    await asyncio.to_thread(_write)

def get_chat_entry(app: Application, chat_id: int) -> dict:
    """Получает запись чата из встроенного хранилища PTB."""
    schedule = app.bot_data.setdefault("schedule", [])
    for entry in schedule:
        if entry["chat_id"] == chat_id:
            return entry
    entry = {"chat_id": chat_id, "lectures": []}
    schedule.append(entry)
    return entry

# ──────────────────────── Smart Math ──────────────────────────

def calc_reminder_time_and_day(original_day: int, time_str: str) -> tuple[int, time, str]:
    """
    Правильно вычисляет время и день напоминания.
    Исправляет баг: если пара в 00:00, напоминание сдвинется на предыдущий день в 23:45.
    """
    hour, minute = map(int, time_str.split(":", 1))
    total_mins = hour * 60 + minute - REMINDER_MINUTES
    
    if total_mins < 0:
        # Перенос на предыдущие сутки
        rem_day = (original_day - 1) % 7
        total_mins += 24 * 60
    else:
        rem_day = original_day
        
    rem_hour = total_mins // 60
    rem_min = total_mins % 60
    
    # PTB требует объект time с таймзоной для корректной работы
    rem_time_obj = time(hour=rem_hour, minute=rem_min, tzinfo=TZ)
    rem_str = f"{rem_hour:02d}:{rem_min:02d}"
    
    return rem_day, rem_time_obj, rem_str

# ──────────────────────── Reminder job ────────────────────────

async def send_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отправка сообщения-напоминания."""
    data = context.job.data
    parity = data.get("parity", "all")

    if parity != "all":
        # Номер недели по ISO
        current_week = datetime.now(TZ).isocalendar()[1]
        is_even_week = (current_week % 2 == 0)
        if (parity == "even" and not is_even_week) or (parity == "odd" and is_even_week):
            return

    text = (
        f"⏰ <b>Напоминание!</b>\n\n"
        f"Через {REMINDER_MINUTES} минут начнётся лекция:\n"
        f"📚 <b>{data['name']}</b>\n"
        f"🕐 Начало в {data['time']}"
    )
    await context.bot.send_message(chat_id=data["chat_id"], text=text, parse_mode="HTML")

def schedule_lecture_job(app: Application, chat_id: int, lecture: dict, lecture_id: int) -> None:
    """Планирует задачу с учетом возможного смещения дня недели."""
    rem_day, rem_time_obj, _ = calc_reminder_time_and_day(lecture["day"], lecture["time"])
    job_name = f"lecture_{chat_id}_{lecture_id}"

    for job in app.job_queue.get_jobs_by_name(job_name):
        job.schedule_removal()

    app.job_queue.run_daily(
        send_reminder,
        time=rem_time_obj,
        days=(rem_day,),
        chat_id=chat_id,
        name=job_name,
        data={
            "chat_id": chat_id,
            "name": lecture["name"],
            "time": lecture["time"],
            "parity": lecture.get("parity", "all"),
        },
    )

# ──────────────────────── Bot handlers ────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Привет! Я бот-напоминалка о лекциях.\n\n"
        "Добавь лекцию командой:\n"
        "<code>/add ПН 09:00 [ЧЕТ/НЕЧЕТ] Название лекции</code>\n\n"
        f"Я напомню за {REMINDER_MINUTES} минут до начала! 🔔\n"
        "Используй /help для списка команд.",
        parse_mode="HTML",
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "📖 <b>Команды бота:</b>\n\n"
        "<code>/add ДЕНЬ ЧЧ:ММ [ЧЕТ/НЕЧЕТ/ВСЕ] Название</code>\n"
        "  — Добавить лекцию\n\n"
        "<code>/remove НОМЕР</code>\n"
        "  — Удалить лекцию из /schedule\n\n"
        "<code>/schedule</code>\n"
        "  — Показать расписание\n\n"
        f"⏰ Напоминания за <b>{REMINDER_MINUTES} минут</b>.\n"
        f"🌍 Часовой пояс: <b>{TIMEZONE_NAME}</b>"
    )
    await update.message.reply_text(text, parse_mode="HTML")

async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or len(context.args) < 3:
        await update.message.reply_text("❌ Формат: <code>/add ДЕНЬ ЧЧ:ММ [ЧЕТ/НЕЧЕТ] Название</code>", parse_mode="HTML")
        return

    day_str = context.args[0].upper()
    time_str = context.args[1]

    if day_str not in DAYS_RU:
        await update.message.reply_text(f"❌ Неизвестный день. Допустимые: {', '.join(DAYS_RU.keys())}")
        return

    try:
        hour, minute = map(int, time_str.split(":", 1))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
        time_formatted = f"{hour:02d}:{minute:02d}"
    except ValueError:
        await update.message.reply_text("❌ Используйте формат времени ЧЧ:ММ.")
        return

    parity, name_start_idx = "all", 2
    potential_parity = context.args[2].upper()
    
    if potential_parity in ["ЧЕТ", "НЕЧЕТ", "ВСЕ"]:
        parity = "even" if potential_parity == "ЧЕТ" else "odd" if potential_parity == "НЕЧЕТ" else "all"
        name_start_idx = 3

    name = " ".join(context.args[name_start_idx:])
    if not name:
        await update.message.reply_text("❌ Укажите название лекции.")
        return

    chat_id = update.effective_chat.id
    lecture = {"day": DAYS_RU[day_str], "time": time_formatted, "parity": parity, "name": name}

    # Безопасное обновление данных и фоновое сохранение
    chat_entry = get_chat_entry(context.application, chat_id)
    chat_entry["lectures"].append(lecture)
    asyncio.create_task(save_schedule_async(context.application))

    schedule_lecture_job(context.application, chat_id, lecture, len(chat_entry["lectures"]) - 1)

    parity_text = " (Чётная)" if parity == "even" else " (Нечётная)" if parity == "odd" else ""
    rem_day, _, rem_str = calc_reminder_time_and_day(lecture["day"], time_formatted)
    day_shift_msg = "\n⚠️ <i>Напоминание перенесено на предыдущий день!</i>" if rem_day != lecture["day"] else ""

    await update.message.reply_text(
        f"✅ Лекция добавлена!\n\n"
        f"📚 <b>{name}</b>{parity_text}\n"
        f"📅 {DAYS_RU_FULL[DAYS_RU[day_str]]}\n"
        f"🕐 {time_formatted}\n"
        f"🔔 Напоминание в {rem_str}{day_shift_msg}",
        parse_mode="HTML",
    )

async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or len(context.args) != 1:
        await update.message.reply_text("❌ Формат: <code>/remove НОМЕР</code>\nНомера в /schedule", parse_mode="HTML")
        return

    try:
        idx = int(context.args[0]) - 1
    except ValueError:
        await update.message.reply_text("❌ Номер должен быть числом.")
        return

    chat_id = update.effective_chat.id
    chat_entry = get_chat_entry(context.application, chat_id)

    if idx < 0 or idx >= len(chat_entry["lectures"]):
        await update.message.reply_text(f"❌ Лекции с номером <b>{idx + 1}</b> нет.")
        return

    # Чистим старые джобы
    for i in range(len(chat_entry["lectures"])):
        for job in context.application.job_queue.get_jobs_by_name(f"lecture_{chat_id}_{i}"):
            job.schedule_removal()

    removed = chat_entry["lectures"].pop(idx)
    asyncio.create_task(save_schedule_async(context.application))

    # Пересобираем оставшиеся
    for i, lecture in enumerate(chat_entry["lectures"]):
        schedule_lecture_job(context.application, chat_id, lecture, i)

    p_str = " [чётная]" if removed.get("parity") == "even" else " [нечётная]" if removed.get("parity") == "odd" else ""
    await update.message.reply_text(
        f"🗑️ Удалено: <b>{removed['name']}</b>{p_str} ({DAYS_RU_FULL[removed['day']]} {removed['time']})",
        parse_mode="HTML"
    )

async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_entry = get_chat_entry(context.application, update.effective_chat.id)

    if not chat_entry["lectures"]:
        await update.message.reply_text("📭 Расписание пусто. Добавь лекцию: <code>/add ПН 09:00 Математика</code>", parse_mode="HTML")
        return

    indexed = sorted(enumerate(chat_entry["lectures"]), key=lambda x: (x[1]["day"], x[1]["time"]))
    lines = ["📅 <b>Расписание лекций:</b>\n"]
    
    current_day = -1
    for orig_idx, lecture in indexed:
        if lecture["day"] != current_day:
            current_day = lecture["day"]
            lines.append(f"\n<b>{DAYS_RU_FULL[current_day]}:</b>")
            
        _, _, rem_str = calc_reminder_time_and_day(lecture["day"], lecture["time"])
        p = lecture.get("parity", "all")
        p_str = " <i>[чётная]</i>" if p == "even" else " <i>[нечётная]</i>" if p == "odd" else ""

        lines.append(f"  {orig_idx + 1}. 🕐 {lecture['time']}{p_str} — {lecture['name']}  <i>(🔔 {rem_str})</i>")

    lines.append(f"\n🌍 Часовой пояс: {TIMEZONE_NAME}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

# ────────────────────────── Main ──────────────────────────────

def main() -> None:
    if not BOT_TOKEN or BOT_TOKEN == "your-telegram-bot-token-here":
        print("❌ BOT_TOKEN не задан! Проверь .env файл.")
        return

    app = Application.builder().token(BOT_TOKEN).build()
    
    # Загружаем расписание в безопасное хранилище PTB
    app.bot_data["schedule"] = load_schedule_sync()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("schedule", cmd_schedule))

    # Инициализация всех таймеров
    count = sum(
        1 for entry in app.bot_data["schedule"] 
        for idx, lecture in enumerate(entry["lectures"]) 
        if not schedule_lecture_job(app, entry["chat_id"], lecture, idx)
    )
    logger.info(f"Успешно загружено и запланировано задач: {count}")

    logger.info("🤖 Bot started! Timezone: %s", TIMEZONE_NAME)
    app.run_polling(allowed_updates=[Update.MESSAGE])

if __name__ == "__main__":
    main()
