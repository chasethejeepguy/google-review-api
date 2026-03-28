"""
Google Reviews Scraper API — FastAPI + async Playwright
Deployed on Render.com free tier.

Concurrency: multiple users can call /scrape simultaneously.
Each request runs in its own isolated Playwright BrowserContext.
A semaphore caps concurrent live sessions to 2 so we stay within
Render's 512 MB free-tier RAM (each Chromium context ~150–200 MB).
Requests that exceed the cap are queued (not rejected) and run when
a slot opens — so every user eventually gets their results.
"""

import asyncio
import logging
import os
import re

from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from playwright.async_api import async_playwright, Browser, Playwright
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────────────────────
API_KEY        = os.environ.get("SCRAPER_API_KEY", "")
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT_SESSIONS", "10"))

# ── Place IDs per dealership ─────────────────────────────────────────────────
PLACE_IDS: dict[str, str] = {
    "cdjr":       "ChIJU-ekTAkBdogRNPmRORtsYRI",
    "mazda":      "ChIJR83zZwkBdogRBRdwXAI5xBg",
    "volkswagen": "ChIJNSv5uA4BdogRF0pUSb0wDyc",
    "nissan":     "ChIJ24-CkQ4BdogRVOHi_Ts67Zs",
    "kia":        "ChIJWRNgnAsBdogRN-dzaNQYX-Q",
    "ford_north": "ChIJ-WyJFD8BdogRQoedZYJRu5E",
    "ford_south": "ChIJ1XPabr4CdogRyDii6N3XhvU",
    "econo":      "ChIJ1XPabr4CdogRyDii6N3XhvU",
}

SEARCH_URLS: dict[str, str] = {
    "cdjr":       "https://www.google.com/maps/search/Auffenberg+Chrysler+Dodge+Jeep+Ram+Shiloh+IL/@38.573678,-89.9196448,17z",
    "mazda":      "https://www.google.com/maps/search/Auffenberg+Mazda+Shiloh+IL/@38.573678,-89.9196448,17z",
    "volkswagen": "https://www.google.com/maps/search/Auffenberg+Volkswagen+O+Fallon+IL/@38.565000,-89.910000,17z",
    "nissan":     "https://www.google.com/maps/search/Auffenberg+Nissan+O+Fallon+IL/@38.565000,-89.910000,17z",
    "kia":        "https://www.google.com/maps/search/Auffenberg+Kia+Shiloh+IL/@38.573678,-89.9196448,17z",
    "ford_north": "https://www.google.com/maps/search/Auffenberg+Ford+North+Shiloh+IL/@38.573678,-89.9196448,17z",
    "ford_south": "https://www.google.com/maps/search/Auffenberg+Ford+South/@38.600000,-89.980000,17z",
    "econo":      "https://www.google.com/maps/search/Auffenberg+Econo+Ford/@38.600000,-89.980000,17z",
}

# ── App-lifetime state ────────────────────────────────────────────────────────
_playwright: Playwright | None = None
_browser:    Browser    | None = None
_semaphore:  asyncio.Semaphore | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Launch one shared Chromium process at startup; reuse it for every request."""
    global _playwright, _browser, _semaphore

    log.info("Launching shared Chromium browser…")
    _playwright = await async_playwright().start()
    _browser    = await _playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
        ],
    )
    _semaphore  = asyncio.Semaphore(MAX_CONCURRENT)
    log.info(f"Browser ready. Max concurrent sessions: {MAX_CONCURRENT}")
    yield
    log.info("Closing browser…")
    await _browser.close()
    await _playwright.stop()


app = FastAPI(title="Google Review Scraper", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["*"],
)


# ── Pydantic request model ────────────────────────────────────────────────────
class ScrapeRequest(BaseModel):
    store:        str  = "cdjr"
    first_name:   str  = ""
    last_name:    str  = ""
    filter_5star: bool = True
    months_back:  int  = 120   # how far back to look (default 10 years)


def relative_date_to_months(date_str: str) -> float:
    """Convert Google's relative date string to approximate months."""
    if not date_str:
        return 0
    s = date_str.lower().strip()
    try:
        if "just now" in s or "moment" in s:
            return 0
        if re.search(r'\d', s):
            n = int(re.search(r'\d+', s).group())
        else:
            n = 1
        if "day"   in s: return n / 30
        if "week"  in s: return n * 7 / 30
        if "month" in s: return float(n)
        if "year"  in s: return n * 12
    except Exception:
        pass
    return 0


# ── Fuzzy name matching ───────────────────────────────────────────────────────
def fuzzy_name_match(text: str, first: str, last: str) -> bool:
    if not text or (not first and not last):
        return False
    text_l  = text.lower()
    first_l = first.lower().strip()
    last_l  = last.lower().strip()

    if first_l and first_l in text_l:
        return True
    if last_l and last_l in text_l:
        return True

    words = re.findall(r"[a-z']+", text_l)
    for w in words:
        for name in [n for n in [first_l, last_l] if len(n) >= 3]:
            if len(w) >= 3 and abs(len(w) - len(name)) <= 1:
                diffs = sum(
                    1 for a, b in zip(w.ljust(len(name)), name.ljust(len(w))) if a != b
                )
                if diffs <= 1:
                    return True
    return False


