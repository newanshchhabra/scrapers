"""
dazed_scraper.py
~~~~~~~~~~~~~~~~
Scrapes recent articles from Dazed Digital across four editorial sections
(fashion, film-tv, music, life-culture) and writes the results to
``dazed_scrape.json``.

Only articles published within the last six months are retained.
Each record in the output contains:
    - section       : source section slug
    - bucket        : "latest" or "trending"
    - headline      : article headline text
    - date_posted   : ISO-8601 date string (YYYY-MM-DD)
    - url           : canonical article URL

Usage
-----
    python dazed_scraper.py

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
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SECTIONS: dict[str, str] = {
    "fashion":      "https://www.dazeddigital.com/fashion",
    "film-tv":      "https://www.dazeddigital.com/film-tv",
    "music":        "https://www.dazeddigital.com/music",
    "life-culture": "https://www.dazeddigital.com/life-culture",
}

CUTOFF_DATE: datetime = datetime.today() - relativedelta(months=6)
CONCURRENCY: int = 4
MAX_LATEST_PER_SECTION: int = 18  # Dazed section pages expose ~18 Latest links

# ---------------------------------------------------------------------------
# Date parsing helpers
# ---------------------------------------------------------------------------

_MONTH_PATTERN = (
    r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
    r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?"
)

_DATE_RE = re.compile(
    rf"\b({_MONTH_PATTERN})\s+\d{{1,2}},\s+\d{{4}}\b",
    re.IGNORECASE,
)

_ARTICLE_URL_RE = re.compile(
    r"^https://www\.dazeddigital\.com/[^/]+/article/\d+/"
)

_WHITESPACE_RE = re.compile(r"\s+")

# Unicode non-breaking / narrow spaces that appear in scraped text
_UNICODE_SPACES = str.maketrans({"\xa0": " ", "\u202f": " ", "\u2009": " "})


# ---------------------------------------------------------------------------
# Text utilities
# ---------------------------------------------------------------------------

def clean(text: str) -> str:
    """Collapse all whitespace runs to a single space and strip edges."""
    return _WHITESPACE_RE.sub(" ", text).strip()


def normalize_spaces(text: str) -> str:
    """Replace Unicode space variants with regular ASCII spaces."""
    return text.translate(_UNICODE_SPACES)


def extract_date_from_text(text: str) -> datetime | None:
    """
    Find the first date matching ``Month DD, YYYY`` in *text* and return it
    as a :class:`datetime`. Returns ``None`` if no match is found or the
    date cannot be parsed.
    """
    text = clean(normalize_spaces(text))
    match = _DATE_RE.search(text)
    if not match:
        return None

    raw = match.group(0)
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue

    return None


def is_article_url(url: str) -> bool:
    """Return True if *url* matches Dazed's article URL pattern."""
    return bool(_ARTICLE_URL_RE.match(url))


# ---------------------------------------------------------------------------
# Section link collection
# ---------------------------------------------------------------------------

#: JavaScript executed inside the browser to extract article links.
#: Walks the anchor list sequentially, tagging each href as "latest" or
#: "trending" based on the nearest preceding heading anchor.
_COLLECT_LINKS_JS = """
() => {
    const anchors = [...document.querySelectorAll("a[href]")];
    const out = [];
    let bucket = null;

    function txt(el) {
        return (el.innerText || el.textContent || "").replace(/\\s+/g, " ").trim();
    }

    for (const a of anchors) {
        const label = txt(a);
        const href  = a.href || "";

        if (label === "Latest")   { bucket = "latest_heading";   continue; }
        if (label === "Trending") { bucket = "trending_heading";  continue; }

        if (label === "Show More" && (bucket === "latest_heading" || bucket === "latest")) {
            bucket = "after_latest";
            continue;
        }

        if (bucket === "latest_heading"  && href.includes("/article/")) bucket = "latest";
        if (bucket === "trending_heading" && href.includes("/article/")) bucket = "trending";

        if ((bucket === "latest" || bucket === "trending") && href.includes("/article/")) {
            out.push({ bucket, href, text: label });
        }

        // Stop once the footer navigation begins
        const footerLabels = ["News","Fashion","Music","Film & TV","Beauty","Life & Culture","Art & Photography"];
        if (bucket === "trending" && footerLabels.includes(label)) break;
    }

    return out;
}
"""


