import os
import time
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import requests

JOURNALS = [
    "The Lancet",
    "JAMA",
    "BMJ",
    "NEJM",
]

KEYWORDS = [
    "AI",
    "intelligence",
]

LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS", "72"))
MAX_PER_JOURNAL = int(os.getenv("MAX_PER_JOURNAL", "20"))

REPORT_DIR = Path("output")

NCBI_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
REQUEST_HEADERS = {
    "User-Agent": "journal-watch/1.0 (github-actions)",
}

JOURNAL_WEBSITE_FEEDS = {
    "The Lancet": [
        "https://www.thelancet.com/rssfeed/lancet_current.xml",
    ],
    "JAMA": [
        "https://jamanetwork.com/rss/site_3.xml?feed=rss",
        "https://jamanetwork.com/rss/site_3",
    ],
    "BMJ": [
        "https://www.bmj.com/rss.xml",
        "https://www.bmj.com/rss/news.xml",
    ],
    "NEJM": [
        "https://www.nejm.org/action/showFeed?type=etoc&feed=rss&jc=nejm",
    ],
}


def parse_pub_datetime(text: str) -> datetime | None:
    if not text:
        return None
    candidates = [
        "%Y %b %d",
        "%Y %b",
        "%Y",
    ]
    for fmt in candidates:
        try:
            dt = datetime.strptime(text, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def parse_feed_datetime(text: str) -> datetime | None:
    if not text:
        return None
    text = text.strip()
    try:
        dt = parsedate_to_datetime(text)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass

    iso_candidate = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso_candidate)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def build_query(journal: str, keywords: list[str], date_from: str) -> str:
    kw_expr = " OR ".join([f'"{k}"[Title]' for k in keywords])
    return (
        f'"{journal}"[Journal] AND ({kw_expr}) '
        f'AND ("{date_from}"[Date - Publication] : "3000"[Date - Publication])'
    )


def get_json(url: str, params: dict, retries: int = 3, backoff: float = 1.5) -> dict:
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, params=params, headers=REQUEST_HEADERS, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(backoff * attempt)
    raise RuntimeError(f"Request failed after {retries} attempts: {url}") from last_error


def esearch(term: str, retmax: int) -> list[str]:
    params = {
        "db": "pubmed",
        "retmode": "json",
        "sort": "pub date",
        "retmax": str(retmax),
        "term": term,
    }
    data = get_json(f"{NCBI_BASE}/esearch.fcgi", params=params)
    return data.get("esearchresult", {}).get("idlist", [])


def esummary(id_list: list[str]) -> list[dict[str, Any]]:
    if not id_list:
        return []
    params = {
        "db": "pubmed",
        "retmode": "json",
        "id": ",".join(id_list),
    }
    data = get_json(f"{NCBI_BASE}/esummary.fcgi", params=params).get("result", {})

    out = []
    for uid in data.get("uids", []):
        item = data.get(uid, {})
        doi = ""
        for article_id in item.get("articleids", []):
            if article_id.get("idtype") == "doi":
                doi = article_id.get("value", "")
                break

        out.append(
            {
                "source": "PubMed",
                "source_type": "pubmed",
                "pmid": uid,
                "title": item.get("title", "").replace("\n", " ").strip(),
                "journal": item.get("fulljournalname", "").strip(),
                "pubdate": item.get("pubdate", "").strip(),
                "published_at": parse_pub_datetime(item.get("pubdate", "").strip()),
                "doi": doi,
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{uid}/",
            }
        )
    return out


def first_text(node: ET.Element, names: list[str]) -> str:
    for name in names:
        el = node.find(name)
        if el is not None and el.text:
            return el.text.strip()
    return ""


def fetch_feed_items(feed_url: str, journal: str) -> list[dict[str, Any]]:
    resp = requests.get(feed_url, headers=REQUEST_HEADERS, timeout=30)
    resp.raise_for_status()
    root = ET.fromstring(resp.text)

    items = []
    entries = root.findall(".//item")
    if not entries:
        entries = root.findall(".//{http://www.w3.org/2005/Atom}entry")

    for node in entries:
        if node.tag.endswith("entry"):
            title = first_text(node, ["{http://www.w3.org/2005/Atom}title"])
            link = ""
            for link_node in node.findall("{http://www.w3.org/2005/Atom}link"):
                href = link_node.attrib.get("href", "").strip()
                rel = link_node.attrib.get("rel", "alternate")
                if href and rel in ("alternate", ""):
                    link = href
                    break
            pub_raw = first_text(
                node,
                [
                    "{http://www.w3.org/2005/Atom}updated",
                    "{http://www.w3.org/2005/Atom}published",
                ],
            )
            description = first_text(node, ["{http://www.w3.org/2005/Atom}summary"])
        else:
            title = first_text(node, ["title"])
            link = first_text(node, ["link"])
            pub_raw = first_text(node, ["pubDate", "dc:date", "date"])
            description = first_text(node, ["description", "content:encoded"])

        items.append(
            {
                "source": journal,
                "source_type": "website",
                "pmid": "",
                "title": title,
                "journal": journal,
                "pubdate": pub_raw,
                "published_at": parse_feed_datetime(pub_raw),
                "doi": "",
                "url": link,
                "summary": description,
            }
        )
    return items


