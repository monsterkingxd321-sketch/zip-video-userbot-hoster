"""Dependency-free video metadata probing (duration/width/height).

Telegram requires ``DocumentAttributeVideo`` (width, height, duration) for native
video messages and refuses to stream exotic containers. Rather than pulling in
ffmpeg, we parse the container headers directly for the formats that matter.
"""

from __future__ import annotations

import logging
import posixpath
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Optional

from app.constants import EXTENSION_TO_MIME

logger = logging.getLogger(__name__)

_MAX_HEADER_SCAN = 8 * 1024 * 1024  # never read more than this while probing


@dataclass(frozen=True)
class VideoMetadata:
    duration: int = 0
    width: int = 0
    height: int = 0
    container: str = "unknown"

    @property
    def is_valid(self) -> bool:
        return self.duration > 0 and self.width > 0 and self.height > 0

    @property
    def has_dimensions(self) -> bool:
        return self.width > 0 and self.height > 0


def extension_of(name: str) -> str:
    return posixpath.splitext(str(name).lower())[1]


def mime_for(extension: str) -> str:
    ext = extension if extension.startswith(".") else f".{extension}"
    return EXTENSION_TO_MIME.get(ext.lower(), f"video/{ext.lstrip('.').lower() or 'mp4'}")


# ---------------------------------------------------------------------------
# ISO base media file format (mp4 / m4v / mov)
# ---------------------------------------------------------------------------
def _iter_boxes(handle: BinaryIO, start: int, end: int):
    """Yield ``(type, header_start, payload_start, payload_end)`` for ISO BMFF boxes."""
    offset = start
    while offset + 8 <= end:
        handle.seek(offset)
        header = handle.read(8)
        if len(header) < 8:
            return
        size, box_type = struct.unpack(">I4s", header)
        header_size = 8
        if size == 1:
            large = handle.read(8)
            if len(large) < 8:
                return
            size = struct.unpack(">Q", large)[0]
            header_size = 16
        elif size == 0:
            size = end - offset
        if size < header_size:
            return
        box_end = min(offset + size, end)
        yield box_type.decode("latin-1"), offset, offset + header_size, box_end
        offset = box_end


def _parse_mdhd(handle: BinaryIO, payload_start: int, payload_end: int) -> tuple[int, int]:
    handle.seek(payload_start)
    body = handle.read(payload_end - payload_start)
    if len(body) < 20:
        return 0, 0
    version = body[0]
    if version == 1:
        if len(body) < 32:
            return 0, 0
        timescale, duration = struct.unpack(">IQ", body[20:32])
    else:
        timescale, duration = struct.unpack(">II", body[12:20])
    return int(timescale), int(duration)


def _parse_mp4(handle: BinaryIO, size: int) -> Optional[VideoMetadata]:
    moov = None
    for box_type, _hs, payload_start, payload_end in _iter_boxes(handle, 0, size):
        if box_type == "moov":
            moov = (payload_start, payload_end)
            break
    if moov is None:
        return None

    movie_duration = 0
    best: Optional[VideoMetadata] = None
    best_area = -1

    for box_type, _hs, payload_start, payload_end in _iter_boxes(handle, moov[0], moov[1]):
        if box_type == "mvhd":
            handle.seek(payload_start)
            body = handle.read(min(payload_end - payload_start, 120))
            if len(body) >= 20:
                version = body[0]
                if version == 1 and len(body) >= 32:
                    timescale, duration = struct.unpack(">IQ", body[20:32])
                else:
                    timescale, duration = struct.unpack(">II", body[12:20])
                if timescale:
                    movie_duration = int(round(duration / timescale))
        elif box_type == "trak":
            width = height = 0
            track_duration = 0
            for sub_type, _shs, sub_start, sub_end in _iter_boxes(handle, payload_start, payload_end):
                if sub_type == "tkhd" and sub_end - sub_start >= 8:
                    handle.seek(sub_end - 8)
                    raw = handle.read(8)
                    if len(raw) == 8:
                        width_fx, height_fx = struct.unpack(">II", raw)
                        width = int(round(width_fx / 65536))
                        height = int(round(height_fx / 65536))
                elif sub_type == "mdia":
                    for mdia_type, _mhs, m_start, m_end in _iter_boxes(handle, sub_start, sub_end):
                        if mdia_type == "mdhd":
                            timescale, duration = _parse_mdhd(handle, m_start, m_end)
                            if timescale:
                                track_duration = int(round(duration / timescale))
            if width > 0 and height > 0:
                area = width * height
                if area > best_area:
                    best_area = area
                    best = VideoMetadata(
                        duration=track_duration or movie_duration,
                        width=width,
                        height=height,
                        container="mp4",
                    )

    if best is None and movie_duration:
        return VideoMetadata(duration=movie_duration, width=0, height=0, container="mp4")
    return best


