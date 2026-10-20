import asyncio
import hashlib
import html
import logging
import os
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, Message, ReplyKeyboardMarkup)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

import db
from pdf_parser import parse_pdf

logging.basicConfig(level=logging.INFO)

TOKEN = os.environ["BOT_TOKEN"]
SECRET = hashlib.sha256(TOKEN.encode()).hexdigest()
WEBHOOK_PATH = "/webhook/" + SECRET[:24]
ALLOWED = {int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x}

BTN_NEW, BTN_LIST_B, BTN_CALC, BTN_ITEMS = "📦 Новая партия", "📂 Мои партии", "🧮 Рассчитать", "📋 Товары"
MENU_BTNS = {BTN_NEW, BTN_LIST_B, BTN_CALC, BTN_ITEMS}

router = Router()
if ALLOWED:
    router.message.filter(F.from_user.id.in_(ALLOWED))
    router.callback_query.filter(F.from_user.id.in_(ALLOWED))


class S(StatesGroup):
    buy = State()
    sell = State()
    markup = State()
    each = State()


def menu_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_CALC), KeyboardButton(text=BTN_ITEMS)],
                  [KeyboardButton(text=BTN_NEW), KeyboardButton(text=BTN_LIST_B)]],
        resize_keyboard=True)


def money(x) -> str:
    x = Decimal(x)
    s = f"{x:,.2f}" if x != x.to_integral_value() else f"{x:,.0f}"
    return s.replace(",", " ") + " ₽"


def num(x) -> str:
    x = Decimal(x)
    return str(int(x)) if x == x.to_integral_value() else str(x.normalize())


def parse_num(text: str):
    t = text.strip().lower().replace("₽", "").replace("руб", "").replace("р.", "")
    t = re.sub(r"\s+", "", t).replace(",", ".")
    if not re.fullmatch(r"\d+(\.\d+)?", t):
        return None
    try:
        return Decimal(t)
    except InvalidOperation:
        return None


def default_name() -> str:
    return "Партия " + datetime.now().strftime("%d.%m %H:%M")


async def report(uid: int) -> str:
    b = await db.active_batch(uid, default_name())
    t = await db.totals(b["id"])
    if not t["n"]:
        return f"В партии «{html.escape(b['name'])}» пока пусто. Кинь товар или PDF."
    profit = t["revenue"] - t["cost_priced"]
    lines = [f"🧮 <b>{html.escape(b['name'])}</b>",
             f"Позиций: {t['n']} (штук: {num(t['qty'])})",
             f"Закупка: <b>{money(t['cost_all'])}</b>",
             f"Выручка: <b>{money(t['revenue'])}</b>",
             f"Чистая прибыль: <b>{money(profit)}</b>"]
    if t["cost_priced"] > 0:
        lines.append(f"Маржа: {profit / t['cost_priced'] * 100:.1f}%")
    if t["unpriced"]:
        lines.append(f"\n⚠️ У {t['unpriced']} поз. нет цены продажи — в выручку и прибыль не вошли.")
    return "\n".join(lines)


def after_pdf_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📈 Наценка % на всё", callback_data="mk")],
        [InlineKeyboardButton(text="✍️ Цены продажи по одной", callback_data="each")],
        [InlineKeyboardButton(text="🧮 Рассчитать", callback_data="calc")]])


async def ask_next_price(msg: Message, state: FSMContext, uid: int):
    data = await state.get_data()
    b = await db.active_batch(uid, default_name())
    it = await db.next_unpriced(b["id"], data.get("skip", []))
    if not it:
        await state.clear()
        await msg.answer("Готово ✅\n\n" + await report(uid), reply_markup=menu_kb())
        return
    await state.set_state(S.each)
    await state.update_data(item_id=it["id"])
    await msg.answer(
        f"<b>{html.escape(it['name'])}</b>\nЗакупка: {money(it['buy'])} × {num(it['qty'])}\n\n"
        "Цена продажи за 1 шт? Число, либо <code>+30%</code> (наценка), либо «пропуск»."
    )


# ---------- меню ----------
@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    await db.active_batch(m.from_user.id, default_name())
    await m.answer(
        "Привет! Я считаю закупку, выручку и чистую прибыль.\n\n"
        "• Напиши название товара — спрошу цену закупки и продажи.\n"
        "  Несколько штук: <code>Название x5</code>\n"
        "• Или кинь PDF-накладную — разберу все позиции и закупку сам.\n"
        "• «🧮 Рассчитать» — итог по партии.\n\n"
        "/undo — удалить последний товар, /help — подсказка.",
        reply_markup=menu_kb())


