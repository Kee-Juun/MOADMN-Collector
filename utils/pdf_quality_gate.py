from __future__ import annotations

import os
import hashlib
import io
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

from pypdf import PdfReader

try:
    import fitz  # type: ignore[import-not-found]
except Exception:  # pragma: no cover - optional fallback dependency
    fitz = None


WINDOWS_OCR_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Storage.FileAccessMode, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType = WindowsRuntime]

function Await([object]$Operation, [Type]$ResultType) {
    $method = [System.WindowsRuntimeSystemExtensions].GetMethods() |
        Where-Object { $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 } |
        Select-Object -First 1
    $generic = $method.MakeGenericMethod($ResultType)
    $task = $generic.Invoke($null, @($Operation))
    $task.Wait()
    return $task.Result
}

$imagePath = $args[0]
$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($imagePath)) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
$stream.Dispose()
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Write-Output $result.Text
"""


CASE_RE = re.compile(r"\b\d{2}-\d{4}\b")
DATE_RE = re.compile(
    r"\b(?:\d{1,2}/\d{1,2}/\d{2,4}|\d{8}|"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2},\s+\d{4})\b",
    re.IGNORECASE,
)
ORDER_HEADING_RE = re.compile(
    r"^\s*(?:AMENDED|AMENEDED|FINAL|INTERIM|SUPPLEMENTAL|CORRECTED|SECOND\s+AMENDED)?\s*"
    r"(?:ORDER\b|ORDER\s+OF\s+DISMISSAL\b|ORDER\s+FOLLOWING\b|DECISION(?:\s+AND\s+ORDER|\s+ISSUED)?\b|"
    r"DEFAULT\s+DECISION(?:\s+ISSUED)?\b)",
    re.IGNORECASE,
)
SO_ORDERED_RE = re.compile(r"\bSO\s+ORDERED\b", re.IGNORECASE)
NON_ORDER_HEADING_RE = re.compile(
    r"^\s*(?:MOTION\b|MOTION\s+TO\b|MOTION\s+FOR\b|COMPLAINT\b|ANSWER\b|AMENDED\s+ANSWER\b|"
    r"RESPONSE\b|PETITION\b|NOTICE\b|BRIEF\b|AFFIDAVIT\b|EXHIBIT\b|SUBPOENA\b|"
    r"JOINT\s+STATUS\s+REPORT\b|STATUS\s+REPORT\b|PROPOSED\s+FINDINGS\b|"
    r"PROPOSED\s+CONCLUSIONS\b|BUSINESS\s+RECORDS\b|CERTIFICATE\s+OF\s+SERVICE\b)",
    re.IGNORECASE,
)
NON_ORDER_SIGNAL_RE = re.compile(
    r"\b(?:REPLY\s+BRIEF|BRIEF|PROPOSED\s+FINDINGS|PROPOSED\s+CONCLUSIONS|"
    r"JOINT\s+STATUS\s+REPORT|STATUS\s+REPORT|EXHIBIT\s+LIST|AMENDED\s+EXHIBIT\s+LIST|"
    r"EXHIBIT\s+[A-Z0-9]+|MOTION\s+TO\s+DISMISS|MOTION\s+FOR|MOTION\s+TO|"
    r"NOTICE\s+OF\s+VOLUNTARY\s+DISMISSAL|NOTICE\s+TO\s+TAKE\s+DEPOSITION|"
    r"AMENDED\s+NOTICE\s+TO\s+TAKE\s+DEPOSITION|SUBPOENA\s+DUCES\s+TECUM|"
    r"CERTIFICATE\s+OF\s+SERVICE|AFFIDAVIT|BUSINESS\s+RECORDS)\b|^\s*DECISION\s*$",
    re.IGNORECASE | re.MULTILINE,
)
CAPTION_SKIP_RE = re.compile(
    r"^(?:BEFORE\s+THE|ADMINISTRATIVE\s+HEARING\s+COMMISSION|STATE\s+OF\s+MISSOURI|"
    r"IN\s+THE\s+MATTER\s+OF|Petitioner\.?|Respondent\.?|Appellant\.?|Appellee\.?|"
    r"Complainant\.?|Defendant\.?|FILED\b|Electronically\s+received\b|v\.|vs\.)$",
    re.IGNORECASE,
)


@dataclass
class PdfQualityResult:
    accepted: bool
    reason: str = ""
    evidence: str = ""
    text_chars: int = 0
    page_count: int = 0
    text_source: str = "pdf"
    duplicate_of: str = ""
    other_case_ids: list[str] = field(default_factory=list)


class PdfQualityGate:
    """Content-level guardrail for finalized MOADMN PDFs.

    The website metadata can label a row as an order even when the downloaded
    attachment is a filing, exhibit, or consolidated order. This gate checks the
    actual PDF body before the file is counted as collected.
    """

    def __init__(
        self,
        *,
        reject_non_order: bool = True,
        reject_content_duplicates: bool = True,
        reject_neutralized_duplicates: bool = True,
        reject_cross_docket_mentions: bool = True,
        tesseract_path: str = "",
        tesseract_lang: str = "eng",
    ) -> None:
        self.reject_non_order = reject_non_order
        self.reject_content_duplicates = reject_content_duplicates
        self.reject_neutralized_duplicates = reject_neutralized_duplicates
        self.reject_cross_docket_mentions = reject_cross_docket_mentions
        self.tesseract_path = self._resolve_tesseract_path(tesseract_path)
        self.tesseract_lang = (tesseract_lang or "eng").strip() or "eng"
        self._byte_hashes: Dict[str, str] = {}
        self._text_hashes: Dict[str, str] = {}
        self._neutral_hashes: Dict[str, str] = {}

    def validate(self, path: Path, matter_id: str, title: str = "") -> PdfQualityResult:
        try:
            data = path.read_bytes()
            text, page_count, text_source = self._extract_text(data)
        except Exception as exc:
            return PdfQualityResult(
                accepted=False,
                reason="PDF quality gate could not read the downloaded PDF",
                evidence=f"{type(exc).__name__}: {exc}",
            )

        byte_hash = hashlib.sha256(data).hexdigest()
        normalized = self._normalize_text(text)
        text_hash = self._sha_text(normalized) if normalized else ""
        neutral = self._neutralize_text(text)
        neutral_hash = self._sha_text(neutral) if neutral else ""
        own_case = self._case_id(matter_id)
        other_case_ids = [case_id for case_id in sorted(set(CASE_RE.findall(text))) if case_id != own_case]
        has_cross_docket_signal = bool(other_case_ids)

        if self.reject_non_order:
            order_status = self._order_status(text, title)
            if order_status:
                reason, evidence = order_status
                return PdfQualityResult(
                    accepted=False,
                    reason=reason,
                    evidence=evidence,
                    text_chars=len(text),
                    page_count=page_count,
                    text_source=text_source,
                    other_case_ids=other_case_ids,
                )

        if self.reject_content_duplicates and byte_hash in self._byte_hashes:
            return PdfQualityResult(
                accepted=False,
                reason="duplicate PDF bytes",
                evidence=f"Matches previously accepted file {self._byte_hashes[byte_hash]}",
                duplicate_of=self._byte_hashes[byte_hash],
                text_chars=len(text),
                page_count=page_count,
                text_source=text_source,
                other_case_ids=other_case_ids,
            )

        if self.reject_content_duplicates and text_hash and text_hash in self._text_hashes:
            return PdfQualityResult(
                accepted=False,
                reason="duplicate PDF text",
                evidence=f"Matches previously accepted file {self._text_hashes[text_hash]}",
                duplicate_of=self._text_hashes[text_hash],
                text_chars=len(text),
                page_count=page_count,
                text_source=text_source,
                other_case_ids=other_case_ids,
            )

        if (
            self.reject_neutralized_duplicates
            and has_cross_docket_signal
            and neutral_hash
            and neutral_hash in self._neutral_hashes
        ):
            return PdfQualityResult(
                accepted=False,
                reason="duplicate PDF content after case/date neutralization",
                evidence=f"Matches previously accepted file {self._neutral_hashes[neutral_hash]}",
                duplicate_of=self._neutral_hashes[neutral_hash],
                text_chars=len(text),
                page_count=page_count,
                text_source=text_source,
                other_case_ids=other_case_ids,
            )

        # Cross-docket mentions are treated as a consolidation signal, not an
        # automatic rejection. We keep one representative copy and let the
        # duplicate-content checks suppress the later matching downloads.
        self._byte_hashes[byte_hash] = path.name
        if text_hash:
            self._text_hashes[text_hash] = path.name
        if neutral_hash and has_cross_docket_signal:
            self._neutral_hashes[neutral_hash] = path.name
        return PdfQualityResult(
            accepted=True,
            text_chars=len(text),
            page_count=page_count,
            text_source=text_source,
            other_case_ids=other_case_ids,
            evidence=self._case_context(text, other_case_ids[0]) if other_case_ids else "",
        )

    @classmethod
    def content_cluster_key(cls, path: Path) -> str:
        data = path.read_bytes()
        try:
            text, _, _ = cls()._extract_text(data)
        except Exception:
            return hashlib.sha256(data).hexdigest()

        neutral = cls._neutralize_text(text)
        if neutral:
            return cls._sha_text(neutral)

        normalized = cls._normalize_text(text)
        if normalized:
            return cls._sha_text(normalized)

        return hashlib.sha256(data).hexdigest()

    def _extract_text(self, data: bytes) -> tuple[str, int, str]:
        primary_exc: Optional[Exception] = None
        page_count = 0
        try:
            reader = PdfReader(io.BytesIO(data))
            parts = []
            for page in reader.pages:
                parts.append(page.extract_text() or "")
            page_count = len(reader.pages)
            text = "\n".join(parts).strip()
            if text:
                return text, page_count, "pdf"
        except Exception as exc:
            primary_exc = exc
            if fitz is None:
                raise primary_exc
        if fitz is None:
            return "", page_count, "pdf"

        doc = None
        try:
            doc = fitz.open(stream=data, filetype="pdf")
            fitz_text = "\n".join((page.get_text() or "") for page in doc).strip()
            if fitz_text:
                return fitz_text, len(doc), "pdf"
            ocr_text, ocr_source = self._extract_text_with_ocr(doc)
            if ocr_text:
                return ocr_text, len(doc), ocr_source
            return "", len(doc), "pdf"
        except Exception:
            if primary_exc is not None:
                raise primary_exc
            raise
        finally:
            if doc is not None:
                doc.close()

    def _extract_text_with_ocr(self, doc, max_pages: int = 3) -> tuple[str, str]:
        if fitz is None:
            return "", "pdf"
        page_count = min(len(doc), max_pages)
        if page_count <= 0:
            return "", "pdf"
        scratch_root = Path.cwd() / "tmp" / "pdf_ocr"
        try:
            scratch_root.mkdir(parents=True, exist_ok=True)
        except Exception:
            return "", "pdf"
        created_images: list[Path] = []
        texts_by_source: Dict[str, list[str]] = {"tesseract": [], "ocr": []}
        try:
            for index in range(page_count):
                page = doc.load_page(index)
                pix = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0), alpha=False)
                image_path = scratch_root / f"{uuid.uuid4().hex}_page_{index + 1}.png"
                created_images.append(image_path)
                pix.save(str(image_path))
                source, page_text = self._ocr_image(image_path)
                if page_text:
                    texts_by_source.setdefault(source, []).append(page_text.strip())
        except Exception:
            return "", "pdf"
        finally:
            for image_path in created_images:
                try:
                    image_path.unlink(missing_ok=True)
                except Exception:
                    pass
        if texts_by_source.get("tesseract"):
            return "\n".join(texts_by_source["tesseract"]).strip(), "tesseract"
        if texts_by_source.get("ocr"):
            return "\n".join(texts_by_source["ocr"]).strip(), "ocr"
        return "", "pdf"

    def _ocr_image(self, image_path: Path) -> tuple[str, str]:
        tesseract_text = self._tesseract_ocr_image(image_path)
        if tesseract_text:
            return "tesseract", tesseract_text
        windows_text = self._windows_ocr_image(image_path)
        if windows_text:
            return "ocr", windows_text
        return "pdf", ""

    @classmethod
    def _resolve_tesseract_path(cls, configured_path: str = "") -> str:
        candidates: list[Path] = []
        if configured_path:
            candidates.append(Path(configured_path))
        which_path = shutil.which("tesseract")
        if which_path:
            candidates.append(Path(which_path))
        candidates.extend(
            [
                Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
                Path(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"),
            ]
        )
        if getattr(sys, "_MEIPASS", None):
            base = Path(getattr(sys, "_MEIPASS"))
            candidates.extend(
                [
                    base / "tesseract" / "tesseract.exe",
                    base / "assets" / "tesseract" / "tesseract.exe",
                ]
            )
        exe_dir = Path(sys.executable).resolve().parent
        candidates.extend(
            [
                exe_dir / "tesseract" / "tesseract.exe",
                exe_dir / "assets" / "tesseract" / "tesseract.exe",
            ]
        )
        seen: set[str] = set()
        for candidate in candidates:
            key = str(candidate).lower()
            if key in seen:
                continue
            seen.add(key)
            if candidate.exists():
                return str(candidate)
        return ""

    def _tesseract_ocr_image(self, image_path: Path) -> str:
        tesseract_path = self.tesseract_path or self._resolve_tesseract_path()
        if not tesseract_path:
            return ""
        env = dict(os.environ)
        tessdata_dir = Path(tesseract_path).resolve().parent / "tessdata"
        if tessdata_dir.exists():
            env["TESSDATA_PREFIX"] = str(tessdata_dir)
        try:
            completed = subprocess.run(
                [
                    tesseract_path,
                    str(image_path),
                    "stdout",
                    "--psm",
                    "6",
                    "-l",
                    self.tesseract_lang,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=25,
                check=False,
                env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            return ""
        if completed.returncode != 0:
            return ""
        return (completed.stdout or "").strip()

    @staticmethod
    def _windows_ocr_image(image_path: Path) -> str:
        if not sys.platform.startswith("win"):
            return ""
        script_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix=".ps1",
                prefix="moadmn_pdf_ocr_",
                delete=False,
            ) as handle:
                handle.write(WINDOWS_OCR_SCRIPT)
                script_path = Path(handle.name)
            completed = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(script_path),
                    str(image_path),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=25,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            return ""
        finally:
            if script_path is not None:
                script_path.unlink(missing_ok=True)
        if completed.returncode != 0:
            return ""
        return (completed.stdout or "").strip()

    @staticmethod
    def _normalize_text(text: str) -> str:
        text = text.replace("\x00", " ")
        text = re.sub(r"\bPage\s+\d+\s+of\s+\d+\b", " ", text, flags=re.IGNORECASE)
        text = re.sub(r"\s+", " ", text)
        return text.strip().lower()

    @classmethod
    def _neutralize_text(cls, text: str) -> str:
        text = cls._normalize_text(text)
        text = CASE_RE.sub("<CASE>", text)
        text = DATE_RE.sub("<DATE>", text)
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _sha_text(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()

    @staticmethod
    def _case_id(matter_id: str) -> str:
        match = CASE_RE.search(matter_id or "")
        return match.group(0) if match else ""

    @staticmethod
    def _first_line_list(text: str, limit: int = 90) -> list[str]:
        lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
        return [line for line in lines if line][:limit]

    @classmethod
    def _first_lines(cls, text: str, limit: int = 90) -> str:
        return "\n".join(cls._first_line_list(text, limit))[:4000]

    @classmethod
    def _is_caption_like_line(cls, line: str) -> bool:
        normalized = re.sub(r"\s+", " ", (line or "")).strip()
        if not normalized:
            return True
        if CAPTION_SKIP_RE.match(normalized):
            return True
        if re.fullmatch(r"[\[\]\(\)\.\-_\s]+", normalized):
            return True
        if CASE_RE.search(normalized) and ("case no" in normalized.lower() or normalized.lower().startswith("no. ")):
            return True
        if normalized.endswith(",") and not ORDER_HEADING_RE.match(normalized) and not NON_ORDER_HEADING_RE.match(normalized):
            return True
        if len(re.sub(r"[^A-Za-z]", "", normalized)) < 3:
            return True
        return False

    @classmethod
    def _first_heading_line(cls, text: str) -> str:
        for line in cls._first_line_list(text, limit=90):
            if cls._is_caption_like_line(line):
                continue
            if ORDER_HEADING_RE.match(line) or NON_ORDER_HEADING_RE.match(line):
                return re.sub(r"\s+", " ", line).strip()
        return ""

    @classmethod
    def _order_status(cls, text: str, title: str) -> Optional[tuple[str, str]]:
        if not text.strip():
            return "no extractable PDF text", ""
        heading = cls._first_heading_line(text)
        if heading:
            if ORDER_HEADING_RE.match(heading):
                return None
            if NON_ORDER_HEADING_RE.match(heading):
                return "non-order PDF body", heading
        if SO_ORDERED_RE.search(text):
            return None
        head_lines = cls._first_line_list(text)
        head = "\n".join(head_lines)[:4000]
        non_order_match = NON_ORDER_SIGNAL_RE.search(head)
        if non_order_match:
            return "non-order PDF body", heading or non_order_match.group(0)
        evidence = heading or head[:700]
        title_normalized = (title or "").lower()
        if "order" in title_normalized or "decision" in title_normalized:
            return "order-labeled PDF has no body order signal", evidence
        return "PDF body is not order-like", evidence

    @staticmethod
    def _case_context(text: str, case_id: str) -> str:
        lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
        for line in lines:
            if case_id in line:
                return line[:700]
        normalized = re.sub(r"\s+", " ", text)
        index = normalized.find(case_id)
        if index >= 0:
            return normalized[max(0, index - 220) : index + 380]
        return f"Found other case ID {case_id}"
