import os
import stat
import sys

from conftest import make_media

from ripaudit.probe import parse_ffprobe, run_ffprobe


def test_parse_container_duration_and_streams():
    payload = {"format": {"duration": "7265.5"}, "streams": [
        {"codec_type": "video", "codec_name": "hevc", "width": 3840, "height": 2160, "disposition": {"default": 1},
         "tags": {"DURATION": "02:01:05.500000000"}},
        {"codec_type": "video", "codec_name": "mjpeg", "disposition": {"attached_pic": 1}},
        {"codec_type": "audio", "tags": {"language": "eng"}},
        {"codec_type": "audio", "tags": {"language": "fre"}},
        {"codec_type": "subtitle", "tags": {"language": "eng"}}]}
    r = parse_ffprobe(payload)
    assert r.status == "ok"
    assert r.duration_seconds == 7265.5 and r.duration_source == "container"
    assert (r.video_codec, r.width, r.video_stream_count) == ("hevc", 3840, 1)
    assert r.audio_languages == ["eng", "fre"] and r.subtitle_count == 1


def test_missing_container_duration_falls_back_to_video_tag():
    r = parse_ffprobe({"format": {}, "streams": [{"codec_type": "video", "tags": {"DURATION": "01:30:00.000"}}]})
    assert r.duration_seconds == 5400 and r.duration_source == "video_stream"
    assert r.status == "ok_with_warnings"


def test_invalid_duration():
    r = parse_ffprobe({"format": {"duration": "N/A"}, "streams": [{"codec_type": "video"}]})
    assert r.status == "invalid_duration"


def test_inconsistent_durations_warn():
    r = parse_ffprobe({"format": {"duration": "7200"}, "streams": [{"codec_type": "video", "duration": "5000"}]})
    assert r.status == "ok_with_warnings"
    assert any("differs" in w for w in r.warnings)


def test_probe_warnings_from_stderr_are_kept_and_bounded():
    r = parse_ffprobe({"format": {"duration": "60"}, "streams": [{"codec_type": "video"}]}, "x" * 10000)
    assert r.status == "ok_with_warnings"
    assert len(r.diagnostics) == 4000


def test_real_ffprobe_on_synthetic_file(tmp_path):
    f = make_media(tmp_path / "-starts with dash $(touch pwned).mkv", 3)
    r = run_ffprobe(f, timeout=30)
    assert r.status in ("ok", "ok_with_warnings")
    assert 2.5 < r.duration_seconds < 3.6
    assert r.audio_languages == ["eng"]
    assert not (tmp_path / "pwned").exists()


def test_ffprobe_timeout(tmp_path):
    fake = tmp_path / "slowprobe"
    fake.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(5)\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    media = tmp_path / "m.mkv"
    media.write_bytes(b"0")
    r = run_ffprobe(media, timeout=0.5, binary=str(fake))
    assert r.status == "timeout"


def test_file_changed_during_probe(tmp_path):
    media = tmp_path / "m.mkv"
    media.write_bytes(b"0")
    fake = tmp_path / "touchprobe"
    fake.write_text(f"#!{sys.executable}\nimport sys\nopen(sys.argv[-1][5:], 'ab').write(b'more')\nprint('{{}}')\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    assert run_ffprobe(media, timeout=10, binary=str(fake)).status == "changed"


def test_unreadable_file(tmp_path):
    assert run_ffprobe(tmp_path / "nope.mkv", timeout=5).status == "failed"


def test_failed_probe_on_garbage(tmp_path):
    f = tmp_path / "garbage.mkv"
    f.write_bytes(os.urandom(2048))
    assert run_ffprobe(f, timeout=20).status in ("failed", "invalid_duration", "ok_with_warnings")
