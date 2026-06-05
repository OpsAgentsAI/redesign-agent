/**
 * Arize Phoenix MCP server — streamable-HTTP bridge (Cloud Run).
 *
 * WHY THIS EXISTS
 * ---------------
 * The Google Cloud Rapid Agent Hackathon REQUIRES the entry to integrate a Partner
 * Entity's MCP server (rapid-agent.devpost.com/rules). Our partner is Arize. The
 * redesign-agent `observability_agent` (see ../redesign_agents/agent.py) connects to
 * the Arize Phoenix MCP server as an ADK `MCPToolset` over **streamable-HTTP**:
 *
 *     MCPToolset(connection_params=StreamableHTTPConnectionParams(url=ARIZE_MCP_URL,
 *                                                                  headers={...}))
 *
 * Arize Phoenix Cloud exposes only REST + GraphQL — there is no hosted streamable-HTTP
 * MCP endpoint (verified live 2026-06-04, board card 7WdVqy7U). Arize's official MCP
 * server `@arizeai/phoenix-mcp` is a **stdio** server. This service is the missing
 * piece: it runs `@arizeai/phoenix-mcp` as a child process and exposes it over a
 * streamable-HTTP transport so the deployed Agent Engine can reach it.
 *
 * SHAPE
 * -----
 *   GET  /            -> healthz (200 "ok")
 *   GET  /healthz     -> healthz (200 "ok")
 *   POST /mcp         -> MCP streamable-HTTP (initialize + JSON-RPC), bearer-auth gated
 *   GET  /mcp         -> MCP streamable-HTTP (server->client SSE stream for a session)
 *   DELETE /mcp       -> end an MCP session
 *
 * It is a thin, generic PROXY: tools / resources / prompts are forwarded verbatim to the
 * upstream Phoenix MCP server, so this file never needs to know Phoenix's tool catalog.
 *
 * SECURITY
 * --------
 *   * The /mcp endpoint is gated by a bearer token (MCP_AUTH_TOKEN). The agent sends it
 *     as `Authorization: Bearer <token>` (its ARIZE_MCP_API_KEY secret). If MCP_AUTH_TOKEN
 *     is unset the endpoint stays open ONLY for local dev — in prod the deploy always
 *     binds it, so the agent->MCP hop is never left open (card 7WdVqy7U AC).
 *   * The upstream Phoenix credentials (PHOENIX_API_KEY) never leave this process; only
 *     this service talks to Phoenix.
 */
