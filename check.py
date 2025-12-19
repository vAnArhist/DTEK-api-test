#!/usr/bin/env python3
"""
bot.py — Telegram bot that monitors DTEK KEM shutdowns for user-selected address
and notifies when the site updates.

Features:
- /set                     -> bot asks street -> then house (2-step)
- /status                  -> show saved address + last seen updateTimestamp
- /stop                    -> stop monitoring and forget address
- Periodic polling (default: every 5 minutes) using Playwright (Incapsula/CSRF safe)

Requirements:
  pip install "python-telegram-bot[job_queue]==20.*" playwright
  playwright install

Run:
  export BOT_TOKEN="123456:ABC..."
  python3 bot.py
"""

from __future__ import annotations

import os
import json
from datetime import datetime
from typing import Any, Dict, Optional

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from playwright.async_api import async_playwright

# --- DTEK endpoints ---
BASE = "https://www.dtek-kem.com.ua"
PAGE = f"{BASE}/ua/shutdowns"
AJAX = f"{BASE}/ua/ajax"

# --- bot storage ---
STATE_FILE = "bot_state.json"

# --- polling interval (seconds) ---
POLL_EVERY_SEC = int(os.getenv("POLL_EVERY_SEC", "300"))  # 5 min default

# --- conversation states (/set) ---
SET_STREET = 1
SET_HOUSE = 2

# --- temp key in PTB user_data ---
TMP_STREET = "tmp_set_street"


# =========================
# Helpers: storage
# =========================

def _load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _save_state(state: Dict[str, Any]) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def _get_user_cfg(state: Dict[str, Any], chat_id: int) -> Dict[str, Any]:
    return (state.get("users") or {}).get(str(chat_id)) or {}


def _set_user_cfg(state: Dict[str, Any], chat_id: int, cfg: Dict[str, Any]) -> None:
    state.setdefault("users", {})
    state["users"][str(chat_id)] = cfg


def _del_user_cfg(state: Dict[str, Any], chat_id: int) -> None:
    users = state.get("users") or {}
    users.pop(str(chat_id), None)
    state["users"] = users


# =========================
# Helpers: formatting / parsing
# =========================

def _norm_street(s: str) -> str:
    return " ".join((s or "").strip().split())


def _norm_house(s: str) -> str:
    return (s or "").strip()


def _valid_house(h: str) -> bool:
    h = _norm_house(h)
    return bool(h) and any(c.isdigit() for c in h) and len(h) <= 16


def format_house_info(street_ui: str, house: str, j: dict) -> str:
    if not isinstance(j, dict):
        return f"❌ Некоректна відповідь (не dict): {type(j)}"

    if not j.get("result"):
        return f"❌ Помилка: {j.get('text', 'unknown')}"

    m = j.get("data") or {}
    if house not in m:
        sample = ", ".join(list(m.keys())[:15])
        return f"⚠️ Будинок {house} не знайдено. Приклади ключів: {sample} ..."

    item = m[house] or {}
    reasons = ", ".join(item.get("sub_type_reason") or [])
    upd = j.get("updateTimestamp", "")

    return (
        f"🔌 {street_ui}, {house}\n"
        f"Тип: {reasons or '—'}\n"
        f"Оновлено: {upd or '—'}"
    )


def get_house_queue(j: dict, house: str) -> Optional[str]:
    """Returns queue code for house, e.g. 'GPV1.1'."""
    m = j.get("data") or {}
    item = m.get(house) or {}
    reasons = item.get("sub_type_reason") or []
    return reasons[0] if reasons else None


def summarize_fact_for_today(j: dict, queue: str) -> str:
    fact = (j.get("fact") or {})
    preset = (j.get("preset") or {})
    tz = preset.get("time_zone") or {}
    time_type = preset.get("time_type") or {}

    today_ts = fact.get("today")
    data = (fact.get("data") or {})

    # sometimes keys are int, sometimes str
    fact_day = data.get(str(today_ts)) or data.get(today_ts) or {}
    hours = (fact_day.get(queue) or {})

    if not today_ts or not hours:
        return "ℹ️ Немає fact-даних на сьогодні для цієї черги."

    lines = []
    for h in range(1, 25):
        key = str(h)
        slot = (tz.get(key) or [f"{h:02d}?"])[0]
        code = hours.get(key, "—")
        human = time_type.get(code, code)
        lines.append(f"{slot}: {code} ({human})")
    return "📌 FACT (сьогодні):\n" + "\n".join(lines)


