"""
SF-View/LNDoc duplicate checker for MOADMN collector.

Spec: compare by Matter ID + Date Created using a configurable +/- window.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
import re
import time
from typing import Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import unquote, urlparse

from selenium import webdriver
from selenium.webdriver.support.ui import WebDriverWait


@dataclass(frozen=True)
class SFViewMatch:
    path: Optional[Path]
    matter_id: str
    doc_date: Optional[date]
    label: str = ""


@dataclass(frozen=True)
class SFViewIndexEntry:
    label: str
    matter_id: str
    doc_date: Optional[date]


class SFViewChecker:
    _SOURCE_FILENAME_RE = re.compile(
        r"LDC_SMD_(?P<matter_id>\d{2}-\d{4})(?P<variant>[a-z]*)_(?P<doc_date>\d{8})_E2E\.pdf"
        r"(?:\.[^\"'<>\s]+?\.pdf)?",
        re.IGNORECASE,
    )

    def __init__(
        self,
        log: Callable[[str], None],
        config: Dict,
        driver: Optional[webdriver.Chrome] = None,
    ):
        self.log = log
        self.config = config
        self.driver = driver
        self._path_diagnostics_logged = False
        self._source_index_loaded = False
        self._source_index_error = ""
        self._page_source = ""
        self._source_entries: List[SFViewIndexEntry] = []
        self._entries_by_matter_id: Dict[str, List[SFViewIndexEntry]] = {}

    def attach_driver(self, driver: Optional[webdriver.Chrome]) -> None:
        self.driver = driver

    def invalidate_source_index(self) -> None:
        self._source_index_loaded = False
        self._source_index_error = ""
        self._page_source = ""
        self._source_entries = []
        self._entries_by_matter_id = {}

    @staticmethod
    def _path_accessible(root: Optional[Path]) -> tuple[bool, str]:
        if not root:
            return False, ""
        try:
            return bool(root.exists()), ""
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

    def _ensure_path_diagnostics_logged(self) -> Optional[Path]:
        root = self._sfview_path()
        if not self._path_diagnostics_logged:
            configured = self.config.get("sf_view_url") or ""
            accessible, error = self._path_accessible(root)
            self.log(f"SF-View configured path: {configured}")
            self.log(f"SF-View resolved path: {root}")
            self.log(f"SF-View accessible: {accessible}")
            if error:
                self.log(f"SF-View path check error: {error}")
            self._path_diagnostics_logged = True
        return root

    def _sfview_path(self) -> Optional[Path]:
        raw = self.config.get("sf_view_url") or ""
        if not raw:
            return None
        parsed = urlparse(raw)
        if parsed.scheme == "file":
            if parsed.netloc:
                # file://server/share/path -> \\server\share\path
                path_part = unquote(parsed.path or "").lstrip("/").replace("/", "\\")
                raw = f"\\\\{parsed.netloc}\\{path_part}" if path_part else f"\\\\{parsed.netloc}"
            else:
                raw = unquote(parsed.path or "").replace("/", "\\")
        p = Path(raw)
        return p

    def _sfview_url(self) -> str:
        return str(self.config.get("sf_view_url") or "").strip()

    @staticmethod
    def _extract_mmddyyyy(filename: str) -> Optional[date]:
        # Expected pattern: ..._{MMDDYYYY}_... (we accept any 8-digit token)
        m = re.search(r"(^|_)(\d{8})(_|\.|$)", filename)
        if not m:
            return None
        token = m.group(2)
        try:
            return datetime.strptime(token, "%m%d%Y").date()
        except Exception:
            return None

    def _page_ready(self, chrome: webdriver.Chrome) -> bool:
        try:
            ready_state = chrome.execute_script("return document.readyState")
        except Exception:
            ready_state = ""

        source = chrome.page_source or ""
        if "ERR_FILE_NOT_FOUND" in source or "Your file couldn" in source:
            return True
        return ready_state == "complete" and (
            "addRow(" in source or "Index of" in source or "LDC_SMD_" in source
        )

    def _index_page_source(self, source: str) -> None:
        entries: List[SFViewIndexEntry] = []
        by_matter_id: Dict[str, List[SFViewIndexEntry]] = {}
        seen_labels: set[str] = set()

        for match in self._SOURCE_FILENAME_RE.finditer(source):
            label = re.sub(r"\s+", " ", match.group(0).strip())
            label_key = label.lower()
            if label_key in seen_labels:
                continue
            seen_labels.add(label_key)

            matter_id = str(match.group("matter_id") or "").upper()
            doc_date = self._extract_mmddyyyy(label)
            entry = SFViewIndexEntry(label=label, matter_id=matter_id, doc_date=doc_date)
            entries.append(entry)
            by_matter_id.setdefault(matter_id, []).append(entry)

        self._page_source = source
        self._source_entries = entries
        self._entries_by_matter_id = by_matter_id

        if entries:
            self.log(
                f"MOADMN SF-View index ready: {len(entries)} filenames across "
                f"{len(by_matter_id)} matter IDs."
            )
        else:
            self.log("MOADMN SF-View index loaded, but no matching LDC_SMD PDF filenames were found.")

    def _load_source_index_from_browser(self, force_refresh: bool = False) -> bool:
        self._ensure_path_diagnostics_logged()
        if self._source_index_loaded and not force_refresh and not self._source_index_error:
            return True

        if not self.driver:
            self._source_index_error = "No browser session is attached for SF-View indexing."
            return False

        sf_view_url = self._sfview_url()
        if not sf_view_url:
            self._source_index_error = "The SF-View URL is blank."
            self.log(self._source_index_error)
            return False

        original_handle: Optional[str] = None
        handles_before: List[str] = []
        sf_handle: Optional[str] = None

        try:
            original_handle = self.driver.current_window_handle
            handles_before = list(self.driver.window_handles)
            self.log("Opening MOADMN SF-View in a new tab for duplicate verification...")
            self.driver.execute_script("window.open('about:blank', '_blank');")
            WebDriverWait(self.driver, 10).until(lambda d: len(d.window_handles) > len(handles_before))
            handles_after = list(self.driver.window_handles)
            new_handles = [handle for handle in handles_after if handle not in handles_before]
            sf_handle = new_handles[-1] if new_handles else handles_after[-1]
            self.driver.switch_to.window(sf_handle)
            self.driver.get(sf_view_url)
            WebDriverWait(self.driver, 120, poll_frequency=1.0).until(self._page_ready)
            time.sleep(1.0)
            self._index_page_source(self.driver.page_source or "")
            self._source_index_error = ""
            self._source_index_loaded = True
            return True
        except Exception as exc:
            self._source_index_error = f"{type(exc).__name__}: {exc}"
            self._source_entries = []
            self._entries_by_matter_id = {}
            self._page_source = ""
            self._source_index_loaded = True
            self.log(f"MOADMN SF-View page load failed: {self._source_index_error}")
            return False
        finally:
            try:
                if sf_handle and sf_handle in self.driver.window_handles:
                    self.driver.close()
            except Exception:
                pass
            try:
                if original_handle and original_handle in self.driver.window_handles:
                    self.driver.switch_to.window(original_handle)
            except Exception:
                pass

    def prepare_index(self, force_refresh: bool = False) -> bool:
        if self.driver:
            return self._load_source_index_from_browser(force_refresh=force_refresh)
        root = self._ensure_path_diagnostics_logged()
        accessible, _ = self._path_accessible(root)
        return accessible

    def _entries_for_matter_id(self, matter_id: str) -> List[SFViewIndexEntry]:
        normalized = str(matter_id or "").strip().upper()
        if not normalized:
            return []
        if self.driver:
            self._load_source_index_from_browser(force_refresh=False)
            return list(self._entries_by_matter_id.get(normalized, []))
        return []

    def find_matches(self, matter_id: str) -> Iterable[SFViewMatch]:
        root = self._sfview_path()
        if not root or not root.exists():
            return []
        # Broad search: any PDF containing the matter ID.
        pattern = f"*{matter_id}*.pdf"
        matches = []
        try:
            for p in root.rglob(pattern):
                matches.append(SFViewMatch(path=p, matter_id=matter_id, doc_date=self._extract_mmddyyyy(p.name)))
        except Exception:
            return []
        return matches

    def latest_document(self) -> Optional[SFViewMatch]:
        """Return the most recent SF-View PDF by parsed date token, fallback by mtime."""
        root = self._sfview_path()
        if not root or not root.exists():
            return None
        try:
            pdfs = list(root.rglob("*.pdf"))
        except Exception:
            return None
        if not pdfs:
            return None

        dated: list[SFViewMatch] = []
        for p in pdfs:
            doc_date = self._extract_mmddyyyy(p.name)
            if doc_date:
                dated.append(SFViewMatch(path=p, matter_id="", doc_date=doc_date))
        if dated:
            return max(dated, key=lambda m: m.doc_date)

        newest = max(pdfs, key=lambda p: p.stat().st_mtime)
        return SFViewMatch(path=newest, matter_id="", doc_date=None)

    def exists_for_matter_id(self, matter_id: str) -> Tuple[bool, Optional[str]]:
        """Return (exists, example_path) for any LN Doc PDF that contains this matter ID."""
        indexed_entries = self._entries_for_matter_id(matter_id)
        if indexed_entries:
            label = indexed_entries[0].label
            self.log(f"SF-View match found for {matter_id}: {label}")
            return True, label
        if self.driver:
            return False, None

        root = self._ensure_path_diagnostics_logged()
        accessible, error = self._path_accessible(root)
        if not accessible:
            self.log(f"SF-View path not accessible ({root}); proceeding without duplicate flags")
            if error:
                self.log(f"SF-View path access failure: {error}")
            return False, None

        for match in self.find_matches(matter_id):
            label = match.label or (match.path.name if match.path else "")
            self.log(f"SF-View match found for {matter_id}: {label}")
            return True, str(match.path) if match.path else label

        return False, None

    def exists_within_window(self, matter_id: str, created_date: date, window_days: int) -> Tuple[bool, Optional[str]]:
        """Return (exists, example_path) for this matter within +/- window_days of created_date."""
        start = created_date - timedelta(days=window_days)
        end = created_date + timedelta(days=window_days)

        indexed_entries = self._entries_for_matter_id(matter_id)
        for entry in indexed_entries:
            if not entry.doc_date:
                continue
            if start <= entry.doc_date <= end:
                self.log(f"SF-View duplicate found for {matter_id} within window: {entry.label}")
                return True, entry.label
        if self.driver:
            return False, None

        root = self._ensure_path_diagnostics_logged()
        accessible, error = self._path_accessible(root)
        if not accessible:
            self.log(f"SF-View path not accessible ({root}); proceeding without duplicate skipping")
            if error:
                self.log(f"SF-View path access failure: {error}")
            return False, None

        for match in self.find_matches(matter_id):
            if not match.doc_date:
                # If date can't be parsed, we treat as a weak signal; do not auto-skip.
                continue
            if start <= match.doc_date <= end:
                label = match.label or (match.path.name if match.path else "")
                self.log(f"SF-View duplicate found for {matter_id} within window: {label}")
                return True, str(match.path) if match.path else label

        return False, None


