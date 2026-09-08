# 🔔 Telegram Lecture Reminder Bot

Бот для групповых чатов Telegram — напоминает за **15 минут** до начала онлайн-лекций.

## Быстрый старт

### 1. Получите токен бота

Откройте [@BotFather](https://t.me/BotFather) в Telegram и создайте нового бота через `/newbot`.

### 2. Настройте окружение

```bash
# Установите зависимости и создайте env
cd /opt/
git clone https://github.com/nrfx/RemindingTG-Bot
cd /opt/RemindingTG-Bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Создайте .env из примера
cp .env.example .env
```

Откройте `.env` и вставьте свой токен:

```
BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrSTUvwxYZ
```
### (ОБЕСПЕЧИТЬ РАБОТУ БОТА ЧЕРЕЗ SYSTEMD) 

```bash
nano /etc/systemd/system/remindingtg-bot.service
```

```bash
[Unit]
Description=RemindingTG Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple

WorkingDirectory=/opt/RemindingTG-Bot

ExecStart=/opt/RemindingTG-Bot/venv/bin/python /opt/RemindingTG-Bot/bot.py

Restart=always
RestartSec=5

# Чтобы Python сразу писал логи без буферизации
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now remindingtg-bot
```


### 3. Запустите бота

```bash
python bot.py
```

## Команды

| Команда | Описание | Пример |
|---------|----------|--------|
| `/start` | Приветствие | — |
| `/add` | Добавить лекцию | `/add ПН 09:00 Математика` |
| `/remove` | Удалить лекцию | `/remove 1` |
| `/schedule` | Расписание | — |
| `/help` | Справка | — |

### Дни недели

`ПН` `ВТ` `СР` `ЧТ` `ПТ` `СБ` `ВС`

## Часовой пояс

По умолчанию: `Asia/Irkutsk` (UTC+8). Измените в `.env`:

```
TIMEZONE=Europe/Moscow
```