import crypto from "node:crypto";
import path from "node:path";
import { createRequire } from "node:module";
import express from "express";

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import {
  ListToolsRequestSchema,
  CallToolRequestSchema,
  ListResourcesRequestSchema,
  ListResourceTemplatesRequestSchema,
  ReadResourceRequestSchema,
  ListPromptsRequestSchema,
  GetPromptRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";

const PORT = parseInt(process.env.PORT || "8080", 10);
const PHOENIX_BASE_URL = (process.env.PHOENIX_BASE_URL || "https://app.phoenix.arize.com").trim();
const PHOENIX_API_KEY = (process.env.PHOENIX_API_KEY || "").trim();
// Bearer token the agent must present on the /mcp hop. Defaults to the Phoenix key so a
// single secret (phoenix-api-key) drives both the upstream auth and the edge auth.
const MCP_AUTH_TOKEN = (process.env.MCP_AUTH_TOKEN || PHOENIX_API_KEY).trim();
const SERVER_NAME = "arize-phoenix-mcp-proxy";
const SERVER_VERSION = "1.0.0";

// ---------------------------------------------------------------------------
// Upstream: one persistent Phoenix MCP stdio child, shared by every session.
// ---------------------------------------------------------------------------
let upstream = null; // MCP Client connected to @arizeai/phoenix-mcp
let upstreamCaps = {}; // server capabilities advertised by Phoenix

async function getUpstream() {
  if (upstream) return upstream;

  // Launch the official Arize Phoenix MCP server over stdio. It is installed as a
  // dependency, so the bin is resolvable without a runtime npm fetch. Both CLI flags
  // and env are supplied for version tolerance.
  const cliArgs = [resolveBin("@arizeai/phoenix-mcp"), "--baseUrl", PHOENIX_BASE_URL];
  if (PHOENIX_API_KEY) cliArgs.push("--apiKey", PHOENIX_API_KEY);

  const transport = new StdioClientTransport({
    command: process.execPath, // node
    args: cliArgs,
    env: {
      ...process.env,
      PHOENIX_BASE_URL,
      ...(PHOENIX_API_KEY ? { PHOENIX_API_KEY } : {}),
    },
  });

  const client = new Client({ name: `${SERVER_NAME}-upstream`, version: SERVER_VERSION });
  await client.connect(transport);
  upstreamCaps = client.getServerCapabilities() || {};
  upstream = client;
  console.error(
    `[mcp] upstream Phoenix MCP connected (baseUrl=${PHOENIX_BASE_URL}, ` +
      `caps=${Object.keys(upstreamCaps).join(",") || "none"})`
  );
  return upstream;
}

// Resolve the executable JS entry of an installed MCP server package so we can spawn it
// with the current node binary (robust on slim Cloud Run images that lack a global npx).
function resolveBin(pkg) {
  const req = createRequire(import.meta.url);
  const pkgJsonPath = req.resolve(`${pkg}/package.json`);
  const pkgJson = req(`${pkg}/package.json`);
  let bin = pkgJson.bin;
  if (bin && typeof bin === "object") bin = bin[Object.keys(bin)[0]];
  if (!bin) throw new Error(`${pkg} has no bin entry`);
  return path.join(path.dirname(pkgJsonPath), bin);
}

// ---------------------------------------------------------------------------
// Downstream: build a low-level MCP Server that forwards every request to the
// shared upstream client. One Server instance per streamable-HTTP session.
// ---------------------------------------------------------------------------
function buildProxyServer(client) {
  const capabilities = { tools: {} };
  if (upstreamCaps.resources) capabilities.resources = {};
  if (upstreamCaps.prompts) capabilities.prompts = {};

  const server = new Server(
    { name: SERVER_NAME, version: SERVER_VERSION },
    { capabilities }
  );

  // Tools (Phoenix always exposes these — record / query observations, datasets, …).
  server.setRequestHandler(ListToolsRequestSchema, () => client.listTools());
  server.setRequestHandler(CallToolRequestSchema, (req) =>
    client.callTool(req.params, undefined, { timeout: 60000 })
  );

  // Resources + prompts only if Phoenix advertises them (forward verbatim).
  if (upstreamCaps.resources) {
    server.setRequestHandler(ListResourcesRequestSchema, () => client.listResources());
    server.setRequestHandler(ListResourceTemplatesRequestSchema, () =>
      client.listResourceTemplates()
    );
    server.setRequestHandler(ReadResourceRequestSchema, (req) =>
      client.readResource(req.params)
    );
  }
  if (upstreamCaps.prompts) {
    server.setRequestHandler(ListPromptsRequestSchema, () => client.listPrompts());
    server.setRequestHandler(GetPromptRequestSchema, (req) => client.getPrompt(req.params));
  }

  return server;
}

// ---------------------------------------------------------------------------
// HTTP layer (stateful sessions per the streamable-HTTP spec).
// ---------------------------------------------------------------------------
const transports = new Map(); // sessionId -> StreamableHTTPServerTransport

function bearerOk(req) {
  if (!MCP_AUTH_TOKEN) return true; // dev only — prod always binds the token
  const header = req.headers["authorization"] || "";
  const m = /^Bearer\s+(.+)$/i.exec(header);
  if (!m) return false;
  const got = Buffer.from(m[1].trim());
  const want = Buffer.from(MCP_AUTH_TOKEN);
  return got.length === want.length && crypto.timingSafeEqual(got, want);
}

const app = express();
app.use(express.json({ limit: "4mb" }));

app.get(["/", "/healthz"], (_req, res) => res.status(200).type("text/plain").send("ok"));

app.post("/mcp", async (req, res) => {
  if (!bearerOk(req)) {
    return res.status(401).json(rpcError(req.body, -32001, "unauthorized"));
  }

  try {
    const sessionId = req.headers["mcp-session-id"];
    let transport = sessionId ? transports.get(sessionId) : undefined;

    if (!transport) {
      if (!isInitialize(req.body)) {
        return res.status(400).json(rpcError(req.body, -32000, "no valid session; send initialize first"));
      }
      // New session: spin up a fresh proxy server bound to the shared upstream client.
      const client = await getUpstream();
      const server = buildProxyServer(client);
      transport = new StreamableHTTPServerTransport({
        sessionIdGenerator: () => crypto.randomUUID(),
        onsessioninitialized: (sid) => transports.set(sid, transport),
      });
      transport.onclose = () => {
        if (transport.sessionId) transports.delete(transport.sessionId);
      };
      await server.connect(transport);
    }

    await transport.handleRequest(req, res, req.body);
  } catch (err) {
    console.error("[mcp] POST /mcp error:", err);
    if (!res.headersSent) res.status(500).json(rpcError(req.body, -32603, "internal error"));
  }
});

// SSE stream + session teardown for an established session.
async function sessionRoute(req, res) {
  if (!bearerOk(req)) return res.status(401).end();
  const sessionId = req.headers["mcp-session-id"];
  const transport = sessionId ? transports.get(sessionId) : undefined;
  if (!transport) return res.status(400).send("unknown session");
  await transport.handleRequest(req, res);
}
app.get("/mcp", sessionRoute);
app.delete("/mcp", sessionRoute);

function isInitialize(body) {
  if (Array.isArray(body)) return body.some(isInitialize);
  return body && body.method === "initialize";
}

function rpcError(body, code, message) {
  const id = body && !Array.isArray(body) ? body.id ?? null : null;
  return { jsonrpc: "2.0", id, error: { code, message } };
}

app.listen(PORT, () => {
  console.error(
    `[mcp] arize-phoenix-mcp-proxy listening on :${PORT} ` +
      `(auth=${MCP_AUTH_TOKEN ? "on" : "OFF(dev)"})`
  );
});
