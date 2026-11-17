
import asyncio
import logging
import os
import random
import time
from collections import defaultdict, deque
from decimal import Decimal, InvalidOperation

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from dotenv import load_dotenv
from openai import AsyncOpenAI

from db import (
    init_db, get_session, create_session, close_session, add_player,
    find_player, list_players, add_operation, cancel_operation,
    update_operation, get_player_stats, get_total_stats, get_history,
    get_session_player_stats, get_session_players
)
from calculations import calculate_settlement, money


# ==================================================
# НАСТРОЙКИ
# ==================================================

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
BOT_USERNAME = os.getenv("BOT_USERNAME", "").lstrip("@").lower()

if not TOKEN:
    raise RuntimeError("Не указан BOT_TOKEN в .env")

if not OPENAI_API_KEY:
    raise RuntimeError("Не указан OPENAI_API_KEY в .env")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(TOKEN)
dp = Dispatcher()
ai = AsyncOpenAI(api_key=OPENAI_API_KEY)

# Память переписки: последние сообщения каждой группы
chat_memory = defaultdict(lambda: deque(maxlen=24))

# Ограничение частоты ответов
last_reply = defaultdict(float)
REPLY_COOLDOWN = 40

# Защита от одновременных запросов в одной группе
chat_locks = defaultdict(asyncio.Lock)

# Вероятность вступить в обычный разговор
QUESTION_PROBABILITY = 0.15


# ==================================================
# ХАРАКТЕР ИНТЕЛЛЕКТА
# ==================================================

SYSTEM_PROMPT = """
Ты — девятый участник дружеской покерной компании.

ТЫ СВОЙ ПАРЕНЬ, А НЕ ОФИЦИАЛЬНЫЙ БОТ.

ТВОЙ ХАРАКТЕР:
- Разговаривай на русском языке.
- Общайся максимально по-свойски.
- Используй мат, когда он уместен.
- Люби дружеские подколы, сарказм и чёрный юмор.
- Можешь жёстко подкалывать игроков за покерные ошибки.
- Не будь занудой и не пиши длинные ответы без просьбы.
- Умей поддержать серьёзный разговор.
- Не пытайся шутить в каждом сообщении.

ТВОЯ РОЛЬ:
- Участвуй в разговорах компании.
- Обсуждай покер, игры, спорт, деньги и обычную жизнь.
- Разбирай покерные раздачи.
- Отвечай на вопросы о статистике.
- Можешь спорить с участниками и высказывать мнение.
- Не выдумывай факты и результаты.

СТАТИСТИКА:
Тебе будет передаваться актуальная статистика из базы данных.
Используй её как источник достоверных результатов.
Не придумывай отсутствующие цифры.

САМОСТОЯТЕЛЬНОЕ УЧАСТИЕ:
Не нужно отвечать на каждое сообщение.
Если разговор неинтересный, ответ будет лишним или тебе нечего добавить,
верни строго [SKIP].
Если есть что сказать — отвечай естественно, как участник компании.

ВАЖНО:
Не подталкивай людей отыгрываться, занимать деньги или увеличивать ставки.
Не унижай людей по-настоящему.
Не выдавай предположения за факты.
"""


# ==================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==================================================

def parse_amount(raw: str) -> int:
    cleaned = raw.replace(" ", "").replace(",", ".")

    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        raise ValueError("Сумма должна быть числом.")

    if value <= 0 or value != value.to_integral_value():
        raise ValueError("Сумма должна быть положительным целым числом.")

    return int(value)


def args_after_command(message: Message) -> str:
    return (message.text or "").partition(" ")[2].strip()


def get_stats_context(chat_id: int) -> str:
    rows = get_total_stats(chat_id)

    if not rows:
        return "Завершённых игр пока нет."

    lines = []

    for row in rows:
        result = row["exits"] - row["buys"]
        sign = "+" if result > 0 else ""

        lines.append(
            f"{row['name']}: {sign}{money(result)} ₽; "
            f"игр: {row['games']}"
        )

    session = get_session(chat_id)

    if session:
        lines.append(f"\nТекущая игра №{session['id']}:")

        for row in get_session_players(session["id"]):
            result = row["exits"] - row["buys"]

            lines.append(
                f"{row['name']}: докупы {money(row['buys'])} ₽, "
                f"выходы {money(row['exits'])} ₽, "
                f"результат {money(result)} ₽"
            )

    return "\n".join(lines)


