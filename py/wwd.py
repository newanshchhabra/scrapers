"""
wwd_scraper.py
~~~~~~~~~~~~~~
Scrapes recent articles from WWD (Women's Wear Daily) across four editorial
categories (fashion, footwear news, runway, design) and writes the results to
``wwd_scrape.json``.

Only articles published within the last six months are retained.
Each record in the output contains:
    - category      : source category slug
    - headline      : article headline text
    - date_posted   : ISO-8601 date string (YYYY-MM-DD)
    - url           : canonical article URL

Usage
-----
    python wwd_scraper.py

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

CATEGORIES: dict[str, str] = {
    "fashion":       "https://wwd.com/fashion-news/",
    "footwear_news": "https://wwd.com/footwear-news/",
    "runway":        "https://wwd.com/runway/",
    "design":        "https://wwd.com/design/",
}

CUTOFF_DATE: datetime = datetime.today() - relativedelta(months=6)
CONCURRENCY: int = 6
MAX_PAGES_PER_CATEGORY: int = 10

# User-agent string that mirrors a standard desktop browser to reduce blocking.
USER_AGENT: str = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# Date parsing helpers
# ---------------------------------------------------------------------------

_MONTHS = (
    "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec|"
    "January|February|March|April|May|June|July|August|"
    "September|October|November|December"
)

_DATE_RE = re.compile(
    rf"({_MONTHS})\.?\s+\d{{1,2}},\s+\d{{4}}",
    re.IGNORECASE,
)

_WHITESPACE_RE = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# Text utilities
# ---------------------------------------------------------------------------

def clean(text: str | None) -> str:
    """Collapse whitespace runs and strip leading/trailing space."""
    return _WHITESPACE_RE.sub(" ", text or "").strip()


def _normalize_month(raw: str) -> str:
    """Normalise ``Sept`` / ``Sept.`` variants to ``Sep`` for strptime."""
    return raw.replace("Sept.", "Sep.").replace("Sept", "Sep")


def extract_date(text: str | None) -> datetime | None:
    """
    Find the first ``Month DD, YYYY`` date in *text* and return it as a
    :class:`datetime`. Handles abbreviated and full month names, including
    the ``Sept`` variant used by WWD.

    Returns ``None`` if no parseable date is found.
    """
    match = _DATE_RE.search(text or "")
    if not match:
        return None

    raw = _normalize_month(match.group(0))
    for fmt in ("%B %d, %Y", "%b. %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue

    return None


def page_url(base: str, page_number: int) -> str:
    """Build a paginated category URL (page 1 is the bare base URL)."""
    return base if page_number == 1 else f"{base.rstrip('/')}/page/{page_number}/"


# ---------------------------------------------------------------------------
# URL collection
# ---------------------------------------------------------------------------

#: Category path prefixes that identify editorial article pages.
_ARTICLE_PREFIXES = (
    "/fashion-news/",
    "/footwear-news/",
    "/runway/",
    "/design/",
)

#: Path fragments that indicate non-article pages to be skipped.
_EXCLUDED_FRAGMENTS = (
    "/page/", "/author/", "/tag/", "/category/", "/video/", "/gallery/",
)

#: Exact paths of category index pages (not articles).
_INDEX_PATHS = {"/fashion-news/", "/footwear-news/", "/runway/", "/design/"}


def _is_article_href(href: str) -> bool:
    """Return True if *href* looks like a WWD article URL."""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(href)
        path = parsed.path

        if "wwd.com" not in parsed.netloc:
            return False
        if path in _INDEX_PATHS:
            return False
        if any(frag in path for frag in _EXCLUDED_FRAGMENTS):
            return False
        return any(path.startswith(prefix) for prefix in _ARTICLE_PREFIXES)
    except Exception:
        return False


async def collect_urls(browser) -> list[dict]:
    """
    Paginate through each category listing and collect unique article URLs.

    Parameters
    ----------
    browser:
        Playwright browser instance (no special context needed here).

    Returns
    -------
    list[dict]
        Each dict has keys ``url`` and ``category``.
    """
    seen: set[str] = set()
    articles: list[dict] = []

    page = await browser.new_page()

    for category, base_url in CATEGORIES.items():
        for page_number in range(1, MAX_PAGES_PER_CATEGORY + 1):
            try:
                url = page_url(base_url, page_number)
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                await page.wait_for_timeout(1_200)

                hrefs: list[str] = await page.locator("a[href]").evaluate_all(
                    "els => [...new Set(els.map(a => a.href))]"
                )

                new_count = 0
                for href in hrefs:
                    if _is_article_href(href) and href not in seen:
                        seen.add(href)
                        articles.append({"url": href, "category": category})
                        new_count += 1

                print(f"[{category}] page {page_number}: +{new_count} URLs")

            except Exception as exc:
                print(f"[WARN] Failed to load {page_url(base_url, page_number)}: {exc}")
                continue

    await page.close()
    print(f"Collected {len(articles)} candidate URLs across all categories")
    return articles


# ---------------------------------------------------------------------------
# Article scraping
# ---------------------------------------------------------------------------

#: JavaScript run inside each article page to extract the headline and date.
_EXTRACT_ARTICLE_JS = """
() => {
    const getText = sel => document.querySelector(sel)?.innerText?.trim() || null;

    const headline =
        getText("h1") ||
        getText(".article-title") ||
        getText("[class*='title']");

    const dateText =
        getText("time") ||
        document.querySelector("time")?.getAttribute("datetime") ||
        getText("[class*='date']") ||
        document.body.innerText;

    return { headline, dateText, bodyText: document.body.innerText };
}
"""


async def scrape_article(
    context, item: dict, sem: asyncio.Semaphore
) -> dict | None:
    """
    Fetch a single WWD article and extract its headline and publication date.

    Articles older than :data:`CUTOFF_DATE` or missing required fields are
    discarded (returns ``None``).

    Parameters
    ----------
    context:
        Playwright browser context (should include the desktop user-agent).
    item:
        Dict with ``url`` and ``category`` keys.
    sem:
        Semaphore used to cap the number of concurrent browser pages.
    """
    async with sem:
        page = await context.new_page()
        try:
            await page.goto(item["url"], wait_until="domcontentloaded", timeout=25_000)
            await page.wait_for_timeout(1_000)

            data: dict = await page.evaluate(_EXTRACT_ARTICLE_JS)

            # Prefer the dedicated date element; fall back to full body text
            date = extract_date(data["dateText"]) or extract_date(data["bodyText"])
            if not date or date < CUTOFF_DATE:
                return None

            headline = clean(data["headline"])
            if not headline:
                return None

            return {
                "category":    item["category"],
                "headline":    headline,
                "date_posted": date.strftime("%Y-%m-%d"),
                "url":         item["url"],
            }

        except Exception as exc:
            print(f"[ERROR] {item['url']} -> {exc}")
            return None
        finally:
            await page.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        urls = await collect_urls(browser)

        # Use a realistic user-agent to reduce the chance of being blocked
        context = await browser.new_context(user_agent=USER_AGENT)

        sem = asyncio.Semaphore(CONCURRENCY)
        tasks = [scrape_article(context, item, sem) for item in urls]
        results = await asyncio.gather(*tasks)

        await browser.close()

    articles = sorted(
        [r for r in results if r is not None],
        key=lambda x: x["date_posted"],
        reverse=True,
    )

    output_path = "wwd_scrape.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(articles, f, indent=2, ensure_ascii=False)

    print(f"Done — {len(articles)} articles written to {output_path}")


if __name__ == "__main__":
    asyncio.run(main())
