import feedparser
import hashlib
import os
import datetime
import re
import html
import xml.etree.ElementTree as ET
from pathlib import Path
from email.utils import parsedate_to_datetime

OUTPUT_PATH    = "output/merged.xml"
FEED_SELF_LINK = os.environ.get(
    "FEED_SELF_LINK",
    "https://yourgithubusername.github.io/rss-merge/output/merged.xml",
)
MAX_ITEMS = 1000

_RFC822_FORMATS = [
    "%a, %d %b %Y %H:%M:%S %z",
    "%a, %d %b %Y %H:%M:%S GMT",
    "%a, %d %b %Y %H:%M:%S +0000",
    "%d %b %Y %H:%M:%S %z",
    "%d %b %Y %H:%M:%S GMT",
]

# Epoch zero — used as sort key when pubDate is missing or unparseable
_EPOCH_ZERO = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)

# -- SANITIZATION --------------------------------------------------------------

_CTRL_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')


def _sanitize_xml(raw: str) -> str:
    """Strip forbidden control chars and fix bare & before ET.fromstring()."""
    raw = _CTRL_RE.sub("", raw)
    raw = re.sub(
        r'&(?!(?:[a-zA-Z][a-zA-Z0-9]*|#[0-9]+|#x[0-9a-fA-F]+);)',
        '&amp;',
        raw,
    )
    return raw

# -- HELPERS -------------------------------------------------------------------

def normalize_link(link: str) -> str:
    if not link:
        return ""
    link = re.sub(r"https?://(www\.)?", "", link)
    link = re.sub(r"/+$", "", link)
    return link.strip().lower()


def unique_id(entry) -> str:
    link  = normalize_link(entry.get("link", ""))
    title = entry.get("title", "").strip().lower()
    return hashlib.md5(f"{link}-{title}".encode("utf-8")).hexdigest()


