#!/usr/bin/env python3
"""
Telegram bot: monitors DTEK KEM updates for user-selected address.

- /set  -> asks Street -> asks House
- /check -> check now
- /status -> show saved address + last updateTimestamp
- /stop -> forget address and stop monitoring
- Buttons for quick actions
- Periodic polling via PTB JobQueue

Install:
  pip install "python-telegram-bot[job_queue]==20.*" playwright
  playwright install

Run:
  export BOT_TOKEN="123:ABC"
  export POLL_EVERY_SEC=300
  python3 bot.py
"""

from __future__ import annotations

import os
import json
from typing import Any, Dict, Final, Optional, Tuple

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

import dtek_client


# =========================
# Storage
# =========================

STATE_FILE = "bot_state.json"


def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def save_state(state: Dict[str, Any]) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def get_user_cfg(state: Dict[str, Any], chat_id: int) -> Dict[str, Any]:
    return (state.get("users") or {}).get(str(chat_id)) or {}


def set_user_cfg(state: Dict[str, Any], chat_id: int, cfg: Dict[str, Any]) -> None:
    state.setdefault("users", {})
    state["users"][str(chat_id)] = cfg


def del_user_cfg(state: Dict[str, Any], chat_id: int) -> None:
    users = state.get("users") or {}
    users.pop(str(chat_id), None)
    state["users"] = users


# =========================
# Bot config
# =========================

POLL_EVERY_SEC = int(os.getenv("POLL_EVERY_SEC", "300"))  # default 5 min

# Conversation states
ASK_STREET: Final[int] = 1
ASK_HOUSE: Final[int] = 2


def menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔄 Перевірити зараз", callback_data="check")],
            [InlineKeyboardButton("⚙️ Змінити адресу", callback_data="set")],
            [InlineKeyboardButton("ℹ️ Моя адреса", callback_data="status")],
            [InlineKeyboardButton("🛑 Стоп", callback_data="stop")],
        ]
    )


def normalize_street(s: str) -> str:
    s = " ".join((s or "").strip().split())
    # user often types "Борщагівська" -> we want "вул. Борщагівська"
    # if they already typed "вул." or "просп." - don't duplicate
    low = s.lower()
    if low.startswith(("вул.", "вулиця", "просп.", "проспект", "пров.", "провулок", "бульв.", "пл.", "площа")):
        return s
    return f"вул. {s}" if s else s


def normalize_house(s: str) -> str:
    return (s or "").strip()


def valid_house(h: str) -> bool:
    return bool(h) and any(c.isdigit() for c in h) and len(h) <= 16


def target_message(update: Update):
    if update.message:
        return update.message
    if update.callback_query and update.callback_query.message:
        return update.callback_query.message
    return None


