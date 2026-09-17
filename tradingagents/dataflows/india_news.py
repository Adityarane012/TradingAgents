"""Indian financial news RSS fetcher — an alternate get_global_news vendor.

Yahoo Finance search (the yfinance vendor behind get_global_news) has thin
coverage of Indian market news. Economic Times and Mint both publish free,
keyless RSS feeds with dense India-specific markets coverage, so this module
plugs in as another VENDOR_METHODS["get_global_news"] entry — selectable via
``data_vendors: {"news_data": "india_rss"}`` (or a fallback chain like
``"india_rss,yfinance"``) — rather than a separate tool needing its own agent
wiring.

Feed selection: candidates floated for this also included Business Standard
and Financial Express, but checked live (2026-09-17): Business Standard's
markets RSS returns HTTP 403 (blocked for non-browser clients), and the
commonly-cited Financial Express "market/feed" URL serves an HTML page, not
RSS. Both were dropped rather than shipped as vendor entries that always
fail.

Not made the default: unlike yfinance (Yahoo's search API, broadly stable),
two RSS feeds are a much smaller, more failure-prone surface — either
publisher can change its feed path or format without notice. Users who want
India-specific macro news opt in explicitly via data_vendors.
"""

from __future__ import annotations

import email.utils
import http.client
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .config import get_config
from .date_window import in_window

logger = logging.getLogger(__name__)

# Verified live on 2026-09-17: both return well-formed RSS 2.0 with plain
# (non-namespaced) title/link/description/pubDate — see module docstring for
# the two candidates that were tried and dropped.
_FEEDS = {
    "Economic Times": "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms",
    "Mint": "https://www.livemint.com/rss/markets",
}
_UA = "tradingagents/0.2 (+https://github.com/TauricResearch/TradingAgents)"
_TIMEOUT = 10.0

# Feeds are a page of recent headlines; cap the read so a misbehaving or
# compromised endpoint can't stream an unbounded body into memory (matches
# reddit.py's _MAX_FEED_BYTES).
_MAX_FEED_BYTES = 5 * 1024 * 1024


def _read_capped(resp) -> bytes:
    data = resp.read(_MAX_FEED_BYTES + 1)
    if len(data) > _MAX_FEED_BYTES:
        raise http.client.HTTPException(
            f"India news RSS feed exceeded {_MAX_FEED_BYTES} bytes; refusing to parse"
        )
    return data


def _parse_pub_date(raw: str | None) -> datetime | None:
    """Parse an RFC 822 pubDate, e.g. ``Thu, 17 Sep 2026 13:26:11 +0530``."""
    if not raw:
        return None
    try:
        return email.utils.parsedate_to_datetime(raw.strip())
    except (TypeError, ValueError):
        return None


def _fetch_feed(name: str, url: str) -> list[dict] | None:
    """Fetch and parse one RSS feed.

    Returns ``None`` on a failed fetch (network/parse error) and ``[]`` for a
    feed that fetched fine but had no items — the caller must keep these
    apart so a failure never gets reported as "no news" (#1295's rule,
    applied here too).
    """
    req = Request(
        url,
        headers={
            "User-Agent": _UA,
            "Accept": "application/rss+xml, application/xml, text/xml",
        },
    )
    try:
        with urlopen(req, timeout=_TIMEOUT) as resp:
            root = ET.fromstring(_read_capped(resp))
    except HTTPError as exc:
        logger.warning("India news RSS fetch failed for %s: %s", name, exc)
        return None
    except (OSError, http.client.HTTPException, ET.ParseError) as exc:
        logger.warning("India news RSS fetch failed for %s: %s", name, exc)
        return None

    items = []
    for item in root.findall(".//item"):
        title_el = item.find("title")
        link_el = item.find("link")
        desc_el = item.find("description")
        pub_el = item.find("pubDate")
        items.append(
            {
                "title": (title_el.text or "").strip() if title_el is not None else "",
                "link": (link_el.text or "").strip() if link_el is not None else "",
                "summary": (desc_el.text or "").strip() if desc_el is not None else "",
                "pub_date": _parse_pub_date(pub_el.text if pub_el is not None else None),
                "source": name,
            }
        )
    return items


def get_global_news_india(
    curr_date: str,
    look_back_days: int | None = None,
    limit: int | None = None,
) -> str:
    """Fetch Indian market/macro news from Economic Times + Mint RSS feeds.

    Same signature and return contract as ``get_global_news_yfinance`` (a
    formatted string, never raises) so it drops into
    ``VENDOR_METHODS["get_global_news"]`` as an alternative implementation.

    Unlike the yfinance vendor, these are fixed feeds, not a search — every
    item within the lookback window is returned (newest first, capped at
    ``limit``) rather than matched against ``global_news_queries``.
    """
    config = get_config()
    if look_back_days is None:
        look_back_days = config["global_news_lookback_days"]
    if limit is None:
        limit = config["global_news_article_limit"]

    curr_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    start_dt = curr_dt - timedelta(days=look_back_days)
    start_date = start_dt.strftime("%Y-%m-%d")

    all_items: list[dict] = []
    failed: list[str] = []
    for name, url in _FEEDS.items():
        items = _fetch_feed(name, url)
        if items is None:
            failed.append(name)
            continue
        all_items.extend(items)

    if not all_items:
        if failed:
            # Every configured feed failed to fetch — this is not the same
            # claim as "no news exists," so it must not be reported as such.
            return (
                f"<India news unavailable: every feed failed to fetch "
                f"({', '.join(failed)}); this is not an absence of news>"
            )
        return f"No global news found for {curr_date}"

    # Look-ahead-safe window filter, same rule as every other dated source
    # (news/StockTwits/Reddit) — an item published after curr_date must not
    # leak into a historical run (#1220).
    windowed = [a for a in all_items if in_window(a["pub_date"], start_dt, curr_dt)]

    if not windowed:
        msg = f"No global news found between {start_date} and {curr_date}"
        if failed:
            msg += f"\n<unavailable (fetch failed): {', '.join(failed)}>"
        return msg

    # Newest first, then de-dup by title (both feeds occasionally cover the
    # same story) before applying the caller's limit.
    windowed.sort(key=lambda a: a["pub_date"] or datetime.min, reverse=True)
    seen_titles: set[str] = set()
    kept = []
    for article in windowed:
        key = article["title"].strip().lower()
        if key and key in seen_titles:
            continue
        if key:
            seen_titles.add(key)
        kept.append(article)
        if len(kept) >= limit:
            break

    news_str = ""
    for article in kept:
        news_str += f"### {article['title']} (source: {article['source']})\n"
        if article["summary"]:
            news_str += f"{article['summary']}\n"
        if article["link"]:
            news_str += f"Link: {article['link']}\n"
        news_str += "\n"

    header = f"## Indian Market News (Economic Times / Mint), from {start_date} to {curr_date}:\n\n"
    if failed:
        header += f"<unavailable (fetch failed): {', '.join(failed)}>\n\n"
    return header + news_str