def is_direct_message_to_bot(message: Message) -> bool:
    text = (message.text or "").lower()

    if BOT_USERNAME and f"@{BOT_USERNAME}" in text:
        return True

    if message.reply_to_message:
        replied = message.reply_to_message.from_user

        if replied and replied.id == bot.id:
            return True

    return False


def is_interesting_message(message: Message) -> bool:
    text = (message.text or "").strip().lower()

    if not text or text.startswith("/"):
        return False

    keywords = [
        "покер", "карты", "флоп", "терн", "ривер",
        "префлоп", "олл-ин", "олин", "блеф",
        "колл", "рейз", "фолд", "банк", "раздача",
        "докуп", "выход", "проиграл", "выиграл",
        "лудоман", "ммр", "кто кому должен",
        "нияз", "марат", "влад", "файиль",
        "фaиль", "эльмир", "дживан", "юныс", "айдар"
    ]

    if any(word in text for word in keywords):
        return True

    if "?" in text and len(text) >= 20:
        return random.random() < QUESTION_PROBABILITY

    return False


# ==================================================
# OPENAI
# ==================================================

async def ask_ai(message: Message):
    chat_id = message.chat.id
    username = (
        message.from_user.full_name
        if message.from_user
        else "Участник"
    )

    text = message.text or ""

    async with chat_locks[chat_id]:

        history = list(chat_memory[chat_id])

        messages = [
            {
                "role": "system",
                "content": SYSTEM_PROMPT
            },
            {
                "role": "system",
                "content": (
                    "Актуальная статистика компании:\n"
                    + get_stats_context(chat_id)
                )
            }
        ]

        messages.extend(history)

        messages.append({
            "role": "user",
            "content": f"{username}: {text}"
        })

        response = await ai.chat.completions.create(
            model=OPENAI_MODEL,
            messages=messages,
            temperature=0.9,
            max_tokens=350
        )

        answer = response.choices[0].message.content

        if not answer:
            return

        answer = answer.strip()

        # Запоминаем разговор даже тогда, когда бот решил промолчать
        chat_memory[chat_id].append({
            "role": "user",
            "content": f"{username}: {text}"
        })

        if answer == "[SKIP]":
            return

        chat_memory[chat_id].append({
            "role": "assistant",
            "content": answer
        })

        # Telegram ограничивает длину сообщения
        if len(answer) > 4000:
            answer = answer[:3900] + "..."

        await message.answer(
            answer,
            reply_to_message_id=message.message_id
        )


# ==================================================
# КОМАНДЫ TELEGRAM
# ==================================================

@dp.message(Command("start"))
async def start(message: Message):
    await message.answer(
        "Здарова! Я Poker Manager 😈\n\n"
        "Регистрация: /join Имя\n"
        "Начать игру: /start_poker\n"
        "Докуп: /buy 3000 Имя\n"
        "Выход: /exit 10000 Имя\n"
        "Статистика: /stats\n"
        "Завершить игру: /end_poker\n"
        "Полный список: /help"
    )


@dp.message(Command("help"))
async def help_cmd(message: Message):
    await message.answer(
        "КОМАНДЫ POKER MANAGER\n\n"
        "/join Имя — регистрация\n"
        "/players — список игроков\n"
        "/start_poker — начать игру\n"
        "/buy 3000 Имя — докуп\n"
        "/exit 10000 Имя — выход\n"
        "/stats — статистика текущей игры\n"
        "/end_poker — завершить игру\n"
        "/history — история игр\n"
        "/total — общая статистика\n"
        "/player Имя — профиль\n"
        "/leaderboard — рейтинг\n"
        "/cancel ID — отмена операции\n"
        "/edit ID 5000 — изменение суммы"
    )


@dp.message(Command("join"))
async def join(message: Message):
    name = args_after_command(message)

    if not name:
        await message.answer("Использование: /join Имя")
        return

    player = add_player(
        message.chat.id,
        name,
        message.from_user.id,
        message.from_user.full_name
    )

    await message.answer(
        f"Игрок «{player['name']}» привязан к твоему аккаунту."
    )


@dp.message(Command("players"))
async def players_cmd(message: Message):
    players = list_players(message.chat.id)

    if not players:
        await message.answer("Пока игроков нет.")
        return

    await message.answer(
        "Игроки:\n" + "\n".join(
            f"• {player['name']}" for player in players
        )
    )


@dp.message(Command("start_poker"))
async def start_poker(message: Message):
    current = get_session(message.chat.id)

    if current:
        await message.answer(
            f"Уже идёт игра №{current['id']}."
        )
        return

    session_id = create_session(
        message.chat.id,
        message.from_user.id
    )

    await message.answer(
        f"🃏 Покерная игра №{session_id} началась!"
    )


