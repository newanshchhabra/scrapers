"""
032c_scraper.py
~~~~~~~~~~~~~~~
Scrapes recent articles from 032c Magazine and writes the results to
``032c_scrape.json``.

Only articles published within the last six months are retained.
Each record in the output contains:
    - headline      : article headline text
    - date_posted   : ISO-8601 date string (YYYY-MM-DD)
    - url           : canonical article URL

Usage
-----
    python 032c_scraper.py

Dependencies
------------
    playwright, python-dateutil
    playwright install chromium
"""

import asyncio
import json
import re
from datetime import datetime

from dateutil.relativedelta import relativedelta
from playwright.async_api import async_playwright

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE: str = "https://magazine.032c.com/magazine"

CUTOFF_DATE: datetime = datetime.today() - relativedelta(months=6)
CONCURRENCY: int = 6
MAX_PAGES: int = 7

# ---------------------------------------------------------------------------
# Date parsing helpers
# ---------------------------------------------------------------------------

_MONTHS = (
    "January|February|March|April|May|June|"
    "July|August|September|October|November|December"
)

_DATE_RE = re.compile(rf"({_MONTHS})\s+\d{{1,2}},\s+\d{{4}}")

_WHITESPACE_RE = re.compile(r"\s+")

# ---------------------------------------------------------------------------
# Text utilities
# ---------------------------------------------------------------------------

def clean(text: str) -> str:
    """Collapse whitespace runs and strip leading/trailing space."""
    return _WHITESPACE_RE.sub(" ", text).strip()


def extract_date(text: str) -> datetime | None:
    """
    Find the first ``Month DD, YYYY`` date in *text* and return it as a
    :class:`datetime`. Returns ``None`` if no match is found.
    """
    match = _DATE_RE.search(text)
    return datetime.strptime(match.group(0), "%B %d, %Y") if match else None


def page_url(page_number: int) -> str:
    """Build a paginated magazine index URL (page 1 is the bare base URL)."""
    return BASE if page_number == 1 else f"{BASE}?Article%5Bpage%5D={page_number}"


# ---------------------------------------------------------------------------
# URL collection
# ---------------------------------------------------------------------------

#: JavaScript executed inside the browser to extract article hrefs from a
#: listing page, filtering out category indexes and paginated variants.
_COLLECT_URLS_JS = """
els => [...new Set(
    els.map(a => a.href).filter(h => {
        const url   = new URL(h);
        const parts = url.pathname.replace(/\\/+$/, "").split("/");
        return (
            url.pathname.startsWith("/magazine/") &&
            parts.length >= 3 &&
            parts[2].length > 0 &&
            parts[2] !== "category" &&
            !url.search.includes("page")
        );
    })
)]
"""


async def collect_urls(browser) -> list[str]:
    """
    Paginate through the 032c magazine index and return unique article URLs.

    Parameters
    ----------
    browser:
        Playwright browser instance.

    Returns
    -------
    list[str]
        Deduplicated article URLs found across all listing pages.
    """
    seen: set[str] = set()
    urls: list[str] = []

    page = await browser.new_page()

    for n in range(1, MAX_PAGES + 1):
        await page.goto(page_url(n), wait_until="networkidle", timeout=30_000)
        await page.wait_for_timeout(1_500)

        batch: list[str] = await page.locator("a[href*='/magazine/']").evaluate_all(
            _COLLECT_URLS_JS
        )

        new_urls = [u for u in batch if u not in seen]
        seen.update(new_urls)
        urls.extend(new_urls)

        print(f"[index] page {n}: +{len(new_urls)} URLs ({len(urls)} total)")

    await page.close()
    return urls


# ---------------------------------------------------------------------------
# Article scraping
# ---------------------------------------------------------------------------

#: JavaScript run inside each article page to extract the headline and body text.
_EXTRACT_ARTICLE_JS = """
() => ({
    headline: document.querySelector("h1")?.innerText?.trim() ?? null,
    bodyText: document.body.innerText
})
"""


async def scrape_article(
    context, url: str, sem: asyncio.Semaphore
) -> dict | None:
    """
    Fetch a single 032c article and extract its headline and publication date.

    Articles older than :data:`CUTOFF_DATE` or missing required fields are
    discarded (returns ``None``).

    Parameters
    ----------
    context:
        Playwright browser context.
    url:
        Full article URL to scrape.
    sem:
        Semaphore used to cap the number of concurrent browser pages.
    """
    async with sem:
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="networkidle", timeout=20_000)
            await page.wait_for_timeout(800)

            result: dict = await page.evaluate(_EXTRACT_ARTICLE_JS)

            date = extract_date(result["bodyText"])
            if not date or date < CUTOFF_DATE:
                return None

            headline = clean(result["headline"]) if result["headline"] else None
            if not headline:
                return None

            return {
                "headline":    headline,
                "date_posted": date.strftime("%Y-%m-%d"),
                "url":         url,
            }

        except Exception as exc:
            print(f"[ERROR] {url} -> {exc}")
            return None
        finally:
            await page.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    start = datetime.now()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        urls = await collect_urls(browser)

        context = await browser.new_context()
        sem = asyncio.Semaphore(CONCURRENCY)

        tasks = [scrape_article(context, url, sem) for url in urls]
        results = await asyncio.gather(*tasks)

        await browser.close()

    articles = sorted(
        [r for r in results if r is not None],
        key=lambda a: a["date_posted"],
        reverse=True,
    )

    output_path = "032c_scrape.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(articles, f, indent=2, ensure_ascii=False)

    elapsed = (datetime.now() - start).total_seconds()
    print(f"Done — {len(articles)} articles written to {output_path} ({elapsed:.1f}s)")


if __name__ == "__main__":
    asyncio.run(main())

