#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Optional, Any, Dict

from urllib.parse import urlencode
from playwright.async_api import async_playwright


BASE = "https://www.dtek-kem.com.ua"
PAGE = f"{BASE}/ua/shutdowns"
AJAX = f"{BASE}/ua/ajax"


def pretty(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


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


async def fetch_dtek(street_value: str, *, headless: bool = True, debug: bool = False) -> dict:
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

        update_fact = datetime.now().strftime("%d.%m.%Y+%H:%M")
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

        if debug:
            print("csrf:", "YES" if csrf else "NO")
            print("POST", AJAX)
            print("form(encoded):", urlencode(form))

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


def make_update_marker(j: dict) -> str:
    ut = (j.get("updateTimestamp") or "").strip()
    if ut:
        return f"updateTimestamp:{ut}"

    fact = j.get("fact") or {}
    fu = (fact.get("update") or fact.get("updateFact") or "").strip()
    if fu:
        return f"fact.update:{fu}"

    return "unknown"


# Optional: allow running this module as CLI checker
if __name__ == "__main__":
    import argparse

    async def _cli() -> None:
        parser = argparse.ArgumentParser(description="Fetch DTEK shutdown info for a house")
        parser.add_argument("-s", "--street", required=True, help="Street name (as in XHR), e.g. 'вул. Борщагівська'")
        parser.add_argument("-H", "--house", required=True, help="House number")
        parser.add_argument("--show-browser", action="store_true", help="Show browser window (disable headless)")
        args = parser.parse_args()
        
        street_value = "вул. " + args.street

        j = await fetch_dtek(args.street, headless=not args.show_browser)
        print(format_house_info(street_value, args.house, j))
        q = get_house_queue(j, args.house)
        print("\n🏷️ queue:", q or "—")
        if q:
            print("\n" + summarize_fact_for_today(j, q))

    asyncio.run(_cli())
