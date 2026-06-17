/**
 * MCP Server exposing pf_send_message tool.
 *
 * A0 connects to this as an external MCP server via SSE transport.
 * Config in A0 settings: {"mcpServers": {"privacyflow-channel": {"url": "http://host:PORT/sse", "type": "sse"}}}
 *
 * The only tool exposed is pf_send_message, which wraps POST /api/v1/messages/send.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { sendMessage } from "./pf_client.ts";
import type { Messenger } from "./types.ts";

const VALID_MESSENGERS = ["signal", "simplex", "session"] as const;

/**
 * Create and configure the MCP server.
 */
export function createMcpServer(): McpServer {
  const server = new McpServer({
    name: Deno.env.get("MCP_SERVER_NAME") || "privacyflow-channel",
    version: "0.1.0",
  });

  server.tool(
    "pf_send_message",
    "Send a message to a PrivacyFlow contact or group via Signal, SimpleX, or Session.",
    {
      contactId: z.string().describe("Recipient's contact identifier (phone number for Signal, Session ID for Session, connection ID for SimpleX)"),
      message: z.string().max(10000).describe("Message content to send (max 10,000 characters)"),
      messenger: z.enum(VALID_MESSENGERS).describe("Messenger protocol: signal, simplex, or session"),
      groupId: z.string().optional().describe("Group ID when sending to a group. Omit for DM."),
    },
    async (params) => {
      console.log(`[mcp] 📤 pf_send_message called: ${params.messenger} → ${params.contactId}${params.groupId ? ` (group: ${params.groupId})` : ""}`);

      try {
        const result = await sendMessage(
          params.contactId,
          params.message,
          params.messenger as Messenger,
          params.groupId,
        );

        if (result.totalFailed > 0) {
          const failures = result.failedMessages.map((f) => f.error).join("; ");
          return {
            content: [{
              type: "text" as const,
              text: `⚠️ Partial failure: ${result.totalAccepted} sent, ${result.totalFailed} failed. Errors: ${failures}`,
            }],
          };
        }

        return {
          content: [{
            type: "text" as const,
            text: `✅ Message sent successfully to ${params.contactId} via ${params.messenger}`,
          }],
        };
      } catch (error) {
        const errMsg = error instanceof Error ? error.message : String(error);
        console.error(`[mcp] ❌ pf_send_message failed:`, errMsg);
        return {
          content: [{
            type: "text" as const,
            text: `❌ Failed to send message: ${errMsg}`,
          }],
          isError: true,
        };
      }
    },
  );

  console.log("[mcp] ✅ MCP server created with pf_send_message tool");
  return server;
}