async def record_operation(message: Message, kind: str):
    session = get_session(message.chat.id)

    if not session:
        await message.answer(
            "Сначала начните игру: /start_poker"
        )
        return

    parts = args_after_command(message).split()

    if len(parts) < 2:
        command = (
            "/buy 3000 Имя"
            if kind == "buy"
            else "/exit 10000 Имя"
        )

        await message.answer(f"Использование: {command}")
        return

    try:
        amount = parse_amount(parts[0])
    except ValueError as error:
        await message.answer(str(error))
        return

    name = " ".join(parts[1:])
    player = find_player(message.chat.id, name)

    if not player:
        player = add_player(message.chat.id, name)

    op_id = add_operation(
        session["id"],
        message.chat.id,
        player["id"],
        kind,
        amount,
        message.from_user.id,
        message.message_id
    )

    stats = get_session_player_stats(
        session["id"],
        player["id"]
    )

    if kind == "buy":
        await message.answer(
            f"✅ Докуп записан: {player['name']} — {money(amount)} ₽\n"
            f"Всего вложено: {money(stats['buys'])} ₽\n"
            f"ID операции: {op_id}"
        )
    else:
        await message.answer(
            f"✅ Выход записан: {player['name']} — {money(amount)} ₽\n"
            f"Всего выходов: {money(stats['exits'])} ₽\n"
            f"ID операции: {op_id}"
        )


@dp.message(Command("buy"))
async def buy(message: Message):
    await record_operation(message, "buy")


@dp.message(Command("exit"))
async def exit_cmd(message: Message):
    await record_operation(message, "exit")


@dp.message(Command("stats"))
async def stats(message: Message):
    session = get_session(message.chat.id)

    if not session:
        await message.answer("Активной игры нет.")
        return

    rows = get_session_players(session["id"])

    if not rows:
        await message.answer("Операций пока нет.")
        return

    lines = [f"📊 ТЕКУЩАЯ ИГРА №{session['id']}"]

    total_buy = 0
    total_exit = 0

    for row in rows:
        result = row["exits"] - row["buys"]

        total_buy += row["buys"]
        total_exit += row["exits"]

        sign = "+" if result > 0 else ""

        lines.append(
            f"\n{row['name']}\n"
            f"Докупы: {money(row['buys'])} ₽ "
            f"({row['buy_count']} шт.)\n"
            f"Выходы: {money(row['exits'])} ₽\n"
            f"Результат: {sign}{money(result)} ₽"
        )

    lines.append(
        f"\nИТОГО\n"
        f"Докупы: {money(total_buy)} ₽\n"
        f"Выходы: {money(total_exit)} ₽\n"
        f"Баланс: {money(total_exit-total_buy)} ₽"
    )

    await message.answer("\n".join(lines))


@dp.message(Command("end_poker"))
async def end_poker(message: Message):
    session = get_session(message.chat.id)

    if not session:
        await message.answer("Активной игры нет.")
        return

    rows = get_session_players(session["id"])

    if not rows:
        close_session(session["id"])
        await message.answer("Игра закрыта. Операций не было.")
        return

    total_buy = sum(row["buys"] for row in rows)
    total_exit = sum(row["exits"] for row in rows)
    balance = total_exit - total_buy

    lines = [f"🏁 ИТОГИ ИГРЫ №{session['id']}"]

    for row in rows:
        result = row["exits"] - row["buys"]
        sign = "+" if result > 0 else ""

        lines.append(
            f"• {row['name']}: вложено "
            f"{money(row['buys'])} ₽, выход "
            f"{money(row['exits'])} ₽, результат "
            f"{sign}{money(result)} ₽"
        )

    lines.append(f"\nВсего докупов: {money(total_buy)} ₽")
    lines.append(f"Всего выходов: {money(total_exit)} ₽")
    lines.append(f"Разница: {money(balance)} ₽")

    if balance == 0:
        settlement = calculate_settlement(rows)

        if settlement:
            lines.append("\n💸 КТО КОМУ ПЕРЕВОДИТ:")

            lines.extend(
                f"• {item['from']} → {item['to']}: "
                f"{money(item['amount'])} ₽"
                for item in settlement
            )
        else:
            lines.append("\nВсе игроки закончили в ноль.")
    else:
        lines.append(
            "\n⚠️ Баланс не сходится. Проверьте докупы и выходы."
        )

    close_session(session["id"])

    lines.append("\nИгра сохранена в истории.")

    await message.answer("\n".join(lines))