@router.message(Command("help"))
async def help_(m: Message, state: FSMContext):
    await start(m, state)


@router.message(F.text == BTN_NEW)
async def new_batch(m: Message, state: FSMContext):
    await state.clear()
    await db.new_batch(m.from_user.id, default_name())
    await m.answer("📦 Новая партия создана. Кидай товары или PDF.", reply_markup=menu_kb())


@router.message(F.text == BTN_LIST_B)
async def my_batches(m: Message, state: FSMContext):
    await state.clear()
    rows = await db.list_batches(m.from_user.id)
    if not rows:
        await m.answer("Партий пока нет.")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{r['name']} · {num(r['qty'])} шт", callback_data=f"b:{r['id']}"),
         InlineKeyboardButton(text="🗑", callback_data=f"d:{r['id']}")] for r in rows])
    await m.answer("Выбери партию (🗑 — удалить):", reply_markup=kb)


@router.callback_query(F.data.startswith("b:"))
async def pick_batch(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await db.set_active(c.from_user.id, int(c.data[2:]))
    await c.answer("Партия выбрана")
    await c.message.answer(await report(c.from_user.id))


@router.callback_query(F.data.startswith("d:"))
async def del_batch(c: CallbackQuery):
    await db.delete_batch(c.from_user.id, int(c.data[2:]))
    await c.answer("Удалено")
    await c.message.edit_text("Партия удалена. Открой «📂 Мои партии» заново.")


@router.message(F.text == BTN_CALC)
@router.message(Command("calc"))
async def calc(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(await report(m.from_user.id))


@router.callback_query(F.data == "calc")
async def calc_cb(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.answer()
    await c.message.answer(await report(c.from_user.id))


@router.message(F.text == BTN_ITEMS)
@router.message(Command("list"))
async def list_items(m: Message, state: FSMContext):
    await state.clear()
    b = await db.active_batch(m.from_user.id, default_name())
    rows = await db.items(b["id"])
    if not rows:
        await m.answer("В партии пока пусто.")
        return
    out, chunk = [], f"📋 <b>{html.escape(b['name'])}</b>\n"
    for i, r in enumerate(rows, 1):
        sell = money(r["sell"]) if r["sell"] is not None else "—"
        line = f"{i}. {html.escape(r['name'])}\n    {money(r['buy'])} → {sell} × {num(r['qty'])}\n"
        if len(chunk) + len(line) > 3800:
            out.append(chunk)
            chunk = ""
        chunk += line
    out.append(chunk)
    for part in out:
        await m.answer(part)


@router.message(Command("undo"))
async def undo(m: Message, state: FSMContext):
    await state.clear()
    b = await db.active_batch(m.from_user.id, default_name())
    r = await db.delete_last(b["id"])
    await m.answer(f"Удалил: {html.escape(r['name'])}" if r else "Удалять нечего.")


# ---------- PDF ----------
@router.message(F.document)
async def pdf_in(m: Message, state: FSMContext, bot: Bot):
    d = m.document
    if not ((d.mime_type or "").endswith("pdf") or (d.file_name or "").lower().endswith(".pdf")):
        await m.answer("Нужен файл PDF.")
        return
    await state.clear()
    await m.answer("Читаю PDF…")
    buf = BytesIO()
    await bot.download(d, destination=buf)
    try:
        res = await asyncio.to_thread(parse_pdf, buf.getvalue())
    except Exception:
        logging.exception("pdf parse failed")
        res = None
    if not res or not res.items:
        await m.answer("Не смог разобрать этот PDF 😕 Добавь товары вручную или пришли другой файл.")
        return
    b = await db.active_batch(m.from_user.id, default_name())
    await db.add_items(b["id"], [(i.name, i.qty, i.price, None) for i in res.items])
    qty = sum(i.qty for i in res.items)
    text = (f"✅ Добавил в «{html.escape(b['name'])}»: {len(res.items)} поз., {num(qty)} шт.\n"
            f"Закупка по PDF: <b>{money(res.calc_total)}</b>")
    warn = []
    if res.doc_total is not None and res.doc_total != res.calc_total:
        warn.append(f"в PDF итого {money(res.doc_total)}")
    if res.doc_qty is not None and res.doc_qty != qty:
        warn.append(f"в PDF {res.doc_qty} шт")
    if warn:
        text += "\n⚠️ Не сходится: " + ", ".join(warn) + ". Проверь через «📋 Товары»."
    if not res.named_ok:
        text += "\n⚠️ Часть названий склеилась неточно."
    text += "\n\nКак задать цену продажи?"
    await m.answer(text, reply_markup=after_pdf_kb())


@router.callback_query(F.data == "mk")
async def markup_ask(c: CallbackQuery, state: FSMContext):
    await state.set_state(S.markup)
    await c.answer()
    await c.message.answer("Напиши наценку в процентах, например <code>30</code>. "
                           "Применю ко всем товарам без цены продажи.")


@router.message(S.markup)
async def markup_set(m: Message, state: FSMContext):
    pct = parse_num((m.text or "").replace("%", ""))
    if pct is None:
        await m.answer("Нужно число, например 30.")
        return
    b = await db.active_batch(m.from_user.id, default_name())
    n = await db.apply_markup(b["id"], pct)
    await state.clear()
    await m.answer(f"Наценка {num(pct)}% применена к {n} поз. (округлил до рубля).\n\n"
                   + await report(m.from_user.id), reply_markup=menu_kb())


@router.callback_query(F.data == "each")
async def each_start(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.update_data(skip=[])
    await ask_next_price(c.message, state, c.from_user.id)


@router.message(S.each)
async def each_price(m: Message, state: FSMContext):
    text = (m.text or "").strip().lower()
    data = await state.get_data()
    b = await db.active_batch(m.from_user.id, default_name())
    item_id = data["item_id"]
    if text in ("пропуск", "skip", "-"):
        await state.update_data(skip=data.get("skip", []) + [item_id])
        await ask_next_price(m, state, m.from_user.id)
        return
    it = next((r for r in await db.items(b["id"]) if r["id"] == item_id), None)
    if it is None:
        await state.clear()
        await m.answer("Не нашёл товар, начни заново.")
        return
    mm = re.fullmatch(r"\+?\s*([\d.,]+)\s*%", text)
    if mm and parse_num(mm.group(1)) is not None:
        sell = (it["buy"] * (1 + parse_num(mm.group(1)) / 100)).quantize(Decimal("1"))
    else:
        sell = parse_num(text)
    if sell is None:
        await m.answer("Не понял. Пришли число, +30% или «пропуск».")
        return
    await db.set_sell(item_id, b["id"], sell)
    await ask_next_price(m, state, m.from_user.id)


# ---------- ручной ввод товара ----------
@router.message(S.buy)
async def got_buy(m: Message, state: FSMContext):
    v = parse_num(m.text or "")
    if v is None:
        await m.answer("Пришли цену числом, например 250.")
        return
    await state.update_data(buy=str(v))
    await state.set_state(S.sell)
    await m.answer("За сколько продаёшь? (за 1 шт)")


@router.message(S.sell)
async def got_sell(m: Message, state: FSMContext):
    v = parse_num(m.text or "")
    if v is None:
        await m.answer("Пришли цену числом, например 400.")
        return
    d = await state.get_data()
    b = await db.active_batch(m.from_user.id, default_name())
    await db.add_items(b["id"], [(d["name"], Decimal(d["qty"]), Decimal(d["buy"]), v)])
    t = await db.totals(b["id"])
    await state.clear()
    await m.answer(
        f"✅ {html.escape(d['name'])} × {d['qty']}: закупка {money(Decimal(d['buy']))}, продажа {money(v)}\n"
        f"В партии: {t['n']} поз. Кидай следующий или жми «🧮 Рассчитать».", reply_markup=menu_kb())


@router.message(F.text & ~F.text.startswith("/"))
async def new_item(m: Message, state: FSMContext):
    text = m.text.strip()
    if text in MENU_BTNS:
        return
    mm = re.fullmatch(r"(.+?)\s*[xхX×]\s*(\d+)", text)
    name, qty = (mm.group(1).strip(), mm.group(2)) if mm else (text, "1")
    await state.update_data(name=name, qty=qty)
    await state.set_state(S.buy)
    await m.answer(f"<b>{html.escape(name)}</b> × {qty}\nЗа сколько купил? (за 1 шт)")


# ---------- запуск ----------
async def on_startup(bot: Bot):
    await db.init()
    base = os.environ.get("RENDER_EXTERNAL_URL")
    if base:
        await bot.set_webhook(base + WEBHOOK_PATH, secret_token=SECRET,
                              allowed_updates=["message", "callback_query"], drop_pending_updates=False)


def main():
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    dp.startup.register(on_startup)
    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text="ok"))
    SimpleRequestHandler(dp, bot, secret_token=SECRET).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    web.run_app(app, host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))


if __name__ == "__main__":
    main()
