"""
MOADMN scraping/collection thread.

Implements the workflow in `MOADMN.md`:
- Create `Downloads/MOADMN Collections/MOADMN_mm-dd-yyyy_hh-mm-ss-ms/`
- Navigate to instructions page -> portal -> login -> Matters
- Filter matters by year prefix (25-/26-), sort by Matter ID, set items/page=100
- For each matter: Documents tab -> search "order" -> collect docs in date range
- Duplicate check against SF-View (±1 year) before downloading
- Download + rename to `LDC_SMD_{matter_id}_{mmddyyyy}_E2E.pdf`
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
import traceback
import re
from urllib.parse import urlparse

from PyQt6.QtCore import QThread, pyqtSignal

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, StaleElementReferenceException

from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options

from config.config import ConfigManager
from utils.logger import Logger
from utils.lndoc_tracker_checker import LNDocTrackerChecker, TrackerCheckFailed
from utils.pdf_quality_gate import PdfQualityGate, PdfQualityResult
from utils.sfview_checker import SFViewChecker
from reporting.excel_reporter import ExcelReporter


@dataclass
class DocumentItem:
    matter_id: str
    title: str
    date_created: Optional[date]
    download_click: Optional[Callable[[], None]] = None  # late-bound click action
    pdf_evidence: str = ""
    row_text: str = ""


@dataclass
class MatterTarget:
    href: str
    matter_id: str = ""


DOWNLOAD_CAPTURE_SUFFIXES = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".txt")
DOWNLOAD_RETRY_ATTEMPTS = 3


INCLUDE_TITLE_PATTERNS = [
    "Order denying motion to dismiss or correct caption",
    "Order - Deny Motion to Dismiss",
    "Order - Motion for Hearing",
    "Order - Rescind Status Report Due and Parties to Notify",
    "Order - Status Report Requested",
    "Order - Status Report Due",
    "Order - JSR Due",
    "Order - Respondent Status Report Due",
    "Order - Resp Status Report Due",
    "Order - Grant Continuance and Reschedule",
    "Order - Grant Continuance",
    "Order - Grant Motion to Continue",
    "Order - Grant Petitioner's Motion to Continue",
    "Order - Granting Petitioner's Motion for Continuance",
    "Order - MTC Granted Status Reported Due",
    "Order - MTC Granted Status Report Due",
    "Order - MTC Granted Status Report",
    "Order - Continuance Granted Status Report Due",
    "Order - Grant Motion to File Amended Answer",
    "Order - Grant Motion to File Answer Out of Time",
    "Order - Grant Motion to File Answ. Out of Time",
    "Order - Grant Motion for Additional Time to File Answer",
    "Order - Grant Motion to File Out of Time",
    "Order - Grant Motion to File Amended Compl",
    "Order - Grant Leave to File Amended Complaint",
    "Order - parties to notify when ready for hearing",
    "Order Status Report Requested - MMO",
    "Order - Granting Extension of Time",
    "Order - Grant Request for Extension of Time",
    "Order - Grant Motion Leave to Withdraw as Attorney",
    "Order - Grant Motion to Withdraw as Attorney",
    "Order - written arguments on effect federal case due",
    "Order granting mtn for continuance of deposition",
    "Order - Granting Respondent's motion",
    "Order granting mtn to correct error and substitute",
    "Order - Hearing scheduled",
    "Order - Hearing Set",
    "Order - Hearing Reset",
    "Order - Hearing Rescheduled",
    "Order - Hearings Rescheduled",
    "Order - Evidentiary Hearing Rescheduled",
    "Order - Set Status Hearing",
    "Order - Setting Status Hearing",
    "Order - Set for Status Hearing",
    "Order - Status Hearing Scheduled",
    "Order - Status Hearing Rescheduled",
    "Order - Set Status and Evidentiary Hearings",
    "Order - Cancel EH & Set SH",
    "Order - Cont EH & Set SH",
    "Order - Continue EH",
    "Order - Continue SH",
    "Order - Rescheduling EHD",
    "Amended Order - Rescheduling EHD",
    "Order - Resetting EH",
    "Order - MTC Granted and Reschedule",
    "Order - MTC Granted and Rescheduled",
    "Order - MTC Granted and Reschedule SH",
    "Order - MTC granted and Reschedule",
    "Order - Grant MTC and Reschedule",
    "Order - Grant MTC, schedule SHD",
    "Order adopting proposed protective order",
    "Order Status Report Due",
    "Order rescinding",
    "Decision issued",
    "Default Decision issued",
    "Order of Dismissal",
    "Order of Dismiisal",
    "Order - Dismissal",
    "Order - Petitioner's Dismissal",
    "Order - Petitioner's Dismissal - DOR",
    "Order - Dismissal DOR - No Dispute",
    "Order - Dismissal DOR - No Dispute - Sales/Use",
    "Order - No Show Dismiss",
    "Order - DOR No Show",
    "Order - Set Aside and Consolidate",
    "Order Consolidating for Discovery and Hearing",
    "Order - Consolidating",
    "Order - Consolidating cases",
    "Order of Consolidation",
    "Order - Show Cause",
    "Order - Post SH",
    "Order - Post SHD",
    "Post SH Order",
    "Order - Post EH - Status Report Requested",
    "Order - Grant Motion to Reinstate",
]

EXCLUDE_TITLE_PATTERNS = [
    "Scheduling Order",
    "Protective order issued",
    "Unopposed Motion for Protective Order",
    "Motion for Protective Order",
    "Exhibit A - Proposed Protective Order",
    "Show Cause Order",
    "Show-cause order",
    "Order - Show Cause",
    "Order - Show Cause Response",
    "Response to Show Cause Order",
    "Respondent's Response to Show Cause Order",
    "Resp. Response to Show Cause Order",
    "Petitioner's Response to Show Cause Order",
    "Pet. Response to Show Cause Order",
    "Answer",
    "Amended Answer",
    "Complaint",
    "Motion to Dismiss",
    "Petitioner's Motion to Dismiss",
    "Motion to Continue Trial Setting",
    "Consent Order",
    "Joint Request for Consent Order",
    "Continuance Order",
    "Continuance Order Set SH",
    "Exhibit 8 - Order on Motion to Quash",
    "Ex. 6 Order",
    "Respondent's Exhibit 15 - Order of Revocation",
    "Order - Answer Due",
    "Order - Respondent's Answer Due",
    "Order - Resp to File Answer",
    "Order - Close Complaint",
]


def _mmddyyyy(d: date) -> str:
    return d.strftime("%m%d%Y")


def _run_folder_name(now: datetime) -> str:
    # Spec: MOADMN_mm-dd-yyyy_hh-mm-ss-m.s (unique). We use milliseconds suffix.
    ms = int(now.microsecond / 1000)
    return f"MOADMN_{now:%m-%d-%Y}_{now:%H-%M-%S}-{ms:03d}"


class MOADMNScraper(QThread):
    log_signal = pyqtSignal(str)
    progress_signal = pyqtSignal(int)
    status_signal = pyqtSignal(str)
    finished_signal = pyqtSignal(bool, str, str)  # ok, message, run_dir

    class UserCancelled(Exception):
        """Raised when user requests a cooperative stop."""

    class SignInIssue(Exception):
        """Raised when the portal sign-in step cannot complete safely."""

    def __init__(self, start_date: date, end_date: date, headless: bool = False):
        super().__init__()
        self.start_date = start_date
        self.end_date = end_date
        self.headless = headless
        self.config = ConfigManager()
        self.pdf_quality_gate: Optional[PdfQualityGate] = self._build_pdf_quality_gate()
        self.driver: Optional[webdriver.Chrome] = None
        self.run_dir: Optional[Path] = None
        self.primary_window_handle: Optional[str] = None

        self.downloaded: List[Dict] = []
        self.skipped_duplicates: List[Dict] = []
        self.excluded_non_order: List[Dict] = []
        self.review_rows: List[Dict] = []
        self.download_failures: List[Dict] = []
        self.pending_download_renames: List[Dict] = []
        self._tracker_matter_map: Dict[str, Dict[str, object]] = {}
        self._sfview_checker: Optional[SFViewChecker] = None

        # Logger will be bound once run_dir is created so logs go both to file and console.
        self.logger: Optional[Logger] = None

    def _build_pdf_quality_gate(self) -> Optional[PdfQualityGate]:
        if not bool(self.config.get("pdf_quality_gate_enabled", True)):
            return None
        return PdfQualityGate(
            reject_non_order=bool(self.config.get("pdf_quality_reject_non_order", True)),
            reject_content_duplicates=bool(self.config.get("pdf_quality_reject_content_duplicates", True)),
            reject_neutralized_duplicates=bool(self.config.get("pdf_quality_reject_neutralized_duplicates", True)),
            reject_cross_docket_mentions=bool(self.config.get("pdf_quality_reject_cross_docket_mentions", True)),
            tesseract_path=str(self.config.get("tesseract_path", "") or ""),
            tesseract_lang=str(self.config.get("tesseract_lang", "eng") or "eng"),
        )

    def _log(self, msg: str) -> None:
        """Log to file/console (via Logger) and to the GUI in real time."""
        msg = self._format_log_message(msg)
        # Console + file
        if self.logger is not None:
            try:
                self.logger.info(msg)
            except Exception:
                pass
        # GUI log panel
        try:
            self.log_signal.emit(msg)
        except Exception:
            pass

    def _status(self, msg: str) -> None:
        """Emit human-friendly status AND record it in the log/console."""
        msg = self._format_status_message(msg)
        # Log as info so statuses appear in the run log and console.
        self._log(f"[STATUS] {msg}")
        try:
            self.status_signal.emit(msg)
        except Exception:
            pass

    def _describe_exception(self, exc: Exception, include_url: bool = True) -> str:
        exc_type = type(exc).__name__
        candidates: List[str] = []

        for value in (str(exc), getattr(exc, "msg", "")):
            if not isinstance(value, str):
                continue
            cleaned = re.sub(r"\s+", " ", value).strip()
            if not cleaned:
                continue
            if cleaned.lower() in {"message", "message:"}:
                continue
            if cleaned not in candidates:
                candidates.append(cleaned)

        parts = [exc_type]
        if candidates:
            parts.append(candidates[0])
        else:
            parts.append("The browser or driver did not return a useful error message.")

        if include_url and self.driver:
            try:
                current_url = (self.driver.current_url or "").strip()
            except Exception:
                current_url = ""
            if current_url:
                parts.append(f"URL: {current_url}")

        return " | ".join(parts)

    def _portal_block_signs(self) -> List[str]:
        phrases = [
            "access denied",
            "forbidden",
            "verify you are human",
            "captcha",
            "unusual traffic",
            "blocked",
            "bot detection",
            "attention required",
            "request unsuccessful",
            "incident id",
            "akamai",
            "cloudflare",
        ]
        snippets: List[str] = []
        try:
            combined = " ".join(
                [
                    self.driver.title or "",
                    self.driver.current_url or "",
                    self.driver.page_source or "",
                ]
            ).lower()
        except Exception:
            combined = ""

        for phrase in phrases:
            if phrase in combined and phrase not in snippets:
                snippets.append(phrase)
        return snippets

    def _build_sign_in_issue_message(self, detail: str = "") -> str:
        message = (
            "Sign-in could not be completed. Please check that your VPN is connected to an approved "
            "location such as Cincinnati or Cleveland. The MOADMN site may also have detected or blocked "
            "the automation session. Please contact the developer to resolve this issue."
        )
        block_signs = self._portal_block_signs()
        if block_signs:
            message += f" Possible site-block signs detected: {', '.join(block_signs[:4])}."
        if detail:
            message += f" Details: {detail}"
        return message

    @staticmethod
    def _normalize_operator_text(msg: str) -> str:
        text = msg or ""
        replacements = {
            "Ã¢â‚¬Â¦": "...",
            "â€¦": "...",
            "…": "...",
            "…": "...",
            "Â±": "+/-",
        }
        replacements["\u2026"] = "..."
        replacements["\u00c2\u00b1"] = "+/-"
        replacements["\u00b1"] = "+/-"
        for old, new in replacements.items():
            text = text.replace(old, new)
        return text

    @staticmethod
    def _clean_display_value(value: str) -> str:
        text = re.sub(r"\s+", " ", (value or "").strip())
        if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
            text = text[1:-1]
        return text

    def _format_status_message(self, msg: str) -> str:
        text = self._normalize_operator_text(msg)
        patterns = [
            (r"^Loading MOADMN portal\.\.\.$", "Opening the MOADMN portal..."),
            (r"^Signing in\.\.\.$", "Signing in to the public portal..."),
            (r"^Opening Matters\.\.\.$", "Opening the Matters page..."),
            (r"^Listing matters \(([^)]+)\)\.\.\.$", r"Building the \1 matter list..."),
            (r"^Scanning matters \(([^)]+)\)\.\.\.$", r"Scanning the \1 matter list..."),
            (
                r"^Loading page (\d+) of the ([^ ]+) matter list\.\.\.$",
                lambda m: f"Loading page {m.group(1)} of the {m.group(2)} matter list...",
            ),
            (
                r"^Reviewing the matter list in LN Doc Tracker\.\.\.$",
                "Opening LN Doc Tracker and loading existing filings...",
            ),
            (
                r"^Checking LN Doc for matter ([^ ]+) \((\d+) of (\d+)\)\.\.\.$",
                lambda m: f"Checking LN Doc duplicates for {m.group(1)} ({m.group(2)} of {m.group(3)})...",
            ),
            (
                r"^Preparing the ([^ ]+) matter list for document review\.\.\.$",
                lambda m: f"Preparing the {m.group(1)} matter list for document review...",
            ),
            (
                r"^Refreshing the ([^ ]+) matter list before continuing document review\.\.\.$",
                lambda m: f"Refreshing the {m.group(1)} matter list before continuing...",
            ),
            (
                r"^Reviewing matter (\d+)/(\d+): ([^\.]+)\.\.\.$",
                lambda m: f"Reviewing matter {m.group(3)} ({m.group(1)} of {m.group(2)})...",
            ),
            (
                r"^Downloading matter (\d+)/(\d+): ([^\.]+)\.\.\.$",
                lambda m: f"Downloading documents for {m.group(3)} ({m.group(1)} of {m.group(2)})...",
            ),
            (
                r"^Finding matter ([^\.]+) in the list\.\.\.$",
                lambda m: f"Finding matter {m.group(1)} in the list...",
            ),
            (
                r"^Opening matter ([^\.]+)\.\.\.$",
                lambda m: f"Opening matter {m.group(1)}...",
            ),
            (
                r"^Opening the Documents tab for ([^\.]+)\.\.\.$",
                lambda m: f"Opening the Documents tab for {m.group(1)}...",
            ),
            (
                r"^Switching ([^\.]+) to document list view\.\.\.$",
                lambda m: f"Switching {m.group(1)} to document list view...",
            ),
            (
                r"^Searching documents for Order items in ([^\.]+)\.\.\.$",
                lambda m: f"Searching documents for Order items in {m.group(1)}...",
            ),
            (
                r"^Checking matching document rows in ([^\.]+)\.\.\.$",
                lambda m: f"Checking matching document rows in {m.group(1)}...",
            ),
            (
                r"^Downloading an Order PDF for ([^\.]+)\.\.\.$",
                lambda m: f"Downloading an Order PDF for {m.group(1)}...",
            ),
            (r"^Finalizing downloaded file names\.\.\.$", "Finalizing downloaded file names..."),
            (r"^Saving the run report\.\.\.$", "Saving the run report..."),
            (
                r"^Page (\d+) - Matter (\d+)/(\d+) \(processed (\d+)\)\.\.\.$",
                lambda m: (
                    f"Reviewing page {m.group(1)}, matter {m.group(2)} of {m.group(3)} "
                    f"({m.group(4)} reviewed)..."
                ),
            ),
        ]
        for pattern, replacement in patterns:
            matched = re.match(pattern, text)
            if not matched:
                continue
            if callable(replacement):
                return replacement(matched)
            return re.sub(pattern, replacement, text)
        return text

    def _format_log_message(self, msg: str) -> str:
        text = self._normalize_operator_text(msg)
        if "\n" in text or "Stacktrace:" in text:
            return text

        ui_patterns = [
            (
                r"^\[STATUS\] (.+)$",
                lambda m: f"Status: {m.group(1)}",
            ),
            (
                r"^Run folder: (.+)$",
                lambda m: f"Run folder created: {m.group(1)}",
            ),
            (
                r"^MOADMN run started \| date range ([^|]+) - ([^|]+) \| headless=(.+)$",
                lambda m: (
                    f"Run started for {m.group(1).strip()} to {m.group(2).strip()} "
                    f"(headless: {'on' if m.group(3).strip().lower() == 'true' else 'off'})."
                ),
            ),
            (
                r"^Portal session already active; skipping sign-in\.$",
                lambda m: "Existing portal session detected. Skipping sign-in.",
            ),
            (
                r"^Matter list filter confirmed \| prefix=([^|]+) \| changed=([^|]+) \| visible_ids=(\d+) \| matching_ids=(\d+) \| no_results=(.+)$",
                lambda m: f"Matter list ready for {m.group(1).strip()} ({m.group(3)} matters shown).",
            ),
            (
                r"^Matter list filter did not verify \| prefix=([^|]+).*$",
                lambda m: f"Could not confirm the matter list for {m.group(1).strip()}.",
            ),
            (
                r"^Matter ID page snapshot \| filter=([^|]+) \| page=(\d+) \| visible=(\d+) \| new=(\d+)$",
                lambda m: (
                    f"Loaded page {m.group(2)} for {m.group(1).strip()} "
                    f"({m.group(3)} matters shown, {m.group(4)} new)."
                ),
            ),
            (
                r"^Queued matter IDs from prefix scan \| prefix=([^|]+) \| new=(\d+) \| total_queue=(\d+)$",
                lambda m: (
                    f"Queued {m.group(2)} matters from {m.group(1).strip()} "
                    f"({m.group(3)} total queued)."
                ),
            ),
            (
                r"^Could not verify prefix filter ([^;]+); skipping this prefix\.$",
                lambda m: f"Could not confirm the matter list for {m.group(1).strip()}. Skipping this year.",
            ),
            (
                r"^Exact matter filter confirmed \| matter_id=([^|]+).*$",
                lambda m: f"Matter {m.group(1).strip()} was found in the list.",
            ),
            (
                r"^Exact matter filter did not verify \| matter_id=([^|]+).*$",
                lambda m: f"Could not confirm the search result for matter {m.group(1).strip()}.",
            ),
            (
                r"^Exact matter filter returned no results \| matter_id=(.+)$",
                lambda m: f"No matter was returned for {m.group(1).strip()}.",
            ),
            (
                r"^Could not find a clickable matter target for exact filter result \| matter_id=([^|]+).*$",
                lambda m: f"The search result for matter {m.group(1).strip()} could not be opened.",
            ),
            (
                r"^Opening exact-filtered matter \| matter_id=([^|]+) \| href=.*$",
                lambda m: f"Opening matter {m.group(1).strip()}.",
            ),
            (
                r"^Opening exact-filtered matter in a new tab \| matter_id=([^|]+) \| href=.*$",
                lambda m: f"Opening matter {m.group(1).strip()} in a new tab.",
            ),
            (
                r"^Matter overview confirmed \| matter_id=(.+)$",
                lambda m: f"Matter {m.group(1).strip()} is open.",
            ),
            (
                r"^Matter overview did not become ready after exact filter open \| matter_id=(.+)$",
                lambda m: f"The matter page did not finish loading for {m.group(1).strip()}.",
            ),
            (
                r"^Exact-filtered matter mismatch \| requested=([^|]+) \| actual=(.+)$",
                lambda m: (
                    f"The opened matter did not match {m.group(1).strip()} "
                    f"(opened: {m.group(2).strip()})."
                ),
            ),
            (
                r"^Opening Documents tab and searching for Order items \| matter_id=(.+)$",
                lambda m: f"Searching documents for Order items in matter {m.group(1).strip()}.",
            ),
            (
                r"^Document search UI state \| matter_id=([^|]+) \| state=(.+)$",
                lambda m: f"Document search finished for {m.group(1).strip()} (view: {m.group(2).strip()}).",
            ),
            (
                r"^Reviewing listed documents \| matter_id=(.+)$",
                lambda m: f"Reviewing the document list for {m.group(1).strip()}.",
            ),
            (
                r"^Searching listed documents for Order items \| matter_id=(.+)$",
                lambda m: f"Searching the listed documents for Order items in matter {m.group(1).strip()}.",
            ),
            (
                r"^Document list view ready \| matter_id=([^|]+) \| state=(.+)$",
                lambda m: f"Document list loaded for {m.group(1).strip()} (view: {m.group(2).strip()}).",
            ),
            (
                r"^Order document review \| matter_id=([^|]+) \| order_found=([^|]+) \| order_pdf=([^|]+) \| evidence=([^|]+) \| rows=(\d+) \| title=(.*)$",
                lambda m: (
                    f"Order review for {m.group(1).strip()}: "
                    f"document found={m.group(2).strip()}, pdf confirmed={m.group(3).strip()}, "
                    f"evidence={m.group(4).strip()}, rows={m.group(5).strip()}."
                ),
            ),
            (
                r"^Could not open matter review tab \| matter_id=([^|]+) \| reason=(.+)$",
                lambda m: f"Could not review matter {m.group(1).strip()}. Reason: {m.group(2).strip()}.",
            ),
            (
                r"^Refreshing Matters list and retrying exact search \| matter_id=([^|]+) \| prefix=(.+)$",
                lambda m: f"Refreshing the Matters list and trying {m.group(1).strip()} again.",
            ),
            (
                r"^Retry exact search still did not verify \| matter_id=([^|]+) \| prefix=(.+)$",
                lambda m: f"The second search attempt still could not confirm matter {m.group(1).strip()}.",
            ),
            (
                r"^Skipping non-order document \| matter_id=([^|]+) \| title=(.+?) \| date_created=(.+)$",
                lambda m: (
                    f"Skipped a non-Order document in matter {m.group(1).strip()}: "
                    f"{self._clean_display_value(m.group(2))}."
                ),
            ),
            (
                r"^Document search summary \| matter_id=([^|]+) \| grid_rows=(\d+) \| order_titles=(\d+) \| order_in_range=(\d+) \| order_out_of_range=(\d+) \| order_missing_date=(\d+) \| non_order_rows=(\d+)$",
                lambda m: (
                    f"Reviewed documents for {m.group(1).strip()}: {m.group(2)} rows, "
                    f"{m.group(3)} Order items, {m.group(4)} in range, {m.group(5)} out of range, "
                    f"{m.group(6)} missing dates, and {m.group(7)} other documents."
                ),
            ),
            (
                r"^No document grid rows detected after Order search \| matter_id=(.+)$",
                lambda m: f"No document rows were shown after the Order search for {m.group(1).strip()}.",
            ),
            (
                r"^No collectible Order documents found for this matter after search \| matter_id=(.+)$",
                lambda m: f"No Order documents matched the selected date range for {m.group(1).strip()}.",
            ),
            (
                r"^No in-range Order documents found during discovery \| matter_id=(.+)$",
                lambda m: f"Review complete for {m.group(1).strip()}. No in-range Order documents were found.",
            ),
            (
                r"^Discovery decision \| matter_id=([^|]+) \| title=(.+) \| date_created=([^|]+) \| decision=(.+)$",
                lambda m: (
                    f"Review result for {m.group(1).strip()}: {m.group(4).strip()} "
                    f"({m.group(3).strip()}, {self._clean_display_value(m.group(2))})."
                ),
            ),
            (
                r"^LN Doc review result \| matter_id=([^|]+) \| decision=([^|]+) \| matched_file=(.+)$",
                lambda m: (
                    f"LN Doc review result for {m.group(1).strip()}: {m.group(2).strip()} "
                    f"(matched file: {m.group(3).strip()})."
                ),
            ),
            (
                r"^Download failed for ([^(]+) \(([^)]+)\): ([^:]+): (.+)$",
                lambda m: (
                    f"Download failed for {m.group(1).strip()} "
                    f"({m.group(2).strip()}). Reason: {m.group(4).strip()}."
                ),
            ),
            (
                r"^No candidate order documents found on portal\. Latest SF-View file observed: (.+) \(date=(.+)\)$",
                lambda m: (
                    f"No qualifying Order documents were found on the portal. "
                    f"Latest LNDoc file seen: {m.group(1).strip()} (date: {m.group(2).strip()})."
                ),
            ),
            (
                r"^Could not query latest SF-View document: (.+)$",
                lambda m: f"Could not read the latest LNDoc file. Reason: {m.group(1).strip()}.",
            ),
            (
                r"^Completed\. Reviewed: (\d+); For collect: (\d+); Do not collect: (\d+); Downloaded: (\d+)$",
                lambda m: (
                    f"Run complete. Reviewed {m.group(1)} items, marked {m.group(2)} for collect, "
                    f"marked {m.group(3)} do not collect, and downloaded {m.group(4)} files."
                ),
            ),
            (
                r"^No candidate order documents found on portal\..*$",
                lambda m: "No qualifying Order documents were found on the portal.",
            ),
            (
                r"^Restoring Matters page \| filter=([^|]+) \| page=(\d+) \| expected_href=(.+)$",
                lambda m: f"Returning to the matter list for {m.group(1).strip()} (page {m.group(2)}).",
            ),
            (
                r"^Using current filtered matter list as page 1 baseline for (.+)$",
                lambda m: f"Using the current filtered list for page 1 of {m.group(1).strip()}.",
            ),
            (
                r"^--- Matter list page (\d+) \(filter (.+)\) ---$",
                lambda m: f"Reviewing matter list page {m.group(1)} for {m.group(2).strip()}.",
            ),
            (
                r"^Matter href snapshot collected \| page=(\d+) \| filter=([^|]+) \| hrefs=(\d+) \| elapsed=([0-9.]+)s$",
                lambda m: (
                    f"Captured {m.group(3)} matter links on page {m.group(1)} "
                    f"for {m.group(2).strip()}."
                ),
            ),
            (
                r"^No unseen matter links remaining on page (\d+); advancing pagination\.$",
                lambda m: f"Finished page {m.group(1)}. Moving to the next page.",
            ),
            (
                r"^Repeated matter ID page content detected for (.+); stopping pagination\.$",
                lambda m: f"Stopped pagination for {m.group(1).strip()} because the next page repeated the same results.",
            ),
            (
                r"^Next page did not produce a new matter ID signature for (.+); stopping pagination\.$",
                lambda m: f"Stopped pagination for {m.group(1).strip()} because the next page did not change.",
            ),
            (
                r"^Navigating directly to Matters list: (.+)$",
                lambda m: "Returning directly to the Matters list.",
            ),
            (
                r"^Direct navigation to Matters list failed: (.+)$",
                lambda m: f"Direct navigation back to the Matters list failed. Reason: {m.group(1).strip()}.",
            ),
            (
                r"^Could not reopen Matters list through portal entry flow: (.+)$",
                lambda m: f"Could not reopen the Matters list through the portal flow. Reason: {m.group(1).strip()}.",
            ),
            (
                r"^Could not advance to restored page (\d+); stopped at page (\d+)$",
                lambda m: f"Could not return to page {m.group(1)}. The portal stopped at page {m.group(2)}.",
            ),
            (
                r"^Page signature did not change while restoring page (\d+)$",
                lambda m: f"The portal did not move to page {m.group(1)} while restoring the matter list.",
            ),
            (
                r"^Expected matter href not found after restore: (.+)$",
                lambda m: "The expected matter entry was not present after the list was restored.",
            ),
            (
                r"^Matter list not ready after (\d+)s \| url=(.+)$",
                lambda m: f"The matter list did not finish loading within {m.group(1)} seconds.",
            ),
            (
                r"^No matter links found on this page; stopping pagination for this filter\.$",
                lambda m: "No matter links were available on this page. Pagination has been stopped.",
            ),
            (
                r"^Detected repeated page content; stopping pagination safety guard\.$",
                lambda m: "The page repeated the same content. Pagination has been stopped as a safety check.",
            ),
            (
                r"^Matter href no longer available on the live page; retrying from refreshed state: (.+)$",
                lambda m: "The selected matter was no longer available in the live list. Refreshing and trying again.",
            ),
            (
                r"^Skipping non-matter link candidate: (.+)$",
                lambda m: "Skipped a list item that was not a matter link.",
            ),
            (
                r"^Clicking matter link \(page (\d+), slot (\d+)\): (.+)$",
                lambda m: f"Opening a matter from page {m.group(1)}, item {m.group(2)}.",
            ),
            (
                r"^Matter overview did not render after click; returning to list and continuing\.$",
                lambda m: "The matter page did not finish loading. Returning to the list.",
            ),
            (
                r"^Could not extract Matter ID on this overview; returning to list\.$",
                lambda m: "Could not read the matter ID from the opened page. Returning to the list.",
            ),
            (
                r"^Matter ID extracted: (.+)$",
                lambda m: f"Matter ID confirmed: {m.group(1).strip()}.",
            ),
            (
                r"^Could not restore Matters list after processing a matter\.$",
                lambda m: "Could not return to the Matters list after reviewing a matter.",
            ),
            (
                r"^Could not restore Matters list after an error; stopping current page processing\.$",
                lambda m: "Could not return to the Matters list after an error. Stopping the current page.",
            ),
            (
                r"^Could not restore current page before checking pagination; stopping pagination for this filter\.$",
                lambda m: "Could not return to the current page before checking for the next page. Pagination has been stopped.",
            ),
            (
                r"^Next page did not produce distinct content; stopping pagination for this filter\.$",
                lambda m: "The next page did not show new content. Pagination has been stopped.",
            ),
            (
                r"^Run report saved \(summary only, no data rows\): (.+)$",
                lambda m: f"Run report saved with a summary only: {m.group(1)}",
            ),
            (
                r"^Discovery Decisions sheet: (\d+) rows$",
                lambda m: f"Added the Discovery Decisions sheet to the run report ({m.group(1)} rows).",
            ),
            (
                r"^For Collect sheet: (\d+) rows$",
                lambda m: f"Added the For Collect sheet to the run report ({m.group(1)} rows).",
            ),
            (
                r"^Do Not Collect sheet: (\d+) rows$",
                lambda m: f"Added the Do Not Collect sheet to the run report ({m.group(1)} rows).",
            ),
            (
                r"^Collected sheet: (\d+) documents$",
                lambda m: f"Added the Collected sheet to the run report ({m.group(1)} documents).",
            ),
            (
                r"^Skipped sheet: (\d+) rows$",
                lambda m: f"Added the Skipped sheet to the run report ({m.group(1)} rows).",
            ),
            (
                r"^Skipped sheet: (\d+) documents$",
                lambda m: f"Added the Skipped sheet to the run report ({m.group(1)} documents).",
            ),
            (
                r"^Download Failures sheet: (\d+) rows$",
                lambda m: f"Added the Download Failures sheet to the run report ({m.group(1)} rows).",
            ),
            (
                r"^Excluded \(Non-Order\) sheet: (\d+) documents$",
                lambda m: f"Added the Excluded (Non-Order) sheet to the run report ({m.group(1)} documents).",
            ),
            (
                r"^Run report saved: (.+)$",
                lambda m: f"Run report saved: {m.group(1)}",
            ),
        ]
        for pattern, replacement in ui_patterns:
            matched = re.match(pattern, text)
            if not matched:
                continue
            return replacement(matched)
        return text

    def _progress(self, pct: int) -> None:
        try:
            self.progress_signal.emit(max(0, min(100, int(pct))))
        except Exception:
            pass

    def _make_run_dir(self) -> Path:
        base = Path.home() / (self.config.get("download_path") or "Downloads/MOADMN Collections")
        base.mkdir(parents=True, exist_ok=True)
        run = base / _run_folder_name(datetime.now())
        run.mkdir(parents=True, exist_ok=True)
        return run

    def _collected_dir(self) -> Path:
        if self.run_dir is None:
            raise RuntimeError("Run directory is not ready.")
        target = self.run_dir / str(self.config.get("collected_folder", "Collected"))
        target.mkdir(parents=True, exist_ok=True)
        return target

    def _excluded_dir(self) -> Path:
        if self.run_dir is None:
            raise RuntimeError("Run directory is not ready.")
        target = self.run_dir / str(self.config.get("excluded_folder", "Excluded"))
        target.mkdir(parents=True, exist_ok=True)
        return target

    def _ensure_output_dirs(self) -> None:
        self._collected_dir()
        self._excluded_dir()

    def _relocate_output_file(self, source: Path, destination_dir: Path) -> Optional[Path]:
        if not source.exists():
            return None
        destination_dir.mkdir(parents=True, exist_ok=True)
        target = self._unique_download_target(destination_dir, source.name)
        if source.resolve() == target.resolve():
            return source
        try:
            source.replace(target)
        except PermissionError:
            shutil.move(str(source), str(target))
        return target

    def _normalize_run_output_layout(self) -> None:
        if self.run_dir is None:
            return

        collected_dir = self._collected_dir()
        excluded_dir = self._excluded_dir()
        legacy_excluded_dir = self.run_dir / "Excluded - PDF Quality Gate"

        if legacy_excluded_dir.exists() and legacy_excluded_dir.is_dir():
            for child in list(legacy_excluded_dir.iterdir()):
                if child.is_file():
                    self._relocate_output_file(child, excluded_dir)
            try:
                legacy_excluded_dir.rmdir()
            except OSError:
                pass

        for child in list(self.run_dir.iterdir()):
            if child in {collected_dir, excluded_dir}:
                continue
            if not child.is_file():
                continue
            suffix = child.suffix.lower()
            if child.name.startswith("MOADMN_Run_Report_") and suffix == ".xlsx":
                continue
            if child.name.startswith("Log_") and suffix in {".log", ".txt"}:
                continue
            target_dir = collected_dir if suffix == ".pdf" else excluded_dir
            self._relocate_output_file(child, target_dir)

        for child in list(collected_dir.iterdir()):
            if child.is_file() and child.suffix.lower() != ".pdf":
                self._relocate_output_file(child, excluded_dir)

    def _sync_untracked_excluded_files(self) -> None:
        excluded_dir = self._excluded_dir()
        known_names: set[str] = set()
        known_paths: set[str] = set()

        for row in self.excluded_non_order:
            for key in ("filename", "quarantine_filename"):
                value = str(row.get(key) or "").strip()
                if not value:
                    continue
                known_names.add(Path(value).name)
                known_paths.add(value)

        for child in sorted(excluded_dir.iterdir()):
            if not child.is_file() or child.suffix.lower() == ".pdf":
                continue
            if child.name in known_names or str(child) in known_paths:
                continue
            reason = "downloaded non-PDF file"
            suffix = child.suffix.lower() or "[no extension]"
            self.excluded_non_order.append(
                {
                    "matter_id": "",
                    "doc_title": child.name,
                    "date_created": "",
                    "filename": child.name,
                    "decision": "skip",
                    "criteria_result": "exclude",
                    "document_check_status": "excluded non-PDF download",
                    "order_document_found": "N",
                    "order_pdf_found": "N",
                    "quality_exclusion_reason": reason,
                    "quality_evidence": f"Found in Excluded folder after run finalization (extension {suffix})",
                    "quarantine_filename": str(child),
                    "error": reason,
                    "skipped_document_notes": f"Excluded because the downloaded file was not a PDF ({suffix}).",
                }
            )
            self._log(
                f"Added untracked excluded file to report | filename={child.name} | "
                f"reason=downloaded non-PDF file"
            )

    def _launch_driver(self, download_dir: Path) -> webdriver.Chrome:
        opts = Options()
        if self.headless:
            # new headless is more stable on recent Chrome
            opts.add_argument("--headless=new")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")

        prefs = {
            "download.default_directory": str(download_dir),
            "download.prompt_for_download": False,
            "download.directory_upgrade": True,
            "safebrowsing.enabled": True,
        }
        opts.add_experimental_option("prefs", prefs)

        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=opts)
        driver.set_window_size(1400, 900)
        return driver

    def _wait_click(self, xpath: str, timeout: int = 20) -> None:
        try:
            el = WebDriverWait(self.driver, timeout).until(EC.element_to_be_clickable((By.XPATH, xpath)))
            el.click()
        except Exception as e:
            # Provide actionable context in logs
            self._log(f"Click failed | xpath={xpath!r} | exc={type(e).__name__}: {e!r}")
            raise

    def _wait_send_keys(
        self,
        xpath: str,
        text: str,
        timeout: int = 20,
        clear: bool = True,
        sensitive: bool = False,
    ) -> None:
        try:
            el = WebDriverWait(self.driver, timeout).until(EC.presence_of_element_located((By.XPATH, xpath)))
            if clear:
                try:
                    el.clear()
                except Exception:
                    pass
            el.send_keys(text)
        except Exception as e:
            logged_text = "***" if sensitive else text
            self._log(
                f"Send keys failed | xpath={xpath!r} | text={logged_text!r} | exc={type(e).__name__}: {e!r}"
            )
            raise

    def _open_portal_entry_from_instructions(self) -> None:
        """
        Open the portal from the public instructions page using a resilient fallback chain.
        """
        candidate_xpaths = []
        configured_xpath = self.config.get("portal_button_xpath")
        if configured_xpath:
            candidate_xpaths.append(configured_xpath)
        candidate_xpaths.extend(
            [
                '//a[contains(@href,"ahcportal.mo.gov")]',
                '//a[contains(normalize-space(.), "Enter the Missouri Administrative Hearing Commission Portal")]',
                '//a[contains(normalize-space(.), "Case Search portal")]',
            ]
        )

        last_error = ""
        for xpath in candidate_xpaths:
            try:
                element = WebDriverWait(self.driver, 6).until(
                    EC.presence_of_element_located((By.XPATH, xpath))
                )
                href = (element.get_attribute("href") or "").strip()
                try:
                    WebDriverWait(self.driver, 3).until(
                        EC.element_to_be_clickable((By.XPATH, xpath))
                    )
                    try:
                        element.click()
                    except Exception:
                        self.driver.execute_script("arguments[0].click();", element)
                except Exception:
                    if not href:
                        raise
                    self._log(
                        f"Portal entry link was found but not clickable with xpath {xpath!r}; opening it directly."
                    )
                    self.driver.get(href)
                return
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                continue

        fallback_url = "https://ahcportal.mo.gov/ProLawWorkspace/"
        self._log(
            "Could not use the instructions-page portal button; opening the portal directly. "
            f"Last portal-button error: {last_error or 'unknown error'}"
        )
        self.driver.get(fallback_url)

    def _element_present(self, xpath: Optional[str], timeout: int = 3) -> bool:
        if not xpath:
            return False
        try:
            WebDriverWait(self.driver, timeout).until(EC.presence_of_element_located((By.XPATH, xpath)))
            return True
        except Exception:
            return False

    def _portal_workspace_url(self) -> str:
        fallback = "https://ahcportal.mo.gov/ProLawWorkspace/"
        try:
            current_url = self.driver.current_url or ""
        except Exception:
            current_url = ""

        if "ProLawWorkspace/" in current_url:
            base = current_url.split("#", 1)[0]
            return base if base.endswith("/") else f"{base}/"
        return fallback

    def _matter_list_url(self) -> str:
        return f"{self._portal_workspace_url()}#/matters/matter-list"

    def _await_portal_entry_state(self, timeout: int = 20) -> str:
        """
        Wait until the portal is ready either for sign-in or for direct in-app use.
        The recovery path can land on an already-authenticated session, so we
        must not assume the login form will always be present.
        """
        start = time.time()
        while time.time() - start < timeout:
            if self._element_present(self.config.get("login_user_xpath"), timeout=1):
                return "login"
            if self._element_present(self.config.get("matters_nav_xpath"), timeout=1):
                return "session"
            try:
                cur = (self.driver.current_url or "").lower()
            except Exception:
                cur = ""
            if "#/matters/" in cur:
                return "session"
            time.sleep(0.3)
        raise TimeoutException("Portal did not reach a login or session-ready state")

    def _login_and_open_matters(self) -> None:
        self._status("Loading MOADMN portal…")
        self.driver.get(self.config.get("instructions_url"))
        # Portal button opens the Case Search Portal, often in a NEW TAB.
        # We must switch to that new tab before continuing.
        before_handles = list(self.driver.window_handles)
        self._open_portal_entry_from_instructions()
        try:
            WebDriverWait(self.driver, 10).until(lambda d: len(d.window_handles) > len(before_handles))
            after_handles = list(self.driver.window_handles)
            new_handles = [h for h in after_handles if h not in before_handles]
            if new_handles:
                self.driver.switch_to.window(new_handles[-1])
        except TimeoutException:
            # Fallback: some browsers may open it in the same tab.
            pass

        self._status("Signing in…")
        entry_state = self._await_portal_entry_state(timeout=20)
        if entry_state == "login":
            self._wait_send_keys(self.config.get("login_user_xpath"), self.config.get("login_user"))
            self._wait_send_keys(
                self.config.get("login_password_xpath"),
                self.config.get("login_password"),
                sensitive=True,
            )
            self._wait_click(self.config.get("login_submit_xpath"))
            try:
                WebDriverWait(self.driver, 20).until(
                    lambda d: self._element_present(self.config.get("matters_nav_xpath"), timeout=1)
                    or "#/matters/" in ((d.current_url or "").lower())
                )
            except TimeoutException as exc:
                current_url = ""
                try:
                    current_url = (self.driver.current_url or "").strip()
                except Exception:
                    current_url = ""
                detail = "The portal stayed on the sign-in page after the login attempt."
                if current_url:
                    detail += f" Current URL: {current_url}"
                raise self.SignInIssue(self._build_sign_in_issue_message(detail)) from exc
        else:
            self._log("Portal session already active; skipping sign-in.")

        self._status("Opening Matters…")
        try:
            if "#/matters/matter-list" not in ((self.driver.current_url or "").lower()):
                self._wait_click(self.config.get("matters_nav_xpath"), timeout=10)
        except Exception:
            self.driver.get(self._matter_list_url())

        if not self._wait_for_matter_list_ready(timeout=20):
            self.driver.get(self._matter_list_url())
            if not self._wait_for_matter_list_ready(timeout=20):
                raise TimeoutException("Matters list did not load after portal entry")

        try:
            self.primary_window_handle = self.driver.current_window_handle
        except Exception:
            pass

    def _return_to_matters_list(self) -> None:
        """Return to the Matters list (used to reset pagination/filter between scans)."""
        if not self._open_matters_list():
            raise TimeoutException("Could not return to Matters list")

    def _switch_to_primary_window(self) -> bool:
        try:
            handles = list(self.driver.window_handles)
        except Exception:
            return False

        if not handles:
            return False

        target = self.primary_window_handle if self.primary_window_handle in handles else handles[0]
        try:
            self.driver.switch_to.window(target)
            self.primary_window_handle = target
            return True
        except Exception as exc:
            self._log(f"Could not switch back to the main MOADMN tab: {exc}")
            return False

    def _close_non_primary_windows(self) -> int:
        try:
            handles = list(self.driver.window_handles)
        except Exception:
            return 0

        if not handles:
            return 0

        if not self.primary_window_handle or self.primary_window_handle not in handles:
            self.primary_window_handle = handles[0]
        primary = self.primary_window_handle

        closed = 0
        for handle in handles:
            if handle == primary:
                continue
            try:
                self.driver.switch_to.window(handle)
                self.driver.close()
                closed += 1
            except Exception as exc:
                self._log(f"Could not close an extra browser tab during recovery: {exc}")

        self._switch_to_primary_window()
        return closed

    def _restore_document_review_context(self, prefix: str) -> bool:
        if not self._switch_to_primary_window():
            return False

        closed_tabs = self._close_non_primary_windows()
        if closed_tabs:
            noun = "tab" if closed_tabs == 1 else "tabs"
            self._log(f"Closed {closed_tabs} extra browser {noun} before continuing.")

        if not self._open_matters_list():
            self._log("Could not reopen the Matters list.")
            return False

        try:
            self.driver.refresh()
        except Exception as exc:
            self._log(f"Could not refresh the Matters page during recovery: {exc}")

        if not self._wait_for_matter_list_ready(timeout=15):
            if not self._open_matters_list():
                self._log("The Matters list did not recover after refresh.")
                return False

        self._set_sort_and_paging()
        if not self._apply_matter_list_prefix_filter(prefix, timeout=20):
            self._log(f"Could not confirm the {prefix} filter after recovery.")
            return False

        return True

    def _prepare_prefix_document_review(self, prefix: str, reason: str) -> bool:
        attempts = max(1, int(self.config.get("document_review_prefix_retry_attempts", 2) or 2))
        backoff_seconds = max(0, int(self.config.get("document_review_prefix_retry_backoff_seconds", 3) or 0))

        for attempt in range(1, attempts + 1):
            suffix = f" Attempt {attempt} of {attempts}." if attempts > 1 else ""
            self._status(reason.rstrip(".") + "...")
            self._log(f"{reason}{suffix}")
            try:
                if self._restore_document_review_context(prefix):
                    return True
            except Exception as exc:
                self._log(f"Could not prepare the {prefix} matter list: {exc}")

            if attempt < attempts and backoff_seconds:
                time.sleep(backoff_seconds)

        return False

    def _set_sort_and_paging(self) -> None:
        self._wait_click(self.config.get("sort_button_xpath"))
        self._wait_click(self.config.get("sort_by_matter_id_xpath"))

        # Items per page = 100
        sel_xpath = self.config.get("items_per_page_select_xpath")
        sel_el = WebDriverWait(self.driver, 20).until(EC.presence_of_element_located((By.XPATH, sel_xpath)))
        Select(sel_el).select_by_value(str(self.config.get("items_per_page_value", "100")))
        time.sleep(1.0)

    def _matter_list_text_snapshot(self) -> str:
        try:
            container = self.driver.find_element(By.XPATH, '//*[@id="site-content"]//app-matter-list')
            return re.sub(r"\s+", " ", (container.text or "")).strip()
        except Exception:
            return ""

    def _visible_matter_ids_on_list(self) -> List[str]:
        text = self._matter_list_text_snapshot()
        seen: set[str] = set()
        ids: List[str] = []
        for candidate in re.findall(r"\b\d{2}-\d{4}\b", text):
            if candidate not in seen:
                seen.add(candidate)
                ids.append(candidate)
        return ids

    def _matter_list_has_no_results(self) -> bool:
        text = self._matter_list_text_snapshot().lower()
        return any(phrase in text for phrase in ("no results", "no records", "0 results", "0 records"))

    def _apply_matter_list_prefix_filter(self, prefix: str, timeout: int = 20) -> bool:
        """
        Apply the year prefix filter on the Matters list, commit it with Enter,
        and verify the visible list changed or is already consistent with the prefix.
        """
        filter_xpath = self.config.get("filter_xpath")
        before_signature = self._page_signature()
        before_snapshot = self._matter_list_text_snapshot()

        try:
            field = WebDriverWait(self.driver, timeout).until(
                EC.element_to_be_clickable((By.XPATH, filter_xpath))
            )
            try:
                field.click()
            except Exception:
                pass
            try:
                field.send_keys(Keys.CONTROL, "a")
                field.send_keys(Keys.DELETE)
            except Exception:
                try:
                    field.clear()
                except Exception:
                    pass
            field.send_keys(prefix)
            field.send_keys(Keys.ENTER)
        except Exception as e:
            self._log(
                f"Matter list filter apply failed | prefix={prefix!r} | xpath={filter_xpath!r} | "
                f"exc={type(e).__name__}: {e!r}"
            )
            return False

        current_value = ""
        last_signature = before_signature
        last_ids: List[str] = []
        last_no_results = False
        start = time.time()
        while time.time() - start < timeout:
            try:
                current_value = (self.driver.find_element(By.XPATH, filter_xpath).get_attribute("value") or "").strip()
            except Exception:
                current_value = ""

            last_signature = self._page_signature()
            snapshot = self._matter_list_text_snapshot()
            last_ids = self._visible_matter_ids_on_list()
            matching_ids = [candidate for candidate in last_ids if candidate.startswith(prefix)]
            fully_consistent = bool(last_ids) and len(matching_ids) == len(last_ids)
            changed = (last_signature != before_signature) or (snapshot != before_snapshot)
            last_no_results = self._matter_list_has_no_results()

            if current_value == prefix and (changed or fully_consistent or last_no_results):
                self._log(
                    f"Matter list filter confirmed | prefix={prefix} | changed={changed} | "
                    f"visible_ids={len(last_ids)} | matching_ids={len(matching_ids)} | "
                    f"no_results={last_no_results}"
                )
                return True

            time.sleep(0.4)

        self._log(
            f"Matter list filter did not verify | prefix={prefix} | input_value={current_value!r} | "
            f"before_signature={before_signature or '[empty]'} | after_signature={last_signature or '[empty]'} | "
            f"visible_ids={last_ids} | no_results={last_no_results}"
        )
        return False

    def _current_page_matter_ids(self, prefix: Optional[str] = None) -> List[str]:
        ids: List[str] = []
        seen: set[str] = set()
        for candidate in self._visible_matter_ids_on_list():
            if prefix and not candidate.startswith(prefix):
                continue
            if candidate not in seen:
                seen.add(candidate)
                ids.append(candidate)
        return ids

    def _visible_target_matter_ids(self, prefix: Optional[str] = None) -> List[str]:
        ids: List[str] = []
        seen: set[str] = set()
        for target in self._current_matter_targets(active_prefix=prefix):
            if target.matter_id and target.matter_id not in seen:
                seen.add(target.matter_id)
                ids.append(target.matter_id)
        return ids

    def _matter_id_page_signature(self, prefix: Optional[str] = None) -> str:
        return "|".join(self._current_page_matter_ids(prefix=prefix)[:25])

    def _wait_for_matter_id_signature_change(
        self,
        previous_signature: str,
        prefix: Optional[str] = None,
        timeout: int = 10,
    ) -> bool:
        start = time.time()
        while time.time() - start < timeout:
            current_signature = self._matter_id_page_signature(prefix=prefix)
            if current_signature and current_signature != previous_signature:
                return True
            time.sleep(0.4)
        return False

    def _wait_for_matter_page_ids_ready(
        self,
        prefix: str,
        previous_signature: str = "",
        timeout: int = 12,
    ) -> Tuple[List[str], bool]:
        """
        Wait for the visible matter IDs on the current page to stabilize after a
        prefix filter or pagination change. Returns `(ids, no_results)`.
        """
        start = time.time()
        stable_passes = 0
        last_ids: List[str] = []
        last_signature = previous_signature
        last_no_results = False

        while time.time() - start < timeout:
            ids = self._current_page_matter_ids(prefix=prefix)
            no_results = self._matter_list_has_no_results()
            signature = "|".join(ids[:25])

            if ids:
                if signature and signature == last_signature:
                    stable_passes += 1
                else:
                    stable_passes = 1
                    last_signature = signature
                last_ids = ids
                last_no_results = False
                if stable_passes >= 2:
                    return ids, False
            elif no_results:
                return [], True
            else:
                stable_passes = 0

            time.sleep(0.4)

        return last_ids, last_no_results

    def _apply_matter_list_exact_filter(self, matter_id: str, timeout: int = 20) -> bool:
        filter_xpath = self.config.get("filter_xpath")
        before_signature = self._matter_id_page_signature()
        before_snapshot = self._matter_list_text_snapshot()

        try:
            field = WebDriverWait(self.driver, timeout).until(
                EC.element_to_be_clickable((By.XPATH, filter_xpath))
            )
            try:
                field.click()
            except Exception:
                pass
            try:
                field.send_keys(Keys.CONTROL, "a")
                field.send_keys(Keys.DELETE)
            except Exception:
                try:
                    field.clear()
                except Exception:
                    pass
            field.send_keys(matter_id)
            field.send_keys(Keys.ENTER)
        except Exception as e:
            self._log(
                f"Exact matter filter apply failed | matter_id={matter_id!r} | xpath={filter_xpath!r} | "
                f"exc={type(e).__name__}: {e!r}"
            )
            return False

        current_value = ""
        last_ids: List[str] = []
        last_target_ids: List[str] = []
        last_no_results = False
        last_signature = before_signature
        visible_match_passes = 0
        start = time.time()
        while time.time() - start < timeout:
            try:
                current_value = (self.driver.find_element(By.XPATH, filter_xpath).get_attribute("value") or "").strip()
            except Exception:
                current_value = ""

            last_signature = self._matter_id_page_signature()
            snapshot = self._matter_list_text_snapshot()
            last_ids = self._current_page_matter_ids()
            last_target_ids = self._visible_target_matter_ids()
            matching_targets = [target for target in self._current_matter_targets() if target.matter_id == matter_id]
            changed = (last_signature != before_signature) or (snapshot != before_snapshot)
            last_no_results = self._matter_list_has_no_results()
            visible_match = matter_id in last_ids

            if current_value == matter_id and (last_no_results or matching_targets):
                self._log(
                    f"Exact matter filter confirmed | matter_id={matter_id} | changed={changed} | "
                    f"visible_ids={last_ids[:5]}{'...' if len(last_ids) > 5 else ''} | "
                    f"target_ids={last_target_ids[:5]}{'...' if len(last_target_ids) > 5 else ''} | "
                    f"no_results={last_no_results}"
                )
                return True

            if current_value == matter_id and visible_match:
                visible_match_passes += 1
                if visible_match_passes >= 2:
                    self._log(
                        f"Exact matter filter confirmed from visible IDs only | matter_id={matter_id} | "
                        f"changed={changed} | visible_ids={last_ids[:5]}{'...' if len(last_ids) > 5 else ''} | "
                        f"target_ids={last_target_ids[:5]}{'...' if len(last_target_ids) > 5 else ''} | "
                        f"no_results={last_no_results}"
                    )
                    return True
            else:
                visible_match_passes = 0

            time.sleep(0.4)

        self._log(
            f"Exact matter filter did not verify | matter_id={matter_id} | input_value={current_value!r} | "
            f"before_signature={before_signature or '[empty]'} | after_signature={last_signature or '[empty]'} | "
            f"visible_ids={last_ids} | target_ids={last_target_ids} | no_results={last_no_results}"
        )
        return False

    def _wait_for_matching_matter_targets(
        self,
        matter_id: str,
        timeout: int = 5,
        active_prefix: Optional[str] = None,
    ) -> List[MatterTarget]:
        start = time.time()
        last_targets: List[MatterTarget] = []
        while time.time() - start < timeout:
            last_targets = [
                target
                for target in self._current_matter_targets(active_prefix=active_prefix)
                if target.matter_id == matter_id
            ]
            if last_targets:
                return last_targets
            time.sleep(0.4)
        return last_targets

    def _collect_matter_ids_for_active_prefix(self, prefix: str) -> List[str]:
        discovered: List[str] = []
        seen: set[str] = set()
        page_number = 1
        repeated_signatures = 0
        previous_signature = ""

        while True:
            if self.isInterruptionRequested():
                raise self.UserCancelled("Collection cancelled by user")

            self._status(f"Loading page {page_number} of the {prefix} matter list...")
            ids, no_results = self._wait_for_matter_page_ids_ready(prefix=prefix, previous_signature=previous_signature)
            new_ids = [candidate for candidate in ids if candidate not in seen]
            for candidate in new_ids:
                seen.add(candidate)
                discovered.append(candidate)

            self._log(
                f"Matter ID page snapshot | filter={prefix} | page={page_number} | "
                f"visible={len(ids)} | new={len(new_ids)}"
            )

            if not ids:
                if no_results:
                    self._log(f"The {prefix} filter returned no matter IDs on page {page_number}.")
                else:
                    self._log(f"No matter IDs became visible for {prefix} on page {page_number} before timeout.")
                break

            signature = "|".join(ids[:25])
            if signature and signature == previous_signature:
                repeated_signatures += 1
            else:
                repeated_signatures = 0
                previous_signature = signature
            if repeated_signatures >= 1:
                self._log(f"Repeated matter ID page content detected for {prefix}; stopping pagination.")
                break

            if not self._next_page():
                break
            if not self._wait_for_matter_id_signature_change(signature, prefix=prefix, timeout=10):
                self._log(f"Next page did not produce a new matter ID signature for {prefix}; stopping pagination.")
                break
            page_number += 1

        return discovered

    def _open_matter_by_exact_id(self, matter_id: str) -> bool:
        attempts = max(1, int(self.config.get("document_review_matter_open_retry_attempts", 3) or 3))
        backoff_seconds = max(0, int(self.config.get("document_review_matter_open_retry_backoff_seconds", 3) or 0))
        target_wait_seconds = max(1, int(self.config.get("document_review_exact_target_wait_seconds", 5) or 5))

        for attempt in range(1, attempts + 1):
            self._return_to_matters_list()
            self._set_sort_and_paging()
            verified = (
                self._apply_matter_list_exact_filter(matter_id, timeout=20)
                if attempt == 1
                else self._retry_exact_matter_search(matter_id, prefix=f"{matter_id[:2]}-")
            )
            if not verified:
                if attempt < attempts and backoff_seconds:
                    time.sleep(backoff_seconds)
                continue

            if self._matter_list_has_no_results():
                self._log(f"Exact matter filter returned no results | matter_id={matter_id}")
                if attempt < attempts and backoff_seconds:
                    time.sleep(backoff_seconds)
                continue

            targets = [target for target in self._current_matter_targets() if target.matter_id == matter_id]
            if not targets:
                targets = self._wait_for_matching_matter_targets(matter_id, timeout=target_wait_seconds)
            if not targets:
                self._log(
                    f"Could not find a clickable matter target for exact filter result | matter_id={matter_id} | "
                    f"visible_ids={self._current_page_matter_ids()}"
                )
                if attempt < attempts and backoff_seconds:
                    time.sleep(backoff_seconds)
                continue

            target = targets[0]
            click_ok = False
            for click_attempt in range(2):
                el = self._find_matter_link_by_href(target.href)
                if el is None:
                    break
                try:
                    el.click()
                    click_ok = True
                    break
                except StaleElementReferenceException:
                    time.sleep(0.3)
            if not click_ok:
                self._log(f"Could not click exact-filter matter target | matter_id={matter_id} | href={target.href}")
                if attempt < attempts and backoff_seconds:
                    time.sleep(backoff_seconds)
                continue

            self._log(f"Opening exact-filtered matter | matter_id={matter_id} | href={target.href}")
            if not self._wait_for_matter_overview_ready(target.href, timeout=20):
                self._log(f"Matter overview did not become ready after exact filter open | matter_id={matter_id}")
                if attempt < attempts and backoff_seconds:
                    time.sleep(backoff_seconds)
                continue

            actual_matter_id = self._extract_matter_id(preferred_prefix=f"{matter_id[:2]}-") or self._extract_matter_id()
            if actual_matter_id != matter_id:
                self._log(
                    f"Exact-filtered matter mismatch | requested={matter_id} | actual={actual_matter_id or '[none]'}"
                )
                if attempt < attempts and backoff_seconds:
                    time.sleep(backoff_seconds)
                continue

            self._log(f"Matter overview confirmed | matter_id={matter_id}")
            return True

        return False

    def _return_from_matter_overview(self) -> bool:
        try:
            self.driver.back()
            if self._wait_for_matter_list_ready(timeout=10):
                return True
        except Exception:
            pass
        return self._open_matters_list()

    @staticmethod
    def _match_document_item(items: List[DocumentItem], doc_title: str, date_created: date) -> Optional[DocumentItem]:
        def _norm(text: str) -> str:
            return re.sub(r"\s+", " ", (text or "")).strip().lower()

        exact_title = _norm(doc_title)
        for item in items:
            if item.date_created == date_created and _norm(item.title) == exact_title:
                return item
        for item in items:
            if item.date_created == date_created and (
                exact_title in _norm(item.title) or _norm(item.title) in exact_title
            ):
                return item
        return None

    @staticmethod
    def _parse_mmddyyyy_text(text: str) -> Optional[date]:
        source = (text or "").strip()
        if not source:
            return None

        patterns_and_formats = [
            (r"\b\d{1,2}/\d{1,2}/\d{4}\b", ["%m/%d/%Y"]),
            (
                r"\b(?:Jan|January|Feb|February|Mar|March|Apr|April|May|Jun|June|Jul|July|Aug|August|Sep|Sept|September|Oct|October|Nov|November|Dec|December)\.?\s+\d{1,2},\s+\d{4}\b",
                ["%b %d, %Y", "%B %d, %Y"],
            ),
            (
                r"\b(?:Jan|January|Feb|February|Mar|March|Apr|April|May|Jun|June|Jul|July|Aug|August|Sep|Sept|September|Oct|October|Nov|November|Dec|December)\.?\s+\d{1,2}\s+\d{4}\b",
                ["%b %d %Y", "%B %d %Y"],
            ),
        ]

        for pattern, formats in patterns_and_formats:
            match = re.search(pattern, source, flags=re.IGNORECASE)
            if not match:
                continue
            candidate = re.sub(r"\s+", " ", match.group(0).replace(".", "")).strip()
            for fmt in formats:
                try:
                    return datetime.strptime(candidate, fmt).date()
                except Exception:
                    continue
        return None

    def _collect_document_row_texts(self, row) -> List[str]:
        seen: set[str] = set()
        texts: List[str] = []

        def push(value: str) -> None:
            normalized = re.sub(r"\s+", " ", (value or "")).strip()
            if normalized and normalized not in seen:
                seen.add(normalized)
                texts.append(normalized)

        try:
            push(row.text or "")
        except Exception:
            pass

        try:
            for cell in row.find_elements(
                By.XPATH,
                './/*[@role="gridcell" or @role="cell" or contains(@class,"ag-cell") or self::td or self::span]',
            ):
                try:
                    push(cell.text or "")
                except Exception:
                    continue
        except Exception:
            pass

        try:
            payload = self.driver.execute_script(
                """
                const row = arguments[0];
                const texts = [];
                const seen = new Set();
                const push = (value) => {
                    const normalized = (value || '').replace(/\\s+/g, ' ').trim();
                    if (normalized && !seen.has(normalized)) {
                        seen.add(normalized);
                        texts.push(normalized);
                    }
                };
                const attrs = ['row-id', 'row-index', 'aria-rowindex'];
                const keys = {};
                attrs.forEach((attr) => {
                    const value = row.getAttribute(attr) || '';
                    if (value) keys[attr] = value;
                });
                const addNodeTexts = (node) => {
                    if (!node) return;
                    push(node.innerText || node.textContent || '');
                    ['title', 'aria-label', 'data-value', 'value'].forEach((attr) => {
                        if (node.getAttribute) push(node.getAttribute(attr) || '');
                    });
                    node.querySelectorAll('[role="gridcell"],[role="cell"],td,div.ag-cell,span').forEach((child) => {
                        push(child.innerText || child.textContent || '');
                        ['title', 'aria-label', 'data-value', 'value'].forEach((attr) => {
                            if (child.getAttribute) push(child.getAttribute(attr) || '');
                        });
                    });
                };
                addNodeTexts(row);
                const grid = document.querySelector('#proLawCustomGrid');
                if (grid && Object.keys(keys).length) {
                    const related = new Set();
                    grid.querySelectorAll('[row-id],[row-index],[aria-rowindex]').forEach((node) => {
                        for (const [attr, value] of Object.entries(keys)) {
                            if ((node.getAttribute(attr) || '') === value) {
                                related.add(node);
                                break;
                            }
                        }
                    });
                    related.forEach((node) => addNodeTexts(node));
                }
                return texts;
                """,
                row,
            )
        except Exception:
            payload = []

        for value in payload or []:
            push(value)
        return texts

    def _open_matters_list(self) -> bool:
        """Best-effort way back to the matter list without assuming a fresh login page."""
        if self._wait_for_matter_list_ready(timeout=3):
            return True

        try:
            self._wait_click(self.config.get("matters_nav_xpath"), timeout=5)
            if self._wait_for_matter_list_ready(timeout=10):
                return True
        except Exception:
            pass

        try:
            target_url = self._matter_list_url()
            self._log(f"Navigating directly to Matters list: {target_url}")
            self.driver.get(target_url)
            if self._wait_for_matter_list_ready(timeout=15):
                return True
        except Exception as e:
            self._log(f"Direct navigation to Matters list failed: {e}")

        try:
            self._login_and_open_matters()
            return self._wait_for_matter_list_ready(timeout=15)
        except Exception as e:
            self._log(f"Could not reopen Matters list through portal entry flow: {e}")
            return False

    def _wait_for_matter_overview_ready(self, target_url: str, timeout: int = 20) -> bool:
        """
        Confirm the SPA actually rendered the matter overview after navigation.
        In some sessions the portal redirects deep links back to `#/matters/matter-list`,
        which makes the Documents tab unavailable.
        """
        start = time.time()
        last_url = ""
        last_text = ""
        stable_text_passes = 0
        expected_token = ""
        try:
            frag = urlparse(target_url).fragment or ""
            m = re.search(r"/matters/matter-overview/([^/?#]+)", frag)
            expected_token = (m.group(1).strip() if m else "")
        except Exception:
            expected_token = ""
        while time.time() - start < timeout:
            try:
                last_url = self.driver.current_url or ""
            except Exception:
                last_url = ""

            if "#/matters/matter-list" in last_url:
                time.sleep(0.4)
                continue

            # Expected: overview route + overview component present
            if "matter-overview" in last_url:
                if expected_token:
                    try:
                        route = urlparse(last_url).fragment or ""
                        m = re.search(r"/matters/matter-overview/([^/?#]+)", route)
                        current_token = (m.group(1).strip() if m else "")
                    except Exception:
                        current_token = ""
                    if current_token and current_token != expected_token:
                        time.sleep(0.4)
                        continue
                try:
                    overview = self.driver.find_element(By.XPATH, '//*[@id="site-content"]//app-matter-overview')
                    text = re.sub(r"\s+", " ", (overview.text or "")).strip()
                    if text:
                        if text == last_text:
                            stable_text_passes += 1
                        else:
                            stable_text_passes = 0
                            last_text = text
                        if re.search(r"\b\d{2}-\d{4}\b", text) or stable_text_passes >= 2:
                            return True
                except Exception:
                    pass

            time.sleep(0.4)

        snippet = (last_text[:160] + "...") if len(last_text) > 160 else last_text
        self._log(
            f"Matter overview not ready after {timeout}s | target={target_url!r} | "
            f"last_url={last_url!r} | overview_text={snippet!r}"
        )
        return False

    def _click_documents_tab(self) -> None:
        """Click Documents tab with fallback selectors."""
        xpaths = [
            self.config.get("documents_tab_xpath"),
            '//*[@id="tab-Documents"]',
            '//*[@id="tab-Documents"]/span',
            '//*[@role="tab" and (contains(., "Documents") or contains(., "DOCUMENTS"))]',
            '//*[self::button or self::a][contains(., "Documents") or contains(., "DOCUMENTS")]',
        ]
        xpaths = [x for x in xpaths if x]
        last_exc: Optional[Exception] = None
        for xp in xpaths:
            try:
                self._wait_click(xp, timeout=10)
                return
            except Exception as e:
                last_exc = e
        raise last_exc or TimeoutException()

    def _collect_matter_links_from_page(self, active_prefix: Optional[str] = None) -> List[str]:
        hrefs: List[str] = []
        for a in self._find_matter_link_elements(active_prefix=active_prefix):
            try:
                href = a.get_attribute("href") or ""
                if href and href not in hrefs:
                    hrefs.append(href)
            except Exception:
                continue
        return hrefs

    def _find_matter_link_elements(self, active_prefix: Optional[str] = None):
        """
        Return clickable matter-overview anchors scoped to the visible matter list.

        Prefer UUID-based matter routes when present because the portal often renders
        older or cross-linked `LAF-MATT-*` anchors alongside the active filtered list.
        """
        candidates = self.driver.find_elements(
            By.XPATH,
            '//*[@id="site-content"]//app-matter-list//a[contains(@href, "/matters/matter-overview/")]',
        )
        valid = []
        uuid_candidates = []
        for el in candidates:
            try:
                href = el.get_attribute("href") or ""
                if not self._is_valid_matter_href(href):
                    continue
                try:
                    if not el.is_displayed():
                        continue
                except Exception:
                    pass
                valid.append(el)
            except Exception:
                continue

        return valid

    def _current_matter_targets(self, active_prefix: Optional[str] = None) -> List[MatterTarget]:
        try:
            raw_targets = self.driver.execute_script(
                """
                const prefix = arguments[0] || '';
                const root = document.querySelector('#site-content app-matter-list');
                if (!root) return [];
                const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return !!style && style.display !== 'none' && style.visibility !== 'hidden'
                        && rect.width > 0 && rect.height > 0;
                };
                const docketIds = (text) => {
                    const matches = (text || '').match(/\\b\\d{2}-\\d{4}\\b/g);
                    return matches ? Array.from(new Set(matches)) : [];
                };
                const anchors = Array.from(root.querySelectorAll('a[href*="/matters/matter-overview/"]'));
                return anchors
                    .filter((a) => visible(a))
                    .map((a) => {
                        let node = a;
                        let ids = [];
                        for (let depth = 0; depth < 7 && node; depth += 1) {
                            ids = docketIds((node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim());
                            if (ids.length) break;
                            node = node.parentElement;
                        }
                        const matchingIds = prefix ? ids.filter((id) => id.startsWith(prefix)) : ids;
                        return {
                            href: a.href || '',
                            matter_id: matchingIds[0] || ids[0] || '',
                        };
                    });
                """,
                active_prefix or "",
            )
        except Exception:
            raw_targets = []

        deduped: List[MatterTarget] = []
        seen_hrefs: set[str] = set()
        for item in raw_targets or []:
            href = (item or {}).get("href", "") if isinstance(item, dict) else ""
            matter_id = (item or {}).get("matter_id", "") if isinstance(item, dict) else ""
            if not href or href in seen_hrefs or not self._is_valid_matter_href(href):
                continue
            seen_hrefs.add(href)
            deduped.append(MatterTarget(href=href, matter_id=matter_id))

        if active_prefix:
            matching = [target for target in deduped if target.matter_id.startswith(active_prefix)]
            if matching:
                return matching
        return deduped

    def _current_matter_hrefs(self, active_prefix: Optional[str] = None) -> List[str]:
        return [target.href for target in self._current_matter_targets(active_prefix=active_prefix)]

    def _page_signature(self, active_prefix: Optional[str] = None) -> str:
        return "|".join(self._current_matter_hrefs(active_prefix=active_prefix)[:10])

    def _find_matter_link_by_href(self, target_href: str, active_prefix: Optional[str] = None):
        for el in self._find_matter_link_elements(active_prefix=active_prefix):
            try:
                if (el.get_attribute("href") or "") == target_href:
                    return el
            except Exception:
                continue
        return None

    def _wait_for_matter_href(self, target_href: str, timeout: int = 10, active_prefix: Optional[str] = None) -> bool:
        start = time.time()
        while time.time() - start < timeout:
            if self._find_matter_link_by_href(target_href, active_prefix=active_prefix) is not None:
                return True
            time.sleep(0.4)
        return False

    def _wait_for_page_signature_change(
        self,
        previous_signature: str,
        timeout: int = 10,
        active_prefix: Optional[str] = None,
    ) -> bool:
        start = time.time()
        while time.time() - start < timeout:
            signature = self._page_signature(active_prefix=active_prefix)
            if signature and signature != previous_signature:
                return True
            time.sleep(0.4)
        return False

    @staticmethod
    def _is_valid_matter_href(href: str) -> bool:
        """Allow only canonical matter-overview links."""
        if not href or "/matters/matter-overview/" not in href:
            return False
        lowered = href.lower()
        blocked_suffixes = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".txt")
        if lowered.endswith(blocked_suffixes):
            return False
        if "exhibit%20" in lowered or "exhibit " in lowered:
            return False
        try:
            frag = urlparse(href).fragment or ""
            m = re.search(r"/matters/matter-overview/([^/?#]+)(/.*)?$", frag)
            if not m:
                return False
            token = m.group(1).strip()
            trailing = m.group(2) or ""
            # Canonical links should not have extra route segments after the token.
            if trailing and trailing not in ("/",):
                return False
            is_laf = re.fullmatch(r"LAF-MATT-\d+", token) is not None
            is_uuid = re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", token) is not None
            return is_laf or is_uuid
        except Exception:
            return False

    def _restore_matters_page(
        self,
        prefix: Optional[str] = None,
        page_number: int = 1,
        expected_href: Optional[str] = None,
    ) -> bool:
        """Recover to a known matter-list page and optionally verify a specific href is present."""
        self._log(
            f"Restoring Matters page | filter={prefix or '[none]'} | page={page_number} | "
            f"expected_href={expected_href or '[none]'}"
        )
        if not self._open_matters_list():
            return False

        if prefix is None:
            return self._wait_for_matter_list_ready(timeout=10)

        self._set_sort_and_paging()
        if not self._apply_matter_list_prefix_filter(prefix, timeout=20):
            return False
        if not self._wait_for_matter_list_ready(timeout=20, active_prefix=prefix):
            return False

        current_page = 1
        while current_page < page_number:
            previous_signature = self._page_signature(active_prefix=prefix)
            if not self._next_page():
                self._log(f"Could not advance to restored page {page_number}; stopped at page {current_page}")
                return False
            if not self._wait_for_page_signature_change(previous_signature, timeout=10, active_prefix=prefix):
                self._log(f"Page signature did not change while restoring page {page_number}")
                return False
            current_page += 1

        if expected_href and not self._wait_for_matter_href(expected_href, timeout=10, active_prefix=prefix):
            self._log(f"Expected matter href not found after restore: {expected_href}")
            return False
        return True

    def _wait_for_matter_list_ready(self, timeout: int = 20, active_prefix: Optional[str] = None) -> bool:
        """Wait until we're back on the matter list page and links are present."""
        start = time.time()
        while time.time() - start < timeout:
            try:
                cur = self.driver.current_url or ""
            except Exception:
                cur = ""
            if "#/matters/matter-list" in cur:
                try:
                    self.driver.find_element(By.XPATH, '//*[@id="site-content"]//app-matter-list')
                except Exception:
                    # app-matter-list might not be present; still allow link-based readiness
                    pass
                links = self._current_matter_hrefs(active_prefix=active_prefix)
                if links:
                    return True
            time.sleep(0.5)
        self._log(f"Matter list not ready after {timeout}s | url={getattr(self.driver,'current_url',None)!r}")
        return False

    def _next_page(self) -> bool:
        try:
            btn = self.driver.find_element(By.XPATH, self.config.get("next_page_xpath"))
            if not btn.is_enabled():
                return False
            btn.click()
            time.sleep(1.0)
            return True
        except Exception:
            return False

    @staticmethod
    def _select_matter_id_candidates(text: str, preferred_prefix: Optional[str] = None) -> Optional[str]:
        if not text:
            return None

        labeled = re.findall(r"Matter ID\s*:?\s*(\d{2}-\d{4})", text, flags=re.IGNORECASE)
        generic = re.findall(r"\b\d{2}-\d{4}\b", text)

        candidates: List[str] = []
        seen: set[str] = set()
        for candidate in labeled + generic:
            if candidate not in seen:
                seen.add(candidate)
                candidates.append(candidate)

        if preferred_prefix:
            for candidate in candidates:
                if candidate.startswith(preferred_prefix):
                    return candidate
        return candidates[0] if candidates else None

    def _extract_matter_id(self, preferred_prefix: Optional[str] = None) -> Optional[str]:
        """
        Extract Matter ID (e.g. 26-0002).

        The portal is an SPA and the exact DOM can vary, so we:
        - wait for the page to render
        - try the configured XPath first
        - try a few structural fallbacks
        - finally, regex-scan the page text/source for NN-NNNN
        """
        # Wait for SPA render / element presence
        try:
            WebDriverWait(self.driver, 20).until(lambda d: d.execute_script("return document.readyState") == "complete")
        except Exception:
            pass
        try:
            WebDriverWait(self.driver, 20).until(EC.presence_of_element_located((By.XPATH, '//*[@id="site-content"]')))
        except Exception:
            pass

        # 1) Configured XPath (from MOADMN.md)
        xpaths_to_try = []
        cfg_xpath = self.config.get("matter_id_xpath")
        if cfg_xpath:
            xpaths_to_try.append(cfg_xpath)

        # 2) Structural fallbacks (more tolerant)
        xpaths_to_try.extend(
            [
                '//*[@id="site-content"]//app-matter-overview//dl',
                '//*[@id="site-content"]//app-matter-overview',
                '//*[@id="site-content"]//*[contains(text(),"Matter") and contains(text(),"ID")]',
            ]
        )

        for xp in xpaths_to_try:
            try:
                el = WebDriverWait(self.driver, 5).until(EC.presence_of_element_located((By.XPATH, xp)))
                txt = (el.text or "").strip()
                matter_id = self._select_matter_id_candidates(txt, preferred_prefix=preferred_prefix)
                if matter_id:
                    return matter_id
            except Exception:
                continue

        # 3) Regex scan within the matter overview container text (avoids reusing stale IDs from other UI regions)
        try:
            overview = self.driver.find_element(By.XPATH, '//*[@id="site-content"]//app-matter-overview')
            txt = (overview.text or "").strip()
            matter_id = self._select_matter_id_candidates(txt, preferred_prefix=preferred_prefix)
            if matter_id:
                return matter_id
            html = overview.get_attribute("outerHTML") or ""
            matter_id = self._select_matter_id_candidates(html, preferred_prefix=preferred_prefix)
            if matter_id:
                return matter_id
        except Exception:
            pass

        # 4) Regex scan page_source as last resort.
        # If the active prefix is present anywhere in the page, prefer that over
        # older docket numbers that can appear in side panels or stale content.
        try:
            src = self.driver.page_source or ""
            matter_id = self._select_matter_id_candidates(src, preferred_prefix=preferred_prefix)
            if matter_id:
                return matter_id
        except Exception:
            pass

        # 5) Fallback: parse route token from URL fragment (SPA route) only if it
        # already looks like a business docket ID. Internal `LAF-MATT-*` tokens
        # are not safe for duplicate checks or output naming.
        try:
            cur = self.driver.current_url or ""
            parsed = urlparse(cur)
            route = parsed.fragment or ""
            m = re.search(r"/matters/matter-overview/([^/?#]+)", route)
            seg = m.group(1).strip() if m else ""
            if seg and re.fullmatch(r"\d{2}-\d{4}", seg):
                if not preferred_prefix or seg.startswith(preferred_prefix):
                    return seg
        except Exception:
            pass

        # No match
        return None

    def _search_documents_for_order(self, matter_id: str) -> None:
        self._log(f"Opening Documents tab and searching for Order items | matter_id={matter_id}")
        self._click_documents_tab()
        time.sleep(0.7)
        self._wait_send_keys(self.config.get("filter_xpath"), "order")
        self._wait_click(self.config.get("document_search_button_xpath"))
        time.sleep(1.2)
        self._log(f"Document search UI state | matter_id={matter_id} | state={self._document_results_state()}")

    def _document_result_rows(self):
        selectors = [
            '//*[@id="proLawCustomGrid"]//*[contains(@class,"ag-center-cols-container")]//*[contains(@class,"ag-row")]',
            '//*[@id="proLawCustomGrid"]//*[@role="row" and not(contains(@class,"ag-header"))]',
            '//*[@id="proLawCustomGrid"]//tr[td]',
            '//*[@id="site-content"]//*[contains(@class,"document") and (@role="row" or self::tr or self::div)]',
        ]
        rows = []
        seen_ids: set[str] = set()
        for xp in selectors:
            try:
                candidates = self.driver.find_elements(By.XPATH, xp)
            except Exception:
                candidates = []
            for el in candidates:
                try:
                    key = el.id
                except Exception:
                    key = ""
                if key and key in seen_ids:
                    continue
                try:
                    txt = (el.text or "").strip()
                except Exception:
                    txt = ""
                if not txt:
                    continue
                if key:
                    seen_ids.add(key)
                rows.append(el)
            if rows:
                break
        return rows

    def _document_results_state(self) -> str:
        rows = self._document_result_rows()
        if rows:
            return "rows"
        try:
            if self.driver.find_elements(By.XPATH, '//*[@id="proLawCustomGrid"]'):
                return "empty-grid"
        except Exception:
            pass
        try:
            text = (self.driver.find_element(By.XPATH, '//*[@id="site-content"]').text or "").lower()
        except Exception:
            text = ""
        if any(phrase in text for phrase in ("no results", "no records", "0 results", "0 records")):
            return "no-results"
        return "unknown"

    def _wait_for_document_results_ready(self, timeout: int = 12) -> str:
        start = time.time()
        last_state = "unknown"
        while time.time() - start < timeout:
            last_state = self._document_results_state()
            if last_state != "unknown":
                return last_state
            time.sleep(0.4)
        return last_state

    def _click_document_list_view(self) -> None:
        xpaths = [
            self.config.get("documents_list_view_xpath"),
            '//*[@id="tab-Documents-panel"]/matter-document/div[1]/ul/li[1]/fieldset/div/label[2]',
            '//*[@id="tab-Documents-panel"]//*[contains(@class,"list") and (self::label or self::button or self::span)]',
            '//*[@id="tab-Documents-panel"]//*[contains(., "List View")]',
        ]
        xpaths = [xp for xp in xpaths if xp]
        last_exc: Optional[Exception] = None
        for xp in xpaths:
            try:
                self._wait_click(xp, timeout=10)
                return
            except Exception as exc:
                last_exc = exc
        raise last_exc or TimeoutException()

    @staticmethod
    def _text_has_pdf_signal(text: str) -> bool:
        normalized = (text or "").lower()
        if not normalized:
            return False
        if ".pdf" in normalized or "application/pdf" in normalized:
            return True
        return bool(re.search(r"(?<![a-z0-9])pdf(?![a-z0-9])", normalized))

    def _pdf_evidence_for_row(self, row, text_candidates: List[str]) -> str:
        for candidate in text_candidates:
            if self._text_has_pdf_signal(candidate):
                return "text"

        try:
            payload = self.driver.execute_script(
                """
                const row = arguments[0];
                const attrs = ['class', 'title', 'aria-label', 'alt', 'src', 'href', 'type', 'data-filetype', 'data-type'];
                const values = [];
                const seen = new Set();
                const push = (value) => {
                    const normalized = (value || '').toString().trim();
                    if (normalized && !seen.has(normalized)) {
                        seen.add(normalized);
                        values.push(normalized);
                    }
                };
                const addAttrs = (node) => {
                    if (!node || !node.getAttribute) return;
                    attrs.forEach((attr) => push(node.getAttribute(attr) || ''));
                };
                addAttrs(row);
                row.querySelectorAll('*').forEach((child) => addAttrs(child));
                return values;
                """,
                row,
            )
        except Exception:
            payload = []

        for candidate in payload or []:
            normalized = (candidate or "").lower()
            if not normalized:
                continue
            if self._text_has_pdf_signal(normalized):
                return "icon/attribute"
            if any(token in normalized for token in ("file-pdf", "pdf-icon", "icon-pdf", "fa-file-pdf", "mime-pdf")):
                return "icon/attribute"

        return "none"

    @staticmethod
    def _normalize_document_phrase(text: str) -> str:
        normalized = (text or "").lower()
        normalized = normalized.replace("’", "'")
        normalized = normalized.replace("&", " and ")
        normalized = normalized.replace("/", " ")
        normalized = normalized.replace("-", " ")
        normalized = normalized.replace("resp't", "respondent")
        normalized = normalized.replace("'", "")

        regex_replacements = (
            (r"\bmtn\b", "motion"),
            (r"\bresp\b", "respondent"),
            (r"\brespt\b", "respondent"),
            (r"\bansw\b", "answer"),
            (r"\bcompl\b", "complaint"),
            (r"\bdismiisal\b", "dismissal"),
            (r"\bjsr\b", "status report"),
        )
        for pattern, replacement in regex_replacements:
            normalized = re.sub(pattern, replacement, normalized)
        normalized = re.sub(r"[^a-z0-9\s]", " ", normalized)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        return normalized

    def _score_phrase_match(self, title: str, phrase: str) -> float:
        title_norm = self._normalize_document_phrase(title)
        phrase_norm = self._normalize_document_phrase(phrase)
        if not title_norm or not phrase_norm:
            return 0.0
        if title_norm == phrase_norm:
            return 1.0
        if phrase_norm in title_norm or title_norm in phrase_norm:
            return 0.96

        title_tokens = set(title_norm.split())
        phrase_tokens = set(phrase_norm.split())
        if not title_tokens or not phrase_tokens:
            return 0.0

        overlap = len(title_tokens & phrase_tokens) / max(len(phrase_tokens), 1)
        ratio = SequenceMatcher(None, title_norm, phrase_norm).ratio()
        return max(overlap, ratio)

    def _best_phrase_match(self, title: str, phrases: List[str]) -> Tuple[str, float]:
        best_phrase = ""
        best_score = 0.0
        for phrase in phrases:
            score = self._score_phrase_match(title, phrase)
            if score > best_score:
                best_score = score
                best_phrase = phrase
        return best_phrase, best_score

    def _forced_exclude_title_match(self, title: str) -> str:
        normalized = self._normalize_document_phrase(title)
        if not normalized:
            return ""

        exact_families = [
            ("answer", "Answer"),
            ("amended answer", "Amended Answer"),
            ("complaint", "Complaint"),
            ("motion to dismiss", "Motion to Dismiss"),
            ("petitioners motion to dismiss", "Petitioner's Motion to Dismiss"),
            ("response to show cause order", "Response to Show Cause Order"),
            ("respondents response to show cause order", "Respondent's Response to Show Cause Order"),
            ("petitioners response to show cause order", "Petitioner's Response to Show Cause Order"),
            ("pet response to show cause order", "Pet. Response to Show Cause Order"),
            ("resp response to show cause order", "Resp. Response to Show Cause Order"),
            ("show cause order", "Show Cause Order"),
            ("show cause", "Show Cause Order"),
            ("consent order", "Consent Order"),
            ("joint request for consent order", "Joint Request for Consent Order"),
            ("continuance order", "Continuance Order"),
            ("continuance order set sh", "Continuance Order Set SH"),
            ("order answer due", "Order - Answer Due"),
            ("order respondents answer due", "Order - Respondent's Answer Due"),
            ("order respondent answer due", "Order - Respondent's Answer Due"),
            ("order respondent to file answer", "Order - Resp to File Answer"),
            ("order close complaint", "Order - Close Complaint"),
        ]
        for family, label in exact_families:
            if normalized == family or normalized.startswith(family + " "):
                return label

        if normalized.startswith("motion to "):
            return "Motion to Dismiss"
        if normalized.startswith("response to "):
            return "Response"
        if normalized.startswith("petitioners response to "):
            return "Petitioner's Response"
        if normalized.startswith("respondents response to "):
            return "Respondent's Response"
        if normalized.startswith("joint request"):
            return "Joint Request"
        if normalized.startswith("exhibit ") or normalized.startswith("ex "):
            return "Exhibit"
        if normalized.startswith("respondents exhibit "):
            return "Respondent's Exhibit"
        if normalized.startswith("petitioners memorandum in opposition"):
            return "Petitioner's Memorandum in Opposition"
        if normalized.startswith("petitioners request that order of dismissal be set aside"):
            return "Petitioner's Request that Order of Dismissal be Set Aside"

        if normalized.startswith("order ") and "show cause" in normalized:
            return "Order - Show Cause"
        return ""

    def _classify_document_phrase(self, title: str) -> Dict[str, str | float]:
        if self._is_order_like_title(title):
            include_phrase, include_score = self._best_phrase_match(title, INCLUDE_TITLE_PATTERNS)
            if include_phrase and include_score >= 0.9:
                return {
                    "result": "include",
                    "matched_phrase": include_phrase,
                    "score": include_score,
                    "reason": "matched include criteria",
                }
            return {
                "result": "include",
                "matched_phrase": "Default order collect",
                "score": 0.0,
                "reason": "default order collect",
            }

        forced_exclude = self._forced_exclude_title_match(title)
        if forced_exclude:
            return {
                "result": "exclude",
                "matched_phrase": forced_exclude,
                "score": 1.0,
                "reason": "matched exclude criteria",
            }

        return {
            "result": "no-match",
            "matched_phrase": "",
            "score": 0.0,
            "reason": "title is not order-like",
        }

    @staticmethod
    def _summarize_values(values: List[str], limit: int = 4) -> str:
        cleaned: List[str] = []
        seen: set[str] = set()
        for value in values:
            text = re.sub(r"\s+", " ", (value or "")).strip()
            if not text or text in seen:
                continue
            seen.add(text)
            cleaned.append(text)
        if not cleaned:
            return ""
        if len(cleaned) <= limit:
            return "; ".join(cleaned)
        remaining = len(cleaned) - limit
        return "; ".join(cleaned[:limit]) + f"; +{remaining} more"

    def _extract_document_row_title(self, row, text_candidates: List[str]) -> str:
        title = ""
        try:
            cells = row.find_elements(
                By.XPATH,
                './/*[@role="gridcell" or @role="cell" or contains(@class,"ag-cell") or self::td]',
            )
        except Exception:
            cells = []

        for cell in cells:
            try:
                text = re.sub(r"\s+", " ", (cell.text or "")).strip()
            except Exception:
                text = ""
            if not text or self._parse_mmddyyyy_text(text):
                continue
            title = text
            break

        if not title:
            for candidate in text_candidates:
                if self._parse_mmddyyyy_text(candidate):
                    continue
                title = candidate
                break
        return title

    def _is_order_like_title(self, title: str) -> bool:
        raw_normalized = (title or "").lower().replace("’", "'")
        raw_normalized = raw_normalized.replace("&", " and ")
        raw_normalized = raw_normalized.replace("/", " ")
        raw_normalized = raw_normalized.replace("-", " ")
        raw_normalized = re.sub(r"[^a-z0-9\s']", " ", raw_normalized)
        raw_normalized = re.sub(r"\s+", " ", raw_normalized).strip()

        raw_disqualifying_prefixes = (
            "answer",
            "amended answer",
            "complaint",
            "motion to",
            "petitioner's motion",
            "petitioners motion",
            "respondent's motion",
            "respondents motion",
            "response to",
            "petitioner's response",
            "petitioners response",
            "respondent's response",
            "respondents response",
            "pet response",
            "resp response",
            "joint request",
            "exhibit",
            "ex ",
            "petitioner's request",
            "petitioners request",
        )
        if raw_normalized.startswith(raw_disqualifying_prefixes):
            return False

        normalized = self._normalize_document_phrase(title)
        if not normalized:
            return False
        disqualifying_prefixes = (
            "answer",
            "amended answer",
            "complaint",
            "motion to",
            "petitioners motion",
            "respondents motion",
            "response to",
            "petitioners response",
            "respondents response",
            "pet response",
            "resp response",
            "joint request",
            "exhibit",
            "ex ",
            "petitioners request",
        )
        if normalized.startswith(disqualifying_prefixes):
            return False
        if (
            normalized.startswith("order")
            or normalized.startswith("amended order")
            or normalized.startswith("decision")
            or normalized.startswith("default decision")
        ):
            return True
        tokens = set(normalized.split())
        return "order" in tokens or "decision" in tokens

    def _click_visible_element(self, xpath: str, timeout: int = 10) -> bool:
        start = time.time()
        last_exc: Optional[Exception] = None
        while time.time() - start < timeout:
            try:
                elements = self.driver.find_elements(By.XPATH, xpath)
            except Exception as exc:
                last_exc = exc
                elements = []

            for element in elements:
                try:
                    if not element.is_displayed():
                        continue
                except Exception:
                    continue
                try:
                    element.click()
                    return True
                except Exception as exc:
                    last_exc = exc
                    try:
                        self.driver.execute_script("arguments[0].click();", element)
                        return True
                    except Exception as js_exc:
                        last_exc = js_exc
                        continue
            time.sleep(0.2)

        if last_exc:
            raise last_exc
        return False

    def _make_document_download_click(self, row_index: int) -> Callable[[], None]:
        def _click_download() -> None:
            live_rows = self._document_result_rows()
            if row_index >= len(live_rows):
                raise RuntimeError(f"Document row index out of range: {row_index}")

            row = live_rows[row_index]
            last_error: Optional[Exception] = None
            for attempt in range(3):
                try:
                    self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", row)
                except Exception:
                    pass
                try:
                    row.click()
                except Exception:
                    pass

                menu_candidates = []
                try:
                    menu_candidates = row.find_elements(
                        By.XPATH,
                        './/button[contains(@aria-label,"Action") or contains(@aria-label,"More") or contains(@class,"kebab")]',
                    )
                except Exception:
                    menu_candidates = []

                if not menu_candidates:
                    try:
                        row_buttons = [
                            button
                            for button in row.find_elements(By.XPATH, './/button')
                            if button.is_displayed()
                        ]
                        if row_buttons:
                            menu_candidates = row_buttons[-1:]
                    except Exception:
                        menu_candidates = []

                clicked = False
                for button in menu_candidates:
                    try:
                        self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", button)
                    except Exception:
                        pass
                    try:
                        button.click()
                        clicked = True
                        break
                    except Exception:
                        try:
                            self.driver.execute_script("arguments[0].click();", button)
                            clicked = True
                            break
                        except Exception as exc:
                            last_error = exc
                            continue

                if not clicked:
                    actions_xpath = self.config.get("document_actions_button_xpath")
                    if actions_xpath:
                        try:
                            self._wait_click(actions_xpath, timeout=10)
                            clicked = True
                        except Exception as exc:
                            last_error = exc

                if not clicked:
                    continue

                time.sleep(0.4)
                download_xpaths = [
                    '//*[@id="gridDownloadButton"]',
                    '//*[@role="menu"]//*[normalize-space()="Download"]',
                    '//*[(@role="menuitem" or self::button or self::a or self::li or self::span or self::div) and normalize-space()="Download"]',
                    '//*[contains(@class,"menu") or contains(@class,"dropdown") or contains(@class,"popover")]//*[normalize-space()="Download"]',
                ]
                for xpath in download_xpaths:
                    try:
                        if self._click_visible_element(xpath, timeout=4):
                            return
                    except Exception as exc:
                        last_error = exc
                        continue

                try:
                    body = self.driver.find_element(By.TAG_NAME, "body")
                    body.send_keys(Keys.ESCAPE)
                except Exception:
                    pass
                time.sleep(0.3)

            if last_error:
                raise TimeoutException("The Download menu item did not appear after opening the Actions menu.") from last_error
            raise TimeoutException("The Download menu item did not appear after opening the Actions menu.")

        return _click_download

    def _collect_document_items_from_current_results(self, matter_id: str) -> List[DocumentItem]:
        items: List[DocumentItem] = []
        rows = self._document_result_rows()
        for idx, row in enumerate(rows):
            try:
                text_candidates = self._collect_document_row_texts(row)
            except Exception:
                text_candidates = []

            title = self._extract_document_row_title(row, text_candidates)
            created = None
            for candidate in text_candidates:
                created = self._parse_mmddyyyy_text(candidate)
                if created:
                    break

            full_text = " | ".join(text_candidates)
            items.append(
                DocumentItem(
                    matter_id=matter_id,
                    title=title,
                    date_created=created,
                    download_click=self._make_document_download_click(idx),
                    pdf_evidence=self._pdf_evidence_for_row(row, text_candidates),
                    row_text=full_text,
                )
            )
        return items

    def _tracker_review_window(self) -> Tuple[date, date]:
        days_before = max(0, int(self.config.get("tracker_date_window_days_before", 7) or 0))
        days_after = max(0, int(self.config.get("tracker_date_window_days_after", 7) or 0))
        return (
            self.start_date - timedelta(days=days_before),
            self.end_date + timedelta(days=days_after),
        )

    def _retry_exact_matter_search(self, matter_id: str, prefix: Optional[str] = None) -> bool:
        self._log(
            f"Refreshing Matters list and retrying exact search | matter_id={matter_id} | "
            f"prefix={prefix or '[none]'}"
        )
        if not self._open_matters_list():
            return False

        try:
            self.driver.refresh()
        except Exception:
            pass

        if not self._wait_for_matter_list_ready(timeout=15):
            if not self._open_matters_list():
                return False

        self._set_sort_and_paging()
        if prefix and not self._apply_matter_list_prefix_filter(prefix, timeout=20):
            self._log(
                f"Prefix filter retry did not verify before exact search | matter_id={matter_id} | "
                f"prefix={prefix}; continuing with exact filter fallback."
            )
        return self._apply_matter_list_exact_filter(matter_id, timeout=20)

    @staticmethod
    def _apply_final_review_decision(row: Dict) -> None:
        status = str(row.get("document_check_status") or "").strip().lower()
        if not status:
            return
        if status == "collected":
            row["decision"] = "for collect"
            return
        if (
            status.startswith("skipped -")
            or status in {"no matching order rows", "reviewed"}
        ):
            row["decision"] = "skip"

    def _open_exact_filtered_matter_in_new_tab(self, matter_id: str, prefix: Optional[str] = None) -> Optional[str]:
        self._status(f"Finding matter {matter_id} in the list...")
        attempts = max(1, int(self.config.get("document_review_matter_open_retry_attempts", 3) or 3))
        backoff_seconds = max(0, int(self.config.get("document_review_matter_open_retry_backoff_seconds", 3) or 0))
        target_wait_seconds = max(1, int(self.config.get("document_review_exact_target_wait_seconds", 5) or 5))

        for attempt in range(attempts):
            verified = (
                self._apply_matter_list_exact_filter(matter_id, timeout=20)
                if attempt == 0
                else self._retry_exact_matter_search(matter_id, prefix=prefix)
            )
            if not verified:
                if attempt < attempts - 1:
                    if backoff_seconds:
                        time.sleep(backoff_seconds)
                    continue
                self._log(
                    f"Retry exact search still did not verify | matter_id={matter_id} | "
                    f"prefix={prefix or '[none]'}"
                )
                self._log(f"Could not open matter review tab | matter_id={matter_id} | reason=exact filter did not verify")
                return None

            if self._matter_list_has_no_results():
                if attempt < attempts - 1:
                    if backoff_seconds:
                        time.sleep(backoff_seconds)
                    continue
                self._log(f"Could not open matter review tab | matter_id={matter_id} | reason=no matter results were returned")
                return None

            targets = [target for target in self._current_matter_targets() if target.matter_id == matter_id]
            if not targets:
                targets = self._wait_for_matching_matter_targets(
                    matter_id,
                    timeout=target_wait_seconds,
                    active_prefix=prefix,
                )
            if not targets:
                if attempt < attempts - 1:
                    if backoff_seconds:
                        time.sleep(backoff_seconds)
                    continue
                self._log(
                    f"Could not open matter review tab | matter_id={matter_id} | "
                    f"reason=no clickable matter target was found"
                )
                return None

            target = targets[0]
            parent_handle = self.driver.current_window_handle
            before_handles = list(self.driver.window_handles)
            try:
                self._status(f"Opening matter {matter_id}...")
                self.driver.execute_script("window.open(arguments[0], '_blank');", target.href)
                WebDriverWait(self.driver, 10).until(lambda d: len(d.window_handles) > len(before_handles))
                after_handles = list(self.driver.window_handles)
                new_handles = [handle for handle in after_handles if handle not in before_handles]
                if not new_handles:
                    raise RuntimeError("No new browser tab was detected.")
                new_handle = new_handles[-1]
                self.driver.switch_to.window(new_handle)
                self._log(f"Opening exact-filtered matter in a new tab | matter_id={matter_id} | href={target.href}")
                if not self._wait_for_matter_overview_ready(target.href, timeout=20):
                    raise RuntimeError("The matter overview page did not finish loading.")

                actual_matter_id = self._extract_matter_id(preferred_prefix=f"{matter_id[:2]}-") or self._extract_matter_id()
                if actual_matter_id != matter_id:
                    raise RuntimeError(f"The opened matter did not match {matter_id}; opened {actual_matter_id or '[none]'} instead.")

                self._log(f"Matter overview confirmed | matter_id={matter_id}")
                return new_handle
            except Exception as exc:
                try:
                    current_handle = self.driver.current_window_handle
                except Exception:
                    current_handle = None
                try:
                    if current_handle and current_handle not in before_handles:
                        self.driver.close()
                except Exception:
                    pass
                try:
                    if parent_handle and parent_handle in self.driver.window_handles:
                        self.driver.switch_to.window(parent_handle)
                except Exception:
                    pass
                if attempt == 0:
                    continue
                self._log(f"Could not open matter review tab | matter_id={matter_id} | reason={exc}")
                return None
        self._log(f"Could not open matter review tab | matter_id={matter_id} | reason=unknown review-tab failure")
        return None

    def _review_order_documents_in_list_view(self, matter_id: str) -> Dict[str, str | int]:
        self._log(f"Reviewing listed documents | matter_id={matter_id}")
        self._status(f"Opening the Documents tab for {matter_id}...")
        self._click_documents_tab()
        time.sleep(0.8)
        self._status(f"Switching {matter_id} to document list view...")
        self._click_document_list_view()
        state = self._wait_for_document_results_ready(timeout=12)
        self._log(f"Document list view ready | matter_id={matter_id} | state={state}")

        self._log(f"Searching listed documents for Order items | matter_id={matter_id}")
        self._status(f"Searching documents for Order items in {matter_id}...")
        self._wait_send_keys(self.config.get("filter_xpath"), "Order")
        self._wait_click(self.config.get("document_search_button_xpath"))
        time.sleep(1.0)
        state = self._wait_for_document_results_ready(timeout=12)
        self._log(f"Document search UI state | matter_id={matter_id} | state={state}")

        self._status(f"Checking matching document rows in {matter_id}...")
        items = self._collect_document_items_from_current_results(matter_id)
        reviewed_titles: List[str] = []
        order_titles: List[str] = []
        pdf_titles: List[str] = []
        include_phrases: List[str] = []
        exclude_phrases: List[str] = []
        duplicate_hits: List[str] = []
        duplicate_locations: List[str] = []
        skipped_notes: List[str] = []
        downloaded_titles: List[str] = []
        downloaded_files: List[str] = []
        collected_dates: List[date] = []
        strongest_pdf_evidence = "none"
        review_window_start, review_window_end = self._tracker_review_window()

        for item in items:
            title = re.sub(r"\s+", " ", (item.title or item.row_text or "")).strip()
            if not title:
                continue
            reviewed_titles.append(title)

            phrase_match = self._classify_document_phrase(title)
            result_type = str(phrase_match["result"])
            matched_phrase = str(phrase_match["matched_phrase"] or "")

            if result_type == "exclude":
                if matched_phrase:
                    exclude_phrases.append(matched_phrase)
                skipped_notes.append(
                    f"{title} [Skipped by exclude rule: {matched_phrase or 'exclude criteria'}]"
                )
                self._log(
                    f"Skipping document row | matter_id={matter_id} | title={title!r} | "
                    f"reason=Matched exclude rule {matched_phrase or 'exclude criteria'}"
                )
                continue

            if not self._is_order_like_title(title):
                skipped_notes.append(f"{title} [Title is not an Order/Decision document]")
                self._log(
                    f"Skipping document row | matter_id={matter_id} | title={title!r} | "
                    f"reason=Title is not an Order/Decision document"
                )
                continue

            order_titles.append(title)
            if item.pdf_evidence != "none":
                pdf_titles.append(title)
                if strongest_pdf_evidence == "none":
                    strongest_pdf_evidence = item.pdf_evidence

            if item.pdf_evidence == "none":
                skipped_notes.append(f"{title} [PDF not confirmed]")
                self._log(
                    f"Skipping Order row | matter_id={matter_id} | title={title!r} | "
                    f"reason=PDF could not be confirmed"
                )
                continue
            if not item.date_created:
                skipped_notes.append(f"{title} [Created date missing]")
                self._log(
                    f"Skipping Order row | matter_id={matter_id} | title={title!r} | "
                    f"reason=Created date is missing"
                )
                continue
            if not (review_window_start <= item.date_created <= review_window_end):
                skipped_notes.append(
                    f"{title} [Created {item.date_created:%m/%d/%Y} is outside the LN Doc Tracker date window]"
                )
                self._log(
                    f"Skipping Order row | matter_id={matter_id} | title={title!r} | "
                    f"reason=Created {item.date_created:%m/%d/%Y} is outside the LN Doc Tracker window "
                    f"({review_window_start:%m/%d/%Y} to {review_window_end:%m/%d/%Y})"
                )
                continue
            if result_type != "include":
                skipped_notes.append(f"{title} [Did not match an include rule]")
                self._log(
                    f"Skipping Order row | matter_id={matter_id} | title={title!r} | "
                    f"reason=Did not match an include rule"
                )
                continue

            if matched_phrase:
                include_phrases.append(matched_phrase)

            existing_online_match = self._existing_online_document_match(item.matter_id, item.date_created)
            if existing_online_match:
                duplicate_case_id = str(existing_online_match.get("case_id") or item.matter_id).strip().upper()
                duplicate_source = str(existing_online_match.get("source") or "online database").strip()
                duplicate_location = str(existing_online_match.get("location") or "").strip()
                duplicate_hits.append(f"{duplicate_source}:{duplicate_case_id}")
                if duplicate_location:
                    duplicate_locations.append(duplicate_location)
                skipped_notes.append(
                    f"{title} [Already exists in {duplicate_source} under {duplicate_case_id}]"
                )
                self._log(
                    f"Skipping Order row | matter_id={matter_id} | title={title!r} | "
                    f"reason=Existing online duplicate found | existing_case_id={duplicate_case_id} | "
                    f"source={duplicate_source} | location={duplicate_location or '[none]'}"
                )
                continue

            target_filename = self._build_target_filename(matter_id, item.date_created)
            try:
                if not item.download_click:
                    raise RuntimeError("Download action is unavailable for this row")
                self._status(f"Downloading an Order PDF for {matter_id}...")
                self._log(
                    f"Collecting Order row | matter_id={matter_id} | title={title!r} | "
                    f"created={item.date_created:%m/%d/%Y} | matched_include={matched_phrase or '[none]'}"
                )
                raw_download = self._download_file_with_retry(
                    item.download_click,
                    self._collected_dir(),
                    matter_id=matter_id,
                    title=title,
                    target_filename=target_filename,
                )
                if not raw_download:
                    raise RuntimeError("Timed out waiting for download")
                if raw_download.suffix.lower() != ".pdf":
                    self._exclude_non_pdf_download(
                        raw_download,
                        matter_id=matter_id,
                        title=title,
                        date_created=item.date_created,
                        target_filename=target_filename,
                    )
                    skipped_notes.append(
                        f"{title} [Downloaded non-PDF file: {raw_download.name}]"
                    )
                    continue
                downloaded_titles.append(title)
                downloaded_files.append(target_filename)
                collected_dates.append(item.date_created)
                self.pending_download_renames.append(
                    {
                        "matter_id": matter_id,
                        "doc_title": title,
                        "date_created": item.date_created,
                        "raw_path": str(raw_download),
                        "target_name": target_filename,
                    }
                )
                self._log(
                    f"Downloaded Order row | matter_id={matter_id} | title={title!r} | raw_filename={raw_download.name} | pending_final_name={target_filename}"
                )
            except Exception as exc:
                self._log(
                    f"Download failed for {matter_id} ({target_filename}): {type(exc).__name__}: {exc}"
                )
                self.download_failures.append(
                    {
                        "matter_id": matter_id,
                        "doc_title": title,
                        "date_created": item.date_created or "",
                        "error": str(exc),
                    }
                )
                skipped_notes.append(f"{title} [Download failed: {exc}]")

        primary_title = downloaded_titles[0] if downloaded_titles else (order_titles[0] if order_titles else "")
        primary_date = collected_dates[0] if collected_dates else ""
        if downloaded_titles:
            check_status = "collected"
            criteria_result = "include"
        elif order_titles:
            if duplicate_hits:
                check_status = "skipped - duplicate already exists online"
                criteria_result = "duplicate"
            elif pdf_titles:
                if skipped_notes:
                    check_status = "skipped - no collectible include match"
                    criteria_result = "skip"
                else:
                    check_status = "reviewed"
                    criteria_result = "skip"
            else:
                check_status = "skipped - PDF not confirmed"
                criteria_result = "skip"
        elif reviewed_titles:
            if exclude_phrases:
                check_status = "skipped - exclude criteria"
                criteria_result = "exclude"
            else:
                check_status = "skipped - no order-like titles"
                criteria_result = "skip"
        else:
            check_status = "no matching Order rows"
            criteria_result = "none"

        result = {
            "doc_title": primary_title,
            "date_created": primary_date,
            "order_document_found": "Y" if order_titles else "N",
            "order_pdf_found": "Y" if pdf_titles else "N",
            "order_pdf_evidence": strongest_pdf_evidence,
            "order_document_title": primary_title,
            "document_rows_seen": len(items),
            "documents_view_state": state,
            "document_check_status": check_status,
            "criteria_result": criteria_result,
            "window_start": review_window_start,
            "window_end": review_window_end,
            "matched_include_phrase": self._summarize_values(include_phrases),
            "matched_exclude_phrase": self._summarize_values(exclude_phrases),
            "duplicate_path": self._summarize_values(duplicate_locations),
            "document_phrase_log": self._summarize_values(reviewed_titles),
            "skipped_document_notes": self._summarize_values(skipped_notes, limit=6),
            "downloaded_filenames": self._summarize_values(downloaded_files),
            "documents_collected_count": len(downloaded_files),
        }
        self._log(
            f"Order document review | matter_id={matter_id} | order_found={result['order_document_found']} | "
            f"order_pdf={result['order_pdf_found']} | evidence={result['order_pdf_evidence']} | "
            f"rows={result['document_rows_seen']} | collected={result['documents_collected_count']} | "
            f"status={result['document_check_status']} | title={primary_title or '[none]'}"
        )
        return result

    def _extract_order_documents(self, matter_id: str, record_non_orders: bool = True) -> List[DocumentItem]:
        """
        Best-effort extraction from `proLawCustomGrid`.
        The exact grid DOM can vary, so we use a few fallback strategies.
        """
        items: List[DocumentItem] = []
        grid_rows_seen = 0
        order_title_matches = 0
        order_in_range_matches = 0
        order_out_of_range = 0
        rows_without_date = 0
        non_order_rows = 0

        # Strategy 1: ag-grid style rows
        rows = self._document_result_rows()
        grid_rows_seen = len(rows)
        for idx, r in enumerate(rows):
            try:
                # Extract title and date for ALL documents (needed for reporting)
                title = ""
                created = None
                text_candidates = self._collect_document_row_texts(r)
                title_cells = r.find_elements(By.XPATH, './/*[contains(@class,"ag-cell")][1]')
                if title_cells:
                    title = (title_cells[0].text or "").strip()
                if not title:
                    generic_cells = r.find_elements(
                        By.XPATH,
                        './/*[@role="gridcell" or @role="cell" or contains(@class,"ag-cell") or self::td]',
                    )
                    for cell in generic_cells:
                        txt = (cell.text or "").strip()
                        if txt:
                            title = txt
                            break

                if not title:
                    for candidate in text_candidates:
                        if self._parse_mmddyyyy_text(candidate):
                            continue
                        title = candidate
                        break

                for candidate in text_candidates:
                    created = self._parse_mmddyyyy_text(candidate)
                    if created:
                        break
                 
                # Skip non-order documents (but include them in excluded list with date for reporting)
                if not title or "order" not in title.lower():
                    non_order_rows += 1
                    # Only log excluded documents if we have a title (avoid logging empty rows)
                    if title and record_non_orders:
                        created_txt = created.strftime("%m/%d/%Y") if created else "N/A"
                        self._log(
                            f"Skipping non-order document | matter_id={matter_id} | "
                            f"title={title!r} | date_created={created_txt}"
                        )
                        self.excluded_non_order.append(
                            {
                                "matter_id": matter_id,
                                "doc_title": title,
                                "date_created": created if created else "",
                            }
                        )
                    continue

                order_title_matches += 1
                if not created:
                    rows_without_date += 1
                    continue
                if not (self.start_date <= created <= self.end_date):
                    order_out_of_range += 1
                    continue
                order_in_range_matches += 1

                # download action: try "..." menu inside row
                def _click_download(row_index=idx):
                    # Re-resolve row by index at click time to avoid stale references.
                    live_rows = self._document_result_rows()
                    if row_index >= len(live_rows):
                        raise RuntimeError(f"Document row index out of range: {row_index}")
                    row = live_rows[row_index]
                    menu = row.find_element(By.XPATH, './/button[contains(@aria-label,"More") or contains(@class,"kebab")]')
                    menu.click()
                    time.sleep(0.3)
                    # click download
                    dl = self.driver.find_element(By.XPATH, '//button[contains(., "Download") or contains(., "download")]')
                    dl.click()

                items.append(DocumentItem(matter_id=matter_id, title=title, date_created=created, download_click=_click_download))
            except Exception:
                continue

        # Strategy 2: fall back to the exact XPaths from MOADMN.md for first row only
        if not items:
            try:
                title_el = self.driver.find_element(By.XPATH, '//*[@id="proLawCustomGrid"]/div[1]/div[1]/div[1]/div[3]/div[1]/div/div')
                date_el = self.driver.find_element(By.XPATH, '//*[@id="proLawCustomGrid"]/div[1]/div[1]/div[1]/div[3]/div[3]/div')
                title = (title_el.text or "").strip()
                date_txt = (date_el.text or "").strip()
                created = datetime.strptime(date_txt, "%m/%d/%Y").date()
                if title and "order" in title.lower() and (self.start_date <= created <= self.end_date):
                    order_title_matches = max(order_title_matches, 1)
                    order_in_range_matches = max(order_in_range_matches, 1)
                    items.append(DocumentItem(matter_id=matter_id, title=title, date_created=created))
            except Exception:
                pass

        self._log(
            f"Document search summary | matter_id={matter_id} | grid_rows={grid_rows_seen} | "
            f"order_titles={order_title_matches} | order_in_range={order_in_range_matches} | "
            f"order_out_of_range={order_out_of_range} | order_missing_date={rows_without_date} | "
            f"non_order_rows={non_order_rows}"
        )
        if not rows and not items:
            self._log(f"No document grid rows detected after Order search | matter_id={matter_id}")
        elif not items:
            self._log(f"No collectible Order documents found for this matter after search | matter_id={matter_id}")

        return items

    def _wait_for_download_file(self, download_dir: Path, timeout: int = 60) -> Optional[Path]:
        """Wait for a new downloaded file to appear and settle, but do not rename it yet."""
        return self._wait_for_download_file_from_existing(download_dir, timeout=timeout)

    def _download_candidate_files(self, download_dir: Path) -> List[Path]:
        return [
            p
            for p in download_dir.iterdir()
            if p.is_file() and p.suffix.lower() in DOWNLOAD_CAPTURE_SUFFIXES
        ]

    def _wait_for_download_file_from_existing(
        self,
        download_dir: Path,
        timeout: int = 60,
        existing_names: Optional[set[str]] = None,
    ) -> Optional[Path]:
        start = time.time()
        existing = set(existing_names or {p.name for p in self._download_candidate_files(download_dir)})
        stable_name = ""
        stable_size = -1
        stable_passes = 0
        while time.time() - start < timeout:
            # ignore in-progress downloads
            if list(download_dir.glob("*.crdownload")):
                time.sleep(0.5)
                continue
            downloads = [p for p in self._download_candidate_files(download_dir) if p.name not in existing]
            if downloads:
                newest = max(downloads, key=lambda p: p.stat().st_mtime)
                try:
                    current_size = newest.stat().st_size
                except OSError:
                    current_size = -1
                if newest.name == stable_name and current_size == stable_size and current_size > 0:
                    stable_passes += 1
                else:
                    stable_name = newest.name
                    stable_size = current_size
                    stable_passes = 1
                if stable_passes >= 3:
                    return newest
            time.sleep(0.5)
        return None

    def _download_file_with_retry(
        self,
        download_click: Callable[[], None],
        download_dir: Path,
        *,
        matter_id: str,
        title: str,
        target_filename: str,
        attempts: int = DOWNLOAD_RETRY_ATTEMPTS,
        timeout: int = 60,
    ) -> Path:
        baseline = {p.name for p in self._download_candidate_files(download_dir)}
        last_error = "Timed out waiting for download"

        for attempt in range(1, attempts + 1):
            if attempt > 1:
                self._status(f"Retrying download for {matter_id} ({attempt}/{attempts})...")
                self._log(
                    f"Retrying download | matter_id={matter_id} | title={title!r} | "
                    f"target={target_filename} | attempt={attempt} of {attempts}"
                )
            try:
                download_click()
            except Exception as exc:
                last_error = f"Download click failed on attempt {attempt} of {attempts}: {exc}"
                if attempt >= attempts:
                    raise RuntimeError(last_error) from exc
                self._log(
                    f"Download click failed before file capture | matter_id={matter_id} | "
                    f"title={title!r} | attempt={attempt} of {attempts} | "
                    f"error={type(exc).__name__}: {exc}"
                )
                time.sleep(1.0)
                continue

            raw_download = self._wait_for_download_file_from_existing(
                download_dir,
                timeout=timeout,
                existing_names=baseline,
            )
            if raw_download:
                return raw_download

            last_error = f"Timed out waiting for download on attempt {attempt} of {attempts}"
            self._log(
                f"Download attempt timed out | matter_id={matter_id} | title={title!r} | "
                f"target={target_filename} | attempt={attempt} of {attempts}"
            )

        raise RuntimeError(f"Timed out waiting for download after {attempts} attempts")

    def _exclude_non_pdf_download(
        self,
        path: Path,
        *,
        matter_id: str,
        title: str,
        date_created: Optional[date],
        target_filename: str,
    ) -> None:
        excluded_dir = self._excluded_dir()
        target_stem = Path(target_filename).stem
        target_name = f"{target_stem}{path.suffix.lower()}"
        quarantine_path = self._unique_download_target(excluded_dir, target_name)
        move_error = ""

        try:
            path.replace(quarantine_path)
        except Exception as exc:
            move_error = f"{type(exc).__name__}: {exc}"
            quarantine_path = path

        evidence_parts = [f"raw_filename={path.name}", f"downloaded_extension={path.suffix.lower() or '[none]'}"]
        if move_error:
            evidence_parts.append(f"quarantine_move_failed={move_error}")
        evidence = "; ".join(evidence_parts)

        self.excluded_non_order.append(
            {
                "matter_id": matter_id,
                "doc_title": title,
                "date_created": date_created or "",
                "filename": quarantine_path.name,
                "decision": "skip",
                "criteria_result": "exclude",
                "document_check_status": "excluded non-PDF download",
                "order_document_found": "Y",
                "order_pdf_found": "N",
                "order_document_title": title,
                "quality_exclusion_reason": "downloaded non-PDF file",
                "quality_evidence": evidence,
                "quarantine_filename": str(quarantine_path),
                "error": "downloaded non-PDF file",
                "skipped_document_notes": f"Excluded because the downloaded file was not a PDF ({path.suffix.lower() or '[none]'}).",
            }
        )
        self._log(
            f"Excluded non-PDF download | matter_id={matter_id} | title={title!r} | "
            f"raw_filename={path.name} | saved_as={quarantine_path.name} | "
            f"reason=downloaded non-PDF file"
        )

    def _duplicate_window_days(self) -> int:
        return max(0, int(self.config.get("sfview_window_days", 7) or 0))

    @staticmethod
    def _extract_filename_mmddyyyy(text: str) -> Optional[date]:
        match = re.search(r"(^|_)(\d{8})(?:_|\.|$)", (text or "").strip(), flags=re.IGNORECASE)
        if not match:
            return None
        try:
            return datetime.strptime(match.group(2), "%m%d%Y").date()
        except Exception:
            return None

    @staticmethod
    def _tracker_match_label(tracker_match: Dict[str, object], limit: int = 120) -> str:
        matched_label = str(tracker_match.get("filename") or tracker_match.get("row_text") or "").strip()
        if limit > 0 and len(matched_label) > limit:
            matched_label = matched_label[: limit - 3].rstrip() + "..."
        return matched_label

    def _matching_tracker_duplicate_label(
        self,
        tracker_entry: Dict[str, object],
        created_date: date,
        window_days: int,
    ) -> str:
        labels: List[str] = []

        filenames = tracker_entry.get("filenames")
        if isinstance(filenames, list):
            for value in filenames:
                label = str(value or "").strip()
                if label and label not in labels:
                    labels.append(label)

        primary_label = str(tracker_entry.get("filename") or "").strip()
        if primary_label and primary_label not in labels:
            labels.append(primary_label)

        for label in labels:
            tracker_date = self._extract_filename_mmddyyyy(label)
            if tracker_date and abs((tracker_date - created_date).days) <= window_days:
                return label
        return ""

    def _existing_online_document_match(
        self,
        matter_id: str,
        created_date: Optional[date],
        related_case_ids: Optional[List[str]] = None,
    ) -> Optional[Dict[str, str]]:
        if not created_date:
            return None

        case_ids: List[str] = []
        seen_case_ids: set[str] = set()
        for candidate in [matter_id, *(related_case_ids or [])]:
            normalized = str(candidate or "").strip().upper()
            if not normalized or normalized in seen_case_ids:
                continue
            seen_case_ids.add(normalized)
            case_ids.append(normalized)

        if not case_ids:
            return None

        window_days = self._duplicate_window_days()
        for case_id in case_ids:
            tracker_entry = self._tracker_matter_map.get(case_id) or {}
            if not tracker_entry:
                continue
            label = self._matching_tracker_duplicate_label(tracker_entry, created_date, window_days)
            if label:
                return {
                    "case_id": case_id,
                    "source": "LN Doc Tracker",
                    "location": label,
                }

        if self._sfview_checker:
            for case_id in case_ids:
                exists, location = self._sfview_checker.exists_within_window(case_id, created_date, window_days)
                if exists:
                    return {
                        "case_id": case_id,
                        "source": "MOADMN SF-View",
                        "location": str(location or "").strip(),
                    }

        return None

    def _build_target_filename(self, matter_id: str, date_created: date, variant: str = "") -> str:
        matter_token = f"{matter_id}{variant}" if variant else matter_id
        return (
            f"{self.config.get('file_prefix')}_{matter_token}_{_mmddyyyy(date_created)}_"
            f"{self.config.get('file_suffix')}{self.config.get('file_ext')}"
        )

    @staticmethod
    def _variant_letter(index: int) -> str:
        chars: List[str] = []
        value = max(0, int(index))
        while True:
            value, remainder = divmod(value, 26)
            chars.append(chr(ord("a") + remainder))
            if value == 0:
                break
            value -= 1
        return "".join(reversed(chars))

    def _assign_pending_download_variant_names(self) -> None:
        if not self.pending_download_renames:
            return

        groups: Dict[tuple[str, str], List[Dict]] = {}
        for item in self.pending_download_renames:
            matter_id = str(item["matter_id"])
            date_created = item["date_created"]
            date_key = date_created.isoformat() if hasattr(date_created, "isoformat") else str(date_created)
            groups.setdefault((matter_id, date_key), []).append(item)

        for (_, _), items in groups.items():
            if not items:
                continue

            if len(items) == 1:
                items[0]["resolved_target_name"] = self._build_target_filename(
                    str(items[0]["matter_id"]),
                    items[0]["date_created"],
                )
                continue

            cluster_positions: Dict[str, int] = {}
            cluster_count = 0
            for item in items:
                raw_path = Path(item["raw_path"])
                try:
                    cluster_key = PdfQualityGate.content_cluster_key(raw_path)
                except Exception:
                    cluster_key = f"raw::{raw_path.name}"
                item["_content_cluster_key"] = cluster_key
                if cluster_key not in cluster_positions:
                    cluster_positions[cluster_key] = cluster_count
                    cluster_count += 1

            if cluster_count <= 1:
                resolved_name = self._build_target_filename(
                    str(items[0]["matter_id"]),
                    items[0]["date_created"],
                )
                for item in items:
                    item["resolved_target_name"] = resolved_name
                continue

            matter_id = str(items[0]["matter_id"])
            date_created = items[0]["date_created"]
            self._log(
                f"Detected {cluster_count} distinct same-date Order PDFs | "
                f"matter_id={matter_id} | date={date_created:%m/%d/%Y}"
            )
            for item in items:
                variant = self._variant_letter(cluster_positions[item["_content_cluster_key"]])
                resolved_name = self._build_target_filename(
                    str(item["matter_id"]),
                    item["date_created"],
                    variant,
                )
                item["resolved_target_name"] = resolved_name
                self._log(
                    f"Resolved filename variant | matter_id={item['matter_id']} | "
                    f"title={item['doc_title']!r} | filename={resolved_name}"
                )

    @staticmethod
    def _unique_download_target(download_dir: Path, target_name: str) -> Path:
        final = download_dir / target_name
        if final.exists():
            stem = final.stem
            ext = final.suffix
            i = 2
            while True:
                alt = download_dir / f"{stem}_{i}{ext}"
                if not alt.exists():
                    final = alt
                    break
                i += 1
        return final

    def _quality_gate_download_record(
        self,
        path: Path,
        *,
        matter_id: str,
        title: str,
        date_created,
        download_dir: Path,
    ) -> Optional[Dict]:
        if self.pdf_quality_gate is None:
            return {
                "matter_id": matter_id,
                "doc_title": title,
                "date_created": date_created,
                "filename": path.name,
            }

        result: PdfQualityResult = self.pdf_quality_gate.validate(path, matter_id, title)

        if result.accepted:
            existing_online_match = self._existing_online_document_match(
                matter_id,
                date_created if isinstance(date_created, date) else None,
                related_case_ids=result.other_case_ids,
            )
        else:
            existing_online_match = None

        if result.accepted and existing_online_match:
            duplicate_case_id = str(existing_online_match.get("case_id") or "").strip().upper()
            duplicate_source = str(existing_online_match.get("source") or "online database").strip()
            duplicate_location = str(existing_online_match.get("location") or "").strip()
            consolidated = bool(duplicate_case_id and duplicate_case_id != matter_id.upper())
            reason = (
                "consolidated Order already exists online"
                if consolidated
                else "Order already exists online"
            )

            quarantine_dir = self._excluded_dir()
            quarantine_path = self._unique_download_target(quarantine_dir, path.name)
            quarantine_error = ""
            try:
                path.replace(quarantine_path)
            except Exception as exc:
                quarantine_error = f"{type(exc).__name__}: {exc}"
                quarantine_path = path

            evidence_parts = []
            if result.evidence:
                evidence_parts.append(result.evidence)
            if duplicate_case_id:
                evidence_parts.append(f"existing_case_id={duplicate_case_id}")
            if duplicate_source:
                evidence_parts.append(f"existing_source={duplicate_source}")
            if duplicate_location:
                evidence_parts.append(f"existing_location={duplicate_location}")
            if result.other_case_ids:
                evidence_parts.append(f"other_case_ids={', '.join(result.other_case_ids)}")
            if quarantine_error:
                evidence_parts.append(f"quarantine_move_failed={quarantine_error}")
            evidence = "; ".join(evidence_parts)

            self.excluded_non_order.append(
                {
                    "matter_id": matter_id,
                    "doc_title": title,
                    "date_created": date_created or "",
                    "filename": path.name,
                    "decision": "skip",
                    "criteria_result": "duplicate",
                    "duplicate_path": duplicate_location,
                    "document_check_status": (
                        "skipped - consolidated duplicate already exists online"
                        if consolidated
                        else "skipped - duplicate already exists online"
                    ),
                    "order_document_found": "Y",
                    "order_pdf_found": "Y",
                    "order_document_title": title,
                    "quality_exclusion_reason": reason,
                    "quality_evidence": evidence,
                    "quarantine_filename": str(quarantine_path),
                    "error": reason,
                    "skipped_document_notes": f"Skipped because this Order already exists online via "
                    f"{duplicate_source}"
                    + (f" ({duplicate_location})" if duplicate_location else "."),
                }
            )
            self._log(
                f"Excluded finalized PDF because it already exists online | matter_id={matter_id} | "
                f"title={title!r} | existing_case_id={duplicate_case_id or matter_id.upper()} | "
                f"source={duplicate_source} | location={duplicate_location or '[none]'} | "
                f"filename={path.name}"
            )
            return None

        if result.accepted:
            source = f" | text_source={result.text_source}" if result.text_source else ""
            extra = ""
            if result.other_case_ids:
                extra = f" | consolidated_with={', '.join(result.other_case_ids)}"
            self._log(
                f"PDF quality gate passed | matter_id={matter_id} | title={title!r} | "
                f"filename={path.name} | pages={result.page_count} | text_chars={result.text_chars}{source}{extra}"
            )
            return {
                "matter_id": matter_id,
                "doc_title": title,
                "date_created": date_created,
                "filename": path.name,
            }

        quarantine_dir = self._excluded_dir()
        quarantine_path = self._unique_download_target(quarantine_dir, path.name)
        quarantine_error = ""
        try:
            path.replace(quarantine_path)
        except Exception as exc:
            quarantine_error = f"{type(exc).__name__}: {exc}"
            quarantine_path = path

        evidence_parts = []
        if result.evidence:
            evidence_parts.append(result.evidence)
        if result.text_source and result.text_source != "pdf":
            evidence_parts.append(f"text_source={result.text_source}")
        if result.other_case_ids:
            evidence_parts.append(f"other_case_ids={', '.join(result.other_case_ids)}")
        if result.duplicate_of:
            evidence_parts.append(f"duplicate_of={result.duplicate_of}")
        if quarantine_error:
            evidence_parts.append(f"quarantine_move_failed={quarantine_error}")
        evidence = "; ".join(evidence_parts)

        self.excluded_non_order.append(
            {
                "matter_id": matter_id,
                "doc_title": title,
                "date_created": date_created or "",
                "filename": path.name,
                "decision": "skip",
                "criteria_result": "exclude",
                "document_check_status": "excluded by PDF quality gate",
                "order_document_found": "Y",
                "order_pdf_found": "Y",
                "order_document_title": title,
                "quality_exclusion_reason": result.reason,
                "quality_evidence": evidence,
                "quarantine_filename": str(quarantine_path),
                "error": result.reason,
                "skipped_document_notes": f"Excluded by PDF quality gate: {result.reason}"
                + (f" ({evidence})" if evidence else ""),
            }
        )
        self._log(
            f"Excluded finalized PDF by quality gate | matter_id={matter_id} | title={title!r} | "
            f"filename={path.name} | reason={result.reason} | evidence={evidence or '[none]'} | "
            f"quarantine={quarantine_path}"
        )
        return None

    def _finalize_pending_download_renames(self, download_dir: Path) -> None:
        if not self.pending_download_renames:
            return

        self._log(f"Finalizing {len(self.pending_download_renames)} downloaded files.")
        time.sleep(2.0)
        self._assign_pending_download_variant_names()

        remaining: List[Dict] = []
        for item in self.pending_download_renames:
            raw_path = Path(item["raw_path"])
            target_name = str(item.get("resolved_target_name") or item["target_name"])
            matter_id = str(item["matter_id"])
            title = str(item["doc_title"])
            date_created = item["date_created"]

            target_path = self._unique_download_target(download_dir, target_name)
            deadline = time.time() + 45
            renamed = False
            last_error = ""

            while time.time() < deadline:
                if not raw_path.exists():
                    last_error = "Raw downloaded file is no longer available."
                    break
                try:
                    raw_path.rename(target_path)
                    renamed = True
                    break
                except Exception as exc:
                    last_error = str(exc)
                    time.sleep(0.5)

            if renamed:
                record = self._quality_gate_download_record(
                    target_path,
                    matter_id=matter_id,
                    title=title,
                    date_created=date_created,
                    download_dir=download_dir,
                )
                if record:
                    self.downloaded.append(record)
                    self._log(
                        f"Finalized downloaded file | matter_id={matter_id} | title={title!r} | filename={target_path.name}"
                    )
                continue

            self.download_failures.append(
                {
                    "matter_id": matter_id,
                    "doc_title": title,
                    "date_created": date_created or "",
                    "error": f"Could not rename the downloaded file after download completion: {last_error or 'unknown error'}",
                }
            )
            self._log(
                f"Could not finalize downloaded file | matter_id={matter_id} | title={title!r} | error={last_error or 'unknown error'}"
            )
            remaining.append(item)

        self.pending_download_renames = remaining

    def _load_tracker_matter_map(self) -> Dict[str, Dict[str, object]]:
        if not self.driver:
            raise TrackerCheckFailed("The browser session is not available for the LN Doc Tracker review.")

        tracker = LNDocTrackerChecker(
            self.driver,
            self._log,
            self.config.config,
            cancel_check=self.isInterruptionRequested,
            status=self._status,
        )

        original_handle = self.driver.current_window_handle
        handles_before = list(self.driver.window_handles)
        tracker_handle: Optional[str] = None

        try:
            self._log("Opening LN Doc Tracker in a new tab.")
            self.driver.execute_script("window.open('about:blank', '_blank');")
            WebDriverWait(self.driver, 10).until(lambda d: len(d.window_handles) > len(handles_before))
            handles_after = list(self.driver.window_handles)
            new_handles = [handle for handle in handles_after if handle not in handles_before]
            tracker_handle = new_handles[-1] if new_handles else handles_after[-1]
            self.driver.switch_to.window(tracker_handle)
            return tracker.load_tracker_matter_map(self.start_date, self.end_date)
        except TrackerCheckFailed:
            raise
        except Exception as exc:
            raise TrackerCheckFailed(f"The LN Doc Tracker tab could not be opened: {exc}") from exc
        finally:
            try:
                if tracker_handle and tracker_handle in self.driver.window_handles:
                    self.driver.close()
            except Exception:
                pass
            try:
                if original_handle in self.driver.window_handles:
                    self.driver.switch_to.window(original_handle)
            except Exception:
                pass

    def _run_second_sfview_verification(
        self,
        matter_ids: List[str],
        sfview: SFViewChecker,
    ) -> Dict[str, Dict[str, str]]:
        if not matter_ids:
            return {}

        self._status("Cross-checking remaining matters in MOADMN SF-View...")
        self._log(
            f"Starting second duplicate verification pass in MOADMN SF-View for {len(matter_ids)} matters "
            f"that were not confirmed by LN Doc Tracker."
        )

        if not sfview.prepare_index(force_refresh=True):
            self._log("Second duplicate verification pass in MOADMN SF-View could not be completed.")
            return {}

        confirmed: Dict[str, Dict[str, str]] = {}
        for matter_id in matter_ids:
            if self.isInterruptionRequested():
                raise self.UserCancelled("Collection cancelled by user")

            exists, location = sfview.exists_for_matter_id(matter_id)
            if exists:
                confirmed[matter_id] = {
                    "source": "MOADMN SF-View",
                    "location": str(location or "").strip(),
                }

        self._log(
            f"Second MOADMN SF-View verification pass confirmed {len(confirmed)} additional duplicate matters."
        )
        return confirmed

    def _run_aligned_workflow(self, excel: ExcelReporter, sfview: SFViewChecker) -> tuple[bool, str]:
        self._tracker_matter_map = {}
        prefixes: List[str] = []
        scan_start_year = max(0, self.start_date.year - 1)
        for year in range(scan_start_year, self.end_date.year + 1):
            prefix = f"{year % 100:02d}-"
            if prefix not in prefixes:
                prefixes.append(prefix)
        self._log(f"Matter prefix scan order: {', '.join(prefixes)}")

        queued_matter_ids: List[str] = []
        seen_matter_ids: set[str] = set()
        for prefix in prefixes:
            if self.isInterruptionRequested():
                raise self.UserCancelled("Collection cancelled by user")

            self._status(f"Listing matters ({prefix})â€¦")
            self._return_to_matters_list()
            self._set_sort_and_paging()
            if not self._apply_matter_list_prefix_filter(prefix, timeout=20):
                self._log(f"Could not verify prefix filter {prefix}; skipping this prefix.")
                continue

            prefix_ids = self._collect_matter_ids_for_active_prefix(prefix)
            new_ids = [matter_id for matter_id in prefix_ids if matter_id not in seen_matter_ids]
            for matter_id in new_ids:
                seen_matter_ids.add(matter_id)
                queued_matter_ids.append(matter_id)
            self._log(
                f"Queued matter IDs from prefix scan | prefix={prefix} | "
                f"new={len(new_ids)} | total_queue={len(queued_matter_ids)}"
            )

        total_matters = len(queued_matter_ids)
        if not total_matters:
            self._status("Saving the run report...")
            self._normalize_run_output_layout()
            excel.save_workflow_results(
                self.run_dir,
                self.review_rows,
                self.downloaded,
                self.skipped_duplicates,
                self.download_failures,
                self.excluded_non_order,
            )
            return True, "No matter IDs were found for the selected date range."

        if self.config.get("tracker_enabled", True):
            self._status("Reviewing the matter list in LN Doc Tracker...")
            try:
                tracker_matter_map = self._load_tracker_matter_map()
            except TrackerCheckFailed as exc:
                self._log(f"LN Doc Tracker review could not be completed: {exc}")
                return False, str(exc)
        else:
            self._log("LN Doc Tracker review is disabled in the configuration.")
            tracker_matter_map = {}
        self._tracker_matter_map = tracker_matter_map

        self._log(f"Starting LN Doc duplicate review for {total_matters} cataloged matter IDs.")
        review_rows_by_matter_id: Dict[str, Dict] = {}
        first_pass_for_collect_ids: List[str] = []
        for index, matter_id in enumerate(queued_matter_ids, start=1):
            if self.isInterruptionRequested():
                raise self.UserCancelled("Collection cancelled by user")

            self._progress(45 + min(45, int(index * 45 / total_matters)))
            self._status(f"Checking LN Doc for matter {matter_id} ({index} of {total_matters})...")

            tracker_match = tracker_matter_map.get(matter_id.upper()) or {}
            matched_label = self._tracker_match_label(tracker_match)

            exists = bool(tracker_match)
            decision = "do not collect" if exists else "for collect"
            review_row = {
                "matter_id": matter_id,
                "matter_prefix": f"{matter_id[:2]}-",
                "decision": decision,
                "duplicate_path": matched_label,
                "check_stage": "matter_id_catalog",
                "order_document_found": "",
                "order_pdf_found": "",
                "order_pdf_evidence": "",
                "order_document_title": "",
                "document_rows_seen": "",
                "documents_view_state": "",
                "document_check_status": "duplicate - not reviewed" if exists else "pending document review",
            }
            self.review_rows.append(review_row)
            review_rows_by_matter_id[matter_id] = review_row

            if exists:
                self.skipped_duplicates.append(dict(review_row))
            else:
                first_pass_for_collect_ids.append(matter_id)

            if exists and matched_label:
                self._log(f"LN Doc review result for {matter_id}: do not collect ({matched_label}).")
            elif exists:
                self._log(f"LN Doc review result for {matter_id}: do not collect.")
            else:
                self._log(f"LN Doc review result for {matter_id}: for collect.")

        if first_pass_for_collect_ids:
            second_pass_matches = self._run_second_sfview_verification(first_pass_for_collect_ids, sfview)
            for matter_id, duplicate_match in second_pass_matches.items():
                review_row = review_rows_by_matter_id.get(matter_id)
                if not review_row:
                    continue
                duplicate_source = str(duplicate_match.get("source") or "MOADMN SF-View").strip()
                matched_label = str(duplicate_match.get("location") or "").strip()
                review_row.update(
                    {
                        "decision": "do not collect",
                        "criteria_result": "duplicate",
                        "duplicate_path": matched_label,
                        "document_check_status": f"duplicate - confirmed in {duplicate_source}",
                    }
                )
                self.skipped_duplicates.append(dict(review_row))
                if matched_label:
                    self._log(
                        f"Second duplicate-source pass result for {matter_id}: do not collect via "
                        f"{duplicate_source} ({matched_label})."
                    )
                else:
                    self._log(
                        f"Second duplicate-source pass result for {matter_id}: do not collect via "
                        f"{duplicate_source}."
                    )

        for_collect_rows = [row for row in self.review_rows if (row.get("decision") or "").lower() == "for collect"]
        total_for_collect = len(for_collect_rows)
        if total_for_collect:
            self._log(f"Starting Order document review for {total_for_collect} non-duplicate matter IDs.")
            reviewed_count = 0
            soft_reset_interval = max(0, int(self.config.get("document_review_soft_reset_interval", 50) or 0))
            for prefix in prefixes:
                prefix_rows = [row for row in for_collect_rows if str(row.get("matter_id", "")).startswith(prefix)]
                if not prefix_rows:
                    continue

                if not self._prepare_prefix_document_review(prefix, f"Preparing the {prefix} matter list for document review."):
                    self._log(f"Could not verify prefix filter {prefix}; skipping document review for this year.")
                    for row in prefix_rows:
                        row["document_check_status"] = "prefix filter failed"
                    continue

                for prefix_index, row in enumerate(prefix_rows, start=1):
                    if self.isInterruptionRequested():
                        raise self.UserCancelled("Collection cancelled by user")

                    if soft_reset_interval and prefix_index > 1 and (prefix_index - 1) % soft_reset_interval == 0:
                        if not self._prepare_prefix_document_review(
                            prefix,
                            f"Refreshing the {prefix} matter list before continuing document review.",
                        ):
                            self._log(f"Could not recover the {prefix} matter list; skipping the rest of this year.")
                            for pending_row in prefix_rows[prefix_index - 1:]:
                                pending_row["document_check_status"] = "prefix recovery failed"
                            break

                    reviewed_count += 1
                    matter_id = str(row.get("matter_id", "")).strip()
                    self._progress(45 + min(54, int(reviewed_count * 54 / total_for_collect)))
                    self._status(f"Reviewing matter {reviewed_count}/{max(total_for_collect, 1)}: {matter_id}...")

                    try:
                        parent_handle = self.driver.current_window_handle
                    except Exception:
                        parent_handle = None

                    opened_handle = self._open_exact_filtered_matter_in_new_tab(matter_id, prefix=prefix)
                    if not opened_handle:
                        row["document_check_status"] = "matter open failed"
                        continue

                    try:
                        review_result = self._review_order_documents_in_list_view(matter_id)
                        row.update(review_result)
                        self._apply_final_review_decision(row)
                    except Exception as exc:
                        row.update(
                            {
                                "order_document_found": "N",
                                "order_pdf_found": "N",
                                "order_pdf_evidence": "none",
                                "order_document_title": "",
                                "document_rows_seen": "",
                                "documents_view_state": "unknown",
                                "document_check_status": f"document review failed: {exc}",
                            }
                        )
                        self._log(f"Could not review documents for {matter_id}: {exc}")
                    finally:
                        try:
                            if opened_handle in self.driver.window_handles:
                                self.driver.close()
                        except Exception:
                            pass
                        try:
                            if parent_handle and parent_handle in self.driver.window_handles:
                                self.driver.switch_to.window(parent_handle)
                        except Exception:
                            pass

        self._status("Finalizing downloaded file names...")
        self._finalize_pending_download_renames(self._collected_dir())
        self._progress(100)
        self._status("Saving the run report...")
        self._normalize_run_output_layout()
        excel.save_workflow_results(
            self.run_dir,
            self.review_rows,
            self.downloaded,
            self.skipped_duplicates,
            self.download_failures,
            self.excluded_non_order,
        )
        return (
            True,
            f"Run complete. Cataloged {len(queued_matter_ids)} matter IDs, marked "
            f"{len(self.skipped_duplicates)} do not collect, reviewed "
            f"{len(for_collect_rows)} non-duplicate matters for Order documents, and downloaded "
            f"{len(self.downloaded)} files.",
        )

        candidate_rows: List[Dict] = []
        seen_candidates: set[tuple[str, str, str]] = set()
        total_matters = len(queued_matter_ids)
        for index, matter_id in enumerate(queued_matter_ids, start=1):
            if self.isInterruptionRequested():
                raise self.UserCancelled("Collection cancelled by user")

            if total_matters:
                self._progress(min(45, int(index * 45 / total_matters)))
            self._status(f"Reviewing matter {index}/{max(total_matters, 1)}: {matter_id}â€¦")

            opened = False
            try:
                opened = self._open_matter_by_exact_id(matter_id)
                if not opened:
                    continue

                self._search_documents_for_order(matter_id)
                items = self._extract_order_documents(matter_id, record_non_orders=True)
                if not items:
                    self._log(f"No in-range Order documents found during discovery | matter_id={matter_id}")

                for item in items:
                    candidate_key = (
                        item.matter_id,
                        re.sub(r"\s+", " ", item.title).strip().lower(),
                        item.date_created.isoformat(),
                    )
                    if candidate_key in seen_candidates:
                        continue
                    seen_candidates.add(candidate_key)

                    exists, path = sfview.exists_within_window(item.matter_id, item.date_created, window_days)
                    decision = "do not collect" if exists else "for collect"
                    review_row = {
                        "matter_id": item.matter_id,
                        "doc_title": item.title,
                        "date_created": item.date_created,
                        "decision": decision,
                        "window_start": item.date_created - timedelta(days=window_days),
                        "window_end": item.date_created + timedelta(days=window_days),
                        "duplicate_path": str(path) if path else "",
                    }
                    self.review_rows.append(review_row)
                    self._log(
                        f"Discovery decision | matter_id={item.matter_id} | title={item.title!r} | "
                        f"date_created={item.date_created:%m/%d/%Y} | decision={decision}"
                    )

                    if exists:
                        self.skipped_duplicates.append(dict(review_row))
                    else:
                        candidate_rows.append(dict(review_row))
            finally:
                if opened:
                    self._return_from_matter_overview()

        grouped_candidates: Dict[str, List[Dict]] = {}
        for row in candidate_rows:
            grouped_candidates.setdefault(row["matter_id"], []).append(row)

        total_download_groups = len(grouped_candidates)
        for group_index, (matter_id, rows) in enumerate(grouped_candidates.items(), start=1):
            if self.isInterruptionRequested():
                raise self.UserCancelled("Collection cancelled by user")

            if total_download_groups:
                self._progress(45 + min(45, int(group_index * 45 / total_download_groups)))
            self._status(f"Downloading matter {group_index}/{max(total_download_groups, 1)}: {matter_id}â€¦")

            opened = False
            try:
                opened = self._open_matter_by_exact_id(matter_id)
                if not opened:
                    for row in rows:
                        self.download_failures.append(
                            {
                                "matter_id": row["matter_id"],
                                "doc_title": row["doc_title"],
                                "date_created": row["date_created"],
                                "error": "Could not reopen matter from exact filter for download",
                            }
                        )
                    continue

                self._search_documents_for_order(matter_id)
                items = self._extract_order_documents(matter_id, record_non_orders=False)
                for row in rows:
                    match = self._match_document_item(items, row["doc_title"], row["date_created"])
                    if not match or not match.download_click:
                        self.download_failures.append(
                            {
                                "matter_id": row["matter_id"],
                                "doc_title": row["doc_title"],
                                "date_created": row["date_created"],
                                "error": "Matching Order row was not found during download pass",
                            }
                        )
                        continue

                    target_filename = self._build_target_filename(str(row["matter_id"]), row["date_created"])
                    try:
                        match.download_click()
                        saved = self._wait_for_download_and_rename(self._collected_dir(), target_filename)
                        if not saved:
                            raise RuntimeError("Timed out waiting for download")
                        record = self._quality_gate_download_record(
                            saved,
                            matter_id=str(row["matter_id"]),
                            title=str(row["doc_title"]),
                            date_created=row["date_created"],
                            download_dir=self._collected_dir(),
                        )
                        if record:
                            self.downloaded.append(record)
                    except Exception as e:
                        self._log(
                            f"Download failed for {row['matter_id']} ({target_filename}): {type(e).__name__}: {e}"
                        )
                        self.download_failures.append(
                            {
                                "matter_id": row["matter_id"],
                                "doc_title": row["doc_title"],
                                "date_created": row["date_created"],
                                "error": str(e),
                            }
                        )
            finally:
                if opened:
                    self._return_from_matter_overview()

        if not self.review_rows and not self.downloaded and not self.skipped_duplicates and not self.download_failures:
            try:
                latest = sfview.latest_document()
                if latest:
                    self._log(
                        "No candidate order documents found on portal. "
                        f"Latest SF-View file observed: {latest.path.name} "
                        f"(date={latest.doc_date.strftime('%m/%d/%Y') if latest.doc_date else 'unknown'})"
                    )
            except Exception as e:
                self._log(f"Could not query latest SF-View document: {e}")
            self._normalize_run_output_layout()
            self._sync_untracked_excluded_files()
            excel.save_workflow_results(
                self.run_dir,
                self.review_rows,
                self.downloaded,
                self.skipped_duplicates,
                self.download_failures,
                self.excluded_non_order,
            )
            return True, "No qualifying Order documents were found in the selected date range."

        self._progress(100)
        self._normalize_run_output_layout()
        self._sync_untracked_excluded_files()
        excel.save_workflow_results(
            self.run_dir,
            self.review_rows,
            self.downloaded,
            self.skipped_duplicates,
            self.download_failures,
            self.excluded_non_order,
        )
        return (
            True,
            f"Run complete. Reviewed {len(self.review_rows)} items, marked {len(candidate_rows)} for collect, "
            f"marked {len(self.skipped_duplicates)} do not collect, and downloaded {len(self.downloaded)} files.",
        )

    def run(self) -> None:
        ok = False
        msg = "Collection failed"
        run_dir_str = ""
        excel: Optional[ExcelReporter] = None
        try:
            self.run_dir = self._make_run_dir()
            run_dir_str = str(self.run_dir)
            # Initialize logger now that we know the run directory
            self.logger = Logger(log_dir=str(self.run_dir))
            excel = ExcelReporter(self._log)
            sfview = SFViewChecker(self._log, self.config.config)
            self._sfview_checker = sfview

            self._log(f"Run folder: {self.run_dir}")
            self._log(
                f"MOADMN run started | date range {self.start_date:%m/%d/%Y} - {self.end_date:%m/%d/%Y} | "
                f"headless={self.headless}"
            )
            self._ensure_output_dirs()
            self.driver = self._launch_driver(self._collected_dir())
            sfview.attach_driver(self.driver)

            self._login_and_open_matters()
            self._set_sort_and_paging()
            ok, msg = self._run_aligned_workflow(excel, sfview)
            return

            # Determine which year prefixes to scan based on selected date range (e.g., 25-, 26-)
            prefixes = sorted({f"{self.start_date.year % 100:02d}-", f"{self.end_date.year % 100:02d}-"})

            processed_count = 0
            page_number = 0

            for pi, prefix in enumerate(prefixes):
                if self.isInterruptionRequested():
                    raise self.UserCancelled("Collection cancelled by user")
                self._status(f"Scanning matters ({prefix})…")
                # Reset to Matters list so pagination starts from the first page for each prefix.
                if pi > 0:
                    self._return_to_matters_list()
                    self._set_sort_and_paging()
                if not self._apply_matter_list_prefix_filter(prefix, timeout=20):
                    self._log(f"Could not verify prefix filter {prefix}; stopping this filter scan.")
                    continue
                # Iterate pages until next is disabled; CLICK each matter link instead of deep-linking.
                page_guard = 0
                prefix_page_number = 0
                seen_matter_hrefs: set[str] = set()
                prev_page_signature = ""
                repeated_page_signatures = 0
                while True:
                    if self.isInterruptionRequested():
                        raise self.UserCancelled("Collection cancelled by user")
                    page_guard += 1
                    if page_guard > 200:
                        break
                    prefix_page_number += 1
                    page_number += 1
                    if prefix_page_number == 1:
                        self._log(f"Using current filtered matter list as page 1 baseline for {prefix}")
                        if not self._wait_for_matter_list_ready(timeout=10, active_prefix=prefix):
                            if not self._restore_matters_page(prefix, prefix_page_number):
                                self._log("Matter list could not be restored for this page; stopping pagination for this filter.")
                                break
                    else:
                        if not self._restore_matters_page(prefix, prefix_page_number):
                            self._log("Matter list could not be restored for this page; stopping pagination for this filter.")
                            break
                    self._log(f"--- Matter list page {page_number} (filter {prefix}) ---")

                    # Work from a stable href snapshot instead of a shifting element index.
                    href_scan_started = time.time()
                    page_hrefs = self._current_matter_hrefs(active_prefix=prefix)
                    self._log(
                        f"Matter href snapshot collected | page={page_number} | filter={prefix} | "
                        f"hrefs={len(page_hrefs)} | elapsed={time.time() - href_scan_started:.1f}s"
                    )
                    if not page_hrefs:
                        self._log("No matter links found on this page; stopping pagination for this filter.")
                        break
                    links = page_hrefs
                    page_signature = "|".join(page_hrefs[:5])
                    if page_signature and page_signature == prev_page_signature:
                        repeated_page_signatures += 1
                    else:
                        repeated_page_signatures = 0
                        prev_page_signature = page_signature
                    if repeated_page_signatures >= 2:
                        self._log("Detected repeated page content; stopping pagination safety guard.")
                        break

                    # Re-scan the live page each time so list reshuffles do not stop collection.
                    while True:
                        if self.isInterruptionRequested():
                            raise self.UserCancelled("Collection cancelled by user")
                        if not self._wait_for_matter_list_ready(timeout=8, active_prefix=prefix):
                            if not self._restore_matters_page(prefix, prefix_page_number):
                                self._log("Matter list unavailable for this page; stopping current page processing.")
                                break

                        live_hrefs = self._current_matter_hrefs(active_prefix=prefix)
                        unseen_hrefs = [candidate for candidate in live_hrefs if candidate not in seen_matter_hrefs]
                        if not unseen_hrefs:
                            self._log(f"No unseen matter links remaining on page {page_number}; advancing pagination.")
                            break

                        href = unseen_hrefs[0]
                        processed_count += 1
                        index_on_page = max(1, len(live_hrefs) - len(unseen_hrefs) + 1)
                        # Best-effort progress: this phase is the bulk of the run
                        self._progress(min(79, 1 + processed_count % 79))
                        self._status(f"Page {page_number} - Matter {index_on_page}/{len(links)} (processed {processed_count})…")
                        try:
                            click_ok = False
                            for attempt in range(2):
                                el = self._find_matter_link_by_href(href, active_prefix=prefix)
                                if el is None:
                                    self._log(f"Matter href no longer available on the live page; retrying from refreshed state: {href}")
                                    break
                                try:
                                    el.click()
                                    click_ok = True
                                    break
                                except StaleElementReferenceException:
                                    self._log(
                                        f"Stale link element on click (page {page_number}, slot {index_on_page}), retry {attempt + 1}/2"
                                    )
                                    time.sleep(0.3)
                            if not click_ok:
                                continue
                            if not self._is_valid_matter_href(href):
                                self._log(f"Skipping non-matter link candidate: {href or '[no href]'}")
                                continue
                            self._log(f"Clicking matter link (page {page_number}, slot {index_on_page}): {href or '[no href]'}")
                            seen_matter_hrefs.add(href)

                            # Wait for overview to render
                            if not self._wait_for_matter_overview_ready(href or self.driver.current_url, timeout=20):
                                self._log("Matter overview did not render after click; returning to list and continuing.")
                                # Try to get back to list and continue
                                try:
                                    self.driver.back()
                                except Exception:
                                    self._return_to_matters_list()
                                self._wait_for_matter_list_ready(timeout=20, active_prefix=prefix)
                                continue

                            matter_id = self._extract_matter_id(preferred_prefix=prefix) or ""
                            if not matter_id:
                                self._log("Could not extract Matter ID on this overview; returning to list.")
                                try:
                                    self.driver.back()
                                except Exception:
                                    self._return_to_matters_list()
                                self._wait_for_matter_list_ready(timeout=20, active_prefix=prefix)
                                continue
                            if not matter_id.startswith(prefix):
                                self._log(
                                    f"Extracted Matter ID does not match active filter; skipping suspect page | "
                                    f"expected_prefix={prefix} | actual={matter_id}"
                                )
                                try:
                                    self.driver.back()
                                except Exception:
                                    self._return_to_matters_list()
                                self._wait_for_matter_list_ready(timeout=20, active_prefix=prefix)
                                continue

                            self._log(f"Matter ID extracted: {matter_id}")

                            self._search_documents_for_order(matter_id)
                            items = self._extract_order_documents(matter_id)
                            if items:
                                window_days = int(self.config.get("sfview_window_days", 365))
                                for it in items:
                                    self._log(
                                        f"Candidate document found | matter_id={it.matter_id} | "
                                        f"title={it.title!r} | date_created={it.date_created:%m/%d/%Y}"
                                    )
                                    exists, path = sfview.exists_within_window(it.matter_id, it.date_created, window_days)
                                    if exists:
                                        self.skipped_duplicates.append(
                                            {
                                                "matter_id": it.matter_id,
                                                "doc_title": it.title,
                                                "date_created": it.date_created,
                                                "duplicate_path": str(path) if path else "",
                                            }
                                        )
                                        continue

                                    target_filename = self._build_target_filename(it.matter_id, it.date_created)
                                    try:
                                        if not it.download_click:
                                            raise RuntimeError("Download action not located for this row (UI selector needs update)")
                                        it.download_click()
                                        saved = self._wait_for_download_and_rename(self._collected_dir(), target_filename)
                                        if not saved:
                                            raise RuntimeError("Timed out waiting for download")
                                        record = self._quality_gate_download_record(
                                            saved,
                                            matter_id=it.matter_id,
                                            title=it.title,
                                            date_created=it.date_created,
                                            download_dir=self._collected_dir(),
                                        )
                                        if record:
                                            self.downloaded.append(record)
                                    except Exception as e:
                                        self._log(f"Download failed for {it.matter_id} ({target_filename}): {e}")
                                        self.excluded_non_order.append(
                                            {
                                                "matter_id": it.matter_id,
                                                "doc_title": it.title,
                                                "date_created": it.date_created,
                                                "error": str(e),
                                            }
                                        )

                            # Try browser history first, then deterministically restore the list page if the SPA resets.
                            returned_to_list = False
                            try:
                                self.driver.back()
                                returned_to_list = self._wait_for_matter_list_ready(timeout=20, active_prefix=prefix)
                            except Exception:
                                returned_to_list = False
                            if not returned_to_list:
                                if not self._restore_matters_page(prefix, prefix_page_number):
                                    self._log("Could not restore Matters list after processing a matter.")
                                    break
                        except Exception as e:
                            self._log(
                                f"Error processing matter click flow | exc={type(e).__name__}: {e!r} | url={self.driver.current_url!r}"
                            )
                            self._log(traceback.format_exc())
                            if "invalid session id" in str(e).lower():
                                raise
                            # Attempt to recover back to the current list page before continuing.
                            try:
                                self.driver.back()
                            except Exception:
                                pass
                            if not self._restore_matters_page(prefix, prefix_page_number):
                                self._log("Could not restore Matters list after an error; stopping current page processing.")
                                break

                    if not self._restore_matters_page(prefix, prefix_page_number):
                        self._log("Could not restore current page before checking pagination; stopping pagination for this filter.")
                        break
                    current_signature = self._page_signature(active_prefix=prefix)
                    if not self._next_page():
                        break
                    if not self._wait_for_page_signature_change(current_signature, timeout=10, active_prefix=prefix):
                        self._log("Next page did not produce distinct content; stopping pagination for this filter.")
                        break

            if not self.downloaded and not self.skipped_duplicates:
                try:
                    latest = sfview.latest_document()
                    if latest:
                        self._log(
                            "No candidate order documents found on portal. "
                            f"Latest SF-View file observed: {latest.path.name} "
                            f"(date={latest.doc_date.strftime('%m/%d/%Y') if latest.doc_date else 'unknown'})"
                        )
                except Exception as e:
                    self._log(f"Could not query latest SF-View document: {e}")
                ok = True
                msg = "No new Order documents found in the selected date range"
                self._normalize_run_output_layout()
                excel.save_results(self.run_dir, [], [], self.excluded_non_order)
                return

            self._progress(100)
            self._normalize_run_output_layout()
            excel.save_results(self.run_dir, self.downloaded, self.skipped_duplicates, self.excluded_non_order)
            ok = True
            msg = f"Completed. Downloaded: {len(self.downloaded)}; Skipped duplicates: {len(self.skipped_duplicates)}"
        except self.UserCancelled as e:
            ok = True
            msg = str(e)
            self._log(msg)
            try:
                if self.run_dir and excel is not None:
                    self._normalize_run_output_layout()
                    excel.save_workflow_results(
                        self.run_dir,
                        self.review_rows,
                        self.downloaded,
                        self.skipped_duplicates,
                        self.download_failures,
                        self.excluded_non_order,
                    )
            except Exception as report_err:
                self._log(f"Could not save partial report after cancellation: {report_err}")
        except self.SignInIssue as e:
            ok = False
            msg = str(e)
            self._log(msg)
        except Exception as e:
            ok = False
            msg = f"Collection failed. {self._describe_exception(e)}"
            self._log(msg)
            try:
                self._log(traceback.format_exc())
            except Exception:
                pass
        finally:
            try:
                if self.driver:
                    self.driver.quit()
            except Exception:
                pass
            try:
                self._normalize_run_output_layout()
            except Exception:
                pass
            self.finished_signal.emit(ok, msg, run_dir_str)


