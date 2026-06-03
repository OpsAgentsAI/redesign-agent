"""Hermetic unit tests for the redesign multi-agent's deterministic tools.

No network: urlopen AND socket.getaddrinfo are monkeypatched so the suite is
CI-safe and offline. Mirrors tests/test_tools.py conventions.
"""
from __future__ import annotations

import io
import json
import urllib.error

import pytest

from redesign_agents import tools

_SAMPLE_PAGE = b"""<!doctype html><html><head><title>Northstar - App Development</title></head>
<body>
<header><h1>We build apps</h1></header>
<main>
  <section><h2>Our services</h2><p>Mobile, web and AI development for teams that ship.</p>
    <a href="/contact">Contact us</a></section>
  <section><h2>Why Northstar</h2><p>One team, the whole stack, end to end delivery.</p></section>
</main>
<footer><a href="/about">About</a></footer>
</body></html>"""

_THIN_NO_H1 = b"<html><head><title>Thin</title></head><body><h2>hi</h2><p>short</p></body></html>"


class _FakeResp:
    def __init__(self, status=200, body=b"<html><title>x</title></html>"):
        self.status = status
        self._body = body

    def read(self, n=-1):
        return self._body if n in (-1, None) else self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _allow_dns(monkeypatch, addr="93.184.216.34"):
    """Make socket.getaddrinfo resolve every host to a public IP (hermetic)."""
    monkeypatch.setattr(
        tools.socket, "getaddrinfo",
        lambda host, *a, **k: [(2, 1, 6, "", (addr, 0))],
    )


# --- _url_allowed (SSRF guard) -------------------------------------------- #
def test_url_allowed_public(monkeypatch):
    _allow_dns(monkeypatch)
    assert tools._url_allowed("https://example.com/") is True
    assert tools._url_allowed("http://example.com/path") is True


def test_url_allowed_rejects_metadata_and_private():
    # IP literals are checked directly — no DNS needed.
    assert tools._url_allowed("http://169.254.169.254/") is False
    assert tools._url_allowed("http://127.0.0.1/") is False
    assert tools._url_allowed("http://10.0.0.5/") is False
    assert tools._url_allowed("http://192.168.1.1/") is False


def test_url_allowed_rejects_bad_scheme_and_length():
    assert tools._url_allowed("ftp://example.com/") is False
    assert tools._url_allowed("file:///etc/passwd") is False
    assert tools._url_allowed("") is False
    assert tools._url_allowed("https://example.com/" + "a" * 2100) is False


# --- site_audit ----------------------------------------------------------- #
def test_site_audit_parses_structure(monkeypatch):
    _allow_dns(monkeypatch)
    monkeypatch.setattr(tools.urllib.request, "urlopen",
                        lambda *a, **k: _FakeResp(200, _SAMPLE_PAGE))
    r = tools.site_audit("https://example.com/")
    assert r["ok"] is True and r["status"] == 200
    assert r["title"] == "Northstar - App Development"
    assert r["h1_count"] == 1
    assert r["cta_count"] >= 1 and "Contact us" in r["cta_samples"]
    assert r["section_count"] >= 2
    assert "no_h1" not in r["issues"] and "no_cta" not in r["issues"]


def test_site_audit_flags_issues(monkeypatch):
    _allow_dns(monkeypatch)
    monkeypatch.setattr(tools.urllib.request, "urlopen",
                        lambda *a, **k: _FakeResp(200, _THIN_NO_H1))
    r = tools.site_audit("https://example.com/thin")
    assert "no_h1" in r["issues"]
    assert "no_cta" in r["issues"]
    assert "thin_content" in r["issues"]


