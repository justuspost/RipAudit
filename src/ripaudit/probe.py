"""FFprobe inspection.

Comparison duration selection (documented in docs/how-it-works.md):
1. The container (format) duration, when present and positive.
2. Otherwise the longest duration of any non-attached-picture video stream,
   taken from ``stream.duration`` or the Matroska ``DURATION`` tag.
3. Otherwise the probe is classified ``invalid_duration``.

If the container and primary video stream durations differ by more than
``INCONSISTENCY_SECONDS``, a warning is recorded but the container duration is kept.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

INCONSISTENCY_SECONDS = 5.0
MAX_DIAGNOSTIC_CHARS = 4000
_TAG_DURATION = re.compile(r"^(\d+):(\d{2}):(\d{2}(?:\.\d+)?)$")


@dataclass
class ProbeResult:
    status: str
    duration_seconds: float | None = None
    duration_source: str | None = None
    container_duration: float | None = None
    video_codec: str | None = None
    width: int | None = None
    height: int | None = None
    video_stream_count: int = 0
    audio_count: int = 0
    audio_languages: list[str] = field(default_factory=list)
    subtitle_count: int = 0
    subtitle_languages: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    diagnostics: str = ""


def _float(value) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _tag_duration(tags: dict | None) -> float | None:
    if not tags:
        return None
    for key, value in tags.items():
        if key.upper() == "DURATION" and isinstance(value, str):
            m = _TAG_DURATION.match(value.strip())
            if m:
                h, mi, s = m.groups()
                return _float(int(h) * 3600 + int(mi) * 60 + float(s))
    return None


def parse_ffprobe(payload: dict, stderr: str = "") -> ProbeResult:
    fmt = payload.get("format") or {}
    streams = payload.get("streams") or []
    result = ProbeResult(status="ok")
    result.container_duration = _float(fmt.get("duration"))

    videos = [s for s in streams if s.get("codec_type") == "video"
              and not (s.get("disposition") or {}).get("attached_pic")]
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    subs = [s for s in streams if s.get("codec_type") == "subtitle"]
    result.video_stream_count = len(videos)
    result.audio_count = len(audios)
    result.subtitle_count = len(subs)
    result.audio_languages = [(s.get("tags") or {}).get("language", "und") for s in audios]
    result.subtitle_languages = [(s.get("tags") or {}).get("language", "und") for s in subs]

    video_durations: list[float] = []
    if videos:
        primary = next((s for s in videos if (s.get("disposition") or {}).get("default")), videos[0])
        result.video_codec = primary.get("codec_name")
        result.width = primary.get("width")
        result.height = primary.get("height")
        for s in videos:
            d = _float(s.get("duration")) or _tag_duration(s.get("tags"))
            if d:
                video_durations.append(d)
        if len(videos) > 1:
            result.warnings.append(f"{len(videos)} video streams present; codec/resolution from the default stream.")
    else:
        result.warnings.append("No video stream found.")

    if result.container_duration:
        result.duration_seconds = result.container_duration
        result.duration_source = "container"
        if video_durations:
            longest = max(video_durations)
            if abs(longest - result.container_duration) > INCONSISTENCY_SECONDS:
                result.warnings.append(
                    f"Container duration {result.container_duration:.1f}s differs from video stream "
                    f"duration {longest:.1f}s."
                )
    elif video_durations:
        result.duration_seconds = max(video_durations)
        result.duration_source = "video_stream"
        result.warnings.append("Container duration missing; used longest video stream duration.")
    else:
        result.status = "invalid_duration"

    if stderr.strip():
        result.warnings.append("FFprobe reported warnings; see diagnostics.")
        result.diagnostics = stderr.strip()[:MAX_DIAGNOSTIC_CHARS]
    if result.status == "ok" and result.warnings:
        result.status = "ok_with_warnings"
    return result


def ffprobe_binary() -> str | None:
    return os.environ.get("RIPAUDIT_FFPROBE") or shutil.which("ffprobe")


def run_ffprobe(path: Path, timeout: float, binary: str | None = None) -> ProbeResult:
    """Run ffprobe with an argument list (never a shell).

    The path is passed with the ``file:`` protocol prefix so names beginning with
    ``-`` or containing ``:`` cannot be interpreted as options or protocols.
    """
    exe = binary or ffprobe_binary()
    if not exe:
        return ProbeResult(status="failed", diagnostics="ffprobe executable not found.")
    before = _signature(path)
    if before is None:
        return ProbeResult(status="failed", diagnostics="File is not readable or no longer exists.")
    args = [exe, "-v", "warning", "-hide_banner", "-print_format", "json",
            "-show_format", "-show_streams", "-i", "file:" + str(path)]
    try:
        proc = subprocess.run(args, capture_output=True, timeout=timeout, check=False,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return ProbeResult(status="timeout", diagnostics=f"ffprobe exceeded {timeout:.0f}s timeout.")
    except OSError as exc:
        return ProbeResult(status="failed", diagnostics=f"Could not start ffprobe: {exc}")
    stderr = proc.stderr.decode("utf-8", "replace")
    after = _signature(path)
    if after != before:
        return ProbeResult(status="changed", diagnostics="File changed or disappeared during inspection.")
    if proc.returncode != 0:
        return ProbeResult(status="failed",
                           diagnostics=(stderr.strip() or f"exit code {proc.returncode}")[:MAX_DIAGNOSTIC_CHARS])
    try:
        payload = json.loads(proc.stdout.decode("utf-8", "replace") or "{}")
    except json.JSONDecodeError:
        return ProbeResult(status="failed", diagnostics="ffprobe returned invalid JSON.")
    return parse_ffprobe(payload, stderr)


def _signature(path: Path) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_size, st.st_mtime_ns