async def collect_section_links(
    browser, section_name: str, section_url: str
) -> list[dict]:
    """
    Navigate to a Dazed section page and return deduplicated article records
    for the *Latest* and *Trending* buckets.

    Parameters
    ----------
    browser:
        Playwright browser instance.
    section_name:
        Human-readable section slug used as the ``section`` field in output.
    section_url:
        Full URL of the section landing page.

    Returns
    -------
    list[dict]
        Each dict has keys ``section``, ``bucket``, and ``url``.
    """
    page = await browser.new_page()
    try:
        await page.goto(section_url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(1_500)

        raw_items: list[dict] = await page.evaluate(_COLLECT_LINKS_JS)

        latest: list[dict]   = []
        trending: list[dict] = []
        seen_latest:   set[str] = set()
        seen_trending: set[str] = set()

        for item in raw_items:
            href   = item["href"]
            bucket = item["bucket"]

            if not is_article_url(href):
                continue

            record = {"section": section_name, "bucket": bucket, "url": href}

            if bucket == "latest" and href not in seen_latest:
                seen_latest.add(href)
                latest.append(record)
            elif bucket == "trending" and href not in seen_trending:
                seen_trending.add(href)
                trending.append(record)

        latest = latest[:MAX_LATEST_PER_SECTION]

        print(f"[{section_name}] {len(latest)} latest, {len(trending)} trending")
        return latest + trending

    finally:
        await page.close()


# ---------------------------------------------------------------------------
# Article scraping
# ---------------------------------------------------------------------------

async def scrape_article(
    context, item: dict, sem: asyncio.Semaphore
) -> dict | None:
    """
    Fetch a single article page and extract its headline and publication date.

    Articles older than :data:`CUTOFF_DATE` or missing required fields are
    discarded (returns ``None``).

    Parameters
    ----------
    context:
        Playwright browser context.
    item:
        Dict with at minimum ``url``, ``section``, and ``bucket`` keys.
    sem:
        Semaphore used to cap concurrent browser pages.
    """
    async with sem:
        page = await context.new_page()
        try:
            await page.goto(item["url"], wait_until="domcontentloaded", timeout=30_000)
            await page.locator("h1").first.wait_for(timeout=10_000)
            await page.wait_for_timeout(1_000)

            data: dict = await page.evaluate("""() => ({
                headline: document.querySelector("h1")?.innerText || "",
                bodyText: document.body?.innerText  || ""
            })""")

            headline = clean(data["headline"])
            if not headline:
                print(f"[SKIP] No headline: {item['url']}")
                return None

            date = extract_date_from_text(data["bodyText"])
            if not date:
                print(f"[SKIP] No date found: {item['url']}")
                return None

            if date < CUTOFF_DATE:
                return None  # Article is too old

            return {
                "section":     item["section"],
                "bucket":      item["bucket"],
                "headline":    headline,
                "date_posted": date.strftime("%Y-%m-%d"),
                "url":         item["url"],
            }

        except PlaywrightTimeoutError:
            print(f"[TIMEOUT] {item['url']}")
            return None
        except Exception as exc:
            print(f"[ERROR] {item['url']} -> {exc}")
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

        # Gather candidate links from all section pages sequentially to avoid
        # hammering the host with concurrent page loads at the discovery stage.
        discovered: list[dict] = []
        for section_name, section_url in SECTIONS.items():
            items = await collect_section_links(browser, section_name, section_url)
            discovered.extend(items)

        # Deduplicate by (section, bucket, url) triplet
        deduped: list[dict] = list({
            (x["section"], x["bucket"], x["url"]): x
            for x in discovered
        }.values())

        print(f"Collected {len(deduped)} candidate links — scraping articles...")

        context = await browser.new_context()
        sem = asyncio.Semaphore(CONCURRENCY)

        results = await asyncio.gather(
            *[scrape_article(context, item, sem) for item in deduped]
        )

        await browser.close()

    articles = sorted(
        [r for r in results if r is not None],
        key=lambda a: (a["date_posted"], a["section"], a["bucket"]),
        reverse=True,
    )

    output_path = "dazed_scrape.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(articles, f, indent=2, ensure_ascii=False)

    elapsed = (datetime.now() - start).total_seconds()
    print(f"Done — {len(articles)} articles written to {output_path} ({elapsed:.1f}s)")


if __name__ == "__main__":
    asyncio.run(main())