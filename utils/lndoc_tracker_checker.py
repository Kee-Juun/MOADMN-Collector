"""
LN Doc Tracker duplicate checker for MOADMN.

Uses the same tracker-report pattern as the stronger duplicate checkers in
other collector projects, but extracts MOADMN matter IDs like ``26-0001``.
"""

from __future__ import annotations

import re
import time
from datetime import date, timedelta
from typing import Callable, Dict, List, Optional

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


class TrackerCheckFailed(RuntimeError):
    """Raised when the LN Doc Tracker review cannot complete safely."""


class LNDocTrackerChecker:
    def __init__(
        self,
        driver,
        log: Callable[[str], None],
        config: Dict,
        cancel_check: Optional[Callable[[], bool]] = None,
        status: Optional[Callable[[str], None]] = None,
    ):
        self.driver = driver
        self.log = log
        self.config = config
        self._cancel_check = cancel_check
        self._status_callback = status
        self._tracker_rows_snapshot: Optional[List[List[str]]] = None

    def _get(self, key: str, default=None):
        return self.config.get(key, default)

    def _is_cancelled(self) -> bool:
        return bool(self._cancel_check and self._cancel_check())

    def _sleep(self, seconds: float) -> None:
        end_time = time.time() + max(0.0, seconds)
        while time.time() < end_time:
            if self._is_cancelled():
                raise TrackerCheckFailed("The LN Doc Tracker review was cancelled.")
            time.sleep(0.1)

    def _wrap_condition(self, condition):
        def wrapped(driver):
            if self._is_cancelled():
                raise TimeoutException("Cancelled")
            return condition(driver)

        return wrapped

    def _wait_until(self, wait: WebDriverWait, condition):
        return wait.until(self._wrap_condition(condition))

    def _log(self, message: str) -> None:
        try:
            self.log(message)
        except Exception:
            pass

    def _status(self, message: str) -> None:
        if not self._status_callback:
            return
        try:
            self._status_callback(message)
        except Exception:
            pass

    def _compile_tracker_filename_regex(self) -> re.Pattern[str]:
        configured = (self._get("tracker_filename_regex", "") or "").strip()
        if configured:
            # Allow filenames like 25-1401a_03202026 in addition to the base
            # 25-1401_03202026 form so tracker rows created from same-date
            # variant files still resolve back to the base matter ID.
            if "[a-z]*" not in configured.lower():
                configured = configured.replace(r"(\d{2}-\d{4})_", r"(\d{2}-\d{4})[a-z]*_")
            return re.compile(configured, re.IGNORECASE)

        file_prefix = re.escape((self._get("file_prefix", "LDC_SMD") or "LDC_SMD").strip())
        file_suffix = re.escape((self._get("file_suffix", "E2E") or "E2E").strip())
        return re.compile(
            rf"{file_prefix}_(\d{{2}}-\d{{4}})[a-z]*_\d{{8}}_{file_suffix}(?:\.pdf)?",
            re.IGNORECASE,
        )

    def _get_retry_delay(self, attempt: int) -> int:
        base_delay = int(self._get("tracker_retry_backoff_seconds", 5))
        if attempt <= 1:
            return 0
        return max(0, base_delay * (attempt - 1))

    def _reset_tracker_state(self) -> None:
        try:
            self.driver.execute_script("window.stop();")
        except Exception:
            pass
        try:
            self.driver.get("about:blank")
        except Exception:
            pass

    def navigate_to_tracker(self) -> bool:
        url = (self._get("tracker_url", "") or "").strip()
        if not url:
            raise TrackerCheckFailed("The LN Doc Tracker URL is not configured.")

        try:
            self._tracker_rows_snapshot = None
            self._status("Opening LN Doc Tracker...")
            self._log("Opening LN Doc Tracker...")
            self.driver.get(url)
            self._sleep(1.5)
            return True
        except Exception as exc:
            self._log(f"Could not open LN Doc Tracker: {exc}")
            return False

    def setup_query(self) -> bool:
        if not self._get("tracker_use_query_wizard", True):
            return True

        try:
            self._status("Preparing the LN Doc Tracker search...")
            self._log("Preparing the LN Doc Tracker search...")

            first_button = self._wait_until(
                WebDriverWait(self.driver, 10, poll_frequency=0.2),
                EC.element_to_be_clickable(
                    (By.XPATH, self._get("tracker_first_query_xpath", "/html/body/table/tbody/tr/td[2]/table[3]/tbody/tr[1]/td/a/img"))
                ),
            )
            first_button.click()
            self._sleep(1.0)

            second_button = self._wait_until(
                WebDriverWait(self.driver, 10, poll_frequency=0.2),
                EC.element_to_be_clickable(
                    (By.XPATH, self._get("tracker_second_query_xpath", "/html/body/table/tbody/tr/td[2]/center/a/img"))
                ),
            )
            second_button.click()
            self._sleep(1.0)

            open_existing = self._wait_until(
                WebDriverWait(self.driver, 10, poll_frequency=0.2),
                EC.element_to_be_clickable(
                    (By.XPATH, self._get("tracker_open_existing_xpath", '//*[@id="startQuery"]/table/tbody/tr/td[2]/table[2]/tbody/tr[3]/td/input'))
                ),
            )
            open_existing.click()
            self._sleep(0.5)

            next_one = self._wait_until(
                WebDriverWait(self.driver, 10, poll_frequency=0.2),
                EC.element_to_be_clickable(
                    (By.XPATH, self._get("tracker_next1_xpath", '//*[@id="startQuery"]/table/tbody/tr/td[2]/table[2]/tbody/tr[4]/td/input'))
                ),
            )
            next_one.click()
            self._sleep(1.0)

            query_option = self._wait_until(
                WebDriverWait(self.driver, 10, poll_frequency=0.2),
                EC.element_to_be_clickable(
                    (By.XPATH, self._get("tracker_query_option_xpath", '//*[@id="select1"]/option[19]'))
                ),
            )
            query_option.click()
            self._sleep(0.5)

            next_two = self._wait_until(
                WebDriverWait(self.driver, 10, poll_frequency=0.2),
                EC.element_to_be_clickable(
                    (By.XPATH, self._get("tracker_next2_xpath", "/html/body/form/table/tbody/tr/td[2]/table[2]/tbody/tr[3]/td/input"))
                ),
            )
            next_two.click()
            self._sleep(1.0)

            next_three = self._wait_until(
                WebDriverWait(self.driver, 10, poll_frequency=0.2),
                EC.element_to_be_clickable(
                    (By.XPATH, self._get("tracker_next3_xpath", '//input[@type="submit" and @value="  Next  "]'))
                ),
            )
            next_three.click()
            self._sleep(1.0)

            search_button = self._wait_until(
                WebDriverWait(self.driver, 10, poll_frequency=0.2),
                EC.element_to_be_clickable(
                    (By.XPATH, self._get("tracker_search_xpath", "/html/body/form/table/tbody/tr/td[2]/table[2]/tbody/tr[9]/td/input[3]"))
                ),
            )
            search_button.click()
            self._sleep(1.0)
            return True
        except Exception as exc:
            self._log(f"Could not prepare the LN Doc Tracker search: {exc}")
            return False

    def enter_search_criteria(self, start_date: date, end_date: date) -> bool:
        try:
            court_code = (self._get("tracker_court_code", "") or "").strip()
            self._status("Entering the LN Doc Tracker court code and date range...")
            if court_code:
                self._log(f"Applying tracker court filter: {court_code}")
                court_input = self._wait_until(
                    WebDriverWait(self.driver, 10, poll_frequency=0.2),
                    EC.presence_of_element_located(
                        (By.XPATH, self._get("tracker_court_xpath", "/html/body/form/table/tbody/tr/td[2]/table[2]/tbody/tr[27]/td[4]/input"))
                    ),
                )
                court_input.clear()
                court_input.send_keys(court_code)
            else:
                self._log("No tracker court filter is configured. Searching by date range only.")

            days_before = int(self._get("tracker_date_window_days_before", 7))
            days_after = int(self._get("tracker_date_window_days_after", 7))
            tracker_start = start_date - timedelta(days=max(0, days_before))
            tracker_end = end_date + timedelta(days=max(0, days_after))
            date_range = f"{tracker_start:%m/%d/%Y}-{tracker_end:%m/%d/%Y}"
            self._log(f"Using tracker date range {tracker_start:%m/%d/%Y} to {tracker_end:%m/%d/%Y}.")

            date_input = self._wait_until(
                WebDriverWait(self.driver, 10, poll_frequency=0.2),
                EC.presence_of_element_located(
                    (By.XPATH, self._get("tracker_date_xpath", "/html/body/form/table/tbody/tr/td[2]/table[2]/tbody/tr[33]/td[4]/input"))
                ),
            )
            date_input.clear()
            date_input.send_keys(date_range)

            finish_button = self._wait_until(
                WebDriverWait(self.driver, 10, poll_frequency=0.2),
                EC.element_to_be_clickable(
                    (By.XPATH, self._get("tracker_finish_xpath", "/html/body/form/table/tbody/tr/td[2]/table[2]/tbody/tr[72]/td/input[5]"))
                ),
            )
            finish_button.click()
            self._tracker_rows_snapshot = None
            self._sleep(2.0)
            return True
        except Exception as exc:
            self._log(f"Could not enter the LN Doc Tracker search criteria: {exc}")
            return False

    def wait_for_report(self, timeout: int = 60) -> bool:
        self._status("Waiting for LN Doc Tracker results...")
        self._log("Waiting for the LN Doc Tracker report...")
        ready_xpath = self._get("tracker_report_ready_xpath", "/html/body/form/table[2]/tbody/tr[1]/td[1]")

        def report_ready(driver):
            try:
                if "END OF REPORT" in (driver.page_source or ""):
                    return True
            except Exception:
                pass
            try:
                return driver.find_element(By.XPATH, ready_xpath) is not None
            except Exception:
                return False

        try:
            self._wait_until(WebDriverWait(self.driver, timeout, poll_frequency=0.2), report_ready)
            return True
        except TimeoutException:
            self._log(f"The LN Doc Tracker report did not load within {timeout} seconds.")
            return False

    def _extract_tracker_rows_snapshot(self, force_refresh: bool = False) -> List[List[str]]:
        if self._tracker_rows_snapshot is not None and not force_refresh:
            return self._tracker_rows_snapshot

        rows_xpath = self._get("tracker_rows_xpath", "/html/body/form/table[2]/tbody/tr")

        try:
            row_snapshots = self.driver.execute_script(
                """
                const xpath = arguments[0];

                function rowsFromXPath(path) {
                    const result = document.evaluate(
                        path,
                        document,
                        null,
                        XPathResult.ORDERED_NODE_SNAPSHOT_TYPE,
                        null
                    );
                    const rows = [];
                    for (let i = 0; i < result.snapshotLength; i += 1) {
                        rows.push(result.snapshotItem(i));
                    }
                    return rows;
                }

                let rows = rowsFromXPath(xpath);
                if (!rows.length && xpath !== '//table[2]//tr') {
                    rows = rowsFromXPath('//table[2]//tr');
                }

                return rows.map((row) =>
                    Array.from(row.querySelectorAll('td')).map(
                        (cell) => (cell.textContent || '').trim()
                    )
                );
                """,
                rows_xpath,
            )

            if isinstance(row_snapshots, list):
                self._tracker_rows_snapshot = row_snapshots
                return row_snapshots
        except Exception as exc:
            self._log(f"Could not snapshot the tracker table with JavaScript. Falling back to a slower read: {exc}")

        rows = self.driver.find_elements(By.XPATH, rows_xpath)
        if not rows:
            rows = self.driver.find_elements(By.XPATH, "//table[2]//tr")

        row_snapshots: List[List[str]] = []
        for row in rows:
            try:
                cells = row.find_elements(By.TAG_NAME, "td")
                row_snapshots.append([cell.text.strip() for cell in cells])
            except Exception:
                row_snapshots.append([])

        self._tracker_rows_snapshot = row_snapshots
        return row_snapshots

    def extract_tracker_matter_map(self, row_snapshots: Optional[List[List[str]]] = None) -> Dict[str, Dict[str, object]]:
        matter_map: Dict[str, Dict[str, object]] = {}
        filename_re = self._compile_tracker_filename_regex()
        matter_re = re.compile(
            self._get("tracker_matter_id_regex", r"(?<!\d)\d{2}-\d{4}(?!\d)"),
            re.IGNORECASE,
        )
        file_column_index = int(self._get("tracker_file_column_index", 3))
        docket_column_index = int(self._get("tracker_docket_column_index", 8))
        source_counts = {"filename": 0, "docket": 0, "row": 0}

        rows = row_snapshots or self._extract_tracker_rows_snapshot()
        for cells in rows[1:]:
            if not cells:
                continue
            non_empty = [(cell or "").strip() for cell in cells if (cell or "").strip()]
            if not non_empty:
                continue

            row_text = " | ".join(non_empty)
            filename = ""
            if file_column_index > 0 and len(cells) >= file_column_index:
                filename = (cells[file_column_index - 1] or "").strip()
            docket_value = ""
            if docket_column_index > 0 and len(cells) >= docket_column_index:
                docket_value = (cells[docket_column_index - 1] or "").strip()

            candidates: List[tuple[str, str]] = []

            if filename:
                for match in filename_re.findall(filename):
                    if isinstance(match, tuple):
                        matter_id = (match[0] or "").strip()
                    else:
                        matter_id = (match or "").strip()
                    if matter_id:
                        candidates.append(("filename", matter_id))

            if not candidates and docket_value:
                for match in matter_re.findall(docket_value):
                    if match:
                        candidates.append(("docket", match))

            if not candidates:
                for match in matter_re.findall(row_text):
                    if match:
                        candidates.append(("row", match))

            for source, raw_matter_id in candidates:
                matter_id = raw_matter_id.upper()
                if matter_id not in matter_map:
                    matter_map[matter_id] = {
                        "filename": filename,
                        "row_text": row_text,
                        "source": source,
                        "docket_value": docket_value,
                        "filenames": [],
                        "row_texts": [],
                    }
                    source_counts[source] += 1
                entry = matter_map[matter_id]
                filenames = entry.setdefault("filenames", [])
                row_texts = entry.setdefault("row_texts", [])
                if isinstance(filenames, list) and filename and filename not in filenames:
                    filenames.append(filename)
                if isinstance(row_texts, list) and row_text and row_text not in row_texts:
                    row_texts.append(row_text)

        self._log(
            "LN Doc Tracker extraction summary: "
            f"{len(matter_map)} IDs "
            f"({source_counts['filename']} from filenames, "
            f"{source_counts['docket']} from docket values, "
            f"{source_counts['row']} from row text)."
        )

        return matter_map

    def load_tracker_matter_map(self, start_date: date, end_date: date) -> Dict[str, Dict[str, object]]:
        attempts = max(1, int(self._get("tracker_retry_attempts", 2)))
        failure_message = "The LN Doc Tracker review could not be completed."

        for attempt in range(1, attempts + 1):
            if self._is_cancelled():
                raise TrackerCheckFailed("The LN Doc Tracker review was cancelled.")

            if attempt > 1:
                delay = self._get_retry_delay(attempt)
                self._log(f"Retrying the LN Doc Tracker review ({attempt} of {attempts}) after {delay} seconds.")
                if delay:
                    self._sleep(delay)
                self._reset_tracker_state()

            self._status(f"Loading LN Doc Tracker (attempt {attempt} of {attempts})...")
            self._log(f"Starting LN Doc Tracker review attempt {attempt} of {attempts}.")

            if not self.navigate_to_tracker():
                failure_message = "The LN Doc Tracker could not be opened."
                continue
            if not self.setup_query():
                failure_message = "The LN Doc Tracker search could not be prepared."
                continue
            if not self.enter_search_criteria(start_date, end_date):
                failure_message = "The LN Doc Tracker search criteria could not be entered."
                continue
            if not self.wait_for_report(timeout=int(self._get("tracker_report_timeout_seconds", 60))):
                failure_message = "The LN Doc Tracker report did not load."
                continue

            row_snapshots = self._extract_tracker_rows_snapshot(force_refresh=True)
            matter_map = self.extract_tracker_matter_map(row_snapshots)
            self._log(f"LN Doc Tracker report loaded. Found {len(matter_map)} tracked matter IDs.")
            return matter_map

        if self._get("tracker_fail_open", False):
            self._log(f"{failure_message} Continuing without duplicate flags because tracker_fail_open is enabled.")
            return {}

        raise TrackerCheckFailed(failure_message)
