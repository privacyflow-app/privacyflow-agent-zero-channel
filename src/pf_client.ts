/**
 * PrivacyFlow Public API Client
 *
 * Wraps the 4 endpoints from privacyflow-public-backend-api:
 * - GET  /api/v1/health
 * - GET  /api/v1/auth/verify
 * - GET  /api/v1/messages/poll
 * - POST /api/v1/messages/send
 */

import type {
  PollResponse,
  SendRequest,
  SendResponse,
  AuthVerifyResponse,
  Messenger,
  SendMessageInput,
} from "./types.ts";

const MAX_RETRIES = 3;
const RETRY_DELAY_MS = 1000;

function getEnv(key: string, fallback?: string): string {
  const value = Deno.env.get(key);
  if (!value && fallback === undefined) {
    throw new Error(`Missing required env var: ${key}`);
  }
  return value || fallback || "";
}

function getBaseUrl(): string {
  return getEnv("PF_API_BASE").replace(/\/$/, "");
}

function getApiKey(): string {
  return getEnv("PF_API_KEY");
}

function getAppId(): string {
  return getEnv("PF_APP_ID");
}

async function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Fetch with retry logic.
 */
async function fetchWithRetry(
  url: string,
  options: RequestInit,
  retries = MAX_RETRIES,
): Promise<Response> {
  let lastError: Error | null = null;
  for (let attempt = 0; attempt < retries; attempt++) {
    try {
      const response = await fetch(url, options);
      if (response.status >= 500 && attempt < retries - 1) {
        console.error(`[pf_client] Server error ${response.status}, retrying (${attempt + 1}/${retries})`);
        await sleep(RETRY_DELAY_MS * (attempt + 1));
        continue;
      }
      return response;
    } catch (error) {
      lastError = error instanceof Error ? error : new Error(String(error));
      if (attempt < retries - 1) {
        console.error(`[pf_client] Fetch failed, retrying (${attempt + 1}/${retries}):`, lastError.message);
        await sleep(RETRY_DELAY_MS * (attempt + 1));
      }
    }
  }
  throw lastError || new Error("fetchWithRetry exhausted");
}

/**
 * GET /api/v1/health
 */
export async function healthCheck(): Promise<boolean> {
  try {
    const response = await fetch(`${getBaseUrl()}/api/v1/health`);
    return response.ok;
  } catch {
    return false;
  }
}

/**
 * GET /api/v1/auth/verify
 */
export async function verifyAuth(): Promise<AuthVerifyResponse> {
  const response = await fetchWithRetry(
    `${getBaseUrl()}/api/v1/auth/verify`,
    {
      method: "GET",
      headers: {
        "Authorization": `Bearer ${getApiKey()}`,
      },
    },
  );
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`Auth verify failed (${response.status}): ${text}`);
  }
  return await response.json() as AuthVerifyResponse;
}

/**
 * GET /api/v1/messages/poll?limit=N
 *
 * Polls incoming messages from poll:{appId} Redis queue.
 * Messages are removed from the queue on poll (LPOP).
 */
export async function pollMessages(limit = 10): Promise<PollResponse> {
  const response = await fetchWithRetry(
    `${getBaseUrl()}/api/v1/messages/poll?limit=${limit}`,
    {
      method: "GET",
      headers: {
        "Authorization": `Bearer ${getApiKey()}`,
      },
    },
  );
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`Poll failed (${response.status}): ${text}`);
  }
  return await response.json() as PollResponse;
}

/**
 * POST /api/v1/messages/send
 *
 * Sends outgoing messages to outgoing-{messenger} BullMQ queues.
 * A 202 response guarantees messages are in the queue.
 */
export async function sendMessages(messages: SendMessageInput[]): Promise<SendResponse> {
  const body: SendRequest = { messages };
  const response = await fetchWithRetry(
    `${getBaseUrl()}/api/v1/messages/send`,
    {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${getApiKey()}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
    },
  );
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`Send failed (${response.status}): ${text}`);
  }
  return await response.json() as SendResponse;
}

/**
 * Convenience: send a single message.
 */
export async function sendMessage(
  contactId: string,
  message: string,
  messenger: Messenger,
  groupId?: string,
): Promise<SendResponse> {
  const msg: SendMessageInput = {
    appId: getAppId(),
    contactId,
    message,
    messenger,
    ...(groupId ? { groupId } : {}),
  };
  return sendMessages([msg]);
}
