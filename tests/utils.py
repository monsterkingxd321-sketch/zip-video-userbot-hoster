"""Test data helpers."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path


def make_zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in files.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def make_zip_path(path: Path, files: dict[str, bytes]) -> Path:
    path.write_bytes(make_zip(files))
    return path
