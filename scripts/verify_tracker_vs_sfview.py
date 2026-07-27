from __future__ import annotations

import argparse
import re
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

from config.config import ConfigManager
from utils.lndoc_tracker_checker import LNDocTrackerChecker
from utils.sfview_checker import SFViewChecker


@dataclass(frozen=True)
class TargetDocument:
    matter_id: str
    filename: str
    created_date: date


TARGET_FILENAME_RE = re.compile(
    r"^LDC_SMD_(?P<matter_id>\d{2}-\d{4})[a-z]*_(?P<created>\d{8})_E2E\.pdf$",
    re.IGNORECASE,
)


DEFAULT_TARGETS = [
    TargetDocument("26-0169", "LDC_SMD_26-0169_03042026_E2E.pdf", date(2026, 3, 4)),
    TargetDocument("26-0318", "LDC_SMD_26-0318_03312026_E2E.pdf", date(2026, 3, 31)),
]

DEFAULT_SF_VIEW_URL = "file://fntd5bdata09/pclprod/sf-view/PROD/team10/moadmn/"
REPORT_OUTPUT_DIR = PROJECT_ROOT / "outputs" / "duplicate_source_checks"
_REPORT_LINES: List[str] = []


def _timestamp() -> str:
    return datetime.now().strftime("%I:%M:%S %p")


def _emit(line: str) -> None:
    _REPORT_LINES.append(line)
    print(line, flush=True)


def _log(message: str) -> None:
    _emit(f"{_timestamp()} - {message}")


def _report_path() -> Path:
    REPORT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    return REPORT_OUTPUT_DIR / f"tracker_vs_sfview_{datetime.now():%Y%m%d_%H%M%S}.txt"


def _save_report(path: Path) -> None:
    path.write_text("\n".join(_REPORT_LINES) + "\n", encoding="utf-8")


def _target_key(target: TargetDocument) -> str:
    return target.filename.strip().lower()


def _parse_target_document(raw_value: str) -> TargetDocument:
    raw_text = str(raw_value or "").strip()
    if not raw_text:
        raise ValueError("Target value is blank.")

    filename = Path(raw_text).name.strip()
    match = TARGET_FILENAME_RE.match(filename)
    if not match:
        raise ValueError(
            f"Could not parse target filename {filename!r}. Expected format like "
            f"'LDC_SMD_26-0169_03042026_E2E.pdf'."
        )

    matter_id = str(match.group("matter_id") or "").upper()
    created_token = str(match.group("created") or "")
    try:
        created_date = datetime.strptime(created_token, "%m%d%Y").date()
    except Exception as exc:
        raise ValueError(f"Could not parse created date from {filename!r}: {exc}") from exc

    return TargetDocument(matter_id=matter_id, filename=filename, created_date=created_date)


def _iter_target_inputs(args: argparse.Namespace) -> List[str]:
    raw_targets: List[str] = []

    if args.targets:
        raw_targets.extend(args.targets)

    if args.targets_file:
        target_file = Path(args.targets_file)
        if not target_file.exists():
            raise FileNotFoundError(f"Target list file does not exist: {target_file}")
        for line in target_file.read_text(encoding="utf-8").splitlines():
            cleaned = line.strip()
            if not cleaned or cleaned.startswith("#"):
                continue
            raw_targets.append(cleaned)

    if args.targets_folder:
        target_folder = Path(args.targets_folder)
        if not target_folder.exists():
            raise FileNotFoundError(f"Target folder does not exist: {target_folder}")
        if not target_folder.is_dir():
            raise NotADirectoryError(f"Target folder is not a directory: {target_folder}")
        for path in sorted(target_folder.iterdir()):
            if path.is_file() and path.suffix.lower() == ".pdf":
                raw_targets.append(str(path))

    return raw_targets


def _resolve_targets(args: argparse.Namespace) -> List[TargetDocument]:
    raw_targets = _iter_target_inputs(args)
    if not raw_targets:
        return list(DEFAULT_TARGETS)

    targets: List[TargetDocument] = []
    seen: set[str] = set()
    for raw_value in raw_targets:
        target = _parse_target_document(raw_value)
        key = _target_key(target)
        if key in seen:
            continue
        seen.add(key)
        targets.append(target)

    if not targets:
        raise ValueError("No valid targets were found after parsing the supplied inputs.")

    return targets


