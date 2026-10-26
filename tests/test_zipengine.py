from __future__ import annotations

import io
import stat
import zipfile

import pytest

from app.zipengine.extractor import ExtractionCancelled, ZipExtractor
from app.zipengine.safety import (
    PathTraversalError,
    SymlinkEntryError,
    ZipLimits,
    open_archive,
    scan_archive,
    ExtractionBudget,
)
from tests.utils import make_zip


def limits(**overrides):
    values = dict(
        max_archive_bytes=2 * 1024 * 1024,
        max_total_uncompressed_bytes=2 * 1024 * 1024,
        max_single_file_bytes=1024 * 1024,
        max_entries=100,
        max_compression_ratio=0.0,
        max_depth=3,
        min_free_disk_bytes=0,
    )
    values.update(overrides)
    return ZipLimits(**values)


def extractor(**overrides):
    return ZipExtractor(limits(**overrides), nested=True, chunk_size=64 * 1024)


def test_scan_rejects_path_traversal(tmp_path):
    path = tmp_path / "evil.zip"
    path.write_bytes(make_zip({"../evil.mp4": b"not a video"}))
    with open_archive(path) as archive:
        with pytest.raises(PathTraversalError):
            scan_archive(
                archive,
                limits(),
                archive_size=path.stat().st_size,
                depth=0,
                budget=ExtractionBudget(limits()),
            )


def test_scan_rejects_symlink_even_with_directory_suffix(tmp_path):
    path = tmp_path / "symlink.zip"
    info = zipfile.ZipInfo("link/")
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(info, "/etc/passwd")
    with open_archive(path) as archive:
        with pytest.raises(SymlinkEntryError):
            scan_archive(
                archive,
                limits(),
                archive_size=path.stat().st_size,
                depth=0,
                budget=ExtractionBudget(limits()),
            )


def test_extracts_videos_only_and_preserves_bytes(tmp_path):
    payload = b"original-video-bytes" * 100
    path = tmp_path / "bundle.zip"
    path.write_bytes(make_zip({"clip.mp4": payload, "notes.txt": b"ignore me"}))
    report = extractor().extract(path, tmp_path / "out")
    assert report.video_count == 1
    assert report.skipped_non_video == 1
    video = report.videos[0]
    assert video.rel_path == "clip.mp4"
    assert video.path.read_bytes() == payload
    assert video.size == len(payload)


def test_nested_archive_is_extracted_with_prefix(tmp_path):
    inner = make_zip({"inner.mp4": b"inner-bytes"})
    outer = make_zip({"top.mp4": b"top-bytes", "nested.zip": inner})
    path = tmp_path / "outer.zip"
    path.write_bytes(outer)
    report = extractor().extract(path, tmp_path / "out")
    rel_paths = sorted(video.rel_path for video in report.videos)
    assert rel_paths == ["_nested/1/inner.mp4", "top.mp4"]
    assert {video.rel_path: video.path.read_bytes() for video in report.videos} == {
        "_nested/1/inner.mp4": b"inner-bytes",
        "top.mp4": b"top-bytes",
    }
    assert not (tmp_path / "out" / ".scratch").exists()


def test_duplicate_member_names_get_unique_paths(tmp_path):
    path = tmp_path / "dupes.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("same.mp4", b"one")
        archive.writestr("same.mp4", b"two")
    report = extractor().extract(path, tmp_path / "out")
    names = sorted(video.path.name for video in report.videos)
    assert names == ["same.mp4", "same~1.mp4"]
    assert report.bytes_written == 6


def test_video_limit_truncates_without_failing(tmp_path):
    path = tmp_path / "many.zip"
    path.write_bytes(make_zip({f"{i}.mp4": b"x" for i in range(5)}))
    report = extractor(max_entries=100).extract(path, tmp_path / "out")
    assert report.video_count == 5

    limited = ZipExtractor(
        limits(),
        nested=True,
        chunk_size=64 * 1024,
        max_videos_per_job=2,
    )
    report = limited.extract(path, tmp_path / "out2")
    assert report.video_count == 2
    assert report.truncated is True


def test_per_file_limit_is_enforced(tmp_path):
    path = tmp_path / "big.zip"
    path.write_bytes(make_zip({"big.mp4": b"x" * 2048}))
    with pytest.raises(Exception):
        extractor(max_single_file_bytes=1024).extract(path, tmp_path / "out")


def test_cancellation_stops_extraction(tmp_path):
    path = tmp_path / "cancel.zip"
    path.write_bytes(make_zip({"clip.mp4": b"x" * 4096}))
    with pytest.raises(ExtractionCancelled):
        extractor().extract(path, tmp_path / "out", cancel_check=lambda: True)


def test_empty_archive_returns_empty_report(tmp_path):
    path = tmp_path / "empty.zip"
    path.write_bytes(make_zip({}))
    report = extractor().extract(path, tmp_path / "out")
    assert report.video_count == 0
    assert report.archives_scanned == 1
