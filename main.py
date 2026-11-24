import asyncio
import logging
import os
import re
from collections import defaultdict
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from dotenv import load_dotenv

from db import (
    init_db, add_player, find_player, list_players, create_session,
    get_active_session, close_session, add_operation, cancel_operation,
    update_operation, get_history, get_player_stats, get_total_stats,
    get_session_player_stats, get_session_players,
    save_chat_message, get_recent_chat_messages, get_chat_memories,
    get_unsummarized_chat_messages, mark_chat_messages_summarized
)
from calculations import calculate_settlement, money

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("poker-bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
BOT_USERNAME = os.getenv("BOT_USERNAME", "").strip().lstrip("@")

if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN в переменных окружения")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
ai_client = None
if OPENAI_API_KEY:
    from openai import AsyncOpenAI
    ai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# Переписка и долговременные воспоминания хранятся в SQLite.
RECENT_CONTEXT_MESSAGES = int(os.getenv("RECENT_CONTEXT_MESSAGES", "40"))
MEMORY_BATCH_SIZE = int(os.getenv("MEMORY_BATCH_SIZE", "25"))
MEMORY_CONTEXT_LIMIT = int(os.getenv("MEMORY_CONTEXT_LIMIT", "20"))
last_ai_reply = defaultdict(float)

SYSTEM_PROMPT = """
Ты участник дружеской русскоязычной компании, которая играет в покер.
Общайся естественно, коротко, с юмором, подколами и умеренным матом.
Не будь навязчивым: отвечай по делу, не изображай реального человека и не выдумывай
факты о прошлых играх. Если передан контекст статистики — используй только его.
Не придумывай суммы, результаты, карты или действия игроков.
Не подталкивай людей отыгрываться, занимать деньги или повышать ставки.
Если сообщение не требует ответа, верни ровно [SKIP].
"""

def mention_or_reply(message: Message) -> bool:
    text = (message.text or "").lower()
    if BOT_USERNAME and f"@{BOT_USERNAME.lower()}" in text:
        return True
    reply = message.reply_to_message
    return bool(reply and reply.from_user and reply.from_user.id == bot.id)

def likely_poker_chat(text: str) -> bool:
    words = (
        "покер", "раздач", "флоп", "терн", "ривер", "префлоп", "олл-ин",
        "оллин", "банк", "ставк", "бай-ин", "байин", "кто выиграл",
        "кто проиграл", "долг", "должен", "сесси", "карты", "блеф"
    )
    t = text.lower()
    return any(w in t for w in words)

async def stats_context(chat_id: int) -> str:
    totals = await get_total_stats(chat_id)
    if not totals:
        return "Пока нет сохранённой статистики."
    lines = ["Общая статистика игроков (плюс — выигрыш, минус — проигрыш):"]
    for row in totals:
        lines.append(f"- {row['name']}: {money(row['net'])} ₽")
    session = await get_active_session(chat_id)
    if session:
        rows = await get_session_player_stats(session["id"])
        lines.append("Текущая игра:")
        for row in rows:
            lines.append(f"- {row['name']}: бай-ины {money(row['buys'])} ₽, выходы {money(row['cashouts'])} ₽")
    return "\n".join(lines)

async def refresh_long_term_memory(chat_id: int):
    """Сжимает накопившиеся сообщения в долговременные заметки SQLite."""
    if not ai_client:
        return
    pending = await get_unsummarized_chat_messages(chat_id, MEMORY_BATCH_SIZE)
    if len(pending) < MEMORY_BATCH_SIZE:
        return

    previous = await get_chat_memories(chat_id, 12)
    previous_text = "\n".join(f"- {m['content']}" for m in previous) or "Пока нет."
    batch_text = "\n".join(
        f"{m['username']}: {m['content']}" for m in pending
    )
    response = await ai_client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": (
                "Ты ведёшь долговременную память группового покерного чата. "
                "Извлекай только устойчивые и полезные факты: кто участники, "
                "их предпочтения и манера общения, важные договорённости, "
                "повторяющиеся шутки, конфликты и события покерных вечеров. "
                "Не делай выводов о характере без явных оснований, не выдумывай факты, "
                "не сохраняй пароли, ключи и другую секретную информацию. "
                "Пиши кратко по-русски, списком. Если нового важного нет, ответь NO_UPDATE."
            )},
            {"role": "user", "content": f"Предыдущие воспоминания:\n{previous_text}\n\nНовые сообщения:\n{batch_text}"}
        ],
        temperature=0.2,
        max_tokens=500,
    )
    summary = (response.choices[0].message.content or "").strip()
    if summary and summary != "NO_UPDATE":
        await mark_chat_messages_summarized(chat_id, pending[-1]["id"], summary)
    else:
        # Даже если новых фактов нет, отмечаем обработанный диапазон, чтобы не гонять его снова.
        await mark_chat_messages_summarized(chat_id, pending[-1]["id"], "Новых устойчивых фактов в этом фрагменте не выявлено.")

