# Redesign Agent — an AI website-redesign agency, as a multi-agent

> Point it at any live web page. A Google-native **ADK supervisor-worker** agent audits the page,
> proposes a new block layout, and drafts **bilingual (Hebrew + English)** copy — narrating its
> reasoning per block — then **halts at a hard human-approval gate** before anything is published.
>
> Open-source agency template. Built on Google's Agent Development Kit, runs on **Vertex AI Agent
> Engine** with **Gemini**, and integrates the **Arize Phoenix MCP server** for agent observability.

**Live demo:** a differentiated "Northstar Studio" demo page drives the deployed agent end-to-end —
audit → layout → bilingual copy → the approval-gate stop. (Hosted URL in the repo's About.)

---

## Why

Small and mid-size businesses can't keep a standing web team, so their sites rot: stale copy, buried
CTAs, no Hebrew/English parity, weak SEO. An agency rebuild costs $10K–$40K and weeks of back-and-forth.
This turns that rebuild into an **autonomous, supervised agent run** — the agent does the legwork while
a human keeps control of exactly one decision: *approve, or not.*

## Architecture

```
website_redesign_orchestrator        (ADK supervisor)
  ├─ site_audit_agent      → fetches a live page; extracts heading tree / CTAs / block
  │                          sequence; flags issues (no H1, no CTA, thin content, …)
  ├─ layout_agent          → proposes a design-system block order (hero → value-prop →
  │                          social-proof → services → CTA → footer) with a per-block rationale
  ├─ copy_agent            → drafts Hebrew (feminine voice) + English copy per block, reasoning shown
  ├─ publish_agent         → pushes APPROVED drafts to a staging WordPress install (WP REST)
  └─ observability_agent   → records each run's quality signals to Arize Phoenix via its MCP server
```

The **deterministic core** of every tool (audit parsing, layout logic, copy templates, SSRF-guarded
publish client) lives in [`redesign_agents/tools.py`](redesign_agents/tools.py) with **no ADK/Vertex
dependency** and is fully unit-tested — the agents in [`redesign_agents/agent.py`](redesign_agents/agent.py)
are thin wrappers so the behaviour is verifiable without a live model.

**Two safety properties, enforced in code (not just the prompt):**
1. **Human-approval gate** — `wp_publish` refuses to write unless `approved=True`; default is a dry-run preview.
2. **SSRF guard** — the audit + proxy reject private / loopback / link-local / metadata targets before any fetch or LLM call.

## Partner MCP integration (Arize Phoenix)

`observability_agent` connects to the **Arize Phoenix MCP server** as an ADK `MCPToolset`
(streamable-HTTP) and makes real tool calls to record/inspect each run. Configure it from the
environment / Secret Manager — see [`docs/ARIZE_MCP.md`](docs/ARIZE_MCP.md). When unset, the agent
degrades gracefully to its core redesign path.

## Quickstart

```bash
pip install -r requirements.txt
python -m pytest tests/ -q          # deterministic tools, hermetic (no network)
```

Deploy the orchestrator to Vertex AI Agent Engine (us-central1):

```bash
pip install "google-cloud-aiplatform[agent_engines]" "google-adk<2"
GOOGLE_GENAI_USE_VERTEXAI=TRUE GOOGLE_CLOUD_PROJECT=<project> \
  GOOGLE_CLOUD_LOCATION=us-central1 python scripts/deploy_agent_engine.py
```

The browsable demo is static (`demo/public/`) + a small abuse-controlled Cloud Run proxy
(`demo/proxy/`) in front of the Agent Engine — preset task + SSRF-validated URL only, never free
prompt text, rate-limited per IP, output-capped.

## Stack

Vertex AI Agent Engine · Gemini 2.5 Flash (Vertex) · Google ADK (`google-adk`) · Arize Phoenix MCP ·
Cloud Run · Firebase Hosting. 100% Google-native model path (no third-party LLM).

## License

[Apache-2.0](LICENSE).
