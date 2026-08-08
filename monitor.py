import os
import re
import time
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import requests

PUBMED_JOURNAL_FAMILIES = {
    "Lancet": {
        "journal": [
            "Lancet*",
            "EClinicalMedicine",
            "Lancet Regional Health*",
        ],
        "abbr": [
            "Lancet*",
            "EClinicalMedicine",
            "Lancet Reg Health*",
        ],
    },
    "JAMA": {
        "journal": [
            "JAMA*",
            "JAMA Network Open",
        ],
        "abbr": [
            "JAMA*",
            "JAMA Netw Open",
        ],
    },
    "BMJ": {
        "journal": [
            "BMJ*",
        ],
        "abbr": [
            "BMJ*",
        ],
    },
    "NEJM": {
        "journal": [
            "New England Journal of Medicine",
            "NEJM*",
            "NEJM Evidence",
            "NEJM AI",
            "NEJM Catalyst*",
        ],
        "abbr": [
            "N Engl J Med*",
            "NEJM*",
            "NEJM Evid",
            "NEJM AI",
        ],
    },
}

DEFAULT_KEYWORDS = [
    "AI",
    "intelligence",
]


def parse_int_env(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError:
        return default
    return value

MAX_PER_JOURNAL = parse_int_env("MAX_PER_JOURNAL", 20)
REPORT_MODE = os.getenv("REPORT_MODE", "monitor").strip().lower()
QUERY_BUILDER = os.getenv("QUERY_BUILDER", "").strip()
DATE_FROM = os.getenv("DATE_FROM", "").strip()
DATE_TO = os.getenv("DATE_TO", "").strip()
SOURCE_CHECK_MODES = {"source_check", "sources_check", "check_sources"}

REPORT_DIR = Path("output")

NCBI_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
REQUEST_HEADERS = {
    "User-Agent": "journal-watch/1.0 (github-actions)",
}

JOURNAL_WEBSITE_FEEDS = {
    "Lancet": [
        "https://www.thelancet.com/rssfeed/lancet_current.xml",
        "https://www.thelancet.com/rssfeed/lanres_current.xml",
        "https://www.thelancet.com/rssfeed/lanpub_current.xml",
        "https://www.thelancet.com/rssfeed/landig_current.xml",
        "https://www.thelancet.com/rssfeed/eclinm_current.xml",
    ],
    "JAMA": [
        "https://jamanetwork.com/rss/site_3.xml",
        "https://jamanetwork.com/rss/site_3",
        "https://jamanetwork.com/rss/site_4.xml",
        "https://jamanetwork.com/rss/site_5.xml",
        "https://jamanetwork.com/rss/site_6.xml",
        "https://jamanetwork.com/rss/site_7.xml",
        "https://jamanetwork.com/rss/site_8.xml",
    ],
    "BMJ": [
        "https://www.bmj.com/rss.xml",
        "https://bmjopen.bmj.com/rss/current.xml",
        "https://bmjmedicine.bmj.com/rss/current.xml",
    ],
    "NEJM": [
        "https://www.nejm.org/action/showFeed?type=etoc&feed=rss&jc=nejm",
        "https://www.nejm.org/action/showFeed?type=etoc&feed=rss&jc=NEJMEvidence",
        "https://www.nejm.org/action/showFeed?type=etoc&feed=rss&jc=NEJMcatalyst",
        "https://www.nejm.org/action/showFeed?type=etoc&feed=rss&jc=NEJMai",
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


def format_pubmed_field_term(term: str, field: str) -> str:
    value = term.strip()
    if "*" in value:
        return f"{value}[{field}]"
    return f'"{value}"[{field}]'


def build_journal_family_clause(family_terms: dict[str, list[str]]) -> str:
    clauses = []
    for term in family_terms.get("journal", []):
        clauses.append(format_pubmed_field_term(term, "Journal"))
    for term in family_terms.get("abbr", []):
        clauses.append(format_pubmed_field_term(term, "TA"))
    return "(" + " OR ".join(clauses) + ")"


def default_query_expression(keywords: list[str]) -> str:
    return " OR ".join([f'"{keyword}"[Title]' if " " in keyword else f"{keyword}[Title]" for keyword in keywords])


def get_active_query_expression(keywords: list[str]) -> str:
    expression = QUERY_BUILDER or default_query_expression(keywords)
    if len(expression) > 1000:
        raise ValueError("QUERY_BUILDER must not exceed 1000 characters")

    fields = re.findall(r"\[([^\]]+)\]", expression)
    if any(field.strip().lower() != "title" for field in fields):
        raise ValueError("QUERY_BUILDER only supports the [Title] field")
    if expression.count("(") != expression.count(")"):
        raise ValueError("QUERY_BUILDER contains unmatched parentheses")

    flat_expression = expression.replace("(", " ").replace(")", " ")
    atom = r'(?:"[^"\r\n]+"|[\w*.-]+)\[Title\]'
    if not re.fullmatch(rf"\s*{atom}(?:\s+(?:AND|OR|NOT)\s+{atom})*\s*", flat_expression, flags=re.IGNORECASE):
        raise ValueError("QUERY_BUILDER must contain [Title] terms joined by AND, OR, or NOT")

    expression = re.sub(r"\[title\]", "[Title]", expression, flags=re.IGNORECASE)
    expression = re.sub(
        r"\b(and|or|not)\b",
        lambda match: match.group(1).upper(),
        expression,
        flags=re.IGNORECASE,
    )
    return expression.strip()


def resolve_date_range(now_utc: datetime) -> tuple[datetime, datetime, str, str]:
    default_to = now_utc.date()
    default_from = default_to - timedelta(days=2)

    try:
        date_from = datetime.strptime(DATE_FROM, "%Y-%m-%d").date() if DATE_FROM else default_from
    except ValueError:
        date_from = default_from

    try:
        date_to = datetime.strptime(DATE_TO, "%Y-%m-%d").date() if DATE_TO else default_to
    except ValueError:
        date_to = default_to

    if date_from > date_to:
        raise ValueError("DATE_FROM cannot be later than DATE_TO")

    range_start = datetime.combine(date_from, datetime.min.time(), tzinfo=timezone.utc)
    range_end_exclusive = datetime.combine(date_to + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    return range_start, range_end_exclusive, date_from.isoformat(), date_to.isoformat()


def extract_keywords_for_title_filter(query_expr: str, fallback: list[str]) -> list[str]:
    candidates = []
    for m in re.findall(r'"([^"]+)"', query_expr):
        token = m.strip()
        if token:
            candidates.append(token)

    # If no quoted tokens are found, collect simple terms.
    if not candidates:
        for token in re.findall(r"\b[a-zA-Z][a-zA-Z0-9_-]{1,}\b", query_expr):
            up = token.upper()
            if up in {"AND", "OR", "NOT", "TITLE", "ABSTRACT", "JOURNAL", "TA"}:
                continue
            candidates.append(token)

    deduped = []
    seen = set()
    for token in candidates:
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(token)

    return deduped[:20] if deduped else fallback


def build_query(family_terms: dict[str, list[str]], query_expr: str, date_from: str, date_to: str) -> str:
    family_expr = build_journal_family_clause(family_terms)
    pubmed_date_from = (datetime.strptime(date_from, "%Y-%m-%d").date() - timedelta(days=1)).isoformat()
    pubmed_date_to = (datetime.strptime(date_to, "%Y-%m-%d").date() + timedelta(days=1)).isoformat()
    return (
        f"{family_expr} AND ({query_expr}) "
        f'AND ("{pubmed_date_from}"[Date - Publication] : "{pubmed_date_to}"[Date - Publication])'
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
    for kw in keywords:
        k = kw.lower().strip()
        if not k:
            continue

        # Avoid false positives like "maintenance" or "airway" when keyword is AI.
        if k == "ai":
            if re.search(r"(?<![a-z0-9])ai(?![a-z0-9])", t):
                return True
            continue

        if k in t:
            return True
    return False


def title_term_matches(title: str, term: str) -> bool:
    value = term.strip().strip('"').casefold()
    if not value:
        return False

    pattern = re.escape(value).replace(r"\*", r"[a-z0-9]*")
    return re.search(rf"(?<![a-z0-9]){pattern}(?![a-z0-9])", title.casefold()) is not None


def title_query_matches(title: str, query_expr: str) -> bool:
    token_pattern = re.compile(
        r'\(|\)|\bAND\b|\bOR\b|\bNOT\b|((?:"[^"\r\n]+"|[\w*.-]+)\[Title\])',
        flags=re.IGNORECASE,
    )
    tokens = [match.group(0) for match in token_pattern.finditer(query_expr)]

    def evaluate_group(index: int) -> tuple[bool, int]:
        value: bool | None = None
        operator = ""

        while index < len(tokens):
            token = tokens[index]
            upper = token.upper()
            if token == ")":
                return bool(value), index + 1
            if upper in {"AND", "OR", "NOT"}:
                operator = upper
                index += 1
                continue
            if token == "(":
                operand, index = evaluate_group(index + 1)
            else:
                term = re.sub(r"\[Title\]$", "", token, flags=re.IGNORECASE)
                operand = title_term_matches(title, term)
                index += 1

            if value is None:
                value = operand
            elif operator == "AND":
                value = value and operand
            elif operator == "OR":
                value = value or operand
            elif operator == "NOT":
                value = value and not operand
            else:
                return False, len(tokens)
            operator = ""

        return bool(value), index

    result, final_index = evaluate_group(0)
    return result and final_index == len(tokens)


def normalize_journal_name(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()
    return normalized[4:] if normalized.startswith("the ") else normalized


def journal_matches_family(journal: str, family_terms: dict[str, list[str]]) -> bool:
    normalized_journal = normalize_journal_name(journal)
    for raw_term in family_terms.get("journal", []):
        wildcard = raw_term.endswith("*")
        normalized_term = normalize_journal_name(raw_term.rstrip("*"))
        if wildcard and normalized_journal.startswith(normalized_term):
            return True
        if not wildcard and normalized_journal == normalized_term:
            return True
    return False


def validate_pubmed_items(
    items: list[dict[str, Any]],
    query_expr: str,
    family_terms: dict[str, list[str]],
) -> list[dict[str, Any]]:
    validated = []
    for item in items:
        title = item.get("title", "")
        journal = item.get("journal", "")
        if not title_query_matches(title, query_expr):
            print(f"[WARN] dropped PubMed title mismatch: PMID={item.get('pmid', '')} | {title}")
            continue
        if not journal_matches_family(journal, family_terms):
            print(f"[WARN] dropped PubMed journal mismatch: PMID={item.get('pmid', '')} | {journal}")
            continue
        validated.append(item)
    return validated


def in_date_range(item_dt: datetime | None, range_start: datetime, range_end_exclusive: datetime) -> bool:
    if item_dt is None:
        return False
    return range_start <= item_dt < range_end_exclusive


def fetch_official_website_items(
    range_start: datetime,
    range_end_exclusive: datetime,
    keywords_for_filter: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    all_items: list[dict[str, Any]] = []
    source_statuses: list[dict[str, Any]] = []
    for journal, feeds in JOURNAL_WEBSITE_FEEDS.items():
        success = False
        for feed_url in feeds:
            try:
                raw_items = fetch_feed_items(feed_url, journal)
                filtered = [
                    item
                    for item in raw_items
                    if keyword_match_title(item.get("title", ""), keywords_for_filter)
                    and in_date_range(item.get("published_at"), range_start, range_end_exclusive)
                ]
                all_items.extend(filtered)
                source_statuses.append(
                    {
                        "family": journal,
                        "url": feed_url,
                        "status": "available",
                        "item_count": len(filtered),
                        "raw_count": len(raw_items),
                        "reason": "",
                    }
                )
                success = True
                if REPORT_MODE not in SOURCE_CHECK_MODES:
                    break
            except Exception as exc:
                source_statuses.append(
                    {
                        "family": journal,
                        "url": feed_url,
                        "status": "unavailable",
                        "item_count": 0,
                        "raw_count": 0,
                        "reason": str(exc),
                    }
                )
                print(f"[WARN] feed failed: {journal} | {feed_url} | {exc}")
        if not success:
            print(f"[WARN] all feeds failed for: {journal}")
    return all_items, source_statuses


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


_OA_CACHE: dict[str, str] = {}


def resolve_open_access_status(doi: str, pmid: str) -> str:
    cache_key = (doi or "").strip().lower() + "|" + (pmid or "").strip()
    if cache_key in _OA_CACHE:
        return _OA_CACHE[cache_key]

    doi = (doi or "").strip()
    pmid = (pmid or "").strip()

    if doi:
        try:
            url = f"https://api.openalex.org/works/https://doi.org/{requests.utils.quote(doi, safe='')}?select=open_access"
            resp = requests.get(url, headers=REQUEST_HEADERS, timeout=20)
            if resp.ok:
                data = resp.json()
                is_oa = data.get("open_access", {}).get("is_oa")
                if isinstance(is_oa, bool):
                    value = "Yes" if is_oa else "No"
                    _OA_CACHE[cache_key] = value
                    return value
        except Exception:
            pass

    if pmid:
        try:
            query = f"EXT_ID:{pmid} AND SRC:MED"
            url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
            params = {"query": query, "format": "json", "pageSize": "1"}
            resp = requests.get(url, params=params, headers=REQUEST_HEADERS, timeout=20)
            if resp.ok:
                data = resp.json()
                result = data.get("resultList", {}).get("result", [])
                if result:
                    is_oa = str(result[0].get("isOpenAccess", "")).upper() == "Y"
                    value = "Yes" if is_oa else "No"
                    _OA_CACHE[cache_key] = value
                    return value
        except Exception:
            pass

    _OA_CACHE[cache_key] = "Unknown"
    return "Unknown"


def attach_open_access(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for item in items:
        item["open_access"] = resolve_open_access_status(item.get("doi", ""), item.get("pmid", ""))
    return items


def send_webhook(text: str) -> None:
    webhook = os.getenv("WEBHOOK_URL", "").strip()
    if not webhook:
        return
    try:
        requests.post(webhook, json={"text": text}, timeout=20)
    except Exception:
        # Keep workflow green even when webhook endpoint is down.
        pass


def write_report(
    items: list[dict],
    generated_at: datetime,
    source_statuses: list[dict[str, Any]],
    active_query: str,
    date_from: str,
    date_to: str,
) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report_name = f"report_{generated_at.strftime('%Y%m%d_%H%M%SZ')}_{REPORT_MODE}.md"
    report_path = REPORT_DIR / report_name
    lines = [
        "# Journal Watch Report",
        "",
        f"- Generated (UTC): {generated_at.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Mode: {REPORT_MODE}",
        f"- Matched records: {len(items)}",
        "",
        "- 检索期刊范围：四大医学顶刊及其全部子刊；",
        f"- 检索关键词策略：{active_query}；",
        f"- 检索时间范围：{date_from} 至 {date_to}。",
        "- 时间字段说明：[Time] 表示 PubMed Publication Date [dp]，不代表其他 PubMed 时间字段。",
        f"- PubMed 实际检索范围：{(datetime.strptime(date_from, '%Y-%m-%d').date() - timedelta(days=1)).isoformat()} 至 {(datetime.strptime(date_to, '%Y-%m-%d').date() + timedelta(days=1)).isoformat()}（为降低时区边界漏检风险，用户输入范围前后各扩展 1 天）。",
        "",
    ]

    if source_statuses or REPORT_MODE in SOURCE_CHECK_MODES:
        available = [s for s in source_statuses if s.get("status") == "available"]
        unavailable = [s for s in source_statuses if s.get("status") != "available"]
        lines.append("## Source Availability")
        lines.append("")
        lines.append("### Available")
        if available:
            for source in available:
                lines.append(
                    f"- {source['family']} | {source['url']} | matched={source.get('item_count', 0)} | raw={source.get('raw_count', 0)}"
                )
        else:
            lines.append("- None")
        lines.append("")
        lines.append("### Unavailable")
        if unavailable:
            for source in unavailable:
                reason = source.get("reason", "").strip().replace("\n", " ")
                lines.append(f"- {source['family']} | {source['url']} | {reason}")
        else:
            lines.append("- None")
        lines.append("")

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
            lines.append(f"   - Open Access: {item.get('open_access', 'Unknown')}")
            lines.append(f"   - Link: {item['url']}")
            lines.append("")

    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def main() -> None:
    now_utc = datetime.now(timezone.utc)
    range_start, range_end_exclusive, date_from, date_to = resolve_date_range(now_utc)
    active_query_expr = get_active_query_expression(DEFAULT_KEYWORDS)
    keywords_for_filter = extract_keywords_for_title_filter(active_query_expr, DEFAULT_KEYWORDS)

    matched_items: list[dict[str, Any]] = []

    for family_name, family_terms in PUBMED_JOURNAL_FAMILIES.items():
        query = build_query(family_terms, active_query_expr, date_from, date_to)
        try:
            ids = esearch(query, MAX_PER_JOURNAL)
            pubmed_items = validate_pubmed_items(esummary(ids), active_query_expr, family_terms)
            matched_items.extend(pubmed_items)
        except Exception as exc:
            # Skip a failed journal query instead of failing the entire workflow.
            print(f"[WARN] journal query failed: {family_name} | {exc}")

    website_items, source_statuses = fetch_official_website_items(
        range_start,
        range_end_exclusive,
        keywords_for_filter,
    )
    matched_items.extend(website_items)
    matched_items = deduplicate_items(matched_items)
    matched_items.sort(key=lambda x: x.get("published_at") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    matched_items = attach_open_access(matched_items)

    report_path = write_report(
        matched_items,
        now_utc,
        source_statuses,
        active_query_expr,
        date_from,
        date_to,
    )

    if matched_items:
        top = matched_items[:5]
        msg_lines = [f"Journal Watch: {len(matched_items)} matched records from {date_from} to {date_to}"]
        for item in top:
            msg_lines.append(f"- {item['title'][:80]} | {item['url']}")
        send_webhook("\n".join(msg_lines))

    print(f"[INFO] report generated: {report_path}")


if __name__ == "__main__":
    main()