# =========================
# DTEK fetch (Playwright)
# =========================

async def fetch_dtek(street_value: str, *, headless: bool = True) -> dict:
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        ctx = await browser.new_context(locale="uk-UA")
        page = await ctx.new_page()

        await page.goto(PAGE, wait_until="networkidle")
        await page.wait_for_timeout(1200)

        csrf = await page.evaluate(
            """() => {
                const m = document.querySelector('meta[name="csrf-token"]');
                if (m && m.content) return m.content;
                if (window.yii && window.yii.getCsrfToken) return window.yii.getCsrfToken();
                return window.csrfToken || window._csrfToken || null;
            }"""
        )

        update_fact = datetime.now().strftime("%d.%m.%Y+%H:%M")  # like XHR
        form = {
            "method": "getHomeNum",
            "data[0][name]": "street",
            "data[0][value]": street_value,
            "data[1][name]": "updateFact",
            "data[1][value]": update_fact,
        }

        headers = {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": BASE,
            "Referer": PAGE,
        }
        if csrf:
            headers["X-CSRF-Token"] = csrf

        resp = await page.request.post(AJAX, form=form, headers=headers)
        ct = resp.headers.get("content-type", "")
        text = await resp.text()

        try:
            j = await resp.json()
        except Exception:
            await browser.close()
            raise RuntimeError(
                f"Server returned non-JSON. Status={resp.status}, ct={ct}, body_snip={text[:200]}"
            )

        await browser.close()
        return j


def _make_update_marker(j: dict) -> str:
    """
    We notify when this marker changes.
    Primary: updateTimestamp (e.g. '16:33 19.12.2025')
    Fallback: fact.update or updateFact fields if present.
    """
    if isinstance(j, dict):
        ut = (j.get("updateTimestamp") or "").strip()
        if ut:
            return f"updateTimestamp:{ut}"

        fact = j.get("fact") or {}
        fu = (fact.get("update") or fact.get("updateFact") or "").strip()
        if fu:
            return f"fact.update:{fu}"

    return "unknown"


# =========================
# Monitoring job
# =========================

