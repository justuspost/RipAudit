import json
import logging

from ripaudit.config import Settings
from ripaudit.security import (
    csv_safe,
    hash_password,
    redact,
    resolve_within,
    validate_service_url,
    verify_password,
)


def test_password_hashing():
    h = hash_password("correct horse battery")
    assert h.startswith("scrypt$") and "correct" not in h
    assert verify_password("correct horse battery", h)
    assert not verify_password("wrong", h)
    assert hash_password("same") != hash_password("same")


def test_redaction():
    text = "GET http://plex:32400/library?X-Plex-Token=abcd1234 Authorization: Bearer eyJabc api_key=k123 mysecretvalue"
    out = redact(text, ["mysecretvalue"])
    for s in ("abcd1234", "eyJabc", "k123", "mysecretvalue"):
        assert s not in out
    assert redact('{"token": "zzzz"}') == '{"token": "[REDACTED]"}'


def test_settings_masking_and_env_secrets(tmp_path):
    s = Settings(tmp_path, environ={"RIPAUDIT_TMDB_CREDENTIAL": "from-env"})
    s.update({"plex": {"token": "stored-plex"}})
    assert s.get()["tmdb"]["credential"] == "from-env"
    m = s.masked()
    assert m["plex"]["token"] == "********" and m["tmdb"]["credential"] == "********"
    # env secret is never written to disk
    assert "from-env" not in (tmp_path / "settings.json").read_text()
    # blank keeps stored secret
    s.update({"plex": {"token": ""}})
    assert s.get()["plex"]["token"] == "stored-plex"
    assert oct((tmp_path / "settings.json").stat().st_mode)[-3:] == "600"


def test_resolve_within(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    (root / "a.mkv").write_bytes(b"x")
    (tmp_path / "secret").write_bytes(b"x")
    (root / "evil.mkv").symlink_to(tmp_path / "secret")
    assert resolve_within(root / "a.mkv", [str(root)])
    assert resolve_within(root / ".." / "secret", [str(root)]) is None
    assert resolve_within(root / "evil.mkv", [str(root)]) is None


def test_url_validation():
    assert validate_service_url("http://192.168.1.5:32400") == []
    assert validate_service_url("file:///etc/passwd")
    assert validate_service_url("http://user:pw@host")
    assert validate_service_url("http://169.254.169.254/latest")
    assert validate_service_url("http://host/?X-Plex-Token=abc")


def test_csv_injection_neutralized():
    assert csv_safe("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert csv_safe("Heat.mkv") == "Heat.mkv"


def test_log_filter_redacts(tmp_path, caplog):
    from ripaudit.web import RedactingFilter
    s = Settings(tmp_path, environ={})
    s.update({"plex": {"token": "supersecret-plex"}})
    logger = logging.getLogger("ripaudit.test")
    logger.addFilter(RedactingFilter(s))
    with caplog.at_level(logging.INFO, logger="ripaudit.test"):
        logger.info("calling plex with %s", "supersecret-plex")
    assert "supersecret-plex" not in caplog.text
    assert json.dumps(s.masked()).count("supersecret") == 0


def test_third_party_logs_cannot_leak_tmdb_key(tmp_path, caplog):
    import httpx

    from ripaudit.integrations.tmdb import TmdbClient
    from ripaudit.web import install_log_redaction
    s = Settings(tmp_path, environ={})
    s.update({"tmdb": {"credential": "leaky-key-999"}})
    install_log_redaction(s)
    client = TmdbClient("leaky-key-999", transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"id": 1, "runtime": 100})))
    with caplog.at_level(logging.DEBUG):
        logging.getLogger().info("url was https://api.themoviedb.org/3/movie/1?api_key=leaky-key-999")
        client.movie(1)
    assert "leaky-key-999" not in caplog.text
