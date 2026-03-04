"""
Artifact Manager — unified output directory for all agent products.

All outputs (records, files, report) go through one directory:
  artifacts/
    data/records.jsonl      ← structured data from save_records()
    data/extracted_data.json ← final export from save_data
    files/                  ← downloaded files (PDF, images, etc.)
    manifest.json           ← index of all artifacts with metadata
    report.json             ← final run report
"""

import json
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("tools.artifacts")

DEFAULT_DIR = os.environ.get("ARTIFACTS_DIR", "./artifacts")


class ArtifactManager:
    """Manages the unified artifacts directory for a single run."""

    def __init__(self, base_dir: str = DEFAULT_DIR):
        self.base_dir = Path(base_dir)
        self._records: list[dict] = []
        self._files: list[dict] = []
        self._started_at: str = ""

    @property
    def data_dir(self) -> Path:
        return self.base_dir / "data"

    @property
    def files_dir(self) -> Path:
        return self.base_dir / "files"

    def init_run(self) -> None:
        """Clean and create artifacts directory for a fresh run."""
        # Clear contents but not the dir itself (may be a Docker mount point)
        if self.base_dir.exists():
            for child in self.base_dir.iterdir():
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(exist_ok=True)
        self.files_dir.mkdir(exist_ok=True)
        self._records = []
        self._files = []
        self._started_at = datetime.utcnow().isoformat() + "Z"
        logger.info(f"Artifacts initialized: {self.base_dir}")

    def add_records(self, records: list[dict]) -> None:
        """Track records collected during the run."""
        self._records.extend(records)

    def add_file(self, metadata: dict) -> None:
        """Track a downloaded file."""
        self._files.append(metadata)

    def add_files(self, file_list: list[dict]) -> None:
        """Track multiple downloaded files."""
        self._files.extend(file_list)

    @property
    def records(self) -> list[dict]:
        return self._records

    @property
    def files(self) -> list[dict]:
        return self._files

    def write_manifest(self, extra: dict | None = None) -> Path:
        """Write manifest.json summarizing all artifacts."""
        manifest = {
            "started_at": self._started_at,
            "completed_at": datetime.utcnow().isoformat() + "Z",
            "artifacts": {
                "records": {
                    "count": len(self._records),
                    "path": "data/records.jsonl" if self._records else None,
                },
                "files": [
                    {
                        "filename": f.get("filename", "unknown"),
                        "size": f.get("size", 0),
                        "type": f.get("type", "unknown"),
                        "source_url": f.get("url", ""),
                        "description": f.get("description", ""),
                        "path": f"files/{f.get('filename', 'unknown')}",
                    }
                    for f in self._files
                ],
            },
        }
        if extra:
            manifest.update(extra)

        path = self.base_dir / "manifest.json"
        with open(path, "w", encoding="utf-8") as fp:
            json.dump(manifest, fp, indent=2, ensure_ascii=False, default=str)
        logger.info(f"Manifest written: {len(self._records)} records, {len(self._files)} files")
        return path

    def save_records_file(self) -> Path | None:
        """Write all tracked records to data/records.jsonl."""
        if not self._records:
            return None
        path = self.data_dir / "records.jsonl"
        with open(path, "w", encoding="utf-8") as f:
            for r in self._records:
                f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
        return path

    def save_export(self, data: list[dict], fmt: str = "json") -> dict:
        """Save final data export (replaces old save_data_tool)."""
        filepath = self.data_dir / f"extracted_data.{fmt}"
        if fmt == "csv":
            import csv
            if data:
                with open(filepath, "w", encoding="utf-8", newline="") as f:
                    writer = csv.DictWriter(f, fieldnames=list(data[0].keys()))
                    writer.writeheader()
                    writer.writerows(data)
        else:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False, default=str)
        return {"saved": len(data), "path": str(filepath), "format": fmt}

    def inspect_file(self, filename: str) -> dict:
        """Inspect a file in artifacts/files/ and return metadata."""
        path = self.files_dir / os.path.basename(filename)
        if not path.exists():
            return {"error": f"File not found: {filename}"}

        info: dict[str, Any] = {
            "filename": path.name,
            "size": path.stat().st_size,
            "size_mb": round(path.stat().st_size / (1024 * 1024), 2),
        }
        ext = path.suffix.lower()

        if ext == ".pdf":
            try:
                from pypdf import PdfReader
                reader = PdfReader(str(path))
                info["pages"] = len(reader.pages)
                if reader.pages:
                    text = reader.pages[0].extract_text() or ""
                    info["text_extractable"] = bool(text.strip())
                    info["first_page_preview"] = text[:500] if text.strip() else "(no text)"
                meta = reader.metadata
                if meta:
                    info["metadata"] = {
                        k: str(v) for k, v in meta.items()
                        if v and len(str(v)) < 200
                    }
            except Exception as e:
                info["pdf_error"] = str(e)

        elif ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff"):
            try:
                from PIL import Image
                img = Image.open(str(path))
                info["dimensions"] = list(img.size)
                info["format"] = img.format
                info["mode"] = img.mode
            except Exception as e:
                info["image_error"] = str(e)

        elif ext in (".csv",):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                info["rows"] = len(lines) - 1  # minus header
                info["header"] = lines[0].strip() if lines else ""
            except Exception as e:
                info["csv_error"] = str(e)

        elif ext in (".json", ".jsonl"):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                if ext == ".jsonl":
                    info["lines"] = content.count("\n")
                else:
                    data = json.loads(content)
                    info["type"] = type(data).__name__
                    if isinstance(data, list):
                        info["items"] = len(data)
            except Exception as e:
                info["json_error"] = str(e)

        return info