async def ask_ai(message: Message):
    if not ai_client:
        return
    chat_id = message.chat.id
    context = await stats_context(chat_id)
    memories = await get_chat_memories(chat_id, MEMORY_CONTEXT_LIMIT)
    recent = await get_recent_chat_messages(chat_id, RECENT_CONTEXT_MESSAGES)

    memory_text = "\n".join(f"- {m['content']}" for m in memories) or "Пока нет долговременных воспоминаний."
    history = []
    for item in recent:
        if item["role"] == "user":
            history.append({"role": "user", "content": f"{item['username']}: {item['content']}"})
        else:
            history.append({"role": "assistant", "content": item["content"]})

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT + "\n\nДолговременная память чата:\n" + memory_text + "\n\nТекущая статистика:\n" + context},
        *history,
    ]
    response = await ai_client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=messages,
        temperature=0.85,
        max_tokens=220,
    )
    answer = (response.choices[0].message.content or "").strip()
    if answer and answer != "[SKIP]":
        await save_chat_message(chat_id, bot.id, "Покерный бот", "assistant", answer)
        await message.reply(answer)

def command_args(message: Message) -> list[str]:
    return (message.text or "").split()[1:]

async def get_or_add_player(chat_id: int, user_id: int, name: str) -> dict:
    player = await find_player(chat_id, user_id)
    if player:
        return player
    return await add_player(chat_id, user_id, name)

@dp.message(Command("start", "help"))
async def cmd_start(message: Message):
    await message.answer(
        "Йоу! Я покерный бот. Добавляйся через /join.\n\n"
        "Команды:\n"
        "/join — зарегистрироваться\n/players — список игроков\n"
        "/start_poker — начать игру\n/buy 1000 — записать бай-ин\n"
        "/exit 2500 — записать сумму выхода\n/stats — статистика текущей игры\n"
        "/end_poker — закончить игру и рассчитать переводы\n"
        "/total — общий баланс\n/leaderboard — рейтинг\n"
        "/history — последние операции\n/cancel ID — отменить операцию\n"
        "/edit ID СУММА — изменить операцию\n"
        "/player ИМЯ — статистика игрока"
    )

@dp.message(Command("join"))
async def cmd_join(message: Message):
    if not message.from_user:
        return
    player = await get_or_add_player(message.chat.id, message.from_user.id, message.from_user.full_name)
    await message.answer(f"{player['name']}, ты в списке. Добро пожаловать за стол 😎")

@dp.message(Command("players"))
async def cmd_players(message: Message):
    players = await list_players(message.chat.id)
    if not players:
        await message.answer("Пока никого нет. Напиши /join.")
        return
    await message.answer("Игроки:\n" + "\n".join(f"• {p['name']}" for p in players))

@dp.message(Command("start_poker"))
async def cmd_start_poker(message: Message):
    players = await list_players(message.chat.id)
    if not players:
        await message.answer("Сначала пусть игроки зарегистрируются через /join.")
        return
    session = await get_active_session(message.chat.id)
    if session:
        await message.answer("Игра уже открыта. Не плодим столы 😁")
        return
    session = await create_session(message.chat.id)
    await message.answer(f"Игра №{session['id']} началась. Бай-ин: /buy СУММА")

@dp.message(Command("buy"))
async def cmd_buy(message: Message):
    if not message.from_user:
        return
    args = command_args(message)
    if not args:
        await message.answer("Формат: /buy 1000")
        return
    try:
        amount = int(args[0])
        if amount <= 0: raise ValueError
    except ValueError:
        await message.answer("Сумма должна быть целым числом больше нуля.")
        return
    session = await get_active_session(message.chat.id)
    if not session:
        await message.answer("Сначала начните игру: /start_poker")
        return
    player = await get_or_add_player(message.chat.id, message.from_user.id, message.from_user.full_name)
    op = await add_operation(session["id"], player["id"], "buy", amount, message.from_user.id)
    await message.answer(f"Записал бай-ин {money(amount)} ₽ для {player['name']} (операция #{op['id']}).")

@dp.message(Command("exit"))
async def cmd_exit(message: Message):
    if not message.from_user:
        return
    args = command_args(message)
    if not args:
        await message.answer("Формат: /exit 2500")
        return
    try:
        amount = int(args[0])
        if amount < 0: raise ValueError
    except ValueError:
        await message.answer("Укажи целую сумму от нуля.")
        return
    session = await get_active_session(message.chat.id)
    if not session:
        await message.answer("Нет открытой игры. Сначала /start_poker")
        return
    player = await get_or_add_player(message.chat.id, message.from_user.id, message.from_user.full_name)
    op = await add_operation(session["id"], player["id"], "cashout", amount, message.from_user.id)
    await message.answer(f"Записал выход {money(amount)} ₽ для {player['name']} (операция #{op['id']}).")

