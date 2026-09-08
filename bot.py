"""
Telegram Lecture Reminder Bot
Асинхронный Telegram-бот с напоминаниями о лекциях.
"""

import asyncio
import json
import logging
import os
from datetime import date, datetime, time, timedelta
from html import escape
from pathlib import Path
from uuid import uuid4
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
    0: "Воскресенье",
    1: "Понедельник",
    2: "Вторник",
    3: "Среда",
    4: "Четверг",
    5: "Пятница",
    6: "Суббота",
}

# Не даёт двум командам одновременно перезаписывать schedule.json.
SAVE_LOCK = asyncio.Lock()


# ────────────────── State & I/O ────────────────────────────────

def atomic_write_schedule_sync(schedule: list[dict]) -> None:
    """Атомарно сохраняет расписание: сначала .tmp, затем os.replace()."""
    tmp_file = SCHEDULE_FILE.with_suffix(SCHEDULE_FILE.suffix + ".tmp")
    data = json.dumps(schedule, ensure_ascii=False, indent=2)

    try:
        with open(tmp_file, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())

        os.replace(tmp_file, SCHEDULE_FILE)
    except Exception:
        # Не оставляем мусорный временный файл после ошибки.
        try:
            tmp_file.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def ensure_lecture_ids(schedule: list[dict]) -> bool:
    """
    Добавляет UUID старым лекциям, созданным до обновления.

    Возвращает True, если данные были изменены и их надо сохранить.
    """
    changed = False

    for entry in schedule:
        for lecture in entry.get("lectures", []):
            if not lecture.get("id"):
                lecture["id"] = uuid4().hex
                changed = True

    return changed


def load_schedule_sync() -> list[dict]:
    """Синхронное чтение расписания при старте."""
    if not SCHEDULE_FILE.exists():
        logger.info("Файл не найден. Будет создан: %s", SCHEDULE_FILE)
        return []

    try:
        with open(SCHEDULE_FILE, "r", encoding="utf-8") as f:
            schedule = json.load(f)

        if not isinstance(schedule, list):
            raise ValueError("Корень schedule.json должен быть списком")

        # Миграция старого schedule.json: UUID добавятся автоматически.
        if ensure_lecture_ids(schedule):
            atomic_write_schedule_sync(schedule)
            logger.info("Старые записи schedule.json автоматически получили UUID")

        return schedule
    except Exception as e:
        logger.error("Ошибка чтения %s: %s", SCHEDULE_FILE, e)
        return []


async def save_schedule_async(app: Application) -> None:
    """Безопасное атомарное сохранение без блокировки event loop."""
    async with SAVE_LOCK:
        # Сериализуем снимок данных до ухода в отдельный поток.
        schedule = app.bot_data.get("schedule", [])
        snapshot = json.loads(json.dumps(schedule, ensure_ascii=False))

        try:
            await asyncio.to_thread(atomic_write_schedule_sync, snapshot)
        except Exception:
            logger.exception("Ошибка сохранения расписания")
            raise


def get_chat_entry(app: Application, chat_id: int) -> dict:
    """Получает запись чата из встроенного хранилища PTB."""
    schedule = app.bot_data.setdefault("schedule", [])

    for entry in schedule:
        if entry["chat_id"] == chat_id:
            return entry

    entry = {"chat_id": chat_id, "lectures": []}
    schedule.append(entry)
    return entry


# ──────────────────────── Week parity ──────────────────────────