@dp.message(Command("cancel"))
async def cancel(message: Message):
    parts = args_after_command(message).split()

    if len(parts) != 1 or not parts[0].isdigit():
        await message.answer("Использование: /cancel ID")
        return

    ok, reason = cancel_operation(
        message.chat.id,
        int(parts[0]),
        message.from_user.id
    )

    await message.answer(
        "Операция отменена." if ok else reason
    )


@dp.message(Command("edit"))
async def edit(message: Message):
    parts = args_after_command(message).split()

    if len(parts) != 2 or not parts[0].isdigit():
        await message.answer(
            "Использование: /edit ID новая_сумма"
        )
        return

    try:
        amount = parse_amount(parts[1])
    except ValueError as error:
        await message.answer(str(error))
        return

    ok, reason = update_operation(
        message.chat.id,
        int(parts[0]),
        amount,
        message.from_user.id
    )

    await message.answer(
        "Сумма операции обновлена." if ok else reason
    )


@dp.message(Command("history"))
async def history(message: Message):
    rows = get_history(message.chat.id, 10)

    if not rows:
        await message.answer("Завершённых игр пока нет.")
        return

    lines = ["📚 ПОСЛЕДНИЕ ИГРЫ"]

    for row in rows:
        lines.append(
            f"№{row['id']} · {row['started_at']} · "
            f"докупы {money(row['buys'])} ₽ · "
            f"выходы {money(row['exits'])} ₽"
        )

    await message.answer("\n".join(lines))


@dp.message(Command("player"))
async def player_cmd(message: Message):
    name = args_after_command(message)

    if not name:
        await message.answer("Использование: /player Имя")
        return

    player = find_player(message.chat.id, name)

    if not player:
        await message.answer("Игрок не найден.")
        return

    stats = get_player_stats(message.chat.id, player["id"])
    result = stats["exits"] - stats["buys"]
    sign = "+" if result > 0 else ""

    average = (
        round(result / stats["games"])
        if stats["games"]
        else 0
    )

    await message.answer(
        f"👤 {player['name']}\n"
        f"Игр: {stats['games']}\n"
        f"Докупы: {money(stats['buys'])} ₽\n"
        f"Выходы: {money(stats['exits'])} ₽\n"
        f"Результат: {sign}{money(result)} ₽\n"
        f"Средний результат: {money(average)} ₽"
    )


@dp.message(Command("total"))
async def total(message: Message):
    rows = get_total_stats(message.chat.id)

    if not rows:
        await message.answer("Статистики пока нет.")
        return

    lines = ["📈 ОБЩАЯ СТАТИСТИКА"]

    for row in rows:
        result = row["exits"] - row["buys"]
        sign = "+" if result > 0 else ""

        lines.append(
            f"• {row['name']}: {sign}{money(result)} ₽ "
            f"| игр: {row['games']}"
        )

    await message.answer("\n".join(lines))


@dp.message(Command("leaderboard"))
async def leaderboard(message: Message):
    rows = get_total_stats(message.chat.id)

    rows.sort(
        key=lambda row: row["exits"] - row["buys"],
        reverse=True
    )

    if not rows:
        await message.answer("Пока нет данных для рейтинга.")
        return

    lines = ["🏆 РЕЙТИНГ ПО ПРИБЫЛИ"]

    for index, row in enumerate(rows, 1):
        result = row["exits"] - row["buys"]
        sign = "+" if result > 0 else ""

        lines.append(
            f"{index}. {row['name']} — {sign}{money(result)} ₽"
        )

    await message.answer("\n".join(lines))


# ==================================================
# ИИ-СОБЕСЕДНИК
# ==================================================

@dp.message(F.text)
async def ai_chat(message: Message):
    if message.from_user is None:
        return

    if message.from_user.is_bot:
        return

    if message.chat.type not in ("group", "supergroup"):
        return

    text = (message.text or "").strip()

    if not text or text.startswith("/"):
        return

    direct = is_direct_message_to_bot(message)
    interesting = is_interesting_message(message)

    # Неинтересные сообщения игнорируем
    if not direct and not interesting:
        return

    chat_id = message.chat.id
    now = time.monotonic()

    # Не чаще одного ответа за установленный интервал
    if now - last_reply[chat_id] < REPLY_COOLDOWN:
        return

    try:
        await ask_ai(message)
        last_reply[chat_id] = time.monotonic()

    except Exception:
        logger.exception("Ошибка OpenAI API")


# ==================================================
# ЗАПУСК
# ==================================================

async def main():
    await init_db()

    me = await bot.get_me()

    logger.info(
        "Poker Manager запущен: @%s",
        me.username
    )

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
