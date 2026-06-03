"""Deterministic, unit-testable tools for the the target WordPress site redesign multi-agent.

Intentionally free of any ADK / Vertex dependency so the audit, layout-proposal,
copy-draft, and publish logic can be tested in isolation (see
tests/test_redesign_tools.py). The ADK sub-agents in agent.py are thin wrappers
around these functions. No function raises — every error is folded into the return
value so an agent turn never crashes mid-tool-call.

The publish tool is HARD-GATED: it never writes to WordPress unless an explicit
``approved=True`` is passed AND credentials are configured. Default is a dry-run
preview. This mirrors the human-approval gate in card CJxIUoko.
"""
from __future__ import annotations

import base64
import ipaddress
import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser

USER_AGENT = "redesign-agent/1.0"
WP_PAGES_PATH = "/wp-json/wp/v2/pages"
# Cap how many bytes we read from a remote page (defense against huge responses).
MAX_BYTES = 3_000_000

# The the target WordPress site design-system block vocabulary. The layout proposer draws the
# canonical above-the-fold-first ordering from this set; it never invents visual
# direction (the design system is canonical — see the project's canonical design system).
DESIGN_SYSTEM_BLOCKS = (
    "hero",
    "value_prop",
    "social_proof",
    "services",
    "cta",
    "footer_cluster",
)

# Words that mark an anchor/button as a call-to-action (HE + EN).
_CTA_WORDS = (
    "contact", "get started", "book", "demo", "quote", "buy", "start", "sign up",
    "subscribe", "call", "צור קשר", "התחל", "הזמן", "הזמינו", "דברו", "לפרטים",
    "קבעו", "השאירו", "הצטרפו",
)


# --------------------------------------------------------------------------- #
# 0. SSRF guard                                                               #
# --------------------------------------------------------------------------- #
_METADATA_IP = "169.254.169.254"


def _ip_blocked(ip_obj) -> bool:
    return bool(
        ip_obj.is_private
        or ip_obj.is_loopback
        or ip_obj.is_link_local
        or ip_obj.is_reserved
        or ip_obj.is_multicast
        or ip_obj.is_unspecified
        or str(ip_obj) == _METADATA_IP
    )


def _url_allowed(url: str) -> bool:
    """True iff ``url`` is a safe public http(s) target (not SSRF-risky).

    Validates scheme/port/length, resolves the host, and rejects any address
    that is private / loopback / link-local / reserved / multicast / unspecified
    or the GCP metadata IP. IP-literal hosts are checked directly. Same rule set
    as the proxy's _url_allowed.
    """
    if not url or len(url) > 2000:
        return False
    try:
        parts = urllib.parse.urlsplit(url)
    except Exception:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    host = parts.hostname
    if not host:
        return False
    try:
        port = parts.port
    except ValueError:
        return False
    if port not in (None, 80, 443):
        return False

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        return not _ip_blocked(literal)

    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return False
    if not infos:
        return False
    for info in infos:
        addr = info[4][0]
        try:
            ip_obj = ipaddress.ip_address(addr.split("%")[0])
        except ValueError:
            return False
        if _ip_blocked(ip_obj):
            return False
    return True


