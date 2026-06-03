"""
redesign-demo-proxy — public, abuse-controlled proxy in front of the live
Vertex AI Agent Engine (reasoningEngines/6353095283278086144, opsagent-prod,
us-central1).

Why a proxy: the Agent Engine is queried with a GCP-authenticated SDK
(google-cloud-aiplatform). We must NOT expose ADC to the browser, and we must
NOT let arbitrary public input reach a billable LLM. So this service:

  * runs with a runtime SA (redesign-demo-proxy@...) that holds aiplatform.user
  * accepts a preset TASK id (fixed prompt template) + a caller-supplied target
    URL that is SSRF-validated BEFORE any LLM call (never free prompt text)
  * caps output length
  * rate-limits per client IP (token bucket)
  * streams the agent's multi-agent reasoning back as Server-Sent Events
  * CORS-allows the demo Hosting origin

GET  /        -> healthz (200 "ok")
GET  /presets -> the allowed preset task list (id + label, HE+EN)
POST /run     -> { "preset": "<id>", "url": "<target>" }
               -> text/event-stream of {type, text, author} JSON events
"""
import ipaddress
import json
import os
import socket
import time
import traceback
import urllib.parse

from flask import Flask, Response, request

import vertexai
from vertexai import agent_engines

PROJECT = os.environ.get("GCP_PROJECT", "opsagent-prod")
LOCATION = os.environ.get("AGENT_LOCATION", "us-central1")
ENGINE_RESOURCE = os.environ.get(
    "AGENT_ENGINE_RESOURCE",
    "projects/523955774086/locations/us-central1/reasoningEngines/6353095283278086144",
)
# Hard cap on streamed characters returned to a public caller.
MAX_OUTPUT_CHARS = int(os.environ.get("MAX_OUTPUT_CHARS", "24000"))
# Default target if the caller omits a URL.
DEFAULT_URL = os.environ.get("DEFAULT_URL", "https://example.com")

# Public callers pick a TASK (fixed prompt template); only the validated URL is
# interpolated. No free prompt text ever reaches the agent.
PRESETS = {
    "audit": {
        "label_en": "Audit page + redesign blocks (HE+EN copy)",
        "label_he": "בדיקת הדף + רצף בלוקים מחדש (עברית+אנגלית)",
        "template": (
            "Audit the page at {url} and propose a redesigned block sequence "
            "with per-block reasoning, plus HE+EN hero copy. Do not publish."
        ),
    },
    "hero": {
        "label_en": "Just the bilingual hero copy (HE + EN)",
        "label_he": "רק קופי להירו דו-לשוני (עברית + אנגלית)",
        "template": (
            "Audit the hero section only of the page at {url} and propose HE+EN "
            "hero copy with per-line reasoning. Do not publish."
        ),
    },
}
DEFAULT_PRESET = "audit"

# --- per-IP token-bucket rate limit (in-memory, best-effort) -------------- #
RATE_MAX = int(os.environ.get("RATE_MAX", "5"))          # runs per window
RATE_WINDOW = float(os.environ.get("RATE_WINDOW", "300"))  # seconds (5 min)
_buckets: dict = {}  # ip -> [tokens, last_refill_ts]


def _rate_ok(ip: str) -> bool:
    now = time.monotonic()
    tokens, last = _buckets.get(ip, (float(RATE_MAX), now))
    # Refill proportionally to elapsed time.
    tokens = min(float(RATE_MAX), tokens + (now - last) * (RATE_MAX / RATE_WINDOW))
    if tokens < 1.0:
        _buckets[ip] = (tokens, now)
        return False
    _buckets[ip] = (tokens - 1.0, now)
    return True


# --- SSRF guard ----------------------------------------------------------- #
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


