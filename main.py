import asyncio
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from dotenv import load_dotenv

from db import (
    init_db, get_session, create_session, close_session, add_player,
    find_player, list_players, add_operation, get_operations, cancel_operation,
    update_operation, get_player_stats, get_total_stats, get_history,
    get_session_stats, get_session_player_stats, get_session_players
)
from calculations import calculate_settlement, money

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Укажите BOT_TOKEN в файле .env")

logging.basicConfig(level=logging.INFO)
bot = Bot(TOKEN)
dp = Dispatcher()


def parse_amount(raw: str) -> int:
    cleaned = raw.replace(" ", "").replace(",", ".")
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        raise ValueError("Сумма должна быть числом, например 3000.")
    if value <= 0 or value != value.to_integral_value():
        raise ValueError("Сумма должна быть положительным целым числом в рублях.")
    return int(value)


def args_after_command(message: Message) -> str:
    return (message.text or "").partition(" ")[2].strip()


def display_name(player):
    return player["name"]


@dp.message(Command("start"))
async def start(message: Message):
    await message.answer(
        "Привет! Я Poker Manager.\n\n"
        "Сначала зарегистрируйся: /join Имя\n"
        "Начать игру: /start_poker\n"
        "Докуп: /buy 3000 Имя\n"
        "Выход: /exit 10000 Имя\n"
        "Статистика: /stats\n"
        "Завершить игру: /end_poker\n\n"
        "Полный список: /help"
    )


@dp.message(Command("help"))
async def help_cmd(message: Message):
    await message.answer(
        "КОМАНДЫ POKER MANAGER\n\n"
        "/join Имя — привязать Telegram ID к игроку\n"
        "/players — список игроков\n"
        "/start_poker — начать новую сессию\n"
        "/buy 3000 Имя — записать докуп\n"
        "/exit 10000 Имя — записать выход\n"
        "/stats — статистика текущей игры\n"
        "/end_poker — завершить игру и рассчитать долги\n"
        "/history — последние игровые сессии\n"
        "/total — общая статистика\n"
        "/player Имя — профиль игрока\n"
        "/leaderboard — рейтинг по прибыли\n"
        "/cancel ID — отменить свою операцию\n"
        "/edit ID 5000 — изменить сумму своей операции\n\n"
        "Суммы указываются в рублях. Один игрок может делать сколько угодно докупов."
    )


@dp.message(Command("join"))
async def join(message: Message):
    name = args_after_command(message)
    if not name:
        await message.answer("Использование: /join Имя")
        return
    player = add_player(message.chat.id, name, message.from_user.id, message.from_user.full_name)
    await message.answer(f"Игрок «{player['name']}» привязан к вашему Telegram-аккаунту.")


@dp.message(Command("players"))
async def players_cmd(message: Message):
    players = list_players(message.chat.id)
    if not players:
        await message.answer("Пока нет игроков. Добавьте себя командой /join Имя.")
        return
    await message.answer("Игроки:\n" + "\n".join(f"• {p['name']}" for p in players))


@dp.message(Command("start_poker"))
async def start_poker(message: Message):
    current = get_session(message.chat.id)
    if current:
        await message.answer(f"Уже идёт сессия №{current['id']}. Сначала завершите её командой /end_poker.")
        return
    session_id = create_session(message.chat.id, message.from_user.id)
    await message.answer(f"🃏 Покерная сессия №{session_id} началась!\nТеперь можно записывать докупы и выходы.")


