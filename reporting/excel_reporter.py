"""
Excel run report generator for MOADMN collector.
"""

from __future__ import annotations

from datetime import datetime, date as date_t
from pathlib import Path
from typing import Callable, Dict, List, Optional

import pandas as pd
import openpyxl


class ExcelReporter:
    def __init__(self, log: Callable[[str], None]):
        self.log = log

    @staticmethod
    def _normalize_row(row: Dict) -> Dict:
        out = dict(row)
        for key, value in list(out.items()):
            if isinstance(value, date_t):
                out[key] = value.strftime("%m/%d/%Y")
        return out

    @staticmethod
    def _row_key(row: Dict) -> tuple[str, str, str]:
        matter_id = str(row.get("matter_id", "")).strip().upper()
        doc_title = str(
            row.get("doc_title")
            or row.get("order_document_title")
            or row.get("Document Title")
            or row.get("Order Document Title")
            or ""
        ).strip().lower()
        date_created = str(row.get("date_created") or row.get("Date Created") or "").strip()
        return matter_id, doc_title, date_created

    @staticmethod
    def _rename_workflow_columns(df: pd.DataFrame) -> pd.DataFrame:
        return df.rename(
            columns={
                "matter_id": "Matter ID",
                "matter_prefix": "Matter Prefix",
                "doc_title": "Document Title",
                "date_created": "Date Created",
                "decision": "Decision",
                "criteria_result": "Criteria Result",
                "matched_include_phrase": "Matched Include Phrase",
                "matched_exclude_phrase": "Matched Exclude Phrase",
                "order_document_found": "Order Document Found",
                "order_pdf_found": "Order PDF Found",
                "order_pdf_evidence": "PDF Evidence",
                "order_document_title": "Order Document Title",
                "document_phrase_log": "Reviewed Document Titles",
                "skipped_document_notes": "Skipped Document Notes",
                "downloaded_filenames": "Downloaded Filenames",
                "documents_collected_count": "Documents Collected",
                "document_rows_seen": "Document Rows Seen",
                "documents_view_state": "Document View State",
                "document_check_status": "Document Check Status",
                "window_start": "Window Start",
                "window_end": "Window End",
                "duplicate_path": "Matched LNDoc File",
                "filename": "Filename",
                "error": "Error",
                "quality_exclusion_reason": "PDF Quality Exclusion Reason",
                "quality_evidence": "PDF Quality Evidence",
                "quarantine_filename": "Quarantined File",
            }
        )

    def save_workflow_results(
        self,
        run_dir: Path,
        review_rows: List[Dict],
        collected: List[Dict],
        do_not_collect: List[Dict],
        failures: Optional[List[Dict]] = None,
        excluded: Optional[List[Dict]] = None,
    ) -> Path:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_file = run_dir / f"MOADMN_Run_Report_{timestamp}.xlsx"
        failures = failures or []
        excluded = excluded or []

        if not review_rows and not collected and not do_not_collect and not failures and not excluded:
            with pd.ExcelWriter(report_file, engine="openpyxl") as writer:
                df = pd.DataFrame(
                    [
                        {
                            "Status": "No review or collection rows were generated for this run.",
                            "Note": "Check matter filters, date range, and portal search behavior.",
                        }
                    ]
                )
                df.to_excel(writer, sheet_name="Summary", index=False)
                self._style_sheet(writer, "Summary", len(df.columns))
            self.log(f"Run report saved (summary only, no data rows): {report_file}")
            return report_file

        skipped_rows: List[Dict] = []
        skipped_keys: set[tuple[str, str, str]] = set()
        collected_keys = {self._row_key(row) for row in collected}

        for row in review_rows:
            normalized = self._normalize_row(row)
            documents_collected = int(row.get("documents_collected_count") or 0)
            check_status = str(row.get("document_check_status") or "").strip().lower()
            if documents_collected > 0 or check_status == "collected":
                continue
            row_key = self._row_key(normalized)
            if row_key in collected_keys or row_key in skipped_keys:
                continue
            skipped_keys.add(row_key)
            skipped_rows.append(normalized)

        if not review_rows:
            for row in do_not_collect:
                normalized = self._normalize_row(row)
                row_key = self._row_key(normalized)
                if row_key in skipped_keys:
                    continue
                skipped_keys.add(row_key)
                skipped_rows.append(normalized)

        for row in failures:
            normalized = self._normalize_row(row)
            normalized.setdefault("decision", "for collect")
            normalized.setdefault("criteria_result", "skip")
            normalized.setdefault("document_check_status", "download failed")
            note = str(normalized.get("skipped_document_notes", "")).strip()
            error = str(normalized.get("error", "")).strip()
            if error:
                normalized["skipped_document_notes"] = (
                    f"{note}; Download failed: {error}" if note else f"Download failed: {error}"
                )
            row_key = self._row_key(normalized)
            if row_key in skipped_keys:
                continue
            skipped_keys.add(row_key)
            skipped_rows.append(normalized)

        for row in excluded:
            normalized = self._normalize_row(row)
            normalized.setdefault("decision", "skip")
            normalized.setdefault("criteria_result", "exclude")
            normalized.setdefault("document_check_status", "excluded by PDF quality gate")
            row_key = self._row_key(normalized)
            if row_key in skipped_keys:
                continue
            skipped_keys.add(row_key)
            skipped_rows.append(normalized)

        with pd.ExcelWriter(report_file, engine="openpyxl") as writer:
            if collected:
                df = pd.DataFrame([self._normalize_row(r) for r in collected])
                df = self._rename_workflow_columns(df)
                df.to_excel(writer, sheet_name="Collected", index=False)
                self._style_sheet(writer, "Collected", len(df.columns))
                self.log(f"Collected sheet: {len(collected)} documents")

            if skipped_rows:
                df = pd.DataFrame(skipped_rows)
                preferred = [
                    "matter_id",
                    "matter_prefix",
                    "doc_title",
                    "date_created",
                    "decision",
                    "criteria_result",
                    "document_check_status",
                    "matched_include_phrase",
                    "matched_exclude_phrase",
                    "duplicate_path",
                    "order_document_found",
                    "order_pdf_found",
                    "order_pdf_evidence",
                    "order_document_title",
                    "document_phrase_log",
                    "skipped_document_notes",
                    "document_rows_seen",
                    "documents_view_state",
                    "window_start",
                    "window_end",
                    "error",
                    "quality_exclusion_reason",
                    "quality_evidence",
                    "quarantine_filename",
                ]
                available = [col for col in preferred if col in df.columns]
                remaining = [col for col in df.columns if col not in available]
                df = df[available + remaining]
                df = self._rename_workflow_columns(df)
                df.to_excel(writer, sheet_name="Skipped", index=False)
                self._style_sheet(writer, "Skipped", len(df.columns))
                self.log(f"Skipped sheet: {len(skipped_rows)} rows")

        self.log(f"Run report saved: {report_file}")
        return report_file

    def save_results(
        self,
        run_dir: Path,
        collected: List[Dict],
        skipped: List[Dict],
        excluded: Optional[List[Dict]] = None,
    ) -> Path:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_file = run_dir / f"MOADMN_Run_Report_{timestamp}.xlsx"
        excluded = excluded or []
        skipped_rows = [self._normalize_row(r) for r in skipped]
        skipped_keys = {self._row_key(row) for row in skipped_rows}

        for row in excluded:
            normalized = self._normalize_row(row)
            normalized.setdefault("decision", "skip")
            normalized.setdefault("criteria_result", "exclude")
            normalized.setdefault("document_check_status", "excluded by PDF quality gate")
            row_key = self._row_key(normalized)
            if row_key in skipped_keys:
                continue
            skipped_keys.add(row_key)
            skipped_rows.append(normalized)

        # If there is literally no data at all, create a small summary sheet
        # instead of letting openpyxl complain that no sheet is visible.
        if not collected and not skipped_rows:
            with pd.ExcelWriter(report_file, engine="openpyxl") as writer:
                df = pd.DataFrame(
                    [
                        {
                            "Status": "No documents were collected, skipped, or excluded for this run.",
                            "Note": "Check date range and portal filters.",
                        }
                    ]
                )
                df.to_excel(writer, sheet_name="Summary", index=False)
                self._style_sheet(writer, "Summary", len(df.columns))
            self.log(f"Run report saved (summary only, no data rows): {report_file}")
            return report_file

        with pd.ExcelWriter(report_file, engine="openpyxl") as writer:
            if collected:
                df = pd.DataFrame([self._normalize_row(r) for r in collected])
                # Rename columns for better readability
                df = df.rename(columns={
                    "matter_id": "Matter ID",
                    "doc_title": "Document Title",
                    "date_created": "Date Created",
                    "filename": "Filename"
                })
                df.to_excel(writer, sheet_name="Collected", index=False)
                self._style_sheet(writer, "Collected", len(df.columns))
                self.log(f"Collected sheet: {len(collected)} documents")
            if skipped_rows:
                df = pd.DataFrame(skipped_rows)
                df = df.rename(columns={
                    "matter_id": "Matter ID",
                    "doc_title": "Document Title",
                    "date_created": "Date Created",
                    "duplicate_path": "Duplicate Found At",
                    "error": "Error",
                    "quality_exclusion_reason": "PDF Quality Exclusion Reason",
                    "quality_evidence": "PDF Quality Evidence",
                    "quarantine_filename": "Quarantined File",
                })
                df.to_excel(writer, sheet_name="Skipped", index=False)
                self._style_sheet(writer, "Skipped", len(df.columns))
                self.log(f"Skipped sheet: {len(skipped_rows)} documents")

        self.log(f"Run report saved: {report_file}")
        return report_file

    @staticmethod
    def _style_sheet(writer: pd.ExcelWriter, sheet_name: str, num_columns: int) -> None:
        ws = writer.sheets[sheet_name]
        fill = openpyxl.styles.PatternFill(start_color="DFF0D8", end_color="DFF0D8", fill_type="solid")
        font = openpyxl.styles.Font(bold=True)
        for col_idx in range(1, num_columns + 1):
            cell = ws.cell(row=1, column=col_idx)
            cell.fill = fill
            cell.font = font