@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    session = await get_active_session(message.chat.id)
    if not session:
        await message.answer("Сейчас нет открытой игры.")
        return
    rows = await get_session_player_stats(session["id"])
    if not rows:
        await message.answer("Пока нет операций.")
        return
    lines = ["Статистика текущей игры:"]
    for r in rows:
        net = int(r["cashouts"]) - int(r["buys"])
        lines.append(f"• {r['name']}: бай-ины {money(r['buys'])} ₽, выход {money(r['cashouts'])} ₽, итог {money(net)} ₽")
    await message.answer("\n".join(lines))

@dp.message(Command("end_poker"))
async def cmd_end_poker(message: Message):
    session = await get_active_session(message.chat.id)
    if not session:
        await message.answer("Нет открытой игры.")
        return
    rows = await get_session_player_stats(session["id"])
    total_buys = sum(int(r["buys"]) for r in rows)
    total_cashouts = sum(int(r["cashouts"]) for r in rows)
    if total_buys != total_cashouts:
        await message.answer(
            f"Баланс не сходится: бай-ины {money(total_buys)} ₽, выходы {money(total_cashouts)} ₽.\n"
            f"Разница: {money(total_buys-total_cashouts)} ₽. Проверь /stats. Игру пока не закрываю."
        )
        return
    transfers = calculate_settlement(rows)
    await close_session(session["id"])
    if not transfers:
        await message.answer("Игра закрыта. Все в ноль — редкий случай, банда 😁")
        return
    lines = ["Игра закрыта. Переводы:"]
    for t in transfers:
        lines.append(f"• {t['from']} → {t['to']}: {money(t['amount'])} ₽")
    await message.answer("\n".join(lines))

@dp.message(Command("cancel"))
async def cmd_cancel(message: Message):
    args = command_args(message)
    if not args or not args[0].isdigit():
        await message.answer("Формат: /cancel ID")
        return
    ok = await cancel_operation(int(args[0]), message.chat.id)
    await message.answer("Операцию отменил." if ok else "Не нашёл активную операцию с таким ID.")

@dp.message(Command("edit"))
async def cmd_edit(message: Message):
    args = command_args(message)
    if len(args) < 2 or not args[0].isdigit() or not args[1].isdigit():
        await message.answer("Формат: /edit ID НОВАЯ_СУММА")
        return
    amount = int(args[1])
    ok = await update_operation(int(args[0]), message.chat.id, amount)
    await message.answer("Сумму обновил." if ok else "Не нашёл активную операцию или сумма некорректна.")

@dp.message(Command("history"))
async def cmd_history(message: Message):
    rows = await get_history(message.chat.id, 15)
    if not rows:
        await message.answer("История пока пустая.")
        return
    lines = ["Последние операции:"]
    for r in rows:
        lines.append(f"#{r['id']} {r['name']} — {'бай-ин' if r['kind']=='buy' else 'выход'} {money(r['amount'])} ₽ ({r['created_at']})")
    await message.answer("\n".join(lines))

@dp.message(Command("player"))
async def cmd_player(message: Message):
    args = command_args(message)
    if not args:
        await message.answer("Формат: /player Имя")
        return
    query = " ".join(args).lower()
    rows = await get_total_stats(message.chat.id)
    found = next((r for r in rows if r["name"].lower() == query), None)
    if not found:
        await message.answer("Не нашёл игрока. Посмотри список через /players.")
        return
    await message.answer(f"{found['name']}: {money(found['net'])} ₽ за всё время.")

@dp.message(Command("total", "leaderboard"))
async def cmd_total(message: Message):
    rows = await get_total_stats(message.chat.id)
    if not rows:
        await message.answer("Статистики пока нет.")
        return
    rows = sorted(rows, key=lambda r: int(r["net"]), reverse=True)
    title = "Общий рейтинг:" if (message.text or "").split()[0].endswith("leaderboard") else "Общий баланс:"
    await message.answer(title + "\n" + "\n".join(f"{i+1}. {r['name']}: {money(r['net'])} ₽" for i, r in enumerate(rows)))

@dp.message(F.text)
async def ai_chat(message: Message):
    if not ai_client or not message.from_user or message.from_user.is_bot:
        return
    if message.chat.type not in ("group", "supergroup"):
        return
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        return
    direct = mention_or_reply(message)
    if not direct and not likely_poker_chat(text):
        return

    # Сохраняем каждое подходящее сообщение, даже если бот сейчас на cooldown.
    await save_chat_message(message.chat.id, message.from_user.id,
                            message.from_user.full_name, "user", text)
    try:
        await refresh_long_term_memory(message.chat.id)
    except Exception:
        log.exception("Long-term memory update failed")

    now = asyncio.get_running_loop().time()
    if now - last_ai_reply[message.chat.id] < 35:
        return
    try:
        await ask_ai(message)
        last_ai_reply[message.chat.id] = now
    except Exception:
        log.exception("AI reply failed")

async def main():
    await init_db()
    me = await bot.get_me()
    global BOT_USERNAME
    if not BOT_USERNAME:
        BOT_USERNAME = (me.username or "").lower()
    log.info("Bot started as @%s", me.username)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