# ── Core scraping logic (runs inside one isolated BrowserContext) ─────────────
async def scrape_in_context(store: str, first: str, last: str, filter_5: bool, months_back: int = 120):
    debug: list[str] = []

    context = await _browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 800},
        locale="en-US",
    )
    # Hide Playwright's webdriver fingerprint
    await context.add_init_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
    )
    page = await context.new_page()

    try:
        # ── Step 1: Warm up on google.com (establishes session cookies) ──────
        log.info(f"[{store}] Warming up on google.com…")
        await page.goto("https://www.google.com", timeout=15000)
        await page.wait_for_timeout(2000)
        for sel in ["#L2AGLb", "#W0wltc", "button:has-text('Accept all')", "button:has-text('Reject all')"]:
            try:
                await page.click(sel, timeout=1500)
                await page.wait_for_timeout(500)
                break
            except Exception:
                pass
        debug.append("warmup: ok")

        # ── Step 2: Navigate to Maps search URL ──────────────────────────────
        search_url = SEARCH_URLS.get(store)
        if not search_url:
            return [], debug + ["no search URL for store"]

        log.info(f"[{store}] Navigating to {search_url[:80]}…")
        await page.goto(search_url, timeout=20000)
        await page.wait_for_timeout(5000)
        debug.append(f"maps nav: ok url={page.url[:80]}")

        # ── Step 3: Activate the Reviews tab ─────────────────────────────────
        reviews_activated = False

        # 3a: Click a [role="tab"] containing "review"
        try:
            tab = page.locator('[role="tab"]').filter(has_text=re.compile(r"review", re.I)).first
            await tab.click(timeout=4000)
            await page.wait_for_timeout(3000)
            reviews_activated = True
            debug.append("clicked reviews tab")
        except Exception:
            pass

        # 3b: Click any button/link whose label contains "Reviews"
        if not reviews_activated:
            for sel in [
                "button:has-text('Reviews')",
                "a:has-text('Reviews')",
                "a:has-text('See all reviews')",
            ]:
                try:
                    await page.click(sel, timeout=2000)
                    await page.wait_for_timeout(3000)
                    reviews_activated = True
                    debug.append(f"clicked: {sel}")
                    break
                except Exception:
                    pass

        # 3c: URL transformation — append !9m1!1b1 to the data param
        if not reviews_activated:
            cur = page.url.split("?")[0]
            if "/maps/place/" in cur and "!9m1!1b1" not in cur:
                if "/data=" in cur:
                    base, data = cur.split("/data=", 1)
                    data = re.sub(r"!9m\d+(!1b[01])*", "", data)
                    data = re.sub(r"!4m(\d+)", lambda m: f"!4m{int(m.group(1))+2}", data)
                    data = re.sub(r"!3m(\d+)", lambda m: f"!3m{int(m.group(1))+2}", data)
                    new_url = base + "/data=" + data + "!9m1!1b1"
                else:
                    new_url = cur + "/data=!9m1!1b1"
                log.info(f"[{store}] URL transform → {new_url[:100]}")
                await page.goto(new_url, timeout=20000)
                await page.wait_for_timeout(6000)
                debug.append(f"url-transform: {new_url[:80]}")
            elif "/maps/search/" in cur:
                # Click first result card on search page
                for card_sel in ["[class*='Nv2PK'] a", "[class*='hfpxzc']", "a[href*='/maps/place/']"]:
                    try:
                        await page.click(card_sel, timeout=3000)
                        await page.wait_for_timeout(5000)
                        debug.append(f"clicked result card: {card_sel}")
                        break
                    except Exception:
                        pass

        # ── Step 4: Scroll repeatedly to load ALL reviews ────────────────────
        # Use scroll_into_view on the last card — most reliable way to trigger
        # Google Maps infinite scroll regardless of panel selector changes.
        stable_passes = 0
        for scroll_pass in range(50):
            current_cards = await page.query_selector_all("div[data-review-id]")
            cur_count = len(current_cards)

            # Strategy A: scroll last card into view (triggers lazy load)
            if current_cards:
                try:
                    await current_cards[-1].scroll_into_view_if_needed()
                except Exception:
                    pass

            # Strategy B: scroll inner reviews panel
            for sel in [
                ".m6QErb.DxyBCb.kA9KIf.dS8AEf",
                ".m6QErb.WNBkOb",
                ".m6QErb",
                "[role='feed']",
                "[data-hveid] [tabindex]",
            ]:
                try:
                    el = await page.query_selector(sel)
                    if el:
                        await page.evaluate("el => { el.scrollTop += 3000; }", el)
                        break
                except Exception:
                    pass

            await page.wait_for_timeout(2500)

            new_count = len(await page.query_selector_all("div[data-review-id]"))
            debug.append(f"scroll {scroll_pass+1}: {cur_count}→{new_count} cards")

            if new_count == cur_count:
                stable_passes += 1
                if stable_passes >= 4:
                    debug.append("count stable 4x — all reviews loaded")
                    break
            else:
                stable_passes = 0

        # ── Step 5: Expand "See more" and extract review cards ───────────────
        for btn_sel in [
            "button[aria-label*='See more']",
            "button[jsaction*='expandReview']",
            "[class*='w8nwRe']",
        ]:
            try:
                for btn in await page.query_selector_all(btn_sel):
                    try:
                        await btn.click()
                    except Exception:
                        pass
                await page.wait_for_timeout(800)
            except Exception:
                pass

        cards = await page.query_selector_all("div[data-review-id]")
        log.info(f"[{store}] Found {len(cards)} cards in DOM")
        debug.append(f"cards in DOM: {len(cards)}")

        all_reviews = []
        for card in cards:
            try:
                review_id = await card.get_attribute("data-review-id") or ""

                # Rating
                rating = 0
                for el in await card.query_selector_all("[aria-label]"):
                    lbl = await el.get_attribute("aria-label") or ""
                    m = re.search(r"(\d)\s*star", lbl, re.I) or re.search(r"(\d)\s*out of 5", lbl, re.I)
                    if m:
                        rating = int(m.group(1))
                        break

                # Text
                text = ""
                for sel in [".wiI7pd", ".MyEned span", "[data-expandable-section] span", "[class*='review-full-text']"]:
                    el = await card.query_selector(sel)
                    if el:
                        t = (await el.inner_text() or "").strip()
                        if len(t) > 15:
                            text = t
                            break
                if not text:
                    longest = ""
                    for s in await card.query_selector_all("span"):
                        t = (await s.inner_text() or "").strip()
                        if len(t) > len(longest):
                            longest = t
                    text = longest

                # Name
                name = ""
                for sel in [".d4r55", ".X43Kjb", "[class*='reviewer']"]:
                    el = await card.query_selector(sel)
                    if el:
                        name = (await el.inner_text() or "").strip()
                        break

                # Photo
                photo = ""
                el = await card.query_selector("img[src*='googleusercontent']") or await card.query_selector("img[src*='ggpht']")
                if el:
                    photo = await el.get_attribute("src") or ""

                # Date
                date = ""
                el = await card.query_selector(".rsqaWe") or await card.query_selector(".DU9Pgb")
                if el:
                    date = (await el.inner_text() or "").strip()

                if review_id and (len(text) > 10 or rating > 0):
                    all_reviews.append({
                        "id":            review_id,
                        "reviewer":      name or "Google Reviewer",
                        "reviewerPhoto": photo,
                        "rating":        rating,
                        "text":          text,
                        "date":          date,
                        "customText":    None,
                        "deleted":       False,
                    })
            except Exception as e:
                log.warning(f"Card parse error: {e}")

        # ── Step 6: Filter ────────────────────────────────────────────────────
        # Remove any review containing "horrible" (case-insensitive)
        all_reviews = [r for r in all_reviews if "horrible" not in r["text"].lower()]

        # Filter by months_back
        if months_back > 0:
            all_reviews = [r for r in all_reviews
                           if relative_date_to_months(r["date"]) <= months_back]

        if filter_5 and (first or last):
            matched = [r for r in all_reviews if r["rating"] == 5 and fuzzy_name_match(r["text"], first, last)]
        elif filter_5:
            matched = [r for r in all_reviews if r["rating"] == 5]
        else:
            matched = all_reviews

        debug.append(f"total={len(all_reviews)} matched={len(matched)}")
        log.info(f"[{store}] {first} {last}: total={len(all_reviews)} matched={len(matched)}")
        return matched, debug

    finally:
        await context.close()


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "service": "google-review-scraper", "max_concurrent": MAX_CONCURRENT}


@app.post("/scrape")
async def scrape(body: ScrapeRequest):
    if body.store not in PLACE_IDS:
        raise HTTPException(status_code=400, detail=f"Unknown store: {body.store}")

    log.info(f"Scrape request: store={body.store} name={body.first_name} {body.last_name}")

    # Acquire a slot — waits if all slots are busy (never rejects; queues the request)
    async with _semaphore:
        try:
            reviews, debug = await scrape_in_context(
                body.store, body.first_name, body.last_name, body.filter_5star, body.months_back
            )
            return {
                "success":   True,
                "total":     len(reviews),
                "matched":   len(reviews),
                "reviews":   reviews,
                "nav_debug": debug,
            }
        except Exception as e:
            log.error(f"Scrape failed: {e}")
            raise HTTPException(status_code=500, detail=str(e))