def academic_week_number(target_date: date) -> int:
    """
    Возвращает номер учебной недели по правилу ИРНИТУ.

    Неделя, на которую приходится 1 сентября, считается первой
    (нечётной), затем недели чередуются. Дни августа, попавшие
    в эту же неделю, тоже относятся уже к первой учебной неделе.
    """
    september_first_this_year = date(target_date.year, 9, 1)
    first_week_monday_this_year = (
        september_first_this_year
        - timedelta(days=september_first_this_year.weekday())
    )

    if target_date >= first_week_monday_this_year:
        first_week_monday = first_week_monday_this_year
    else:
        september_first_previous_year = date(target_date.year - 1, 9, 1)
        first_week_monday = (
            september_first_previous_year
            - timedelta(days=september_first_previous_year.weekday())
        )

    return ((target_date - first_week_monday).days // 7) + 1


def is_even_academic_week(target_date: date) -> bool:
    """True для чётной учебной недели ИРНИТУ."""
    return academic_week_number(target_date) % 2 == 0


def ptb_weekday(target_datetime: datetime) -> int:
    """Переводит Python weekday (ПН=0) в формат PTB JobQueue (ВС=0)."""
    return (target_datetime.weekday() + 1) % 7


def get_lecture_date(now: datetime, lecture_day: int) -> date:
    """
    Определяет дату самой лекции, а не дату запуска напоминания.

    Это важно для лекций около полуночи: напоминание может уйти
    в воскресенье, а сама лекция быть уже в понедельник другой недели.
    """
    current_day = ptb_weekday(now)
    days_until_lecture = (lecture_day - current_day) % 7
    return now.date() + timedelta(days=days_until_lecture)


# ──────────────────────── Reminder math ────────────────────────

def calc_reminder_time_and_day(
    original_day: int,
    time_str: str,
) -> tuple[int, time, str]:
    """
    Вычисляет время и день напоминания.

    Например, для ПН 00:00 и напоминания за 15 минут получится
    ВС 23:45.
    """
    hour, minute = map(int, time_str.split(":", 1))
    total_mins = hour * 60 + minute - REMINDER_MINUTES

    if total_mins < 0:
        rem_day = (original_day - 1) % 7
        total_mins += 24 * 60
    else:
        rem_day = original_day

    rem_hour = total_mins // 60
    rem_min = total_mins % 60

    rem_time_obj = time(hour=rem_hour, minute=rem_min, tzinfo=TZ)
    rem_str = f"{rem_hour:02d}:{rem_min:02d}"
    return rem_day, rem_time_obj, rem_str


# ──────────────────────── Reminder job ─────────────────────────

async def send_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отправляет сообщение-напоминание."""
    data = context.job.data
    parity = data.get("parity", "all")

    if parity != "all":
        now = datetime.now(TZ)
        lecture_date = get_lecture_date(now, data["lecture_day"])
        is_even_week = is_even_academic_week(lecture_date)

        if (parity == "even" and not is_even_week) or (
            parity == "odd" and is_even_week
        ):
            return

    safe_name = escape(str(data["name"]))

    text = (
        "⏰ <b>Напоминание!</b>\n\n"
        f"Через {REMINDER_MINUTES} минут начнётся лекция:\n"
        f"📚 <b>{safe_name}</b>\n"
        f"🕐 Начало в {data['time']}"
    )

    await context.bot.send_message(
        chat_id=data["chat_id"],
        text=text,
        parse_mode="HTML",
    )


def schedule_lecture_job(app: Application, chat_id: int, lecture: dict) -> None:
    """Планирует одну задачу, используя постоянный UUID лекции."""
    lecture_id = lecture.get("id")
    if not lecture_id:
        lecture_id = uuid4().hex
        lecture["id"] = lecture_id

    rem_day, rem_time_obj, _ = calc_reminder_time_and_day(
        lecture["day"],
        lecture["time"],
    )

    job_name = f"lecture_{chat_id}_{lecture_id}"

    # Защита от дублирования одной и той же job.
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
            "lecture_id": lecture_id,
            "lecture_day": lecture["day"],
            "name": lecture["name"],
            "time": lecture["time"],
            "parity": lecture.get("parity", "all"),
        },
    )


# ──────────────────────── Bot handlers ─────────────────────────

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
        f"🌍 Часовой пояс: <b>{escape(TIMEZONE_NAME)}</b>"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or len(context.args) < 3:
        await update.message.reply_text(
            "❌ Формат: <code>/add ДЕНЬ ЧЧ:ММ [ЧЕТ/НЕЧЕТ] Название</code>",
            parse_mode="HTML",
        )
        return

    day_str = context.args[0].upper()
    time_str = context.args[1]

    if day_str not in DAYS_RU:
        await update.message.reply_text(
            f"❌ Неизвестный день. Допустимые: {', '.join(DAYS_RU.keys())}"
        )
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
        parity = (
            "even"
            if potential_parity == "ЧЕТ"
            else "odd"
            if potential_parity == "НЕЧЕТ"
            else "all"
        )
        name_start_idx = 3

    name = " ".join(context.args[name_start_idx:])
    if not name:
        await update.message.reply_text("❌ Укажите название лекции.")
        return

    chat_id = update.effective_chat.id
    lecture = {
        "id": uuid4().hex,
        "day": DAYS_RU[day_str],
        "time": time_formatted,
        "parity": parity,
        "name": name,
    }

    chat_entry = get_chat_entry(context.application, chat_id)
    chat_entry["lectures"].append(lecture)

    # UUID позволяет создать job один раз и больше не зависеть от индекса списка.
    schedule_lecture_job(context.application, chat_id, lecture)

    # Ждём завершения безопасной записи, а не запускаем бесконтрольную create_task().
    try:
        await save_schedule_async(context.application)
    except Exception:
        # Откатываем добавление, если сохранить данные не удалось.
        chat_entry["lectures"].remove(lecture)
        for job in context.application.job_queue.get_jobs_by_name(
            f"lecture_{chat_id}_{lecture['id']}"
        ):
            job.schedule_removal()

        await update.message.reply_text(
            "❌ Не удалось сохранить расписание. Лекция не добавлена."
        )
        return

    parity_text = (
        " (Чётная)"
        if parity == "even"
        else " (Нечётная)"
        if parity == "odd"
        else ""
    )

    rem_day, _, rem_str = calc_reminder_time_and_day(
        lecture["day"],
        time_formatted,
    )
    day_shift_msg = (
        "\n⚠️ <i>Напоминание перенесено на предыдущий день!</i>"
        if rem_day != lecture["day"]
        else ""
    )

    await update.message.reply_text(
        "✅ Лекция добавлена!\n\n"
        f"📚 <b>{escape(name)}</b>{parity_text}\n"
        f"📅 {DAYS_RU_FULL[DAYS_RU[day_str]]}\n"
        f"🕐 {time_formatted}\n"
        f"🔔 Напоминание в {rem_str}{day_shift_msg}",
        parse_mode="HTML",
    )


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or len(context.args) != 1:
        await update.message.reply_text(
            "❌ Формат: <code>/remove НОМЕР</code>\nНомера в /schedule",
            parse_mode="HTML",
        )
        return

    try:
        idx = int(context.args[0]) - 1
    except ValueError:
        await update.message.reply_text("❌ Номер должен быть числом.")
        return

    chat_id = update.effective_chat.id
    chat_entry = get_chat_entry(context.application, chat_id)

    if idx < 0 or idx >= len(chat_entry["lectures"]):
        await update.message.reply_text(
            f"❌ Лекции с номером <b>{idx + 1}</b> нет.",
            parse_mode="HTML",
        )
        return

    removed = chat_entry["lectures"].pop(idx)
    lecture_id = removed["id"]

    # Удаляем только job конкретной лекции — остальные не пересоздаются.
    jobs = context.application.job_queue.get_jobs_by_name(
        f"lecture_{chat_id}_{lecture_id}"
    )
    for job in jobs:
        job.schedule_removal()

    try:
        await save_schedule_async(context.application)
    except Exception:
        # При ошибке возвращаем лекцию на прежнее место и восстанавливаем job.
        chat_entry["lectures"].insert(idx, removed)
        schedule_lecture_job(context.application, chat_id, removed)

        await update.message.reply_text(
            "❌ Не удалось сохранить расписание. Лекция не удалена."
        )
        return

    p_str = (
        " [чётная]"
        if removed.get("parity") == "even"
        else " [нечётная]"
        if removed.get("parity") == "odd"
        else ""
    )

    await update.message.reply_text(
        f"🗑️ Удалено: <b>{escape(str(removed['name']))}</b>{p_str} "
        f"({DAYS_RU_FULL[removed['day']]} {removed['time']})",
        parse_mode="HTML",
    )


async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_entry = get_chat_entry(context.application, update.effective_chat.id)

    if not chat_entry["lectures"]:
        await update.message.reply_text(
            "📭 Расписание пусто. Добавь лекцию: "
            "<code>/add ПН 09:00 Математика</code>",
            parse_mode="HTML",
        )
        return

    # Оставляем прежнюю систему номеров /remove, чтобы не ломать поведение бота.
    indexed = sorted(
        enumerate(chat_entry["lectures"]),
        key=lambda x: (x[1]["day"], x[1]["time"]),
    )

    lines = ["📅 <b>Расписание лекций:</b>\n"]
    current_day = -1

    for orig_idx, lecture in indexed:
        if lecture["day"] != current_day:
            current_day = lecture["day"]
            lines.append(f"\n<b>{DAYS_RU_FULL[current_day]}:</b>")

        _, _, rem_str = calc_reminder_time_and_day(
            lecture["day"],
            lecture["time"],
        )

        p = lecture.get("parity", "all")
        p_str = (
            " <i>[чётная]</i>"
            if p == "even"
            else " <i>[нечётная]</i>"
            if p == "odd"
            else ""
        )

        safe_name = escape(str(lecture["name"]))
        lines.append(
            f" {orig_idx + 1}. 🕐 {lecture['time']}{p_str} — "
            f"{safe_name} <i>(🔔 {rem_str})</i>"
        )

    lines.append(f"\n🌍 Часовой пояс: {escape(TIMEZONE_NAME)}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ────────────────────────── Main ───────────────────────────────

def main() -> None:
    if not BOT_TOKEN or BOT_TOKEN == "your-telegram-bot-token-here":
        print("❌ BOT_TOKEN не задан! Проверь .env файл.")
        return

    app = Application.builder().token(BOT_TOKEN).build()
    app.bot_data["schedule"] = load_schedule_sync()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("schedule", cmd_schedule))

    count = 0
    for entry in app.bot_data["schedule"]:
        for lecture in entry.get("lectures", []):
            schedule_lecture_job(app, entry["chat_id"], lecture)
            count += 1

    logger.info("Успешно загружено и запланировано задач: %s", count)
    logger.info("🤖 Bot started! Timezone: %s", TIMEZONE_NAME)

    app.run_polling(allowed_updates=[Update.MESSAGE])


if __name__ == "__main__":
    main()