def parse_pubdate(raw: str) -> datetime.datetime:
    """
    Parse an RFC 822 / ISO 8601 date string into an aware UTC datetime.
    Returns _EPOCH_ZERO if the string is absent or unparseable — those
    items will sort to the very bottom and be the first evicted.
    """
    if not raw:
        return _EPOCH_ZERO
    # Try the standard RFC 822 parser first (covers the vast majority of feeds)
    try:
        dt = parsedate_to_datetime(raw.strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc)
    except Exception:
        pass
    # Fallback: strptime against known formats
    for fmt in _RFC822_FORMATS:
        try:
            dt = datetime.datetime.strptime(raw.strip(), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            return dt.astimezone(datetime.timezone.utc)
        except ValueError:
            continue
    return _EPOCH_ZERO


def safe_date(raw: str, fallback: str) -> str:
    """Return raw if it round-trips through parse_pubdate; else fallback."""
    if not raw:
        return fallback
    if parse_pubdate(raw) is not _EPOCH_ZERO:
        return raw.strip()
    return fallback


def xml_escape(text: str) -> str:
    return html.escape(text, quote=True)


def load_feeds(file="feed_urls.txt") -> list[str]:
    with open(file, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


def fetch_all_feeds(urls: list[str]) -> list:
    all_entries = []
    for url in urls:
        print(f"Fetching {url}")
        feed = feedparser.parse(url)
        all_entries.extend(feed.entries)
    return all_entries


def deduplicate(entries: list) -> list:
    seen   = set()
    unique = []
    for e in entries:
        uid = unique_id(e)
        if uid not in seen:
            seen.add(uid)
            unique.append(e)
    return unique

# -- EXISTING FEED LOADING -----------------------------------------------------

def _parse_existing_items(path: str) -> list[dict]:
    """
    Read the current output XML and return its <item> elements as dicts.
    Two-stage parse with sanitization fallback so a single bad & never
    loses the whole history.
    """
    if not Path(path).exists():
        return []

    def _items_from_root(root) -> list[dict]:
        channel = root.find("channel")
        if channel is None:
            return []
        items = []
        for item in channel.findall("item"):
            def t(tag):
                el = item.find(tag)
                return (el.text or "").strip() if el is not None else ""
            raw_pub = t("pubDate")
            items.append({
                "link":    t("link"),
                "title":   t("title"),
                "pubDate": raw_pub,
                "dt":      parse_pubdate(raw_pub),   # aware UTC datetime for sorting
                "desc":    t("description"),
                "guid":    t("guid"),
            })
        return items

    # Stage 1: direct parse
    try:
        tree = ET.parse(path)
        return _items_from_root(tree.getroot())
    except ET.ParseError as e:
        print(f"[WARN] Existing XML parse failed: {e} — retrying with sanitization…")

    # Stage 2: sanitize then parse
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
        root  = ET.fromstring(_sanitize_xml(raw))
        items = _items_from_root(root)
        print(f"[INFO] Recovered {len(items)} existing item(s) after sanitization.")
        return items
    except ET.ParseError as e:
        print(f"[WARN] Sanitized parse also failed: {e} — starting fresh.")
        return []

# -- XML GENERATION ------------------------------------------------------------

def make_rss(new_entries: list, existing_items: list[dict]) -> str:
    """
    Merge new entries with existing items, deduplicate by link, sort the
    entire pool by pubDate descending, keep the MAX_ITEMS most-recent, and
    serialise to an RSS string.

    Items with no parseable pubDate sort to the bottom and are the first
    to be evicted when the pool exceeds MAX_ITEMS.
    """
    now = datetime.datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S GMT")

    # Seed the pool with existing items
    seen_links: set[str] = set()
    pool: list[dict]    = []

    for item in existing_items:
        lnk = item.get("link", "").strip()
        if lnk and lnk not in seen_links:
            seen_links.add(lnk)
            pool.append(item)

    # Add new entries, skipping already-seen links
    for e in new_entries:
        lnk = xml_escape(e.get("link", ""))
        if not lnk or lnk in seen_links:
            continue
        seen_links.add(lnk)
        raw_pub = e.get("published", "")
        pool.append({
            "link":    lnk,
            "title":   e.get("title", "No title"),
            "pubDate": safe_date(raw_pub, now),
            "dt":      parse_pubdate(raw_pub),
            "desc":    e.get("summary", ""),
            "guid":    lnk,
        })

    # Sort entire pool newest-first by parsed datetime
    pool.sort(key=lambda x: x["dt"], reverse=True)

    # Evict the oldest (tail) entries beyond MAX_ITEMS
    if len(pool) > MAX_ITEMS:
        dropped = len(pool) - MAX_ITEMS
        pool    = pool[:MAX_ITEMS]
        print(f"  Evicted {dropped} oldest item(s) to stay within {MAX_ITEMS}-item cap.")

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0">',
        "  <channel>",
        "    <title>Combined Politepol Feed</title>",
        f"    <link>{xml_escape(FEED_SELF_LINK)}</link>",
        "    <description>Merged feed from multiple Politepol sources</description>",
        f"    <lastBuildDate>{now}</lastBuildDate>",
    ]

    for item in pool:
        title = item.get("title", "No title")
        link  = item.get("link", "")
        desc  = item.get("desc", "")
        pub   = item.get("pubDate", now)
        guid  = item.get("guid", link)

        lines.append("    <item>")
        lines.append(f"      <title><![CDATA[{title}]]></title>")
        lines.append(f"      <link>{xml_escape(link)}</link>")
        lines.append(f"      <pubDate>{pub}</pubDate>")
        lines.append(f"      <description><![CDATA[{desc}]]></description>")
        lines.append(f'      <guid isPermaLink="false">{xml_escape(guid)}</guid>')
        lines.append("    </item>")

    lines.append("  </channel>")
    lines.append("</rss>")
    return "\n".join(lines)

# -- MAIN ----------------------------------------------------------------------

def main():
    urls    = load_feeds()
    entries = fetch_all_feeds(urls)
    print(f"Fetched {len(entries)} total entries")

    entries = deduplicate(entries)
    print(f"{len(entries)} unique entries after deduplication")

    existing = _parse_existing_items(OUTPUT_PATH)
    print(f"Loaded {len(existing)} existing item(s) from {OUTPUT_PATH}")

    rss = make_rss(entries, existing)

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(rss)
    print("✅ Merged RSS saved.")


if __name__ == "__main__":
    main()