async def record_operation(message: Message, kind: str):
    session = get_session(message.chat.id)
    if not session:
        await message.answer("Сейчас нет активной игры. Начните её командой /start_poker.")
        return
    parts = args_after_command(message).split()
    if len(parts) < 2:
        command = "/buy 3000 Имя" if kind == "buy" else "/exit 10000 Имя"
        await message.answer(f"Использование: {command}")
        return
    try:
        amount = parse_amount(parts[0])
    except ValueError as e:
        await message.answer(str(e))
        return
    name = " ".join(parts[1:])
    player = find_player(message.chat.id, name)
    if not player:
        player = add_player(message.chat.id, name, None, None)
    op_id = add_operation(
        session["id"], message.chat.id, player["id"], kind, amount,
        message.from_user.id, message.message_id
    )
    stats = get_session_player_stats(session["id"], player["id"])
    if kind == "buy":
        await message.answer(
            f"✅ Докуп записан: {player['name']} — {money(amount)} ₽\n"
            f"Всего вложено в этой игре: {money(stats['buys'])} ₽\n"
            f"ID операции: {op_id}"
        )
    else:
        await message.answer(
            f"✅ Выход записан: {player['name']} — {money(amount)} ₽\n"
            f"Всего выходов в этой игре: {money(stats['exits'])} ₽\n"
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
        await message.answer("Активной игры нет. Последнюю статистику можно посмотреть через /history.")
        return
    rows = get_session_players(session["id"])
    if not rows:
        await message.answer(f"Сессия №{session['id']} началась, операций пока нет.")
        return
    lines = [f"📊 ТЕКУЩАЯ ИГРА №{session['id']}"]
    total_buy = total_exit = 0
    for row in rows:
        result = row["exits"] - row["buys"]
        total_buy += row["buys"]
        total_exit += row["exits"]
        sign = "+" if result > 0 else ""
        lines.append(
            f"\n{row['name']}\n"
            f"Докупы: {money(row['buys'])} ₽ ({row['buy_count']} шт.)\n"
            f"Выходы: {money(row['exits'])} ₽\n"
            f"Результат: {sign}{money(result)} ₽"
        )
    lines.append(f"\nИТОГО\nДокупы: {money(total_buy)} ₽\nВыходы: {money(total_exit)} ₽\nБаланс: {money(total_exit-total_buy)} ₽")
    await message.answer("\n".join(lines))


@dp.message(Command("end_poker"))
async def end_poker(message: Message):
    session = get_session(message.chat.id)
    if not session:
        await message.answer("Нет активной игры.")
        return
    rows = get_session_players(session["id"])
    if not rows:
        await message.answer("В этой игре нет операций. Сессия закрыта.")
        close_session(session["id"])
        return
    total_buy = sum(r["buys"] for r in rows)
    total_exit = sum(r["exits"] for r in rows)
    balance = total_exit - total_buy
    lines = [f"🏁 ИТОГИ ПОКЕРНОЙ ИГРЫ №{session['id']}"]
    for r in rows:
        result = r["exits"] - r["buys"]
        sign = "+" if result > 0 else ""
        lines.append(f"• {r['name']}: вложено {money(r['buys'])} ₽, выход {money(r['exits'])} ₽, результат {sign}{money(result)} ₽")
    lines += [f"\nВсего докупов: {money(total_buy)} ₽", f"Всего выходов: {money(total_exit)} ₽", f"Разница: {money(balance)} ₽"]
    if balance == 0:
        settlement = calculate_settlement(rows)
        if settlement:
            lines.append("\n💸 КТО КОМУ ПЕРЕВОДИТ:")
            lines.extend(f"• {item['from']} → {item['to']}: {money(item['amount'])} ₽" for item in settlement)
        else:
            lines.append("\nВсе игроки завершили игру в ноль.")
    else:
        lines.append("\n⚠️ Баланс не сходится. Долги не рассчитаны. Проверьте пропущенные выходы/докупы и исправьте операции.")
    close_session(session["id"])
    lines.append("\nСессия сохранена в истории. Команда /history покажет прошлые игры.")
    await message.answer("\n".join(lines))


@dp.message(Command("cancel"))
async def cancel(message: Message):
    parts = args_after_command(message).split()
    if len(parts) != 1 or not parts[0].isdigit():
        await message.answer("Использование: /cancel ID")
        return
    ok, reason = cancel_operation(message.chat.id, int(parts[0]), message.from_user.id)
    await message.answer("Операция отменена." if ok else reason)


@dp.message(Command("edit"))
async def edit(message: Message):
    parts = args_after_command(message).split()
    if len(parts) != 2 or not parts[0].isdigit():
        await message.answer("Использование: /edit ID новая_сумма\nНапример: /edit 15 5000")
        return
    try:
        amount = parse_amount(parts[1])
    except ValueError as e:
        await message.answer(str(e))
        return
    ok, reason = update_operation(message.chat.id, int(parts[0]), amount, message.from_user.id)
    await message.answer("Сумма операции обновлена." if ok else reason)


@dp.message(Command("history"))
async def history(message: Message):
    rows = get_history(message.chat.id, 10)
    if not rows:
        await message.answer("Завершённых игр пока нет.")
        return
    lines = ["📚 ПОСЛЕДНИЕ ИГРЫ"]
    for r in rows:
        lines.append(f"№{r['id']} · {r['started_at']} · докупы {money(r['buys'])} ₽ · выходы {money(r['exits'])} ₽")
    await message.answer("\n".join(lines))


@dp.message(Command("player"))
async def player_cmd(message: Message):
    name = args_after_command(message)
    if not name:
        await message.answer("Использование: /player Имя")
        return
    player = find_player(message.chat.id, name)
    if not player:
        await message.answer("Игрок не найден. Добавьте его через /join Имя или сначала запишите операцию.")
        return
    s = get_player_stats(message.chat.id, player["id"])
    result = s["exits"] - s["buys"]
    sign = "+" if result > 0 else ""
    await message.answer(
        f"👤 {player['name']}\n"
        f"Игр: {s['games']}\n"
        f"Всего докупов: {money(s['buys'])} ₽\n"
        f"Всего выходов: {money(s['exits'])} ₽\n"
        f"Общий результат: {sign}{money(result)} ₽\n"
        f"Средний результат за игру: {money(round(result / s['games'])) if s['games'] else 0} ₽"
    )


@dp.message(Command("total"))
async def total(message: Message):
    rows = get_total_stats(message.chat.id)
    if not rows:
        await message.answer("Статистики пока нет.")
        return
    lines = ["📈 ОБЩАЯ СТАТИСТИКА"]
    for r in rows:
        result = r["exits"] - r["buys"]
        sign = "+" if result > 0 else ""
        lines.append(f"• {r['name']}: {sign}{money(result)} ₽ | игр: {r['games']}")
    await message.answer("\n".join(lines))


@dp.message(Command("leaderboard"))
async def leaderboard(message: Message):
    rows = get_total_stats(message.chat.id)
    rows.sort(key=lambda r: r["exits"] - r["buys"], reverse=True)
    if not rows:
        await message.answer("Пока нет данных для рейтинга.")
        return
    lines = ["🏆 РЕЙТИНГ ПО ПРИБЫЛИ"]
    for i, r in enumerate(rows, 1):
        result = r["exits"] - r["buys"]
        sign = "+" if result > 0 else ""
        lines.append(f"{i}. {r['name']} — {sign}{money(result)} ₽")
    await message.answer("\n".join(lines))


async def main():
    await init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