async def monitor_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Runs periodically. For each user in STATE_FILE:
      - fetch DTEK
      - compute marker
      - if changed -> send notification
    """
    state = _load_state()
    users = (state.get("users") or {})

    if not users:
        return

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
            j = await fetch_dtek(street_value=street, headless=True)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            if cfg.get("last_error") != err:
                cfg["last_error"] = err
                _set_user_cfg(state, chat_id, cfg)
                _save_state(state)
                await context.bot.send_message(chat_id=chat_id, text=f"⚠️ Помилка запиту DTEK:\n{err}")
            continue

        marker = _make_update_marker(j)

        # clear error if recovered
        if cfg.get("last_error"):
            cfg["last_error"] = ""

        if marker != last_marker:
            cfg["last_marker"] = marker
            cfg["last_updateTimestamp"] = (j.get("updateTimestamp") or "")
            _set_user_cfg(state, chat_id, cfg)
            _save_state(state)

            msg = format_house_info(street_ui, house, j)

            queue = get_house_queue(j, house)
            if queue:
                msg += f"\n\n🏷️ Черга: {queue}\n\n" + summarize_fact_for_today(j, queue)

            await context.bot.send_message(chat_id=chat_id, text=msg)
        else:
            # persist recovery + keep cfg saved
            _set_user_cfg(state, chat_id, cfg)
            _save_state(state)


# =========================
# Telegram commands
# =========================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "Привіт! Я моніторю оновлення графіка DTEK і напишу тобі, коли дані оновляться.\n\n"
        "Команди:\n"
        "  /set     — задати адресу (вулиця → будинок)\n"
        "  /status  — показати налаштування\n"
        "  /stop    — зупинити і забути адресу\n\n"
        f"Перевірка кожні {POLL_EVERY_SEC // 60} хв."
    )
    if update.message:
        await update.message.reply_text(text)


async def set_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message:
        return ConversationHandler.END
    # reset temp
    context.user_data.pop(TMP_STREET, None)
    await update.message.reply_text("Введи назву вулиці (як на сайті DTEK):")
    return SET_STREET


async def set_street(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message:
        return ConversationHandler.END

    street = _norm_street(update.message.text)
    if len(street) < 3:
        await update.message.reply_text("Некоректна вулиця, спробуй ще раз:")
        return SET_STREET

    context.user_data[TMP_STREET] = street
    await update.message.reply_text("Тепер введи номер будинку:")
    return SET_HOUSE


async def set_house(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message:
        return ConversationHandler.END

    house = _norm_house(update.message.text)
    if not _valid_house(house):
        await update.message.reply_text("Некоректний номер будинку, спробуй ще раз:")
        return SET_HOUSE

    street = (context.user_data.get(TMP_STREET) or "").strip()
    if not street:
        await update.message.reply_text("Щось пішло не так. Почни з /set ще раз.")
        return ConversationHandler.END

    chat_id = update.message.chat_id

    # Save config
    state = _load_state()
    cfg = _get_user_cfg(state, chat_id)

    cfg["street"] = street
    cfg["street_ui"] = street
    cfg["house"] = house
    cfg["last_marker"] = ""          # force notify on next poll
    cfg["last_updateTimestamp"] = ""
    cfg["last_error"] = ""

    _set_user_cfg(state, chat_id, cfg)
    _save_state(state)

    # cleanup temp
    context.user_data.pop(TMP_STREET, None)

    await update.message.reply_text(
        f"✅ Збережено:\n{street}, {house}\n"
        f"Я напишу, коли оновиться інформація на сайті.\n"
        f"(перевірка кожні {POLL_EVERY_SEC // 60} хв)"
    )

    # Do an immediate fetch to confirm + show current state
    await update.message.reply_text("⏳ Перевіряю зараз...")
    try:
        j = await fetch_dtek(street_value=street, headless=True)
        marker = _make_update_marker(j)
        cfg["last_marker"] = marker
        cfg["last_updateTimestamp"] = (j.get("updateTimestamp") or "")
        _set_user_cfg(state, chat_id, cfg)
        _save_state(state)

        msg = format_house_info(street, house, j)
        queue = get_house_queue(j, house)
        if queue:
            msg += f"\n\n🏷️ Черга: {queue}\n\n" + summarize_fact_for_today(j, queue)

        await update.message.reply_text(msg)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Не вдалося отримати дані одразу:\n{type(e).__name__}: {e}")

    return ConversationHandler.END


async def set_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message:
        await update.message.reply_text("Скасовано.")
    context.user_data.pop(TMP_STREET, None)
    return ConversationHandler.END


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    chat_id = update.message.chat_id
    state = _load_state()
    cfg = _get_user_cfg(state, chat_id)

    if not cfg:
        await update.message.reply_text("Налаштувань ще немає. Використай /set.")
        return

    street = cfg.get("street") or "—"
    house = cfg.get("house") or "—"
    last_ut = cfg.get("last_updateTimestamp") or "—"
    last_err = (cfg.get("last_error") or "").strip()

    msg = (
        f"📍 Адреса: {street}, {house}\n"
        f"🕒 Останнє оновлення (updateTimestamp): {last_ut}\n"
        f"⏱️ Перевірка: кожні {POLL_EVERY_SEC // 60} хв"
    )
    if last_err:
        msg += f"\n⚠️ Остання помилка: {last_err}"

    await update.message.reply_text(msg)


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    chat_id = update.message.chat_id

    state = _load_state()
    if not _get_user_cfg(state, chat_id):
        await update.message.reply_text("Моніторинг і так не налаштований.")
        return

    _del_user_cfg(state, chat_id)
    _save_state(state)

    await update.message.reply_text("🛑 Ок, зупинив моніторинг і забув адресу.")


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
            SET_STREET: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_street)],
            SET_HOUSE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_house)],
        },
        fallbacks=[CommandHandler("cancel", set_cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(conv_set)
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("stop", cmd_stop))

    # periodic job
    if not app.job_queue:
        raise SystemExit(
            "JobQueue is not available. Install PTB with:\n"
            "  pip install \"python-telegram-bot[job_queue]==20.*\""
        )
    app.job_queue.run_repeating(monitor_job, interval=POLL_EVERY_SEC, first=15)

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