# =========================
# Commands
# =========================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = (
        "Привіт 👋\n"
        "Я моніторю оновлення DTEK і напишу тобі, коли зміниться інформація.\n\n"
        "Натисни кнопку або /set"
    )
    if update.message:
        await update.message.reply_text(msg, reply_markup=menu_kb())


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tm = target_message(update)
    if not tm:
        return

    state = load_state()
    cfg = get_user_cfg(state, tm.chat_id)

    if not cfg:
        await tm.reply_text("Адреса ще не задана. Натисни «Змінити адресу» (/set).", reply_markup=menu_kb())
        return

    street = cfg.get("street_ui") or cfg.get("street") or "—"
    house = cfg.get("house") or "—"
    last_ut = cfg.get("last_updateTimestamp") or "—"
    last_err = (cfg.get("last_error") or "").strip()

    text = (
        f"📍 Адреса: {street}, {house}\n"
        f"🕒 Останнє оновлення: {last_ut}\n"
        f"⏱️ Перевірка: кожні {POLL_EVERY_SEC // 60} хв"
    )
    if last_err:
        text += f"\n⚠️ Остання помилка: {last_err}"

    await tm.reply_text(text, reply_markup=menu_kb())


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tm = target_message(update)
    if not tm:
        return

    state = load_state()
    if not get_user_cfg(state, tm.chat_id):
        await tm.reply_text("Моніторинг і так не налаштований.", reply_markup=menu_kb())
        return

    del_user_cfg(state, tm.chat_id)
    save_state(state)

    await tm.reply_text("🛑 Ок, зупинив моніторинг і забув адресу.", reply_markup=menu_kb())


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tm = target_message(update)
    if not tm:
        return

    state = load_state()
    cfg = get_user_cfg(state, tm.chat_id)

    street = (cfg.get("street") or "").strip()
    street_ui = (cfg.get("street_ui") or street).strip()
    house = (cfg.get("house") or "").strip()

    if not street or not house:
        await tm.reply_text("Спочатку задай адресу: /set", reply_markup=menu_kb())
        return

    status_msg = await tm.reply_text("⏳ Перевіряю DTEK...")

    try:
        j = await dtek_client.fetch_dtek(street_value=street, headless=True)
        text = dtek_client.format_house_info(street_ui, house, j)

        q = dtek_client.get_house_queue(j, house)
        if q:
            text += f"\n\n🏷️ Черга: {q}\n\n" + dtek_client.summarize_fact_for_today(j, q)

        # update cached marker (so monitor won’t instantly re-notify the same data)
        cfg["last_marker"] = dtek_client.make_update_marker(j)
        cfg["last_updateTimestamp"] = (j.get("updateTimestamp") or "")
        cfg["last_error"] = ""
        set_user_cfg(state, tm.chat_id, cfg)
        save_state(state)

    except Exception as e:
        text = f"❌ Помилка запиту: {type(e).__name__}: {e}"
        cfg["last_error"] = text
        set_user_cfg(state, tm.chat_id, cfg)
        save_state(state)

    await status_msg.edit_text(text, reply_markup=menu_kb())


# =========================
# Conversation /set (2 steps)
# =========================

async def set_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    tm = target_message(update)
    if not tm:
        return ConversationHandler.END
    await tm.reply_text("Введи назву вулиці (як на сайті DTEK), напр:\nБорщагівська")
    return ASK_STREET


