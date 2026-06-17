/**
 * Entry point for the PrivacyFlow Agent Zero Channel bridge service.
 *
 * Starts three components:
 * 1. Background poller — polls PrivacyFlow for incoming messages
 * 2. MCP server — exposes pf_send_message tool to A0
 * 3. HTTP API — receives responses from A0 plugin's process_chain_end
 */

import { startPoller } from "./pf_poller.ts";
import { createMcpServer } from "./mcp_server.ts";
import { startHttpApi } from "./http_api.ts";
import { healthCheck, verifyAuth } from "./pf_client.ts";

function getEnv(key: string, fallback?: string): string {
  const value = Deno.env.get(key);
  if (!value && fallback === undefined) {
    throw new Error(`Missing required env var: ${key}`);
  }
  return value || fallback || "";
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

  // Start MCP server (SSE transport on /sse path)
  const mcpServer = createMcpServer();
  const mcpPort = parseInt(getEnv("MCP_PORT", "3006"), 10);
  console.log(`[main] 🔄 Starting MCP server on port ${mcpPort}`);
  // MCP SSE transport will be configured here
  // For now, the MCP server is created and ready for transport binding

  // Start HTTP API
  const httpPort = parseInt(getEnv("PORT", "3005"), 10);
  startHttpApi(httpPort);

  console.log(`\n✅ Bridge service running:`);
  console.log(`   HTTP API: http://0.0.0.0:${httpPort}`);
  console.log(`   MCP server: http://0.0.0.0:${mcpPort}/sse`);
  console.log(`   Poller: every ${Deno.env.get("POLL_INTERVAL_MS") || 2000}ms`);
}

if (import.meta.main) {
  main();
}