# ---------------------------------------------------------------------------
# Matroska / WebM (EBML)
# ---------------------------------------------------------------------------
def _read_vint(handle: BinaryIO, *, keep_marker: bool = False) -> tuple[int, int]:
    first = handle.read(1)
    if not first:
        raise EOFError
    value = first[0]
    if value == 0:
        raise ValueError("invalid EBML vint")
    length = 1
    mask = 0x80
    while not value & mask:
        mask >>= 1
        length += 1
        if length > 8:
            raise ValueError("invalid EBML vint length")
    if keep_marker:
        raw = bytes([value]) + handle.read(length - 1)
        return int.from_bytes(raw, "big"), length
    value &= mask - 1
    for byte in handle.read(length - 1):
        value = (value << 8) | byte
    return value, length


def _read_element_id(handle: BinaryIO) -> tuple[int, int]:
    first = handle.read(1)
    if not first:
        raise EOFError
    value = first[0]
    if value == 0:
        raise ValueError("invalid element id")
    length = 1
    mask = 0x80
    while not value & mask:
        mask >>= 1
        length += 1
        if length > 4:
            raise ValueError("invalid element id length")
    raw = bytes([value]) + handle.read(length - 1)
    return int.from_bytes(raw, "big"), length


def _read_uint(handle: BinaryIO, size: int) -> int:
    return int.from_bytes(handle.read(size), "big")


def _read_float(handle: BinaryIO, size: int) -> float:
    raw = handle.read(size)
    if size == 4:
        return struct.unpack(">f", raw)[0]
    if size == 8:
        return struct.unpack(">d", raw)[0]
    return 0.0


def _parse_mkv(handle: BinaryIO, size: int) -> Optional[VideoMetadata]:
    handle.seek(0)
    timecode_scale = 1_000_000
    duration_raw = 0.0
    width = height = 0
    found_tracks = False
    read_total = 0

    def walk(limit: int, depth: int) -> None:
        nonlocal timecode_scale, duration_raw, width, height, found_tracks, read_total
        if depth > 16:
            return
        while read_total < _MAX_HEADER_SCAN:
            pos = handle.tell()
            if pos >= limit:
                return
            try:
                element_id, _ = _read_element_id(handle)
                read_total += 4
                elem_size, _ = _read_vint(handle)
                read_total += 8
            except (EOFError, ValueError):
                return
            payload_start = handle.tell()
            if elem_size == (1 << 56) - 1 or payload_start + elem_size > limit:
                payload_end = limit
            else:
                payload_end = payload_start + elem_size

            if element_id == 0x18538067:  # Segment
                walk(payload_end, depth + 1)
            elif element_id == 0x1549A966:  # Info
                walk(payload_end, depth + 1)
            elif element_id == 0x1654AE6B:  # Tracks
                found_tracks = True
                walk(payload_end, depth + 1)
            elif element_id == 0x2AD7B1:  # TimecodeScale
                if 0 < elem_size <= 8:
                    timecode_scale = _read_uint(handle, elem_size) or 1_000_000
            elif element_id == 0x4489:  # Duration
                if elem_size in (4, 8):
                    duration_raw = _read_float(handle, elem_size)
            elif element_id == 0xE0:  # Video
                walk(payload_end, depth + 1)
            elif element_id == 0xB0:  # PixelWidth
                if 0 < elem_size <= 8:
                    width = _read_uint(handle, elem_size)
            elif element_id == 0xBA:  # PixelHeight
                if 0 < elem_size <= 8:
                    height = _read_uint(handle, elem_size)
            elif element_id in (0xAE, 0x1F43B675, 0x1C53BB6B, 0x1254C367):
                # TrackEntry / Cluster / Cues / Tags: only descend into TrackEntry,
                # skip media data entirely to stay fast.
                if element_id == 0xAE:
                    walk(payload_end, depth + 1)
            handle.seek(payload_end)

    try:
        walk(min(size, _MAX_HEADER_SCAN), 0)
    except (EOFError, ValueError, struct.error):
        return None

    if not found_tracks and not (width and height):
        return None
    duration = 0
    if duration_raw:
        duration = int(round(duration_raw * timecode_scale / 1_000_000_000))
    return VideoMetadata(duration=duration, width=width, height=height, container="mkv")


