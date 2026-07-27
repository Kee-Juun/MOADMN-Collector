"""
Configuration management for MOADMN Document Collector.

Modeled after `WVPUC0 Project/config/config.py`, but with MOADMN defaults.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict


class ConfigManager:
    """Loads and saves configuration from a JSON file, with PyInstaller support."""

    def __init__(self, config_file: str = "config.json"):
        self.config_file = config_file
        self.last_save_error = ""
        self.config: Dict[str, Any] = self.load_config()

    def _config_path(self) -> Path:
        return Path(__file__).resolve().parent.parent / self.config_file

    def _resource_path(self, rel: str) -> Path:
        base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
        return base / rel

    def load_config(self) -> Dict[str, Any]:
        local_path = self._config_path()
        if local_path.exists():
            return json.loads(local_path.read_text(encoding="utf-8"))

        packaged = self._resource_path(self.config_file)
        if packaged.exists():
            data = json.loads(packaged.read_text(encoding="utf-8"))
            self.save_config(data)
            return data

        default_config: Dict[str, Any] = {
            # Keep headless on by default to avoid popping a browser every run.
            "headless": True,
            # Under user home. App will create `Downloads/MOADMN Collections/MOADMN_...`
            "download_path": "Downloads/MOADMN Collections",
            # Point this to the MOADMN SF-View directory for duplicate checks.
            "sf_view_url": r"file://fntd5bdata09/pclprod/sf-view/PROD/team10/moadmn/",
            # LN Doc Tracker settings for online duplicate review.
            "tracker_enabled": True,
            "tracker_url": "https://tcfabprod.lexisnexis.com/ws/TrackerReport/",
            "tracker_court_code": "STMOADMN",
            "tracker_fail_open": False,
            "tracker_retry_attempts": 3,
            "tracker_retry_backoff_seconds": 5,
            "tracker_use_query_wizard": True,
            "tracker_report_timeout_seconds": 60,
            "tracker_date_window_days_before": 365,
            "tracker_date_window_days_after": 365,
            "tracker_matter_id_regex": r"(?<!\d)\d{2}-\d{4}(?!\d)",
            "tracker_filename_regex": r"LDC_SMD_(\d{2}-\d{4})[a-z]*_\d{8}_E2E(?:\.pdf)?",
            "tracker_file_column_index": 3,
            "tracker_docket_column_index": 8,
            "tracker_rows_xpath": "/html/body/form/table[2]/tbody/tr",
            "tracker_report_ready_xpath": "/html/body/form/table[2]/tbody/tr[1]/td[1]",
            "tracker_first_query_xpath": "/html/body/table/tbody/tr/td[2]/table[3]/tbody/tr[1]/td/a/img",
            "tracker_second_query_xpath": "/html/body/table/tbody/tr/td[2]/center/a/img",
            "tracker_open_existing_xpath": '//*[@id="startQuery"]/table/tbody/tr/td[2]/table[2]/tbody/tr[3]/td/input',
            "tracker_next1_xpath": '//*[@id="startQuery"]/table/tbody/tr/td[2]/table[2]/tbody/tr[4]/td/input',
            "tracker_query_option_xpath": '//*[@id="select1"]/option[19]',
            "tracker_next2_xpath": "/html/body/form/table/tbody/tr/td[2]/table[2]/tbody/tr[3]/td/input",
            "tracker_next3_xpath": '//input[@type="submit" and @value="  Next  "]',
            "tracker_search_xpath": "/html/body/form/table/tbody/tr/td[2]/table[2]/tbody/tr[9]/td/input[3]",
            "tracker_court_xpath": "/html/body/form/table/tbody/tr/td[2]/table[2]/tbody/tr[27]/td[4]/input",
            "tracker_date_xpath": "/html/body/form/table/tbody/tr/td[2]/table[2]/tbody/tr[33]/td[4]/input",
            "tracker_finish_xpath": "/html/body/form/table/tbody/tr/td[2]/table[2]/tbody/tr[72]/td/input[5]",
            # MOADMN portal entry page (instructions page contains the portal button)
            "instructions_url": "https://ahc.mo.gov/case-search-portal-instructions/",
            "portal_button_xpath": '//a[contains(@href,"ahcportal.mo.gov")]',
            "login_user": "",
            "login_password": "",
            "login_user_xpath": '//*[@id="UserID"]',
            "login_password_xpath": '//*[@id="Password"]',
            "login_submit_xpath": '//*[@id="loginForm"]/form/button',
            "matters_nav_xpath": '//*[@id="nav_matters"]',
            "filter_xpath": '//*[@id="my-input"]',
            "sort_button_xpath": '//*[@id="sortOptions"]',
            "sort_by_matter_id_xpath": '//*[@id="sortMenu"]/button[2]',
            "items_per_page_select_xpath": '//*[@id="site-content"]/app-matter-list/section/div/bento-pagination/div/div[1]/div[6]/div/select',
            "items_per_page_value": "100",
            "next_page_xpath": '//*[@id="site-content"]/app-matter-list/section/div/bento-pagination/div/div[1]/div[3]/button',
            "document_review_prefix_retry_attempts": 2,
            "document_review_prefix_retry_backoff_seconds": 3,
            "document_review_soft_reset_interval": 50,
            "document_review_matter_open_retry_attempts": 3,
            "document_review_matter_open_retry_backoff_seconds": 3,
            "document_review_exact_target_wait_seconds": 5,
            "pdf_quality_gate_enabled": True,
            "collected_folder": "Collected",
            "excluded_folder": "Excluded",
            "pdf_quality_quarantine_folder": "Excluded",
            "pdf_quality_reject_non_order": True,
            "pdf_quality_reject_content_duplicates": True,
            "pdf_quality_reject_neutralized_duplicates": True,
            "pdf_quality_reject_cross_docket_mentions": True,
            "tesseract_path": "",
            "tesseract_lang": "eng",
            "documents_tab_xpath": '//*[@id="tab-Documents"]/span',
            "documents_list_view_xpath": '//*[@id="tab-Documents-panel"]/matter-document/div[1]/ul/li[1]/fieldset/div/label[2]',
            "document_actions_button_xpath": '//*[@id="proLawCustomGrid"]/div[1]/div[1]/div[1]/div[2]/div[6]/div/div/div/button',
            "document_search_button_xpath": '//*[@id="DocumentSearchBtn"]',
            "document_download_button_xpath": '//*[@id="gridDownloadButton"]',
            # Extraction hints
            "matter_id_xpath": '//*[@id="site-content"]/app-matter-overview/div/dl/div[1]',
            "document_grid_root_xpath": '//*[@id="proLawCustomGrid"]',
            # Naming convention
            "file_prefix": "LDC_SMD",
            "file_suffix": "E2E",
            "file_ext": ".pdf",
            # Duplicate checking window (spec: +/- 1 week)
            "sfview_window_days": 7,
        }
        self.save_config(default_config)
        return default_config

    def save_config(self, config: Dict[str, Any]) -> bool:
        path = self._config_path()
        temp_path = path.with_name(f"{path.name}.tmp")
        try:
            temp_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
            temp_path.replace(path)
            self.last_save_error = ""
            return True
        except Exception as exc:
            self.last_save_error = str(exc)
            try:
                if temp_path.exists():
                    temp_path.unlink()
            except Exception:
                pass
            return False

    def get(self, key: str, default=None):
        return self.config.get(key, default)

    def set(self, key: str, value) -> bool:
        self.config[key] = value
        return self.save_config(self.config)



