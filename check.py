# Only for checking concept that we can obtain it

#!/usr/bin/env python3
import asyncio
import json
from datetime import datetime
import argparse
from urllib.parse import urlencode

from playwright.async_api import async_playwright

BASE = "https://www.dtek-kem.com.ua"
PAGE = f"{BASE}/ua/shutdowns"
AJAX = f"{BASE}/ua/ajax"

def pretty(obj) -> str:
    """Compact pretty JSON for console."""
    return json.dumps(obj, ensure_ascii=False, indent=2)


def format_house_info(street: str, house: str, j: dict) -> str:
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
        f"🔌 {street}, {house}\n"
        f"Тип: {reasons or '—'}\n"
        f"Оновлено: {upd or '—'}"
    )


def get_house_queue(j: dict, house: str) -> str | None:
    """Returns GPV queue for house, e.g. 'GPV1.1'."""
    m = j.get("data") or {}
    item = m.get(house) or {}
    reasons = item.get("sub_type_reason") or []
    return reasons[0] if reasons else None


def summarize_fact_for_today(j: dict, queue: str) -> str:
    """
    Shows 24h statuses from fact for today for selected queue.
    Output format: 00-01: yes | 01-02: no | ...
    """
    fact = (j.get("fact") or {})
    preset = (j.get("preset") or {})
    tz = preset.get("time_zone") or {}
    time_type = preset.get("time_type") or {}

    today_ts = fact.get("today")
    fact_data = (fact.get("data") or {}).get(str(today_ts)) or (fact.get("data") or {}).get(today_ts) or {}
    hours = (fact_data.get(queue) or {})

    if not today_ts or not hours:
        return "ℹ️ Немає fact-даних на сьогодні для цієї черги."

    lines = []
    for h in range(1, 25):
        key = str(h)
        slot = tz.get(key, [f"{h:02d}?", "", ""])[0]
        code = hours.get(key, "—")
        human = time_type.get(code, code)
        lines.append(f"{slot}: {code} ({human})")
    return "📌 FACT (сьогодні):\n" + "\n".join(lines)


async def fetch(street_value: str, *, headless: bool = True) -> dict:
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,  # для дебагу зручно False
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        ctx = await browser.new_context(locale="uk-UA")
        page = await ctx.new_page()

        await page.goto(PAGE, wait_until="networkidle")
        # Дай сторінці секунду “дожити” (інколи токени/скрипти дозавантажуються)
        await page.wait_for_timeout(1200)

        csrf = await page.evaluate(
            """() => {
                const m = document.querySelector('meta[name="csrf-token"]');
                if (m && m.content) return m.content;
                if (window.yii && window.yii.getCsrfToken) return window.yii.getCsrfToken();
                return window.csrfToken || window._csrfToken || null;
            }"""
        )
        print("csrf:", "YES" if csrf else "NO")

        update_fact = datetime.now().strftime("%d.%m.%Y+%H:%M")  # як у XHR
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

        # Лог того, що реально відправляємо
        print("POST", AJAX)
        print("form(encoded):", urlencode(form))

        resp = await page.request.post(AJAX, form=form, headers=headers)

        ct = resp.headers.get("content-type", "")
        print("status:", resp.status, "ct:", ct)

        text = await resp.text()
        print("snippet:", text[:200].replace("\n", "\\n"))

        # Якщо відповідь не JSON — викинемо зрозумілу помилку
        try:
            j = await resp.json()
        except Exception:
            await browser.close()
            raise RuntimeError(f"Server returned non-JSON. Status={resp.status}, ct={ct}, body_snip={text[:200]}")

        await browser.close()
        return j


async def main():
    parser = argparse.ArgumentParser(description="Fetch DTEK shutdown info for a house")
    parser.add_argument("-s", "--street", help="Street name (as in XHR)")
    parser.add_argument("-H", "--house", help="House number")
    parser.add_argument("--show-browser", action="store_true", help="Show browser window (disable headless)")
    args = parser.parse_args()

    street_value = "вул. " + args.street
    house = args.house # or HOUSE

    # Ретраї (інколи 1-й може дати Error через антибот/таймінги)
    last = None
    for i in range(3):
        print(f"\n=== attempt {i + 1}/3 ===")
        j = await fetch(street_value, headless=not args.show_browser)
        last = j
        if j.get("result") is True:
            break
        await asyncio.sleep(2)

    # 1) короткий summary по будинку
    print("\n" + format_house_info(street_value, house, last))

    # 2) компактний debug: які будинки повернуло (перші 20 ключів)
    keys = list((last.get("data") or {}).keys())
    print("\n📦 data keys (first 20):", ", ".join(keys[:20]) + (" ..." if len(keys) > 20 else ""))

    # 3) покажемо queue + fact на сьогодні для цієї черги
    queue = get_house_queue(last, house)
    print("\n🏷️ queue:", queue or "—")
    if queue:
        print("\n" + summarize_fact_for_today(last, queue))

    # 4) якщо треба — показати весь JSON (але не по дефолту)
    # print("\nFULL JSON:\n", pretty(last))


if __name__ == "__main__":
    asyncio.run(main())
