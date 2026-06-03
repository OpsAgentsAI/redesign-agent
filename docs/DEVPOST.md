# Redesign Agent — Devpost submission

**Hackathon:** Google Cloud Rapid Agent Hackathon · **Partner track:** Arize (observability)

## Elevator pitch
An open-source ADK multi-agent that rebuilds a real web page end-to-end — auditing the page, proposing
a new layout, and drafting bilingual (Hebrew + English) copy with the reasoning shown per block — then
**publishing only after a human approves.** Google-native (Gemini on Vertex AI Agent Engine), with the
**Arize Phoenix MCP server** wired in for per-run observability.

## Business case
SMBs can't afford a standing web team, so their sites rot. An agency rebuild is $10K–$40K and weeks of
back-and-forth. This turns it into a supervised agent run: the agent does the work, a human keeps control
of one decision — approve or not. The same system is a reusable agency template (works on any site).

## Technical
A Google-native ADK supervisor-worker topology: `website_redesign_orchestrator` delegates to
`site_audit_agent → layout_agent → copy_agent → publish_agent`, plus an `observability_agent` that
integrates the **Arize Phoenix MCP server** (real MCP tool calls, not chat). Every tool's deterministic
core is pure-Python and unit-tested; the agents are thin wrappers. Stack: Vertex AI Agent Engine ·
Gemini 2.5 Flash · ADK · Arize Phoenix MCP · Cloud Run · Firebase Hosting.

## Innovation
- **Two safety properties enforced in code, not the prompt:** a human-approval gate before any WordPress
  write (`approved=True` required; default dry-run) and an SSRF guard before any fetch/LLM call.
- **Per-block reasoning surfaced as a first-class output** — *why* this hero, this CTA, this order.
- **Bilingual by construction** — Hebrew (feminine voice) + English drafted together per block.

## Demo
A 3-minute walkthrough: a redesign request for a live page → the multi-agent run with per-block Gemini
reasoning on screen → the human-approval gate ("⏸ Awaiting your approval — stopped before publishing").

## Links
- Repo: `OpsAgentsAI/redesign-agent` (public, Apache-2.0)
- Hosted demo: see repo About
- Arize MCP wiring: [`docs/ARIZE_MCP.md`](ARIZE_MCP.md)
