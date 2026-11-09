"""Security regression tests for the pure helpers in bot.py.

No network or real Telegram token is needed: a dummy token is injected
before the module is imported (TeleBot does not contact Telegram on init).
"""
import importlib
import os
import socket
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load_bot(monkeypatch, allowed="111,222"):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:dummy-token-for-tests")
    monkeypatch.setenv("ALLOWED_USER_IDS", allowed)
    monkeypatch.setenv("ENV_FILE", os.devnull)
    monkeypatch.setenv("DOWNLOAD_DIR", os.path.join(os.path.dirname(__file__), ".tmp_downloads"))
    sys.modules.pop("bot", None)
    return importlib.import_module("bot")


def test_allow_list_accepts_only_listed_users(monkeypatch):
    bot = load_bot(monkeypatch)
    assert bot.is_authorized(111)
    assert bot.is_authorized(222)
    assert not bot.is_authorized(333)


def test_empty_allow_list_fails_closed(monkeypatch):
    bot = load_bot(monkeypatch, allowed="")
    assert not bot.is_authorized(111)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/x",
        "http://localhost/admin",
        "http://127.0.0.1:3000",
        "http://169.254.169.254/latest/meta-data",
        "http://metadata.google.internal/",
        "http://printer.local/",
        "http://[::1]/",
        "http:///nohost",
    ],
)
def test_ssrf_blocks_internal_targets(monkeypatch, url):
    bot = load_bot(monkeypatch)
    safe, _ = bot.is_safe_url(url)
    assert not safe


def test_ssrf_blocks_host_resolving_to_private_ip(monkeypatch):
    bot = load_bot(monkeypatch)
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, 0, 0, "", ("10.0.0.5", 0))]
    )
    assert not bot.is_safe_url("https://evil.example.com/video")[0]


def test_ssrf_blocks_if_any_resolved_address_is_private(monkeypatch):
    bot = load_bot(monkeypatch)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [
            (socket.AF_INET, 0, 0, "", ("93.184.216.34", 0)),
            (socket.AF_INET6, 0, 0, "", ("::1", 0, 0, 0)),
        ],
    )
    assert not bot.is_safe_url("https://mixed.example.com/video")[0]


def test_ssrf_allows_public_host(monkeypatch):
    bot = load_bot(monkeypatch)
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, 0, 0, "", ("93.184.216.34", 0))]
    )
    assert bot.is_safe_url("https://example.com/video")[0]


def test_html_escape_helper_neutralizes_markup(monkeypatch):
    bot = load_bot(monkeypatch)
    assert bot.esc("<b onclick=x>&</b>") == "&lt;b onclick=x&gt;&amp;&lt;/b&gt;"