def _build_driver(headless: bool = True) -> webdriver.Chrome:
    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1600,1200")
    options.add_argument("--log-level=3")
    options.add_experimental_option("excludeSwitches", ["enable-logging"])

    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=options)


def _contains_exact_filename(entry: Dict[str, object], filename: str) -> bool:
    candidates: List[str] = []

    primary = str(entry.get("filename") or "").strip()
    if primary:
        candidates.append(primary)

    filenames = entry.get("filenames")
    if isinstance(filenames, list):
        for value in filenames:
            label = str(value or "").strip()
            if label:
                candidates.append(label)

    filename_lower = filename.lower()
    return any(candidate.lower() == filename_lower for candidate in candidates)


def _find_tracker_row_hits(row_snapshots: List[List[str]], needle: str) -> List[str]:
    hits: List[str] = []
    needle_lower = needle.lower()

    for cells in row_snapshots:
        row_text = " | ".join(str(cell or "").strip() for cell in cells if str(cell or "").strip())
        if row_text and needle_lower in row_text.lower():
            hits.append(row_text)
    return hits


def _find_sf_view_source_hits(source: str, needle: str, limit: int = 10) -> List[str]:
    if not source or not needle:
        return []

    hits: List[str] = []
    needle_lower = needle.lower()
    source_lower = source.lower()
    start_at = 0

    while len(hits) < limit:
        idx = source_lower.find(needle_lower, start_at)
        if idx == -1:
            break

        row_start = source.rfind("addRow(", 0, idx)
        row_end = source.find("</script>", idx)
        if row_start != -1 and row_end != -1:
            snippet = source[row_start : row_end + len("</script>")]
        else:
            snippet = source[max(0, idx - 180) : min(len(source), idx + 260)]

        normalized = re.sub(r"\s+", " ", snippet.strip())
        if normalized not in hits:
            hits.append(normalized)
        start_at = idx + len(needle)

    return hits


def _load_sf_view_page_source(driver: webdriver.Chrome, sf_view_url: str, timeout: int = 120) -> str:
    _log("Opening the SF-View browser page for page-source verification...")
    driver.get(sf_view_url)

    def page_ready(chrome: webdriver.Chrome) -> bool:
        try:
            ready_state = chrome.execute_script("return document.readyState")
        except Exception:
            ready_state = ""

        source = chrome.page_source or ""
        if "ERR_FILE_NOT_FOUND" in source or "Your file couldn" in source:
            return True
        return ready_state == "complete" and ("addRow(" in source or "Index of" in source)

    WebDriverWait(driver, timeout, poll_frequency=1.0).until(page_ready)
    time.sleep(1.0)
    return driver.page_source or ""


def check_ln_doc_tracker(
    cfg: Dict[str, object],
    targets: List[TargetDocument],
    headless: bool = True,
) -> Dict[str, Dict[str, object]]:
    tracker_results: Dict[str, Dict[str, object]] = {}
    driver: Optional[webdriver.Chrome] = None

    start_date = min(target.created_date for target in targets)
    end_date = max(target.created_date for target in targets)

    _log("Opening LN Doc Tracker for focused verification...")
    try:
        driver = _build_driver(headless=headless)
        checker = LNDocTrackerChecker(driver, _log, cfg)

        if not checker.navigate_to_tracker():
            raise RuntimeError("Could not open LN Doc Tracker.")
        if not checker.setup_query():
            raise RuntimeError("Could not prepare the LN Doc Tracker search.")
        if not checker.enter_search_criteria(start_date, end_date):
            raise RuntimeError("Could not enter the LN Doc Tracker date range.")
        if not checker.wait_for_report(timeout=int(cfg.get("tracker_report_timeout_seconds", 60) or 60)):
            raise RuntimeError("LN Doc Tracker did not finish loading.")

        row_snapshots = checker._extract_tracker_rows_snapshot(force_refresh=True)
        matter_map = checker.extract_tracker_matter_map(row_snapshots)
        _log(f"Focused tracker report contains {len(matter_map)} extracted matter IDs.")

        for target in targets:
            _log(
                f"Checking LN Doc Tracker presence for {target.matter_id} "
                f"({target.filename})..."
            )
            entry = matter_map.get(target.matter_id.upper()) or {}
            filename_row_hits = _find_tracker_row_hits(row_snapshots, target.filename)
            matter_row_hits = _find_tracker_row_hits(row_snapshots, target.matter_id)

            tracker_results[_target_key(target)] = {
                "matter_id_in_map": bool(entry),
                "exact_filename_in_map": _contains_exact_filename(entry, target.filename) if entry else False,
                "matter_id_in_raw_rows": bool(matter_row_hits),
                "exact_filename_in_raw_rows": bool(filename_row_hits),
                "map_entry": entry,
                "matter_row_examples": matter_row_hits[:5],
                "filename_row_examples": filename_row_hits[:5],
            }
            _log(
                f"LN Doc Tracker result for {target.matter_id}: "
                f"map_has_id={bool(entry)} | raw_rows_with_id={bool(matter_row_hits)} | "
                f"raw_rows_with_filename={bool(filename_row_hits)}"
            )
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass

    return tracker_results


