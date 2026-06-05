/**
 * Smoke test for the Arize Phoenix MCP streamable-HTTP bridge.
 *
 * Connects a real MCP client to a running instance over streamable-HTTP and asserts that
 * `tools/list` returns at least one Phoenix tool — i.e. the agent->MCP->Phoenix hop works
 * end to end (the hackathon partner-MCP eligibility check, card 7WdVqy7U).
 *
 *   MCP_URL=https://<service>/mcp MCP_AUTH_TOKEN=<system-key> node smoke.mjs
 *
 * Defaults to http://localhost:8080/mcp for local runs.
 */
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

const MCP_URL = process.env.MCP_URL || "http://localhost:8080/mcp";
const TOKEN = (process.env.MCP_AUTH_TOKEN || process.env.ARIZE_MCP_API_KEY || "").trim();

async function main() {
  const transport = new StreamableHTTPClientTransport(new URL(MCP_URL), {
    requestInit: TOKEN ? { headers: { Authorization: `Bearer ${TOKEN}` } } : undefined,
  });
  const client = new Client({ name: "phoenix-mcp-smoke", version: "1.0.0" });

  await client.connect(transport);
  const { tools } = await client.listTools();
  console.log(`tools/list returned ${tools.length} tool(s):`);
  for (const t of tools) console.log(`  - ${t.name}`);

  if (!tools.length) {
    console.error("FAIL: expected at least one Phoenix tool");
    process.exit(1);
  }
  await client.close();
  console.log("OK: Phoenix MCP reachable over streamable-HTTP");
}

main().catch((err) => {
  console.error("FAIL:", err);
  process.exit(1);
});
