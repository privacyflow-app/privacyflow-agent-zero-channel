/**
 * Background poller loop.
 *
 * Polls PrivacyFlow for incoming messages every POLL_INTERVAL_MS.
 * Forwards each message to the A0 plugin endpoint.
 * Skips command messages (handled by PrivacyFlow).
 */

import { pollMessages } from "./pf_client.ts";
import { getContextId, setContextId } from "./context_mapper.ts";
import type { PolledMessage, A0ForwardMessage, A0ForwardResponse } from "./types.ts";

let _running = false;
let _intervalMs = 2000;

function getEnv(key: string, fallback?: string): string {
  const value = Deno.env.get(key);
  if (!value && fallback === undefined) {
    throw new Error(`Missing required env var: ${key}`);
  }
  return value || fallback || "";
}

function getA0BaseUrl(): string {
  return getEnv("A0_API_BASE").replace(/\/$/, "");
}

function getA0ApiKey(): string {
  return getEnv("A0_API_KEY");
}

/**
 * Format message text for A0.
 * For group messages: prefix with [contactId] so agent knows who said what.
 * For DMs: pass through as-is.
 */
function formatMessageText(msg: PolledMessage): string {
  if (msg.isGroupMessage && msg.groupId) {
    return `[${msg.contactId}]: ${msg.content}`;
  }
  return msg.content;
}

/**
 * Forward a message to the A0 plugin endpoint.
 * The A0 plugin handles context lookup/creation and agent communication.
 */
async function forwardToA0(msg: PolledMessage): Promise<void> {
  const forwardMsg: A0ForwardMessage = {
    text: formatMessageText(msg),
    contact_id: msg.contactId,
    messenger: msg.messenger,
    ...(msg.groupId ? { group_id: msg.groupId } : {}),
  };

  const url = `${getA0BaseUrl()}/api/pf_channel_receive`;
  const response = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "x-api-key": getA0ApiKey(),
    },
    body: JSON.stringify(forwardMsg),
  });

  if (!response.ok) {
    const text = await response.text();
    throw new Error(`A0 forward failed (${response.status}): ${text}`);
  }

  const result = await response.json() as A0ForwardResponse;

  // Store context mapping if new context was created
if (result.context_id && !getContextId(msg.contactId, msg.groupId)) {
    setContextId(msg.contactId, msg.groupId, result.context_id);
  }

  console.log(`[poller] ✅ Forwarded message ${msg.messageId} from ${msg.messenger} → context ${result.context_id}`);
}

/**
 * Process a batch of polled messages.
 */
async function processMessages(messages: PolledMessage[]): Promise<void> {
  for (const msg of messages) {
    // Skip command messages — PrivacyFlow handles those
    if (msg.isCommand) {
      console.log(`[poller] ⏭️ Skipping command message ${msg.messageId}`);
      continue;
    }

    try {
      await forwardToA0(msg);
    } catch (error) {
      const errMsg = error instanceof Error ? error.message : String(error);
      console.error(`[poller] ❌ Failed to forward message ${msg.messageId}:`, errMsg);
    }
  }
}

/**
 * Single poll cycle.
 */
async function pollCycle(): Promise<void> {
  try {
    const response = await pollMessages(10);
    if (response.count > 0) {
      console.log(`[poller] 🔄 Polled ${response.count} message(s)`);
      await processMessages(response.messages);
    }
  } catch (error) {
    const errMsg = error instanceof Error ? error.message : String(error);
    console.error(`[poller] ❌ Poll cycle failed:`, errMsg);
  }
}

/**
 * Start the background poller loop.
 */
export function startPoller(intervalMs?: number): void {
  if (intervalMs) {
    _intervalMs = intervalMs;
  } else {
    const envInterval = Deno.env.get("POLL_INTERVAL_MS");
    if (envInterval) {
      _intervalMs = parseInt(envInterval, 10) || 2000;
    }
  }

  if (_running) {
    console.log("[poller] Already running");
    return;
  }

  _running = true;
  console.log(`[poller] 🔄 Starting poller (interval: ${_intervalMs}ms)`);

  // Run immediately, then on interval
  pollCycle();
  setInterval(pollCycle, _intervalMs);
}

/**
 * Stop the poller.
 */
export function stopPoller(): void {
  _running = false;
  console.log("[poller] Stopped");
}
