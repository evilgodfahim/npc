import feedparser
import hashlib
import os
import datetime
import re
import html

OUTPUT_PATH = "output/merged.xml"
FEED_SELF_LINK = os.environ.get(
    "FEED_SELF_LINK",
    "https://yourgithubusername.github.io/rss-merge/output/merged.xml",
)

# RFC 822 date formats feedparser may return
_RFC822_FORMATS = [
    "%a, %d %b %Y %H:%M:%S %z",
    "%a, %d %b %Y %H:%M:%S GMT",
    "%a, %d %b %Y %H:%M:%S +0000",
    "%d %b %Y %H:%M:%S %z",
    "%d %b %Y %H:%M:%S GMT",
]


def normalize_link(link: str) -> str:
    if not link:
        return ""
    link = re.sub(r"https?://(www\.)?", "", link)
    link = re.sub(r"/+$", "", link)
    return link.strip().lower()


def unique_id(entry) -> str:
    link = normalize_link(entry.get("link", ""))
    title = entry.get("title", "").strip().lower()
    return hashlib.md5(f"{link}-{title}".encode("utf-8")).hexdigest()


def safe_date(raw: str, fallback: str) -> str:
    """
    Return raw if it parses as RFC 822; otherwise return fallback.
    Prevents malformed dates from breaking feed validators.
    """
    if not raw:
        return fallback
    for fmt in _RFC822_FORMATS:
        try:
            datetime.datetime.strptime(raw.strip(), fmt)
            return raw.strip()
        except ValueError:
            continue
    # feedparser also exposes a parsed 9-tuple — use it as last resort
    return fallback


def xml_escape(text: str) -> str:
    """
    Escape a string for use in a plain XML text node (not CDATA).
    Handles &, <, >, ", ' — covers URLs and any stray attribute values.
    """
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
    seen = set()
    unique = []
    for e in entries:
        uid = unique_id(e)
        if uid not in seen:
            seen.add(uid)
            unique.append(e)
    return unique


def make_rss(entries: list) -> str:
    now = datetime.datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S GMT")

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0">',
        "  <channel>",
        "    <title>Combined Politepol Feed</title>",
        f"    <link>{xml_escape(FEED_SELF_LINK)}</link>",
        "    <description>Merged feed from multiple Politepol sources</description>",
        f"    <lastBuildDate>{now}</lastBuildDate>",
    ]

    for e in entries:
        title   = e.get("title", "No title")
        link    = e.get("link", "")
        desc    = e.get("summary", "")
        pub_raw = e.get("published", "")
        pub     = safe_date(pub_raw, now)

        lines.append("    <item>")
        lines.append(f"      <title><![CDATA[{title}]]></title>")
        # link MUST be a plain text node — escape & < > characters
        lines.append(f"      <link>{xml_escape(link)}</link>")
        lines.append(f"      <pubDate>{pub}</pubDate>")
        lines.append(f"      <description><![CDATA[{desc}]]></description>")
        # guid prevents feed readers from showing duplicates on re-fetch
        lines.append(f"      <guid isPermaLink=\"false\">{xml_escape(link)}</guid>")
        lines.append("    </item>")

    lines.append("  </channel>")
    lines.append("</rss>")
    return "\n".join(lines)


def main():
    urls    = load_feeds()
    entries = fetch_all_feeds(urls)
    print(f"Fetched {len(entries)} total entries")
    entries = deduplicate(entries)
    print(f"{len(entries)} unique entries after deduplication")
    rss = make_rss(entries)
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(rss)
    print("✅ Merged RSS saved.")


if __name__ == "__main__":
    main()