import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "output"
SITE_DIR = ROOT / "site"
REPORTS_DIR = SITE_DIR / "reports"
INDEX_PATH = SITE_DIR / "reports-index.json"


@dataclass(frozen=True)
class ReportFile:
    name: str
    path: Path
    mtime: float


def collect_reports() -> list[ReportFile]:
    reports: list[ReportFile] = []
    for path in OUTPUT_DIR.glob("report_*.md"):
        reports.append(ReportFile(name=path.name, path=path, mtime=path.stat().st_mtime))
    reports.sort(key=lambda item: item.mtime, reverse=True)
    return reports


def build_site() -> None:
    SITE_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    reports = collect_reports()
    report_entries = []

    for report in reports:
        target = REPORTS_DIR / report.name
        shutil.copy2(report.path, target)
        report_entries.append(
            {
                "name": report.name,
                "url": f"reports/{report.name}",
                "mtime": datetime.fromtimestamp(report.mtime, tz=timezone.utc).isoformat(),
                "size": target.stat().st_size,
            }
        )

    latest = report_entries[0] if report_entries else None
    if latest:
        shutil.copy2(OUTPUT_DIR / latest["name"], SITE_DIR / "latest-report.md")

    index_payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "latest": latest,
        "reports": report_entries,
    }
    INDEX_PATH.write_text(json.dumps(index_payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    build_site()