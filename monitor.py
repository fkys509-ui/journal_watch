import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

JOURNALS = [
    "The Lancet",
    "JAMA",
    "BMJ",
    "NEJM",
]

KEYWORDS = [
    "AI",
    "artificial intelligence",
]

DAYS_BACK = int(os.getenv("DAYS_BACK", "3"))
MAX_PER_JOURNAL = int(os.getenv("MAX_PER_JOURNAL", "20"))

STATE_PATH = Path("state.json")
REPORT_DIR = Path("output")
REPORT_PATH = REPORT_DIR / "latest_report.md"

NCBI_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
REQUEST_HEADERS = {
    "User-Agent": "journal-watch/1.0 (github-actions)",
}


def load_state() -> set[str]:
    if not STATE_PATH.exists():
        return set()
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return set(data.get("seen_pmids", []))
    except Exception:
        return set()


def save_state(seen_pmids: set[str]) -> None:
    data = {"seen_pmids": sorted(seen_pmids)[-5000:]}
    STATE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def build_query(journal: str, keywords: list[str], date_from: str) -> str:
    kw_expr = " OR ".join([f'"{k}"[Title/Abstract]' for k in keywords])
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


def esummary(id_list: list[str]) -> list[dict]:
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
                "pmid": uid,
                "title": item.get("title", "").replace("\n", " ").strip(),
                "journal": item.get("fulljournalname", "").strip(),
                "pubdate": item.get("pubdate", "").strip(),
                "doi": doi,
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{uid}/",
            }
        )
    return out


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
    lines = [
        "# Journal Watch Report",
        "",
        f"- Generated (UTC): {generated_at.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Days back: {DAYS_BACK}",
        f"- New records: {len(items)}",
        "",
    ]

    if not items:
        lines.append("No new records found.")
    else:
        for index, item in enumerate(items, start=1):
            lines.append(f"{index}. {item['title']}")
            lines.append(f"   - Journal: {item['journal'] or 'N/A'}")
            lines.append(f"   - Date: {item['pubdate'] or 'N/A'}")
            lines.append(f"   - PMID: {item['pmid']}")
            lines.append(f"   - DOI: {item['doi'] or 'N/A'}")
            lines.append(f"   - Link: {item['url']}")
            lines.append("")

    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    now_utc = datetime.now(timezone.utc)
    date_from = (now_utc - timedelta(days=DAYS_BACK)).strftime("%Y/%m/%d")

    seen = load_state()
    new_items = []

    for journal in JOURNALS:
        query = build_query(journal, KEYWORDS, date_from)
        try:
            ids = esearch(query, MAX_PER_JOURNAL)
            for item in esummary(ids):
                if item["pmid"] not in seen:
                    new_items.append(item)
        except Exception as exc:
            # Skip a failed journal query instead of failing the entire workflow.
            print(f"[WARN] journal query failed: {journal} | {exc}")

    # De-duplicate by PMID inside this run.
    deduped = {item["pmid"]: item for item in new_items}
    new_items = list(deduped.values())

    write_report(new_items, now_utc)

    for item in new_items:
        seen.add(item["pmid"])
    save_state(seen)

    if new_items:
        top = new_items[:5]
        msg_lines = [f"Journal Watch: {len(new_items)} new papers"]
        for item in top:
            msg_lines.append(f"- {item['title'][:80]} | {item['url']}")
        send_webhook("\n".join(msg_lines))


if __name__ == "__main__":
    main()
