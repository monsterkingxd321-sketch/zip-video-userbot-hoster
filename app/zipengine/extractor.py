"""Streaming ZIP extraction: videos only, nested archives, hard safety limits.

Design notes
------------
* Nothing is ever loaded fully into memory: entries are copied in fixed-size
  chunks straight to disk.
* Non-video payloads are never written at all (they still count towards the
  bomb/entropy budget), which removes most disk-exhaustion vectors.
* Every archive (including nested ones) is pre-validated before extraction and
  shares one :class:`~app.zipengine.safety.ExtractionBudget`.
* Extracted videos keep their original bytes; we never re-encode.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Sequence

from app.constants import DEFAULT_VIDEO_EXTENSIONS
from app.zipengine.probe import extension_of
from app.zipengine.safety import (
    EntryTooLargeError,
    ExtractionBudget,
    ExtractionCancelled,
    NestedArchiveDepthError,
    PathTraversalError,
    ZipLimits,
    ensure_disk_space,
    is_directory,
    is_symlink,
    is_zip_filename,
    open_archive,
    safe_destination,
    sanitize_member_name,
    scan_archive,
)

logger = logging.getLogger(__name__)

CHUNK_SIZE = 1024 * 1024  # 1 MiB streaming window
SCRATCH_DIRNAME = ".scratch"
NESTED_PREFIX = "_nested"

CancelCheck = Callable[[], bool]
ProgressCallback = Callable[[int, int], None]  # (bytes_written, videos_found)


@dataclass(frozen=True)
class ExtractedVideo:
    rel_path: str
    path: Path
    size: int
    depth: int

    @property
    def filename(self) -> str:
        return Path(self.rel_path).name


@dataclass
class ExtractionReport:
    videos: List[ExtractedVideo] = field(default_factory=list)
    archives_scanned: int = 0
    entries_seen: int = 0
    skipped_non_video: int = 0
    bytes_written: int = 0
    warnings: List[str] = field(default_factory=list)
    truncated: bool = False

    @property
    def video_count(self) -> int:
        return len(self.videos)

    @property
    def total_video_bytes(self) -> int:
        return sum(video.size for video in self.videos)

    def sorted_videos(self) -> List[ExtractedVideo]:
        """Send smallest first so the user sees results as early as possible."""
        return sorted(self.videos, key=lambda video: (video.size, video.rel_path))


class ZipExtractor:
    """Extracts video files from ZIP archives (recursively) with bounded concurrency."""

    def __init__(
        self,
        limits: ZipLimits,
        *,
        video_extensions: Iterable[str] = DEFAULT_VIDEO_EXTENSIONS,
        chunk_size: int = CHUNK_SIZE,
        concurrency: int = 2,
        max_videos_per_job: int = 0,
        nested: bool = True,
    ) -> None:
        self.limits = limits
        self.video_extensions = frozenset(ext.lower() for ext in video_extensions)
        self.chunk_size = max(64 * 1024, int(chunk_size))
        self.concurrency = max(1, int(concurrency))
        self.max_videos_per_job = max(0, int(max_videos_per_job))
        self.nested = nested
        self._semaphore: Optional[asyncio.Semaphore] = None

    # -- async entry point ------------------------------------------------
    async def extract_archive(
        self,
        archive_path: Path,
        dest_root: Path,
        *,
        cancel_check: Optional[CancelCheck] = None,
        progress: Optional[ProgressCallback] = None,
    ) -> ExtractionReport:
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.concurrency)
        async with self._semaphore:
            return await asyncio.to_thread(
                self.extract, archive_path, dest_root, cancel_check, progress
            )

    # -- synchronous core (runs in a worker thread) ------------------------
    def extract(
        self,
        archive_path: Path,
        dest_root: Path,
        cancel_check: Optional[CancelCheck] = None,
        progress: Optional[ProgressCallback] = None,
    ) -> ExtractionReport:
        archive_path = Path(archive_path)
        dest_root = Path(dest_root)
        dest_root.mkdir(parents=True, exist_ok=True)

        budget = ExtractionBudget(self.limits)
        report = ExtractionReport()

        pending: List[tuple[Path, int, str]] = [(archive_path, 0, "")]
        scratch_paths: List[Path] = []
        nested_counter = 0

        try:
            while pending:
                current, depth, prefix = pending.pop(0)
                self._raise_if_cancelled(cancel_check)

                if depth > self.limits.max_depth:
                    raise NestedArchiveDepthError(
                        f"nested archives deeper than {self.limits.max_depth} levels"
                    )

                try:
                    archive_size = current.stat().st_size
                except OSError:
                    continue

                with open_archive(current) as zf:
                    _entries, declared_total = scan_archive(
                        zf,
                        self.limits,
                        archive_size=archive_size,
                        depth=depth,
                        budget=budget,
                    )
                    report.archives_scanned += 1
                    extract_root = dest_root / prefix if prefix else dest_root
                    extract_root.mkdir(parents=True, exist_ok=True)
                    ensure_disk_space(extract_root, min(declared_total, self.limits.max_single_file_bytes), self.limits)

                    for info in zf.infolist():
                        self._raise_if_cancelled(cancel_check)
                        if is_symlink(info):
                            raise PathTraversalError("symbolic link entry", detail=info.filename)
                        if is_directory(info):
                            continue

                        rel = sanitize_member_name(info.filename)
                        extension = extension_of(rel)

                        if extension in self.video_extensions:
                            if self.max_videos_per_job and len(report.videos) >= self.max_videos_per_job:
                                report.truncated = True
                                report.warnings.append(
                                    f"stopped after {self.max_videos_per_job} videos (job limit)"
                                )
                                continue
                            video = self._extract_member(
                                zf,
                                info,
                                extract_root,
                                rel=rel,
                                prefix=prefix,
                                depth=depth,
                                budget=budget,
                                report=report,
                                cancel_check=cancel_check,
                                progress=progress,
                            )
                            if video is not None:
                                report.videos.append(video)
                                budget.register_video()
                        elif self.nested and is_zip_filename(rel) and depth + 1 <= self.limits.max_depth:
                            nested_counter += 1
                            scratch = self._extract_to_scratch(
                                zf, info, dest_root, nested_counter, budget, cancel_check
                            )
                            if scratch is not None:
                                scratch_paths.append(scratch)
                                new_prefix = f"{NESTED_PREFIX}/{nested_counter}/"
                                pending.append((scratch, depth + 1, f"{prefix}{new_prefix}"))
                        else:
                            report.skipped_non_video += 1

                    if report.truncated:
                        pending.clear()
        finally:
            self._cleanup_scratch(dest_root, scratch_paths)

        report.entries_seen = budget.entries_seen
        report.bytes_written = budget.written_bytes
        return report

    # -- helpers ----------------------------------------------------------
    def _extract_member(
        self,
        zf,
        info,
        extract_root: Path,
        *,
        rel: str,
        prefix: str,
        depth: int,
        budget: ExtractionBudget,
        report: ExtractionReport,
        cancel_check: Optional[CancelCheck],
        progress: Optional[ProgressCallback],
    ) -> Optional[ExtractedVideo]:
        destination = safe_destination(extract_root, rel)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination = _unique_path(destination)

        ensure_disk_space(extract_root, int(info.file_size or 0), self.limits)

        part = destination.with_name(destination.name + ".part")
        written = 0
        try:
            with zf.open(info) as source, open(part, "wb") as sink:
                while True:
                    self._raise_if_cancelled(cancel_check)
                    chunk = source.read(self.chunk_size)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > self.limits.max_single_file_bytes:
                        raise EntryTooLargeError("entry exceeds the per-file size limit")
                    budget.account_written(len(chunk))
                    sink.write(chunk)
                    if progress is not None:
                        progress(budget.written_bytes, budget.videos_found)
            if written == 0:
                part.unlink(missing_ok=True)
                report.warnings.append(f"skipped empty file {rel}")
                return None
            part.replace(destination)
        except BaseException:
            part.unlink(missing_ok=True)
            raise

        rel_path = f"{prefix}{rel}" if prefix else rel
        return ExtractedVideo(
            rel_path=rel_path, path=destination, size=written, depth=depth
        )

    def _extract_to_scratch(
        self,
        zf,
        info,
        dest_root: Path,
        index: int,
        budget: ExtractionBudget,
        cancel_check: Optional[CancelCheck],
    ) -> Optional[Path]:
        """Extract a nested archive to a scratch dir so it can be opened."""
        scratch_dir = dest_root / SCRATCH_DIRNAME
        scratch_dir.mkdir(parents=True, exist_ok=True)
        target = scratch_dir / f"nested_{index}.zip"
        ensure_disk_space(scratch_dir, int(info.file_size or 0), self.limits)
        written = 0
        with zf.open(info) as source, open(target, "wb") as sink:
            while True:
                self._raise_if_cancelled(cancel_check)
                chunk = source.read(self.chunk_size)
                if not chunk:
                    break
                written += len(chunk)
                if written > self.limits.max_single_file_bytes:
                    raise EntryTooLargeError("nested archive exceeds the per-file size limit")
                budget.account_written(len(chunk))
                sink.write(chunk)
        if written == 0:
            target.unlink(missing_ok=True)
            return None
        return target

    def _cleanup_scratch(self, dest_root: Path, scratch_paths: Sequence[Path]) -> None:
        for path in scratch_paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:  # pragma: no cover
                pass
        scratch_dir = Path(dest_root) / SCRATCH_DIRNAME
        shutil.rmtree(scratch_dir, ignore_errors=True)

    @staticmethod
    def _raise_if_cancelled(cancel_check: Optional[CancelCheck]) -> None:
        if cancel_check is not None and cancel_check():
            raise ExtractionCancelled("task cancelled by user")


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    counter = 1
    while True:
        candidate = parent / f"{stem}~{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def build_extractor(settings, *, nested: bool = True) -> ZipExtractor:
    """Create an extractor configured from application settings."""
    return ZipExtractor(
        ZipLimits.from_settings(settings),
        video_extensions=settings.video_extension_set,
        chunk_size=settings.download_chunk_size_kb * 1024,
        concurrency=settings.extract_concurrency,
        max_videos_per_job=getattr(settings, "max_videos_per_job", 0),
        nested=nested,
    )
