"""ADK supervisor + four sub-agents for the WordPress redesign.

This is the headline multi-agent topology of the hackathon REV-5 entry (card
CJxIUoko) and the core of the Technical score: an orchestrator that delegates to
four workers and surfaces Gemini reasoning per block, with a HARD human-approval
gate before any WordPress publish.

    website_redesign_orchestrator (root_agent)
      |- site_audit_agent     -> audits the requested page (tools.site_audit)
      |- layout_agent         -> proposes a new block sequence (tools.layout_proposal)
      |- copy_agent           -> drafts HE + EN copy per block (tools.copy_draft)
      |- publish_agent        -> pushes approved drafts to staging WP (tools.wp_publish)
      `- observability_agent  -> records run quality to Arize Phoenix via its MCP server

Google-native: Gemini on Vertex AI via the ADK (google-adk). No Anthropic.
Set GOOGLE_GENAI_USE_VERTEXAI=TRUE, GOOGLE_CLOUD_PROJECT=opsagent-prod, and
GOOGLE_CLOUD_LOCATION to a credit-covered region before deploy.

Partner-MCP integration (hackathon eligibility — rapid-agent.devpost.com/rules
requires integrating a Partner Entity's MCP server): observability_agent connects
to the Arize Phoenix MCP server as an ADK MCPToolset, configured from the env
(see tools.arize_mcp_config_from_env + docs/ARIZE_MCP.md). When ARIZE_MCP_URL is
unset the toolset is omitted so the core redesign path still runs unchanged.

The deterministic logic lives in tools.py (no ADK/Vertex dependency, fully
unit-tested); these agents are thin wrappers so the supervisor can invoke them
conversationally. CI never imports this module (it installs pytest only).
"""
from __future__ import annotations

import os
import sys

from google.adk.agents import Agent, SequentialAgent

from .tools import arize_mcp_config_from_env, copy_draft, layout_proposal, site_audit, wp_publish

# MCP tool support ships with google-adk but lazily imports the `mcp` package
# (Python >=3.10). Import defensively so a missing `mcp` dep can never break the
# module import / deploy — the observability_agent simply ships without tools.
try:
    from google.adk.tools.mcp_tool import MCPToolset, StreamableHTTPConnectionParams
except Exception as _mcp_exc:  # pragma: no cover - import-surface fallback
    MCPToolset = None
    StreamableHTTPConnectionParams = None
    print("WARNING: ADK MCP toolset import failed (%r); Arize MCP disabled." % _mcp_exc,
          file=sys.stderr)

MODEL = os.environ.get("REDESIGN_MODEL", "gemini-2.5-flash")


def _build_arize_mcp_toolset():
    """Return an MCPToolset bound to the Arize Phoenix MCP server, or None.

    None is returned (with a loud warning) when ARIZE_MCP_URL is unset or the ADK
    MCP surface is unavailable. A loud warning matters: shipping the hackathon
    entry without the partner-MCP integration would make it INELIGIBLE.
    """
    cfg = arize_mcp_config_from_env()
    if cfg is None or MCPToolset is None or StreamableHTTPConnectionParams is None:
        print(
            "WARNING: Arize Phoenix MCP NOT wired (ARIZE_MCP_URL unset or ADK MCP "
            "unavailable). Partner-MCP integration is a hackathon eligibility "
            "requirement — set the secret + redeploy (see docs/ARIZE_MCP.md).",
            file=sys.stderr,
        )
        return None
    return MCPToolset(
        connection_params=StreamableHTTPConnectionParams(
            url=cfg["url"],
            headers=cfg["headers"],
        ),
    )


_arize_toolset = _build_arize_mcp_toolset()

site_audit_agent = Agent(
    name="site_audit_agent",
    model=MODEL,
    description="Audits the requested page (URL provided in the request): structure, headings, CTAs, issues.",
    instruction=(
        "You audit the requested page (the URL is provided in the request). Call "
        "site_audit with that page URL, then report the title, heading tree, CTA "
        "count and the list of detected issues in a compact structured summary. Do "
        "not propose fixes — that is the layout agent's job."
    ),
    tools=[site_audit],
)

layout_agent = Agent(
    name="layout_agent",
    model=MODEL,
    description="Proposes a new block sequence for a page from its audit, with rationale.",
    instruction=(
        "Given a site_audit result, call layout_proposal and present the proposed "
        "block sequence with the rationale for each block and the single headline "
        "change. Consume the design system; never invent visual direction."
    ),
    tools=[layout_proposal],
)

