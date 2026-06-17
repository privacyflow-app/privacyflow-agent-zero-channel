/**
 * HTTP API server for receiving responses from A0 plugin's process_chain_end extension.
 *
 * POST /api/response — receives agent response and forwards to PrivacyFlow send API.
 * GET  /health   — health check.
 */

import { sendMessages } from "./pf_client.ts";
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
 * Tries to split on paragraph boundaries, then newlines, then spaces, then hard cut.
 */
export function splitMessage(text: string, messenger: Messenger): string[] {
  const limit = MESSENGER_LENGTH_LIMITS[messenger] || 2000;
  if (text.length <= limit) {
    return [text];
  }

  const chunks: string[] = [];
  let remaining = text;

  while (remaining.length > limit) {
    // Try paragraph break first
    let splitAt = remaining.lastIndexOf("\n\n", limit);
    if (splitAt === -1) {
      splitAt = remaining.lastIndexOf("\n", limit);
    }
    if (splitAt === -1) {
      splitAt = remaining.lastIndexOf(" ", limit);
    }
    if (splitAt === -1 || splitAt === 0) {
      splitAt = limit;
    }

    chunks.push(remaining.slice(0, splitAt).trim());
    remaining = remaining.slice(splitAt).trim();
  }

  if (remaining.length > 0) {
    chunks.push(remaining);
  }

  return chunks;
}

/**
 * Handle POST /api/response
 *
 * Receives agent response from A0 plugin and sends it via PrivacyFlow.
 */
async function handleResponse(req: Request): Promise<Response> {
  let body: A0ResponseRequest;
  try {
    body = await req.json() as A0ResponseRequest;
  } catch {
    return new Response(JSON.stringify({ error: "Invalid JSON" }), {
      status: 400,
      headers: { "Content-Type": "application/json" },
    });
  }

  // Validate required fields
  if (!body.contact_id || !body.messenger || !body.message) {
    return new Response(JSON.stringify({ error: "Missing required fields: contact_id, messenger, message" }), {
      status: 400,
      headers: { "Content-Type": "application/json" },
    });
  }

  // Split message if needed
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

    return new Response(JSON.stringify({
      ok: true,
      sent: result.totalAccepted,
      failed: result.totalFailed,
    }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  } catch (error) {
    const errMsg = error instanceof Error ? error.message : String(error);
    console.error(`[http] ❌ Send failed:`, errMsg);
    return new Response(JSON.stringify({ error: errMsg }), {
      status: 502,
      headers: { "Content-Type": "application/json" },
    });
  }
}

/**
 * Handle GET /health
 */
function handleHealth(): Response {
  return new Response(JSON.stringify({ status: "ok", version: "0.1.0" }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

/**
 * Start the HTTP API server.
 */
export function startHttpApi(port: number): void {
  console.log(`[http] 🔄 Starting HTTP API on port ${port}`);

  Deno.serve({ port }, async (req: Request) => {
    const url = new URL(req.url);

    if (req.method === "GET" && url.pathname === "/health") {
      return handleHealth();
    }

    if (req.method === "POST" && url.pathname === "/api/response") {
      return handleResponse(req);
    }

    return new Response(JSON.stringify({ error: "Not found" }), {
      status: 404,
      headers: { "Content-Type": "application/json" },
    });
  });
}