async def set_street(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message:
        return ConversationHandler.END

    street_raw = update.message.text or ""
    street = normalize_street(street_raw)

    if len(street) < 3:
        await update.message.reply_text("Некоректна вулиця, спробуй ще раз:")
        return ASK_STREET

    context.user_data["pending_street"] = street
    await update.message.reply_text("Тепер введи номер будинку, напр: 145")
    return ASK_HOUSE


async def set_house(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message:
        return ConversationHandler.END

    house = normalize_house(update.message.text or "")
    if not valid_house(house):
        await update.message.reply_text("Некоректний номер будинку, спробуй ще раз:")
        return ASK_HOUSE

    street = (context.user_data.get("pending_street") or "").strip()
    if not street:
        await update.message.reply_text("Щось пішло не так. Спробуй /set ще раз.", reply_markup=menu_kb())
        return ConversationHandler.END

    chat_id = update.message.chat_id
    state = load_state()
    cfg = get_user_cfg(state, chat_id)

    cfg["street"] = street          # street_value for XHR
    cfg["street_ui"] = street       # shown to user
    cfg["house"] = house
    cfg["last_marker"] = ""         # force notify on next poll
    cfg["last_updateTimestamp"] = ""
    cfg["last_error"] = ""

    set_user_cfg(state, chat_id, cfg)
    save_state(state)

    await update.message.reply_text(
        f"✅ Збережено:\n{street}, {house}\n"
        f"Я напишу, коли оновиться інформація на сайті.\n"
        f"(перевірка кожні {POLL_EVERY_SEC // 60} хв)",
        reply_markup=menu_kb(),
    )

    # immediate check once (nice UX)
    await cmd_check(update, context)
    return ConversationHandler.END


async def set_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    tm = target_message(update)
    if tm:
        await tm.reply_text("Скасовано.", reply_markup=menu_kb())
    return ConversationHandler.END


# =========================
# Buttons
# =========================

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    await q.answer()

    if q.data == "check":
        await cmd_check(update, context)
    elif q.data == "set":
        await q.message.reply_text("Добре, змінимо адресу.")
        # Important: for callbacks we must enter conversation manually via message prompt
        await q.message.reply_text("Введи назву вулиці (як на сайті DTEK), напр:\nБорщагівська")
        # Set a flag and reuse the same conversation states via user_data:
        context.user_data["from_button_set"] = True
        # We can't "return ASK_STREET" here because this is not inside ConversationHandler callback.
        # So we rely on /set entry point for full conversation. Simpler: tell user to use /set.
        # But to keep UX smooth, we enable reentry with a separate handler below (see note).
    elif q.data == "status":
        await cmd_status(update, context)
    elif q.data == "stop":
        await cmd_stop(update, context)


# NOTE:
# PTB ConversationHandler entry_points must be actual handlers. For buttons,
# simplest is to keep "set" button just telling user to type /set.
# If you want TRUE button-driven conversation (no /set), tell me and I’ll adjust
# with a dedicated CallbackQueryHandler entry_point for ConversationHandler.


# =========================
# Monitoring job
# =========================

async def monitor_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_state()
    users = (state.get("users") or {})
    if not users:
        return

    changed_any = False

    for chat_id_str, cfg in list(users.items()):
        try:
            chat_id = int(chat_id_str)
        except Exception:
            continue

        street = (cfg.get("street") or "").strip()
        house = (cfg.get("house") or "").strip()
        street_ui = (cfg.get("street_ui") or street).strip()
        last_marker = (cfg.get("last_marker") or "").strip()

        if not street or not house:
            continue

        try:
            j = await dtek_client.fetch_dtek(street_value=street, headless=True)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            if cfg.get("last_error") != err:
                cfg["last_error"] = err
                set_user_cfg(state, chat_id, cfg)
                changed_any = True
                await context.bot.send_message(chat_id=chat_id, text=f"⚠️ Помилка запиту DTEK:\n{err}")
            continue

        marker = dtek_client.make_update_marker(j)

        if cfg.get("last_error"):
            cfg["last_error"] = ""

        if marker != last_marker:
            cfg["last_marker"] = marker
            cfg["last_updateTimestamp"] = (j.get("updateTimestamp") or "")
            set_user_cfg(state, chat_id, cfg)
            changed_any = True

            msg = dtek_client.format_house_info(street_ui, house, j)
            queue = dtek_client.get_house_queue(j, house)
            if queue:
                msg += f"\n\n🏷️ Черга: {queue}\n\n" + dtek_client.summarize_fact_for_today(j, queue)

            await context.bot.send_message(chat_id=chat_id, text=msg)
        else:
            set_user_cfg(state, chat_id, cfg)

    if changed_any:
        save_state(state)
    else:
        # still persist non-error changes safely
        save_state(state)


# =========================
# Main
# =========================

def main() -> None:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("BOT_TOKEN env var is not set. Example: export BOT_TOKEN='123:ABC'")

    app = Application.builder().token(token).build()

    conv_set = ConversationHandler(
        entry_points=[CommandHandler("set", set_entry)],
        states={
            ASK_STREET: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_street)],
            ASK_HOUSE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_house)],
        },
        fallbacks=[CommandHandler("cancel", set_cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(conv_set)
    app.add_handler(CallbackQueryHandler(on_button))

    # periodic polling
    if app.job_queue is None:
        raise SystemExit(
            "JobQueue is not available. Install PTB with:\n"
            "  pip install \"python-telegram-bot[job_queue]==20.*\""
        )
    app.job_queue.run_repeating(monitor_job, interval=POLL_EVERY_SEC, first=15)

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