copy_agent = Agent(
    name="copy_agent",
    model=MODEL,
    description="Drafts Hebrew (feminine voice) + English copy per block, with reasoning.",
    instruction=(
        "For each proposed block, call copy_draft for lang='he' and lang='en'. "
        "Hebrew must use feminine grammatical voice. Surface the per-block reasoning "
        "verbatim — that reasoning is the demo's wow moment. You may refine the "
        "deterministic draft, but keep the on-brand heading/body/cta shape."
    ),
    tools=[copy_draft],
)

publish_agent = Agent(
    name="publish_agent",
    model=MODEL,
    description="Publishes approved redesigned pages to the staging WordPress install.",
    instruction=(
        "You publish to staging WordPress ONLY after a human has approved the drafts. "
        "Call wp_publish with approved=True strictly when the orchestrator confirms "
        "human approval was given; otherwise call it without approved (a safe dry-run "
        "preview). Never publish on your own initiative. Report the live URL and the "
        "wp-admin edit link on success."
    ),
    tools=[wp_publish],
)

# Partner-MCP worker (Arize track). Integrates the Arize Phoenix MCP server so the
# orchestrator can record/inspect each redesign run's quality signals through real
# MCP tool calls (not just chat) — the hackathon's "integrate a Partner Entity's MCP
# server + go beyond chat" requirement. Tools are attached only when the MCP server
# is configured; otherwise the agent still exists but its step is a safe no-op.
observability_agent = Agent(
    name="observability_agent",
    model=MODEL,
    description=(
        "Records and inspects the redesign run's quality signals via the Arize "
        "Phoenix MCP server (observability / eval partner track)."
    ),
    instruction=(
        "You are the observability worker. Use the available Arize Phoenix MCP tools "
        "to record this redesign run's quality signals — the page audited, the issues "
        "found, the proposed block sequence, the languages drafted (HE+EN), and "
        "whether the human approved — as an observation / dataset entry in Arize "
        "Phoenix. If no logging tool is exposed, query the most relevant Phoenix "
        "resource (datasets / experiments / prompts) and report what you find. This is "
        "best-effort telemetry: never block or alter the redesign, never publish, and "
        "report a one-line status. If you have no MCP tools available, say so plainly."
    ),
    tools=[_arize_toolset] if _arize_toolset is not None else [],
)

# Deterministic worker pipeline. A SequentialAgent GUARANTEES the full
# audit -> layout -> copy -> observability walk runs in order on every turn — the
# orchestrator no longer relies on the LLM chaining five separate sub-agent transfers
# (which stopped after layout_agent in production, so copy + the Arize MCP tool call
# never fired — card 437QvmKH). observability_agent runs LAST so it has every prior
# step's output in context to record to Arize Phoenix via its MCP server; it is
# best-effort and never blocks the redesign. publish is intentionally NOT in this
# pipeline — it stays behind the human-approval gate on the root supervisor.
redesign_pipeline = SequentialAgent(
    name="redesign_pipeline",
    description=(
        "Runs the redesign workers in order over the requested page: site_audit -> "
        "layout -> copy -> observability (Arize Phoenix MCP). No publishing."
    ),
    sub_agents=[site_audit_agent, layout_agent, copy_agent, observability_agent],
)

root_agent = Agent(
    name="website_redesign_orchestrator",
    model=MODEL,
    description=(
        "Supervisor that redesigns the requested page (URL provided in the request) "
        "end-to-end via a deterministic worker pipeline, with a hard human-approval "
        "gate before any WordPress publish, and Arize Phoenix MCP observability."
    ),
    instruction=(
        "You orchestrate a WordPress redesign of the requested page (the URL is "
        "provided in the request). Run this two-gate flow:\n"
        "1. Delegate to redesign_pipeline ONCE. It deterministically runs the full "
        "sequence: site_audit -> layout (block sequence + rationale) -> copy (HE + EN "
        "per block) -> observability (records the run's quality signals to Arize "
        "Phoenix via its MCP server, best-effort).\n"
        "2. GATE 1 — present the proposed page tree, block sequence with rationale, and "
        "HE/EN drafts from the pipeline, then STOP. Ask the human to approve. Keep "
        "Gemini's per-block reasoning visible — it is the proof the redesign is "
        "principled.\n"
        "3. GATE 2 — never auto-publish, never publish to production. ONLY after the "
        "human explicitly approves in a later turn, delegate to publish_agent to push "
        "the approved drafts to STAGING WordPress (it uses approved=True only then).\n"
        "Reply in the user's language; preserve Hebrew feminine voice."
    ),
    sub_agents=[redesign_pipeline, publish_agent],
)