# ---------------------------------------------------------------------------
# AVI (RIFF)
# ---------------------------------------------------------------------------
def _parse_avi(handle: BinaryIO, size: int) -> Optional[VideoMetadata]:
    header = handle.read(12)
    if len(header) < 12 or header[:4] != b"RIFF" or header[8:12] != b"AVI ":
        return None

    us_per_frame = 0
    total_frames = 0
    width = height = 0

    def walk(limit: int, for_video: bool = False, depth: int = 0) -> None:
        nonlocal us_per_frame, total_frames, width, height
        if depth > 12:
            return
        while handle.tell() + 8 <= limit:
            chunk_id = handle.read(4)
            (chunk_size,) = struct.unpack("<I", handle.read(4))
            payload_start = handle.tell()
            payload_end = min(payload_start + chunk_size, limit)
            if chunk_id == b"LIST":
                list_type = handle.read(4)
                if list_type == b"hdrl":
                    walk(payload_end, depth=depth + 1)
                elif list_type == b"strl":
                    walk(payload_end, for_video=True, depth=depth + 1)
                elif list_type == b"movi":
                    return
            elif chunk_id == b"avih" and chunk_size >= 40:
                body = handle.read(40)
                us_per_frame, total_frames = struct.unpack("<II", body[:8])
            elif chunk_id == b"strh" and chunk_size >= 8:
                fcc_type = handle.read(4)
                if fcc_type == b"vids":
                    walk(payload_end, for_video=True, depth=depth + 1)
            elif chunk_id == b"strf" and for_video and chunk_size >= 12:
                body = handle.read(12)
                width, height = struct.unpack("<ii", body[4:12])
            handle.seek(payload_end)
            if chunk_size % 2:  # RIFF padding
                handle.seek(min(payload_end + 1, limit))

    try:
        walk(min(size, _MAX_HEADER_SCAN))
    except (EOFError, struct.error):
        return None
    duration = int(round(us_per_frame * total_frames / 1_000_000)) if us_per_frame else 0
    if not (width and height) and not duration:
        return None
    return VideoMetadata(duration=duration, width=abs(width), height=abs(height), container="avi")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def probe_video(path: Path, *, extension: Optional[str] = None) -> Optional[VideoMetadata]:
    """Best-effort metadata probe. Returns ``None`` when nothing could be read."""
    target = Path(path)
    try:
        size = target.stat().st_size
    except OSError:
        return None
    if size <= 0:
        return None

    ext = (extension or extension_of(target.name)).lower()
    try:
        with open(target, "rb") as handle:
            if ext in (".mkv", ".webm"):
                return _parse_mkv(handle, size)
            if ext == ".avi":
                return _parse_avi(handle, size)
            if ext in (".mp4", ".m4v", ".mov", ".3gp"):
                meta = _parse_mp4(handle, size)
                if meta is not None:
                    return meta
            # Fall back to trying ISO BMFF, then EBML, then RIFF detection.
            meta = _parse_mp4(handle, size)
            if meta is not None:
                return meta
            handle.seek(0)
            if handle.read(4) == b"\x1a\x45\xdf\xa3":
                return _parse_mkv(handle, size)
            handle.seek(0)
            return _parse_avi(handle, size)
    except (OSError, ValueError, struct.error) as exc:
        logger.debug("video probe failed for %s: %s", target.name, exc)
        return None