def check_sf_view(
    cfg: Dict[str, object],
    targets: List[TargetDocument],
    headless: bool = True,
) -> Dict[str, Dict[str, object]]:
    sf_results: Dict[str, Dict[str, object]] = {}
    checker = SFViewChecker(_log, cfg)
    root = checker._sfview_path()
    driver: Optional[webdriver.Chrome] = None

    root_accessible = False
    root_error = ""
    try:
        root_accessible = bool(root and root.exists())
    except Exception as exc:
        root_error = f"{type(exc).__name__}: {exc}"

    _log(f"SF-View root: {root}")
    _log(f"SF-View accessible: {root_accessible}")
    if root_error:
        _log(f"SF-View access error: {root_error}")

    page_source = ""
    page_title = ""
    page_url = ""
    page_error = ""

    if root_accessible:
        try:
            driver = _build_driver(headless=headless)
            page_source = _load_sf_view_page_source(driver, str(cfg.get("sf_view_url") or ""))
            page_title = driver.title or ""
            page_url = driver.current_url or ""
            _log(f"SF-View page loaded: title={page_title!r} | url={page_url}")
        except Exception as exc:
            page_error = f"{type(exc).__name__}: {exc}"
            _log(f"SF-View page load failed: {page_error}")

    try:
        for target in targets:
            _log(
                f"Checking SF-View exact filename for {target.matter_id} "
                f"({target.filename})..."
            )
            exact_hits: List[str] = []
            matter_hits: List[str] = []
            exact_error = ""
            matter_error = ""

            if page_source:
                try:
                    exact_hits = _find_sf_view_source_hits(page_source, target.filename)
                    _log(
                        f"SF-View exact filename page-source check finished for "
                        f"{target.matter_id}: {len(exact_hits)} hit(s)."
                    )
                except Exception as exc:
                    exact_error = f"{type(exc).__name__}: {exc}"
                    _log(
                        f"SF-View exact filename page-source check failed for "
                        f"{target.matter_id}: {exact_error}"
                    )

                _log(f"Checking SF-View matter ID search for {target.matter_id}...")
                try:
                    matter_hits = _find_sf_view_source_hits(page_source, target.matter_id)
                    _log(
                        f"SF-View matter ID page-source check finished for "
                        f"{target.matter_id}: {len(matter_hits)} hit(s)."
                    )
                except Exception as exc:
                    matter_error = f"{type(exc).__name__}: {exc}"
                    _log(
                        f"SF-View matter ID page-source check failed for "
                        f"{target.matter_id}: {matter_error}"
                    )
            elif page_error:
                exact_error = page_error
                matter_error = page_error
                _log(
                    f"Skipping SF-View searches for {target.matter_id} because the page "
                    f"could not be loaded."
                )
            else:
                _log(
                    f"Skipping SF-View searches for {target.matter_id} because the root "
                    f"is not accessible."
                )

            sf_results[_target_key(target)] = {
                "root": str(root) if root else "",
                "root_accessible": root_accessible,
                "root_error": root_error,
                "page_title": page_title,
                "page_url": page_url,
                "page_error": page_error,
                "exact_filename_hits": exact_hits[:10],
                "matter_id_hits": matter_hits[:10],
                "exact_error": exact_error,
                "matter_error": matter_error,
            }
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass

    return sf_results


