# Arize Phoenix MCP server (streamable-HTTP bridge)

Cloud Run service that exposes the official Arize **Phoenix MCP server**
(`@arizeai/phoenix-mcp`, a stdio server) over **streamable-HTTP** so the
redesign-agent `observability_agent` can reach it as an ADK `MCPToolset`.

This is the **partner-MCP integration** the Google Cloud Rapid Agent Hackathon
requires (rapid-agent.devpost.com/rules). Arize Phoenix Cloud exposes only
REST + GraphQL — no hosted streamable-HTTP MCP endpoint — so the official MCP
server must be self-hosted behind HTTP. That is exactly what this does.

```
agent.py observability_agent
   └─ MCPToolset(StreamableHTTPConnectionParams(url=ARIZE_MCP_URL, headers={Authorization: Bearer …}))
        └─ THIS SERVICE  (Cloud Run, streamable-HTTP at /mcp, bearer-auth gated)
             └─ @arizeai/phoenix-mcp  (stdio child)
                  └─ Arize Phoenix Cloud (REST/GraphQL)
```

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/`, `/healthz` | health check (`200 ok`) |
| `POST` | `/mcp` | MCP streamable-HTTP — `initialize` + JSON-RPC. Bearer-auth gated. |
| `GET` | `/mcp` | server→client SSE stream for an established session |
| `DELETE` | `/mcp` | end an MCP session |

It is a generic proxy: tools / resources / prompts are forwarded verbatim to the
upstream Phoenix MCP server.

## Environment

| Env var | Default | Meaning |
|---|---|---|
| `PHOENIX_BASE_URL` | `https://app.phoenix.arize.com/s/michal` | Phoenix instance the MCP targets. **Phoenix Cloud is multi-tenant — must include the `/s/<space>` prefix** (the "Hostname" in space settings); the bare host 401s every tool call. |
| `PHOENIX_API_KEY` | — | Phoenix System Key (forwarded to the upstream MCP server) |
| `MCP_AUTH_TOKEN` | `= PHOENIX_API_KEY` | bearer token the agent must present on `/mcp` |
| `PORT` | `8080` | Cloud Run port |

A single secret (`phoenix-api-key`) drives both the upstream auth and the edge
auth, so the agent's `ARIZE_MCP_API_KEY` is the same System Key value.

> **Security:** if `MCP_AUTH_TOKEN` is unset the endpoint is open — that is for
> local dev only. The deploy workflow always binds it from Secret Manager, so the
> agent→MCP hop is never left open in prod (card 7WdVqy7U AC).

## Local

```bash
npm install
PHOENIX_API_KEY=<system-key> npm start          # listens on :8080
# in another shell:
MCP_AUTH_TOKEN=<system-key> npm run smoke        # asserts tools/list works
```

## Deploy

CI: `.github/workflows/deploy-mcp-server.yml` (manual `workflow_dispatch`) deploys
to Cloud Run in `opsagent-prod / us-central1` via WIF → `gha-deployer`, binds
`PHOENIX_API_KEY` + `MCP_AUTH_TOKEN` from the `phoenix-api-key` secret, then
best-effort writes the `arize-mcp-url` + `arize-mcp-api-key` secrets the
redesign-agent's `deploy-agent-engine.yml` consumes — closing the loop with
card WCxlUA2H. See the workflow for the exact operator/IAM prerequisites.
