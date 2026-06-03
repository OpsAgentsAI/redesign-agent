# Arize Phoenix partner-MCP integration — operator runbook

The Google Cloud Rapid Agent Hackathon **requires** the agent to integrate a Partner Entity's
MCP server (rapid-agent.devpost.com/rules). Our partner track is **Arize**, and the integration
is the `observability_agent` in `redesign_agents/agent.py`, which connects to the **Arize Phoenix
MCP server** as an ADK `MCPToolset` over streamable-HTTP.

The code ships fully wired. It is **fail-open**: when `ARIZE_MCP_URL` is unset the toolset is
omitted and the core redesign path runs unchanged (so this never broke the live demo). To make
the integration *active* — which eligibility requires — an operator does the steps below once.

## What the code expects (env, set at deploy time)

| Env var | Required | Meaning |
|---|---|---|
| `ARIZE_MCP_URL` | yes | streamable-HTTP endpoint of the Arize/Phoenix MCP server |
| `ARIZE_MCP_API_KEY` | usually | API key/token; sent as `Authorization: Bearer <key>` by default |
| `ARIZE_MCP_AUTH_HEADER` | no | override the header name (e.g. `api_key`, `x-api-key`) → raw key, no `Bearer` |
| `ARIZE_MCP_HEADERS` | no | extra headers as a JSON object, merged last (e.g. `{"X-Space-Id":"…"}`) |

`scripts/deploy_agent_engine.py` forwards these into the managed Agent Engine via
`agent_engines.create(env_vars=…)`. The deploy workflow loads `ARIZE_MCP_URL` / `ARIZE_MCP_API_KEY`
from Secret Manager.

## Standing up the Arize Phoenix MCP endpoint (pick one)

- **Arize AX (cloud)** — use the hosted Phoenix MCP endpoint + your space API key.
- **Self-hosted Phoenix** — run Phoenix (Cloud Run, credit-covered) and expose its MCP server
  (`@arizeai/phoenix-mcp`) over HTTP. The agent only needs a reachable streamable-HTTP URL + key.

Confirm the exact URL + auth-header convention from the Arize console; plug them into the secrets
below (use `ARIZE_MCP_AUTH_HEADER` / `ARIZE_MCP_HEADERS` if Arize expects a non-`Authorization`
header or a space id).

## Operator steps (once)

```bash
PROJECT=opsagent-prod
DEPLOYER=gha-deployer@opsagent-prod.iam.gserviceaccount.com

# 1. Create the secrets (replace the values).
printf '%s' 'https://<your-arize-phoenix>/mcp' | \
  gcloud secrets create arize-mcp-url --project="$PROJECT" --data-file=- 2>/dev/null || \
printf '%s' 'https://<your-arize-phoenix>/mcp' | \
  gcloud secrets versions add arize-mcp-url --project="$PROJECT" --data-file=-

printf '%s' '<ARIZE_API_KEY>' | \
  gcloud secrets create arize-mcp-api-key --project="$PROJECT" --data-file=- 2>/dev/null || \
printf '%s' '<ARIZE_API_KEY>' | \
  gcloud secrets versions add arize-mcp-api-key --project="$PROJECT" --data-file=-

# 2. Let the deploy SA read them.
for S in arize-mcp-url arize-mcp-api-key; do
  gcloud secrets add-iam-policy-binding "$S" --project="$PROJECT" \
    --member="serviceAccount:$DEPLOYER" --role=roles/secretmanager.secretAccessor
done

# 3. Redeploy — the workflow injects the secrets and the script forwards them.
gh workflow run deploy-agent-engine.yml --ref main --repo OpsAgentsAI/redesign-agent
```

No cleartext credentials are committed (rule: `feedback_no_creds_in_trello`).

## Verify the integration is live

1. The deploy log shows `Runtime env keys: [... 'ARIZE_MCP_URL' ...]` and **not** the
   "ARIZE_MCP_URL is not set" warning.
2. Drive a run against the deployed engine (`python scripts/smoke_agent_engine.py`); the
   `observability_agent` step should report it recorded the run (or queried Phoenix) — i.e. it
   made a real MCP tool call rather than "no MCP tools available."
3. The run appears in the Arize Phoenix project (dataset / observation / trace).

## Why it's safe

- Unset env → integration disabled, core redesign path unchanged (fail-open).
- The `observability_agent` is best-effort and a *separate* sub-agent: a Phoenix outage cannot
  block or alter the audit → layout → copy → (approved) publish path.
- It never publishes and never carries PII into telemetry beyond the run's structural signals.
