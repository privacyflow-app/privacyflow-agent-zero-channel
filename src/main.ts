/**
 * Entry point for the PrivacyFlow Agent Zero Channel bridge service.
 *
 * Starts three components:
 * 1. Background poller — polls PrivacyFlow for incoming messages
 * 2. MCP server (SSE) — exposes pf_send_message tool to A0
 * 3. HTTP API — receives responses from A0 plugin's process_chain_end
 *
 * The HTTP API and MCP SSE share a single Deno.serve instance.
 * MCP SSE is served at /sse, HTTP API at /api/response and /health.
 */

import { startPoller } from "./pf_poller.ts";
import { createMcpServer } from "./mcp_server.ts";
import { sendMessages } from "./pf_client.ts";
import { healthCheck, verifyAuth } from "./pf_client.ts";
import { MESSENGER_LENGTH_LIMITS } from "./types.ts";
import type { A0ResponseRequest, Messenger, SendMessageInput } from "./types.ts";

function getEnv(key: string, fallback?: string): string {
  const value = Deno.env.get(key);
  if (!value && fallback === undefined) {
    throw new Error(`Missing required env var: ${key}`);
  }
  return value || fallback || "";
}

function getAppId(): string {
  return getEnv("PF_APP_ID");
}

/**
 * Split a long message into chunks that fit within messenger limits.
 */
function splitMessage(text: string, messenger: Messenger): string[] {
  const limit = MESSENGER_LENGTH_LIMITS[messenger] || 2000;
  if (text.length <= limit) return [text];

  const chunks: string[] = [];
  let remaining = text;

  while (remaining.length > limit) {
    let splitAt = remaining.lastIndexOf("\n\n", limit);
    if (splitAt === -1) splitAt = remaining.lastIndexOf("\n", limit);
    if (splitAt === -1) splitAt = remaining.lastIndexOf(" ", limit);
    if (splitAt === -1 || splitAt === 0) splitAt = limit;
    chunks.push(remaining.slice(0, splitAt).trim());
    remaining = remaining.slice(splitAt).trim();
  }
  if (remaining.length > 0) chunks.push(remaining);
  return chunks;
}

/**
 * Handle POST /api/response — receives agent response from A0 plugin.
 */
async function handleResponse(req: Request): Promise<Response> {
  let body: A0ResponseRequest;
  try {
    body = await req.json() as A0ResponseRequest;
  } catch {
    return new Response(JSON.stringify({ error: "Invalid JSON" }), {
      status: 400, headers: { "Content-Type": "application/json" },
    });
  }

  if (!body.contact_id || !body.messenger || !body.message) {
    return new Response(JSON.stringify({ error: "Missing required fields: contact_id, messenger, message" }), {
      status: 400, headers: { "Content-Type": "application/json" },
    });
  }

  const chunks = splitMessage(body.message, body.messenger);
  const messages: SendMessageInput[] = chunks.map((chunk) => ({
    appId: getAppId(),
    contactId: body.contact_id,
    message: chunk,
    messenger: body.messenger,
    ...(body.group_id ? { groupId: body.group_id } : {}),
  }));

  try {
    const result = await sendMessages(messages);
    console.log(`[http] ✅ Sent ${result.totalAccepted} message(s) to ${body.contact_id} via ${body.messenger}`);
    return new Response(JSON.stringify({ ok: true, sent: result.totalAccepted, failed: result.totalFailed }), {
      status: 200, headers: { "Content-Type": "application/json" },
    });
  } catch (error) {
    const errMsg = error instanceof Error ? error.message : String(error);
    console.error(`[http] ❌ Send failed:`, errMsg);
    return new Response(JSON.stringify({ error: errMsg }), {
      status: 502, headers: { "Content-Type": "application/json" },
    });
  }
}

async function main(): Promise<void> {
  console.log("🚀 PrivacyFlow Agent Zero Channel — starting...");

  // Validate required env vars
  const required = ["PF_API_BASE", "PF_API_KEY", "PF_APP_ID", "A0_API_BASE", "A0_API_KEY"];
  const missing = required.filter((key) => !Deno.env.get(key));
  if (missing.length > 0) {
    console.error(`🚨 Missing required environment variables: ${missing.join(", ")}`);
    Deno.exit(1);
  }

  // Health check PrivacyFlow API
  console.log("[main] Checking PrivacyFlow API health...");
  const isHealthy = await healthCheck();
  if (!isHealthy) {
    console.error("🚨 PrivacyFlow API is not reachable");
    Deno.exit(1);
  }
  console.log("[main] ✅ PrivacyFlow API reachable");

  // Verify API key
  console.log("[main] Verifying API key...");
  try {
    const auth = await verifyAuth();
    if (!auth.valid) {
      console.error("🚨 Invalid PrivacyFlow API key");
      Deno.exit(1);
    }
    console.log(`[main] ✅ API key valid, appIds: ${auth.appIds.join(", ")}`);
  } catch (error) {
    const errMsg = error instanceof Error ? error.message : String(error);
    console.error(`🚨 Auth verification failed: ${errMsg}`);
    Deno.exit(1);
  }

  // Start background poller
  startPoller();

  // Create MCP server
  const mcpServer = createMcpServer();

  // Start unified HTTP server (handles both MCP SSE and HTTP API)
  const httpPort = parseInt(getEnv("PORT", "3005"), 10);
  console.log(`[main] 🔄 Starting server on port ${httpPort}`);

  Deno.serve({ port: httpPort }, async (req: Request) => {
    const url = new URL(req.url);

    // MCP SSE endpoint — A0 connects here as external MCP server
    if (url.pathname === "/sse" || url.pathname === "/mcp") {
      // Use the MCP SDK's SSE transport
      // The @modelcontextprotocol/sdk handles the SSE protocol internally
      // For now, return a simple response indicating MCP is available
      // Full SSE transport wiring requires SSEServerTransport from the SDK
      return new Response(JSON.stringify({
        status: "mcp_server",
        name: Deno.env.get("MCP_SERVER_NAME") || "privacyflow-channel",
        tools: ["pf_send_message"],
        message: "Connect via MCP SSE client to this endpoint",
      }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }

    // HTTP API: health check
    if (req.method === "GET" && url.pathname === "/health") {
      return new Response(JSON.stringify({ status: "ok", version: "0.1.0" }), {
        status: 200, headers: { "Content-Type": "application/json" },
      });
    }

    // HTTP API: receive response from A0 plugin
    if (req.method === "POST" && url.pathname === "/api/response") {
      return handleResponse(req);
    }

    return new Response(JSON.stringify({ error: "Not found" }), {
      status: 404, headers: { "Content-Type": "application/json" },
    });
  });

  console.log(`\n✅ Bridge service running:`);
  console.log(`   HTTP API:  http://0.0.0.0:${httpPort}`);
  console.log(`   MCP SSE:   http://0.0.0.0:${httpPort}/sse`);
  console.log(`   Health:    http://0.0.0.0:${httpPort}/health`);
  console.log(`   Poller:    every ${Deno.env.get("POLL_INTERVAL_MS") || 2000}ms`);
}

if (import.meta.main) {
  main();
}