def test_site_audit_blocks_metadata_ip(monkeypatch):
    # SSRF guard fires before any network call — even if urlopen were reachable.
    called = {"n": 0}
    monkeypatch.setattr(tools.urllib.request, "urlopen",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    r = tools.site_audit("http://169.254.169.254/")
    assert r["ok"] is False and r["status"] == 0 and r["error"] == "blocked_url"
    assert called["n"] == 0  # never touched the network


def test_site_audit_blocks_localhost(monkeypatch):
    # localhost resolves to a loopback address -> blocked. getaddrinfo returns
    # 127.0.0.1 so the resolution path (not just IP-literal path) is exercised.
    monkeypatch.setattr(
        tools.socket, "getaddrinfo",
        lambda host, *a, **k: [(2, 1, 6, "", ("127.0.0.1", 0))],
    )
    called = {"n": 0}
    monkeypatch.setattr(tools.urllib.request, "urlopen",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    r = tools.site_audit("http://localhost/")
    assert r["ok"] is False and r["error"] == "blocked_url"
    assert called["n"] == 0


def test_site_audit_http_error(monkeypatch):
    _allow_dns(monkeypatch)

    def boom(*a, **k):
        raise urllib.error.HTTPError("u", 503, "down", {}, io.BytesIO(b""))
    monkeypatch.setattr(tools.urllib.request, "urlopen", boom)
    r = tools.site_audit("https://example.com/")
    assert r["ok"] is False and r["status"] == 503 and r["error"] == "HTTP 503"


def test_site_audit_network_error(monkeypatch):
    _allow_dns(monkeypatch)

    def boom(*a, **k):
        raise urllib.error.URLError("no route")
    monkeypatch.setattr(tools.urllib.request, "urlopen", boom)
    r = tools.site_audit("https://nope.invalid/")
    assert r["ok"] is False and r["status"] == 0 and r["error"] == "URLError"


# --- layout_proposal ------------------------------------------------------ #
def test_layout_proposal_orders_blocks_and_addresses_issues():
    audit = {"title": "Home", "issues": ["no_h1", "no_cta"], "url": "https://x/"}
    p = tools.layout_proposal(audit)
    assert p["ok"] is True
    assert p["block_sequence"][0] == "hero"
    assert p["block_sequence"] == list(tools.DESIGN_SYSTEM_BLOCKS)
    assert {b["block"] for b in p["proposed_blocks"]} == set(tools.DESIGN_SYSTEM_BLOCKS)
    assert "no_h1" in p["addresses_issues"]
    assert "H1" in p["headline_change"]


def test_layout_proposal_rejects_non_dict():
    assert tools.layout_proposal("nope")["ok"] is False


# --- copy_draft ----------------------------------------------------------- #
def test_copy_draft_hebrew_feminine_and_english():
    he = tools.copy_draft("hero", "he")
    assert he["ok"] is True and he["lang"] == "he"
    # feminine imperative marker present in the HE hero CTA/heading
    assert "בואי" in he["cta"] or "תבני" in he["heading"]
    assert he["reasoning"]
    en = tools.copy_draft("cta", "en")
    assert en["ok"] is True and en["cta"] == "Contact us"


def test_copy_draft_unknown_block_and_lang():
    assert tools.copy_draft("nope", "en")["ok"] is False
    assert tools.copy_draft("hero", "fr")["ok"] is False


# --- wp_publish (approval gate) ------------------------------------------- #
def test_wp_publish_blocks_without_approval(monkeypatch):
    # Even with creds present, no approval => dry-run, no network.
    monkeypatch.setenv("WP_PUBLISH_URL", "https://wp.example")
    monkeypatch.setenv("WP_PUBLISH_USER", "u")
    monkeypatch.setenv("WP_PUBLISH_APP_PASSWORD", "p")
    called = {"n": 0}
    monkeypatch.setattr(tools.urllib.request, "urlopen",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    r = tools.wp_publish(tenant="acme", title="Home", html="<p>hi</p>")
    assert r["published"] is False and r["reason"] == "approval_required"
    assert r["dry_run"] is True and r["preview"]["slug"] == "acme-home"
    assert called["n"] == 0  # never touched the network


def test_wp_publish_approved_but_unbound(monkeypatch):
    for v in ("WP_PUBLISH_URL", "WP_PUBLISH_USER", "WP_PUBLISH_APP_PASSWORD"):
        monkeypatch.delenv(v, raising=False)
    r = tools.wp_publish(tenant="acme", title="Home", html="<p>hi</p>", approved=True)
    assert r["published"] is False and r["reason"] == "wp_credentials_unbound"


def test_wp_publish_approved_posts(monkeypatch):
    monkeypatch.setenv("WP_PUBLISH_URL", "https://wp.example")
    monkeypatch.setenv("WP_PUBLISH_USER", "u")
    monkeypatch.setenv("WP_PUBLISH_APP_PASSWORD", "p")
    captured = {}

    def fake_urlopen(req, timeout=20.0):
        captured["url"] = req.full_url
        captured["data"] = req.data
        captured["auth"] = req.headers.get("Authorization")
        return _FakeResp(200, json.dumps({"id": 42, "link": "https://wp.example/acme-home"}).encode())

    monkeypatch.setattr(tools.urllib.request, "urlopen", fake_urlopen)
    r = tools.wp_publish(tenant="ACME Inc", title="Home Page", html="<p>hi</p>", approved=True)
    assert r["published"] is True and r["id"] == 42
    assert r["slug"] == "acme-inc-home-page"  # tenant-namespaced, sanitized
    assert "/wp-json/wp/v2/pages" in captured["url"]
    assert captured["auth"].startswith("Basic ")
    body = json.loads(captured["data"])
    assert body["status"] == "draft" and body["slug"] == "acme-inc-home-page"


def test_wp_publish_request_failure_is_redacted(monkeypatch):
    monkeypatch.setenv("WP_PUBLISH_URL", "https://wp.example")
    monkeypatch.setenv("WP_PUBLISH_USER", "u")
    monkeypatch.setenv("WP_PUBLISH_APP_PASSWORD", "p")

    def boom(*a, **k):
        raise urllib.error.HTTPError("u", 500, "secret-internal-path", {}, io.BytesIO(b"PII"))
    monkeypatch.setattr(tools.urllib.request, "urlopen", boom)
    r = tools.wp_publish(tenant="acme", title="Home", html="x", approved=True)
    assert r["published"] is False and r["reason"] == "wp_request_failed"
    assert r["status"] == 500
    # raw error body must NOT leak
    assert "secret-internal-path" not in json.dumps(r) and "PII" not in json.dumps(r)


# --- arize_mcp_config_from_env (partner-MCP eligibility) ------------------- #
def test_arize_mcp_config_none_when_url_unset(monkeypatch):
    for v in ("ARIZE_MCP_URL", "ARIZE_MCP_API_KEY", "ARIZE_MCP_AUTH_HEADER", "ARIZE_MCP_HEADERS"):
        monkeypatch.delenv(v, raising=False)
    assert tools.arize_mcp_config_from_env() is None


def test_arize_mcp_config_url_only(monkeypatch):
    for v in ("ARIZE_MCP_API_KEY", "ARIZE_MCP_AUTH_HEADER", "ARIZE_MCP_HEADERS"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("ARIZE_MCP_URL", "https://phoenix.example/mcp")
    cfg = tools.arize_mcp_config_from_env()
    assert cfg == {"url": "https://phoenix.example/mcp", "headers": {}}


def test_arize_mcp_config_default_bearer_auth(monkeypatch):
    monkeypatch.delenv("ARIZE_MCP_AUTH_HEADER", raising=False)
    monkeypatch.delenv("ARIZE_MCP_HEADERS", raising=False)
    monkeypatch.setenv("ARIZE_MCP_URL", "https://phoenix.example/mcp")
    monkeypatch.setenv("ARIZE_MCP_API_KEY", "tok123")
    cfg = tools.arize_mcp_config_from_env()
    assert cfg["headers"]["Authorization"] == "Bearer tok123"


def test_arize_mcp_config_custom_header_raw_key(monkeypatch):
    monkeypatch.delenv("ARIZE_MCP_HEADERS", raising=False)
    monkeypatch.setenv("ARIZE_MCP_URL", "https://phoenix.example/mcp")
    monkeypatch.setenv("ARIZE_MCP_API_KEY", "tok123")
    monkeypatch.setenv("ARIZE_MCP_AUTH_HEADER", "api_key")
    cfg = tools.arize_mcp_config_from_env()
    assert cfg["headers"] == {"api_key": "tok123"}


def test_arize_mcp_config_extra_headers_merge_and_bad_json(monkeypatch):
    monkeypatch.setenv("ARIZE_MCP_URL", "https://phoenix.example/mcp")
    monkeypatch.delenv("ARIZE_MCP_API_KEY", raising=False)
    monkeypatch.setenv("ARIZE_MCP_HEADERS", '{"X-Space-Id": "space-7"}')
    assert tools.arize_mcp_config_from_env()["headers"] == {"X-Space-Id": "space-7"}
    # malformed JSON must not raise — extra headers are simply ignored
    monkeypatch.setenv("ARIZE_MCP_HEADERS", "{not json")
    assert tools.arize_mcp_config_from_env()["headers"] == {}