def _url_allowed(url):
    """Return (ok: bool, reason: str). Rejects SSRF-risky targets.

    Validates scheme/port/length, resolves the host, and rejects any address
    that is private / loopback / link-local / reserved / multicast / unspecified
    or the GCP metadata IP. IP-literal hosts are checked directly.
    """
    if not url or len(url) > 2000:
        return False, "bad_length"
    try:
        parts = urllib.parse.urlsplit(url)
    except Exception:
        return False, "parse_error"
    if parts.scheme not in ("http", "https"):
        return False, "bad_scheme"
    host = parts.hostname
    if not host:
        return False, "no_host"
    try:
        port = parts.port
    except ValueError:
        return False, "bad_port"
    if port not in (None, 80, 443):
        return False, "bad_port"

    # If the host is an IP literal, check it directly.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _ip_blocked(literal):
            return False, "blocked_ip"
        return True, "ok"

    # Otherwise resolve and check every returned address.
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return False, "dns_error"
    if not infos:
        return False, "dns_error"
    for info in infos:
        addr = info[4][0]
        try:
            ip_obj = ipaddress.ip_address(addr.split("%")[0])
        except ValueError:
            return False, "bad_resolved_ip"
        if _ip_blocked(ip_obj):
            return False, "blocked_ip"
    return True, "ok"


app = Flask(__name__)

_remote = None


def get_remote():
    global _remote
    if _remote is None:
        vertexai.init(project=PROJECT, location=LOCATION)
        _remote = agent_engines.get(ENGINE_RESOURCE)
    return _remote


def _cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


@app.route("/", methods=["GET"])
def healthz():
    return _cors(Response("ok", mimetype="text/plain", status=200))


@app.route("/presets", methods=["GET", "OPTIONS"])
def presets():
    if request.method == "OPTIONS":
        return _cors(Response(status=204))
    out = [
        {"id": k, "label_en": v["label_en"], "label_he": v["label_he"]}
        for k, v in PRESETS.items()
    ]
    return _cors(Response(json.dumps(out), mimetype="application/json"))


def _extract_text(event):
    """Pull human-readable text out of an ADK stream event (best effort)."""
    if isinstance(event, str):
        return event
    if not isinstance(event, dict):
        return ""
    # ADK events: content.parts[].text
    content = event.get("content")
    if isinstance(content, dict):
        parts = content.get("parts") or []
        chunks = []
        for p in parts:
            if isinstance(p, dict) and isinstance(p.get("text"), str):
                chunks.append(p["text"])
        if chunks:
            return "".join(chunks)
    for key in ("text", "output", "response"):
        if isinstance(event.get(key), str):
            return event[key]
    return ""


def _author(event):
    if isinstance(event, dict):
        return event.get("author") or event.get("agent") or ""
    return ""


def _client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr or "unknown"


@app.route("/run", methods=["POST", "OPTIONS"])
def run():
    if request.method == "OPTIONS":
        return _cors(Response(status=204))

    body = request.get_json(silent=True) or {}
    preset_id = body.get("preset", DEFAULT_PRESET)
    if preset_id not in PRESETS:
        preset_id = DEFAULT_PRESET
    url = (body.get("url") or DEFAULT_URL).strip()
    ip = _client_ip()

    def gen():
        # Rate limit first — cheapest gate.
        if not _rate_ok(ip):
            yield _sse({"type": "error", "text": "rate_limited"})
            return

        # SSRF validation BEFORE any LLM call (no Gemini spend on a bad URL).
        ok, reason = _url_allowed(url)
        if not ok:
            yield _sse({"type": "error", "text": f"url_not_allowed: {reason}"})
            return

        prompt = PRESETS[preset_id]["template"].format(url=url)

        sent = 0
        yield _sse({"type": "status", "text": f"Querying live Agent Engine ({preset_id})…"})
        try:
            remote = get_remote()
            for event in remote.stream_query(user_id="demo", message=prompt):
                author = _author(event)
                text = _extract_text(event)
                if not text:
                    continue
                if sent >= MAX_OUTPUT_CHARS:
                    yield _sse({"type": "status", "text": "[output cap reached]"})
                    break
                remaining = MAX_OUTPUT_CHARS - sent
                if len(text) > remaining:
                    text = text[:remaining]
                sent += len(text)
                yield _sse({"type": "chunk", "author": author, "text": text})
            yield _sse({"type": "done", "text": ""})
        except Exception as exc:  # surface, don't swallow
            traceback.print_exc()
            yield _sse({"type": "error", "text": f"{type(exc).__name__}: {exc}"})

    resp = Response(gen(), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    return _cors(resp)


def _sse(obj):
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
