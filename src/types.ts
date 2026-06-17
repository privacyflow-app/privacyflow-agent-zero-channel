/**
 * Shared types matching PrivacyFlow public backend API types exactly.
 * Source: privacyflow-public-backend-api/index.ts
 */

export type Messenger = "signal" | "simplex" | "session";

export const VALID_MESSENGERS: Messenger[] = ["signal", "simplex", "session"];

/**
 * Message polled from GET /api/v1/messages/poll
 */
export interface PolledMessage {
  appId: string;
  messenger: Messenger;
  contactId: string;
  messageId: string;
  content: string;
  timestamp: number;
  groupId?: string;
  isGroupMessage: boolean;
  isCommand: boolean;
  command?: string;
}

/**
 * Poll response from GET /api/v1/messages/poll
 */
export interface PollResponse {
  messages: PolledMessage[];
  count: number;
  queue: string;
}

/**
 * Input for POST /api/v1/messages/send
 */
export interface SendMessageInput {
  appId: string;
  contactId: string;
  message: string;
  messenger: Messenger;
  groupId?: string;
}

/**
 * Queued message with metadata
 */
export interface QueuedMessage extends SendMessageInput {
  messageId: string;
  timestamp: number;
}

/**
 * Send request body
 */
export interface SendRequest {
  messages: SendMessageInput[];
}

/**
 * Send response
 */
export interface SendResponse {
  successfulMessages: SuccessfulMessage[];
  failedMessages: FailedMessage[];
  totalAccepted: number;
  totalFailed: number;
}

export interface SuccessfulMessage {
  messageId: string;
  appId: string;
  contactId: string;
  messenger: Messenger;
}

export interface FailedMessage {
  appId: string;
  contactId: string;
  messenger: Messenger;
  error: string;
}

/**
 * Auth verify response
 */
export interface AuthVerifyResponse {
  valid: boolean;
  appIds: string[];
}

/**
 * Message forwarded to A0 plugin endpoint
 */
export interface A0ForwardMessage {
  text: string;
  contact_id: string;
  group_id?: string;
  messenger: Messenger;
}

/**
 * Response from A0 plugin endpoint
 */
export interface A0ForwardResponse {
  context_id: string;
  accepted: boolean;
}

/**
 * Request from A0 plugin's process_chain_end extension
 * POST to bridge /api/response
 */
export interface A0ResponseRequest {
  contact_id: string;
  group_id?: string;
  messenger: Messenger;
  message: string;
}

/**
 * Messenger message length limits (conservative)
 */
export const MESSENGER_LENGTH_LIMITS: Record<Messenger, number> = {
  signal: 2000,
  simplex: 8000,
  session: 2000,
};
