import time
from datetime import datetime
from typing import Optional, Dict, Any

from playwright.async_api import async_playwright, Browser, BrowserContext, Page


BASE = "https://www.dtek-kem.com.ua"
PAGE_URL = BASE + "/ua/shutdowns"
AJAX_URL = BASE + "/ua/ajax"


class DtekClient:
    def __init__(self, cache_ttl_sec: int = 120):
        self._cache_ttl = cache_ttl_sec
        self._cache: Dict[str, tuple[float, Dict[str, Any]]] = {}  # street -> (ts, json)

        self._playwright = None
        self._browser: Optional[Browser] = None
        self._ctx: Optional[BrowserContext] = None
        self._page: Optional[Page] = None

    async def start(self) -> None:
        if self._browser:
            return
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        self._ctx = await self._browser.new_context(locale="uk-UA")
        self._page = await self._ctx.new_page()
        await self._page.goto(PAGE_URL, wait_until="networkidle")
        await self._page.wait_for_timeout(1200)

    async def stop(self) -> None:
        try:
            if self._browser:
                await self._browser.close()
        finally:
            self._browser = None
            self._ctx = None
            self._page = None
            if self._playwright:
                await self._playwright.stop()
            self._playwright = None

    async def _detect_csrf(self) -> Optional[str]:
        if not self._page:
            return None
        return await self._page.evaluate("""() => {
            const m = document.querySelector('meta[name="csrf-token"]');
            if (m && m.content) return m.content;
            if (window.yii && window.yii.getCsrfToken) return window.yii.getCsrfToken();
            return window.csrfToken || window._csrfToken || null;
        }""")

    async def fetch_street_data(self, street_text: str) -> Dict[str, Any]:
        """
        street_text: Напр. "вул. Борщагівська" (без '+' вручну)
        """
        now = time.time()
        cached = self._cache.get(street_text)
        if cached and (now - cached[0]) < self._cache_ttl:
            return cached[1]

        await self.start()
        assert self._page is not None

        csrf = await self._detect_csrf()
        update_fact = datetime.now().strftime("%d.%m.%Y+%H:%M")

        form = {
            "method": "getHomeNum",
            "data[0][name]": "street",
            "data[0][value]": street_text,
            "data[1][name]": "updateFact",
            "data[1][value]": update_fact,
        }
        headers = {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": BASE,
            "Referer": PAGE_URL,
        }
        if csrf:
            headers["X-CSRF-Token"] = csrf

        resp = await self._page.request.post(AJAX_URL, form=form, headers=headers)
        j = await resp.json()

        self._cache[street_text] = (now, j)
        return j