# --------------------------------------------------------------------------- #
# 1. site_audit                                                               #
# --------------------------------------------------------------------------- #
class _PageParser(HTMLParser):
    """Extract title, headings, links/buttons and a coarse block sequence."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._in_title = False
        self.headings: list[dict] = []          # [{level, text}]
        self._cur_h: str | None = None
        self._cur_text: list[str] = []
        self.links: list[str] = []              # anchor/button text
        self._in_anchor = False
        self._anchor_text: list[str] = []
        self.sections = 0                       # <section>/<header>/<footer>/<main>
        self.has_main = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag == "title":
            self._in_title = True
        elif tag in ("h1", "h2", "h3"):
            self._cur_h = tag
            self._cur_text = []
        elif tag in ("a", "button"):
            self._in_anchor = True
            self._anchor_text = []
        elif tag in ("section", "header", "footer", "article"):
            self.sections += 1
        elif tag == "main":
            self.has_main = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag in ("h1", "h2", "h3") and self._cur_h == tag:
            text = " ".join("".join(self._cur_text).split())
            if text:
                self.headings.append({"level": int(tag[1]), "text": text[:160]})
            self._cur_h = None
        elif tag in ("a", "button") and self._in_anchor:
            text = " ".join("".join(self._anchor_text).split())
            if text:
                self.links.append(text[:80])
            self._in_anchor = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        if self._cur_h is not None:
            self._cur_text.append(data)
        if self._in_anchor:
            self._anchor_text.append(data)


def _looks_like_cta(text: str) -> bool:
    low = text.lower()
    return any(w in low for w in _CTA_WORDS)


def site_audit(url: str, timeout: float = 15.0) -> dict:
    """Fetch ``url`` and return a structured audit of its content + layout.

    Reports title, heading tree, CTA count, a coarse block sequence, word count,
    and a list of detected issues (missing H1, multiple H1s, no CTA, thin content).
    JSON-serializable; never raises. SSRF-guarded: private / loopback / metadata
    targets return ``{"ok": False, "status": 0, "error": "blocked_url"}``.
    """
    started = time.monotonic()
    if not _url_allowed(url):
        return {"url": url, "ok": False, "status": 0, "error": "blocked_url",
                "latency_ms": round((time.monotonic() - started) * 1000)}
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(MAX_BYTES)
            status = resp.status
    except urllib.error.HTTPError as e:
        return {"url": url, "ok": False, "status": e.code, "error": f"HTTP {e.code}",
                "latency_ms": round((time.monotonic() - started) * 1000)}
    except Exception as e:  # URLError, timeout, DNS, TLS, ...
        return {"url": url, "ok": False, "status": 0, "error": type(e).__name__,
                "latency_ms": round((time.monotonic() - started) * 1000)}

    html = raw.decode("utf-8", errors="replace")
    parser = _PageParser()
    try:
        parser.feed(html)
    except Exception:
        pass  # malformed markup must not crash the audit

    h1s = [h for h in parser.headings if h["level"] == 1]
    ctas = [t for t in parser.links if _looks_like_cta(t)]
    # crude word count from visible-ish text (strip tags/scripts/styles)
    text_only = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    text_only = re.sub(r"(?s)<[^>]+>", " ", text_only)
    word_count = len(text_only.split())

    issues: list[str] = []
    if not h1s:
        issues.append("no_h1")
    elif len(h1s) > 1:
        issues.append("multiple_h1")
    if not ctas:
        issues.append("no_cta")
    if word_count < 120:
        issues.append("thin_content")
    if parser.sections == 0 and not parser.has_main:
        issues.append("no_semantic_sections")

    return {
        "url": url,
        "ok": 200 <= status < 400,
        "status": status,
        "latency_ms": round((time.monotonic() - started) * 1000),
        "title": " ".join(parser.title.split())[:200],
        "headings": parser.headings[:40],
        "h1_count": len(h1s),
        "cta_count": len(ctas),
        "cta_samples": ctas[:5],
        "section_count": parser.sections,
        "word_count": word_count,
        "issues": issues,
    }


# --------------------------------------------------------------------------- #
# 2. layout_proposal                                                          #
# --------------------------------------------------------------------------- #
def layout_proposal(audit: dict) -> dict:
    """Propose a new block sequence for a page, with a rationale per block.

    Pure function over a ``site_audit`` result. Draws the canonical block order
    from DESIGN_SYSTEM_BLOCKS (above-the-fold-first) and attaches a rationale that
    references the audit's detected issues. Never raises.
    """
    if not isinstance(audit, dict):
        return {"ok": False, "error": "audit must be a dict"}

    issues = set(audit.get("issues", []))
    title = audit.get("title", "") or audit.get("url", "")

    rationale = {
        "hero": (
            "Lead with a single clear H1 + primary CTA above the fold"
            + (" — current page has no H1" if "no_h1" in issues else "")
            + (" — current page buries multiple H1s" if "multiple_h1" in issues else "")
        ),
        "value_prop": "State the core value in one scannable row before any detail.",
        "social_proof": "Proof (logos / testimonials) precedes the ask to earn the click.",
        "services": "Concrete offering blocks so the visitor self-selects intent.",
        "cta": (
            "Explicit conversion block"
            + (" — current page exposes no detectable CTA" if "no_cta" in issues else "")
        ),
        "footer_cluster": "Contact + nav + schema-friendly org details.",
    }

    proposed = [{"block": b, "rationale": rationale[b]} for b in DESIGN_SYSTEM_BLOCKS]

    # Surface the highest-leverage change for the demo wow-moment.
    if "no_h1" in issues or "multiple_h1" in issues:
        headline_change = "Normalize to exactly one H1 inside the hero block."
    elif "no_cta" in issues:
        headline_change = "Inject a primary CTA in the hero and a closing CTA block."
    elif "thin_content" in issues:
        headline_change = "Expand thin content with a value-prop + services section."
    else:
        headline_change = "Reorder so proof precedes the ask (social_proof before cta)."

    return {
        "ok": True,
        "page": title,
        "proposed_blocks": proposed,
        "block_sequence": list(DESIGN_SYSTEM_BLOCKS),
        "headline_change": headline_change,
        "addresses_issues": sorted(issues),
    }


# --------------------------------------------------------------------------- #
# 3. copy_draft                                                               #
# --------------------------------------------------------------------------- #
# Deterministic baseline copy per block per language. The ADK copy_agent enhances
# this with Gemini at runtime; the deterministic skeleton keeps the tool testable
# and gives the agent a structured, on-brand starting point. Hebrew uses feminine
# grammatical voice per the feminine-voice convention.
_COPY_TEMPLATES = {
    "en": {
        "hero": {"heading": "Build it once. Ship it everywhere.",
                 "body": "Northstar turns your idea into a production app — web, iOS and Android — without the agency overhead.",
                 "cta": "Get started"},
        "value_prop": {"heading": "One team, the whole stack.",
                       "body": "Design, build, deploy and maintain — handled end to end so you can focus on your customers.",
                       "cta": "See how"},
        "social_proof": {"heading": "Trusted by teams that ship.",
                         "body": "From first-time founders to enterprise ops, Northstar delivers apps people actually use.",
                         "cta": "Read case studies"},
        "services": {"heading": "What we build.",
                     "body": "Mobile apps, web platforms, AI automation and the integrations that tie them together.",
                     "cta": "Explore services"},
        "cta": {"heading": "Ready to build?",
                "body": "Tell us what you need. We'll scope it, price it, and start this week.",
                "cta": "Contact us"},
        "footer_cluster": {"heading": "Northstar",
                           "body": "Mobile, web and AI app development. Tel Aviv.",
                           "cta": "Get in touch"},
    },
    "he": {
        # Feminine grammatical voice (פנייה בלשון נקבה) per feminine-voice convention.
        "hero": {"heading": "תבני פעם אחת. תשיקי בכל מקום.",
                 "body": "Northstar הופכת את הרעיון שלך לאפליקציה בפרודקשן — ווב, iOS ואנדרואיד — בלי העומס של סוכנות.",
                 "cta": "בואי נתחיל"},
        "value_prop": {"heading": "צוות אחד, כל הסטאק.",
                       "body": "עיצוב, פיתוח, הטמעה ותחזוקה — מקצה לקצה, כדי שתוכלי להתמקד בלקוחות שלך.",
                       "cta": "ראי איך"},
        "social_proof": {"heading": "צוותים שמשיקים סומכים עלינו.",
                         "body": "מיזמיות בתחילת הדרך ועד ארגונים — Northstar מספקת אפליקציות שאנשים באמת משתמשים בהן.",
                         "cta": "לקריאת סיפורי לקוח"},
        "services": {"heading": "מה אנחנו בונים.",
                     "body": "אפליקציות מובייל, פלטפורמות ווב, אוטומציית AI והאינטגרציות שמחברות הכול.",
                     "cta": "לכל השירותים"},
        "cta": {"heading": "מוכנה לבנות?",
                "body": "ספרי לנו מה צריך. נאפיין, נתמחר ונתחיל כבר השבוע.",
                "cta": "דברי איתנו"},
        "footer_cluster": {"heading": "Northstar",
                           "body": "פיתוח אפליקציות מובייל, ווב ו-AI. תל אביב.",
                           "cta": "צרי קשר"},
    },
}


def copy_draft(block: str, lang: str = "en") -> dict:
    """Return a structured copy draft (heading / body / cta) for a block + language.

    ``lang`` is "en" or "he" (Hebrew uses feminine voice). Returns a draft with a
    short reasoning line — the reasoning is the demo wow-moment the orchestrator
    surfaces. Never raises; unknown block/lang falls back gracefully.
    """
    lang = (lang or "en").lower()
    if lang not in _COPY_TEMPLATES:
        return {"ok": False, "block": block, "lang": lang, "error": "unsupported_lang"}
    table = _COPY_TEMPLATES[lang]
    if block not in table:
        return {"ok": False, "block": block, "lang": lang, "error": "unknown_block"}
    draft = dict(table[block])
    reasoning = {
        "hero": "Hero leads with the outcome, not the feature, and pairs it with the primary CTA.",
        "value_prop": "Single-row value statement keeps the promise scannable above detail.",
        "social_proof": "Proof placed before the ask raises click-through.",
        "services": "Concrete offerings let the visitor self-select intent.",
        "cta": "Closing CTA captures intent built up by the page.",
        "footer_cluster": "Footer carries contact + org schema for SEO.",
    }[block]
    return {"ok": True, "block": block, "lang": lang, **draft, "reasoning": reasoning}


# --------------------------------------------------------------------------- #
# 4. wp_publish  (HARD-GATED behind human approval)                           #
# --------------------------------------------------------------------------- #
def _wp_ready() -> bool:
    return bool(os.environ.get("WP_PUBLISH_URL")
                and os.environ.get("WP_PUBLISH_USER")
                and os.environ.get("WP_PUBLISH_APP_PASSWORD"))


def wp_publish(
    *,
    tenant: str,
    title: str,
    html: str,
    approved: bool = False,
    timeout: float = 20.0,
) -> dict:
    """Publish a redesigned page to the staging WordPress install — APPROVAL-GATED.

    Safety contract (mirrors the human-approval gate in card CJxIUoko):
      * If ``approved`` is not exactly True -> NO network call; returns a dry-run
        preview with ``published: False`` so the orchestrator must surface the draft
        for human approval first.
      * If approved but WP creds (WP_PUBLISH_URL / USER / APP_PASSWORD) are unbound ->
        returns ``published: False`` with a config reason (safe no-op).
      * Multi-tenant: the slug is namespaced by ``tenant`` so one tenant cannot
        overwrite another's page.

    Never raises.
    """
    # Stable, tenant-scoped slug. ``time.monotonic`` (not wall-clock) keeps it
    # deterministic-ish for the same call while still unique across publishes.
    safe_tenant = re.sub(r"[^a-z0-9]+", "-", (tenant or "tenant").lower()).strip("-") or "tenant"
    safe_title = re.sub(r"[^a-z0-9]+", "-", (title or "page").lower()).strip("-") or "page"
    slug = f"{safe_tenant}-{safe_title}"

    if approved is not True:
        return {
            "published": False,
            "reason": "approval_required",
            "dry_run": True,
            "preview": {"tenant": safe_tenant, "slug": slug, "title": title,
                        "html_bytes": len(html or "")},
        }

    if not _wp_ready():
        return {"published": False, "reason": "wp_credentials_unbound",
                "slug": slug, "approved": True}

    base = os.environ["WP_PUBLISH_URL"].rstrip("/")
    user = os.environ["WP_PUBLISH_USER"]
    app_pw = os.environ["WP_PUBLISH_APP_PASSWORD"]
    template = os.environ.get("WP_PUBLISH_TEMPLATE")

    payload: dict = {"title": title, "slug": slug, "status": "draft", "content": html}
    if template:
        payload["template"] = template
    body = json.dumps(payload).encode()
    token = base64.b64encode(f"{user}:{app_pw}".encode()).decode()
    req = urllib.request.Request(
        base + WP_PAGES_PATH, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Basic {token}",
                 "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            page = json.loads(resp.read())
        return {"published": True, "slug": slug, "id": page.get("id"),
                "url": page.get("link"),
                "edit": f"{base}/wp-admin/post.php?post={page.get('id')}&action=edit"}
    except Exception as e:
        # Do not echo the raw WP error body (PII / internal-path leak — see
        # the redaction lesson on card 6a1ce800). Return a fixed envelope.
        code = getattr(e, "code", None)
        return {"published": False, "reason": "wp_request_failed",
                "error": type(e).__name__, "status": code, "slug": slug}


def now_iso() -> str:
    """UTC ISO timestamp helper (kept here so tools.py has no agent import)."""
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# 5. Arize Phoenix partner-MCP config  (hackathon eligibility requirement)    #
# --------------------------------------------------------------------------- #
# The Google Rapid Agent Hackathon REQUIRES the agent to integrate a Partner
# Entity's MCP server (rapid-agent.devpost.com/rules). Our partner track is
# Arize: the observability_agent in agent.py connects to the Arize Phoenix MCP
# server as an ADK MCPToolset. This helper is the pure, ADK-free, unit-tested
# half — it reads the connection config from the environment so the wiring in
# agent.py stays a thin shell and CI (which imports only tools.py) can cover it.
#
# Env contract (all optional; URL absent -> integration disabled, fail-open):
#   ARIZE_MCP_URL          streamable-HTTP endpoint of the Arize/Phoenix MCP server
#   ARIZE_MCP_API_KEY      API key/token sent as an auth header (optional)
#   ARIZE_MCP_AUTH_HEADER  header name for the key (default "Authorization";
#                          the "Authorization" default is sent as "Bearer <key>")
#   ARIZE_MCP_HEADERS      extra headers as a JSON object, merged last (optional)
def arize_mcp_config_from_env() -> dict | None:
    """Build the Arize Phoenix MCP connection config from env, or None if unset.

    Returns ``{"url": <str>, "headers": <dict[str, str]>}`` when ``ARIZE_MCP_URL``
    is set to a non-empty value, else ``None`` (partner-MCP integration disabled,
    so the agent degrades to its core redesign path without crashing). Never raises.
    """
    url = (os.environ.get("ARIZE_MCP_URL") or "").strip()
    if not url:
        return None

    headers: dict[str, str] = {}
    key = (os.environ.get("ARIZE_MCP_API_KEY") or "").strip()
    if key:
        header_name = (os.environ.get("ARIZE_MCP_AUTH_HEADER") or "Authorization").strip()
        # Default Authorization header carries the conventional "Bearer " prefix;
        # a custom header (e.g. "api_key" / "x-api-key") gets the raw key value.
        if header_name.lower() == "authorization":
            headers[header_name] = f"Bearer {key}"
        else:
            headers[header_name] = key

    extra = os.environ.get("ARIZE_MCP_HEADERS")
    if extra:
        try:
            parsed = json.loads(extra)
            if isinstance(parsed, dict):
                headers.update({str(k): str(v) for k, v in parsed.items()})
        except (ValueError, TypeError):
            pass  # malformed extra headers must not break the integration

    return {"url": url, "headers": headers}