def keyword_match_title(title: str, keywords: list[str]) -> bool:
    t = title.lower()
    return any(k.lower() in t for k in keywords)


def in_lookback_window(item_dt: datetime | None, cutoff: datetime) -> bool:
    if item_dt is None:
        return False
    return item_dt >= cutoff


def fetch_official_website_items(cutoff: datetime) -> list[dict[str, Any]]:
    all_items: list[dict[str, Any]] = []
    for journal, feeds in JOURNAL_WEBSITE_FEEDS.items():
        success = False
        for feed_url in feeds:
            try:
                raw_items = fetch_feed_items(feed_url, journal)
                filtered = [
                    item
                    for item in raw_items
                    if keyword_match_title(item.get("title", ""), KEYWORDS)
                    and in_lookback_window(item.get("published_at"), cutoff)
                ]
                all_items.extend(filtered)
                success = True
                break
            except Exception as exc:
                print(f"[WARN] feed failed: {journal} | {feed_url} | {exc}")
        if not success:
            print(f"[WARN] all feeds failed for: {journal}")
    return all_items


def deduplicate_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[str, dict[str, Any]] = {}
    for item in items:
        title = (item.get("title") or "").strip().lower()
        doi = (item.get("doi") or "").strip().lower()
        key = doi or title
        if not key:
            continue
        if key in deduped:
            # Prefer PubMed record when duplicates exist across sources.
            if deduped[key].get("source_type") != "pubmed" and item.get("source_type") == "pubmed":
                deduped[key] = item
            continue
        deduped[key] = item
    return list(deduped.values())


def send_webhook(text: str) -> None:
    webhook = os.getenv("WEBHOOK_URL", "").strip()
    if not webhook:
        return
    try:
        requests.post(webhook, json={"text": text}, timeout=20)
    except Exception:
        # Keep workflow green even when webhook endpoint is down.
        pass


def write_report(items: list[dict], generated_at: datetime) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report_name = f"report_{generated_at.strftime('%Y%m%d_%H%M%SZ')}.md"
    report_path = REPORT_DIR / report_name
    lines = [
        "# Journal Watch Report",
        "",
        f"- Generated (UTC): {generated_at.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Lookback hours: {LOOKBACK_HOURS}",
        f"- Matched records: {len(items)}",
        "",
    ]

    if not items:
        lines.append("No new records found.")
    else:
        for index, item in enumerate(items, start=1):
            lines.append(f"{index}. {item['title']}")
            lines.append(f"   - Source: {item.get('source', 'N/A')}")
            lines.append(f"   - Journal: {item['journal'] or 'N/A'}")
            lines.append(f"   - Date: {item['pubdate'] or 'N/A'}")
            lines.append(f"   - PMID: {item['pmid']}")
            lines.append(f"   - DOI: {item['doi'] or 'N/A'}")
            lines.append(f"   - Link: {item['url']}")
            lines.append("")

    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def main() -> None:
    now_utc = datetime.now(timezone.utc)
    cutoff = now_utc - timedelta(hours=LOOKBACK_HOURS)
    date_from = cutoff.strftime("%Y/%m/%d")

    matched_items: list[dict[str, Any]] = []

    for journal in JOURNALS:
        query = build_query(journal, KEYWORDS, date_from)
        try:
            ids = esearch(query, MAX_PER_JOURNAL)
            for item in esummary(ids):
                if in_lookback_window(item.get("published_at"), cutoff):
                    matched_items.append(item)
        except Exception as exc:
            # Skip a failed journal query instead of failing the entire workflow.
            print(f"[WARN] journal query failed: {journal} | {exc}")

    website_items = fetch_official_website_items(cutoff)
    matched_items.extend(website_items)
    matched_items = deduplicate_items(matched_items)
    matched_items.sort(key=lambda x: x.get("published_at") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    report_path = write_report(matched_items, now_utc)

    if matched_items:
        top = matched_items[:5]
        msg_lines = [f"Journal Watch: {len(matched_items)} matched records in last {LOOKBACK_HOURS}h"]
        for item in top:
            msg_lines.append(f"- {item['title'][:80]} | {item['url']}")
        send_webhook("\n".join(msg_lines))

    print(f"[INFO] report generated: {report_path}")


if __name__ == "__main__":
    main()
