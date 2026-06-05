"""Deploy the website-redesign ADK orchestrator to Vertex AI Agent Engine.

Authored for OpsAgentsAI/redesign-agent. Runs in GitHub Actions under
Workload Identity Federation (gha-deployer SA on opsagent-prod). Pure ASCII.

Region is forced to us-central1 here regardless of what the agent code says:
Agent Engine is not available in me-west1. We override the env at deploy time;
we do NOT edit the agent logic.

ADK version note: the repo pins google-adk==2.1.0 for the tested code, but the
managed Agent Engine runtime historically expects google-adk 1.x. This deploy
declares its own requirements list (1.x line) so the managed runtime resolves a
compatible ADK; agent.py imports `from google.adk.agents import Agent`, which
exists on both majors.
"""

from __future__ import annotations

import os
import sys

# When invoked as `python scripts/deploy_agent_engine.py`, Python puts the
# script's own directory (scripts/) on sys.path[0], NOT the repo root, so the
# top-level `redesign_agents` package is not importable. Prepend the repo root
# (the parent of this file's directory) so `from redesign_agents...` resolves.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import vertexai
from vertexai import agent_engines

# AdkApp has moved across SDK releases. Import defensively so this script keeps
# working whether the installed google-cloud-aiplatform exposes it under
# vertexai.agent_engines or vertexai.preview.reasoning_engines.
AdkApp = None
try:
    from vertexai.agent_engines import AdkApp as _AdkApp  # type: ignore

    AdkApp = _AdkApp
except Exception:  # pragma: no cover - import-surface fallback
    try:
        from vertexai.preview.reasoning_engines import AdkApp as _AdkApp  # type: ignore

        AdkApp = _AdkApp
    except Exception as exc:  # pragma: no cover
        print("FATAL: could not import AdkApp from the installed SDK: %r" % exc)
        raise

PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "opsagent-prod")
LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
STAGING_BUCKET = os.environ.get(
    "AGENT_ENGINE_STAGING_BUCKET", "gs://opsagent-prod-agent-engine-staging"
)

# Deploy-time requirements for the managed runtime. Pin ADK to the 1.x line the
# Agent Engine runtime supports; do NOT reuse the repo's 2.1.0 pin here.
# `mcp` is declared explicitly so the ADK MCP toolset (Arize Phoenix partner-MCP
# integration in agent.observability_agent) is importable in the managed runtime.
REQUIREMENTS = [
    "google-cloud-aiplatform[agent_engines,adk]",
    "google-adk>=1.32,<2",
    "mcp>=1.0.0",
]

# Runtime env vars forwarded to the managed Agent Engine. Only keys actually set
# in the deploy environment are forwarded (so unset optional integrations stay
# off rather than shipping empty strings). The Arize MCP keys carry the partner-
# MCP integration; the WP keys gate the (human-approved) publish path.
_RUNTIME_ENV_KEYS = (
    "REDESIGN_MODEL",
    "ARIZE_MCP_URL",
    "ARIZE_MCP_API_KEY",
    "ARIZE_MCP_AUTH_HEADER",
    "ARIZE_MCP_HEADERS",
    "WP_PUBLISH_URL",
    "WP_PUBLISH_USER",
    "WP_PUBLISH_APP_PASSWORD",
    "WP_PUBLISH_TEMPLATE",
)


def _build_runtime_env() -> dict:
    """Collect the runtime env vars to forward into the managed Agent Engine.

    GOOGLE_CLOUD_PROJECT / GOOGLE_CLOUD_LOCATION are RESERVED by the Agent Engine runtime
    (it injects them itself). Forwarding them in env_vars now hard-fails the create with
    `FAILED_PRECONDITION: Environment variable name 'GOOGLE_CLOUD_PROJECT' is reserved`.
    They are still set as process env (in main(), for vertexai.init + the agent import)
    but must NOT be forwarded into the managed runtime's env_vars.
    """
    env = {
        "GOOGLE_GENAI_USE_VERTEXAI": "TRUE",
    }
    for key in _RUNTIME_ENV_KEYS:
        val = os.environ.get(key)
        if val:
            env[key] = val
    return env


def main() -> int:
    print("Deploy target: project=%s location=%s" % (PROJECT, LOCATION))
    print("Staging bucket: %s" % STAGING_BUCKET)
    print("Requirements: %r" % REQUIREMENTS)

    # Force Vertex-backed Gemini for the agents regardless of imported defaults.
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "TRUE"
    os.environ["GOOGLE_CLOUD_PROJECT"] = PROJECT
    os.environ["GOOGLE_CLOUD_LOCATION"] = LOCATION

    # Import the agent AFTER env is set so any module-level region/model reads
    # pick up the us-central1 override.
    from redesign_agents.agent import root_agent

    print("Imported root_agent: name=%s sub_agents=%d" % (
        getattr(root_agent, "name", "?"),
        len(getattr(root_agent, "sub_agents", []) or []),
    ))

    runtime_env = _build_runtime_env()
    print("Runtime env keys: %r" % sorted(runtime_env.keys()))
    if not runtime_env.get("ARIZE_MCP_URL"):
        print(
            "WARNING: ARIZE_MCP_URL is not set for this deploy. The Arize Phoenix "
            "partner-MCP integration will be INACTIVE, which the Rapid Agent "
            "hackathon rules require for eligibility. Provision the secret + grant "
            "the deployer SA access, then redeploy (see docs/ARIZE_MCP.md)."
        )

    vertexai.init(
        project=PROJECT,
        location=LOCATION,
        staging_bucket=STAGING_BUCKET,
    )

    app = AdkApp(agent=root_agent, enable_tracing=True)

    print("Creating Agent Engine (this can take several minutes)...")
    remote = agent_engines.create(
        agent_engine=app,
        requirements=REQUIREMENTS,
        display_name="website-redesign-orchestrator",
        extra_packages=["redesign_agents"],
        env_vars=runtime_env,
    )

    resource_name = getattr(remote, "resource_name", None)
    if not resource_name:
        print("FATAL: create() returned no resource_name: %r" % remote)
        return 1

    print("SUCCESS")
    print("RESOURCE_NAME=%s" % resource_name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