def print_summary(
    targets: List[TargetDocument],
    tracker_results: Dict[str, Dict[str, object]],
    sf_results: Dict[str, Dict[str, object]],
) -> None:
    _emit("")
    _emit("=" * 88)
    _emit("TRACKER VS SF-VIEW CONFIRMATION")
    _emit("=" * 88)

    for target in targets:
        tracker = tracker_results.get(_target_key(target), {})
        sf_view = sf_results.get(_target_key(target), {})

        _emit("")
        _emit(f"Target matter ID : {target.matter_id}")
        _emit(f"Expected file    : {target.filename}")
        _emit(f"Created date     : {target.created_date:%m/%d/%Y}")
        _emit("-" * 88)
        _emit(f"LN Doc Tracker map has matter ID     : {tracker.get('matter_id_in_map')}")
        _emit(f"LN Doc Tracker map has exact filename: {tracker.get('exact_filename_in_map')}")
        _emit(f"Tracker raw rows mention matter ID   : {tracker.get('matter_id_in_raw_rows')}")
        _emit(f"Tracker raw rows mention filename    : {tracker.get('exact_filename_in_raw_rows')}")

        matter_examples = tracker.get("matter_row_examples") or []
        if matter_examples:
            _emit("Tracker matter row examples:")
            for example in matter_examples:
                _emit(f"  - {example}")

        filename_examples = tracker.get("filename_row_examples") or []
        if filename_examples:
            _emit("Tracker filename row examples:")
            for example in filename_examples:
                _emit(f"  - {example}")

        _emit(f"SF-View root accessible             : {sf_view.get('root_accessible')}")
        root_error = sf_view.get("root_error") or ""
        if root_error:
            _emit(f"SF-View root error                  : {root_error}")
        page_title = sf_view.get("page_title") or ""
        if page_title:
            _emit(f"SF-View page title                  : {page_title}")
        page_url = sf_view.get("page_url") or ""
        if page_url:
            _emit(f"SF-View page URL                    : {page_url}")
        page_error = sf_view.get("page_error") or ""
        if page_error:
            _emit(f"SF-View page error                  : {page_error}")

        exact_hits = sf_view.get("exact_filename_hits") or []
        _emit(f"SF-View exact filename hits         : {len(exact_hits)}")
        for hit in exact_hits:
            _emit(f"  - {hit}")

        matter_hits = sf_view.get("matter_id_hits") or []
        _emit(f"SF-View matter ID hits              : {len(matter_hits)}")
        for hit in matter_hits:
            _emit(f"  - {hit}")

        exact_error = sf_view.get("exact_error") or ""
        if exact_error:
            _emit(f"SF-View exact-hit error             : {exact_error}")

        matter_error = sf_view.get("matter_error") or ""
        if matter_error:
            _emit(f"SF-View matter-hit error            : {matter_error}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Confirm whether selected MOADMN filenames exist in LN Doc Tracker and/or SF-View."
        )
    )
    parser.add_argument(
        "targets",
        nargs="*",
        help=(
            "Optional local PDF paths or bare LDC_SMD filenames to verify. "
            "If omitted, the default focused targets are used."
        ),
    )
    parser.add_argument(
        "--targets-file",
        help=(
            "Text file containing one local PDF path or LDC_SMD filename per line."
        ),
    )
    parser.add_argument(
        "--targets-folder",
        help=(
            "Folder containing collected/excluded PDFs to verify. "
            "All '*.pdf' files in the folder will be checked."
        ),
    )
    parser.add_argument(
        "--sf-view-url",
        default=DEFAULT_SF_VIEW_URL,
        help="Override the MOADMN SF-View URL/path used for this diagnostic run.",
    )
    parser.add_argument(
        "--show-browser",
        action="store_true",
        help="Run Chrome with a visible window instead of headless mode.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = dict(ConfigManager().config)
    config["sf_view_url"] = args.sf_view_url
    report_path = _report_path()

    _log("Starting focused duplicate-source verification.")
    _log(f"Using SF-View URL: {config['sf_view_url']}")
    _log(f"Report will be saved to: {report_path}")

    try:
        targets = _resolve_targets(args)
        _log(f"Loaded {len(targets)} target file(s) for verification.")
        tracker_results = check_ln_doc_tracker(config, targets, headless=not args.show_browser)
        sf_results = check_sf_view(config, targets, headless=not args.show_browser)
        print_summary(targets, tracker_results, sf_results)
        _save_report(report_path)
        _log("Verification complete.")
        _log(f"Saved report: {report_path}")
        return 0
    except Exception as exc:
        _log(f"Verification failed: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        try:
            _save_report(report_path)
            _log(f"Saved partial report: {report_path}")
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
