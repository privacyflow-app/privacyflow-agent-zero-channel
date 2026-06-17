# Agent Coding Standards — privacyflow-agent-zero-channel

Deno bridge service connecting PrivacyFlow's public backend API to Agent Zero. Exposes an MCP server, background poller, and HTTP API for bi-directional messaging via Signal, SimpleX, and Session.

---

## Architecture

Two components:

1. **Bridge Service** (Deno, this repo): Polls PrivacyFlow for incoming messages, forwards to A0 plugin endpoint, exposes MCP server with `pf_send_message` tool, provides HTTP API for A0 plugin to send responses back.
2. **A0 Plugin** (`_privacyflow_channel`, lives in Agent Zero user plugins): Receives messages via API endpoint, uses `process_chain_end` extension to push responses back through the bridge.

Configured for **one appId per instance**. Context mapping is in-memory (`{contact_id|group_id → agentZeroContextId}`). No agent profile or prompt modifications.

---

## Commands

```bash
# Start dev server
deno task start

# Run tests
deno task test

# Type check
deno task check

# Lint
deno task lint

# Format
deno task fmt
```

### Pre-commit requirement

**Always run before committing — do not commit with failing checks or tests:**

```bash
deno check src/ && deno lint src/ && deno test
```

---

## Project Structure

```
privacyflow-agent-zero-channel/
├── src/
│   ├── main.ts              # Entry: starts poller + MCP server + HTTP API
│   ├── pf_poller.ts         # Background loop: GET /api/v1/messages/poll → forward to A0
│   ├── pf_client.ts         # PrivacyFlow API client (poll + send)
│   ├── mcp_server.ts        # MCP server exposing pf_send_message tool
│   ├── http_api.ts          # HTTP endpoint for A0 plugin's process_chain_end
│   ├── context_mapper.ts    # In-memory {contact_id|group_id → ctx_id}
│   └── types.ts             # Shared types matching PF API types exactly
├── deno.json
├── Dockerfile
├── .env.example
└── AGENTS.md
```

---

## PrivacyFlow Public API

The bridge integrates with `privacyflow-public-backend-api`. The public API exposes exactly 4 endpoints:

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/v1/health` | GET | Health check |
| `/api/v1/auth/verify` | GET | Validate API key, return appIds |
| `/api/v1/messages/poll` | GET | Poll incoming messages from `poll:{appId}` Redis queue |
| `/api/v1/messages/send` | POST | Send outgoing messages to `outgoing-{messenger}` BullMQ queues |

**PolledMessage**: `{appId, messenger, contactId, messageId, content, timestamp, groupId?, isGroupMessage, isCommand, command?}`

**SendMessageInput**: `{appId, contactId, message, messenger, groupId?}`

No group creation, member management, or app info endpoints. Groups are created in the dashboard. The API is a message relay — poll and send. That's it.

A success response (200/202) from the send endpoint guarantees messages are in the BullMQ queue. If `queue.addBulk()` throws, the API returns 503, not success.

---

## Code Style

### TypeScript/Deno

- **Strict mode** enabled — no `any` without justification
- Use explicit type annotations for function parameters and return types
- Prefer `async/await` over `.then()` chains
- File naming: `kebab-case.ts`
- Export functions individually, not as default exports

### Imports

```typescript
// 1. Deno std (double quotes)
import { serve } from "https://deno.land/std@0.208.0/http/server.ts";

// 2. Third-party (double quotes)
import { Redis } from "npm:ioredis@^5.0.0";

// 3. Local (single quotes, type imports separated)
import { pfClient } from './pf_client.ts';
import type { PolledMessage } from './types.ts';
```

### Naming
- `camelCase` — functions, variables, parameters
- `PascalCase` — interfaces, types, classes
- `SCREAMING_SNAKE_CASE` — module-level constants
- Prefix booleans with `is`/`has`/`can`

### Error Handling

- Always handle errors explicitly — never silent failures
- Log with bracketed service prefix:
  ```typescript
  console.error('[poller] Failed to poll messages:', error.message);
  console.log('[mcp] Tool pf_send_message called');
  ```
- Use structured logging with emojis for visibility (✅ ❌ 🔄 📦 🚨)
- Close connections in `finally` blocks
- Validate environment variables at startup, fail fast on missing vars

---

## Testing Requirements

### 1. Always Test Before Claiming Done

- **NEVER** claim something is "done" or "complete" without running tests
- **ALWAYS** run tests to verify they actually pass
- If tests fail, fix issues before marking task complete

### 2. Test Commands

```bash
# Run all tests
deno test --allow-all --env-file=.env

# Run a specific test file
deno test --allow-all --env-file=.env src/pf_client_test.ts
```

### 3. Test Quality Standards

- Run each test suite before submitting
- Check for resource leaks (TCP connections, file handles, Redis connections)
- Clean up test data after each test
- Handle async operations properly (close connections, await promises)
- Use `--env-file=[path]` flag to load environment variables

---

## Communication

### Status Reporting

- **ACCURATE**: Only report what you've verified works
- **HONEST**: If something is broken, say so
- **DETAILED**: Provide specific test results (pass/fail counts, error messages)

### Violations

**Example of BAD behavior (DO NOT DO THIS):**
```
User: "Write tests for the poller"
You: "✅ All tests created! 18 tests in pf_poller_test.ts"
(Reality: You never ran the tests, they all fail with errors)
```

**Example of GOOD behavior:**
```
User: "Write tests for the poller"
You: "I've created the tests in pf_poller_test.ts. Let me run them to verify..."

[Run tests, check output]

You: "Tests are created but encountering issues:
- 3 tests pass (message parsing)
- 15 tests fail due to Redis connection leaks
- Need to add proper cleanup to close Redis connections

I'll fix these issues now."
```

---

## Third-Party Dependencies

Before adding code that uses third-party libraries:

1. **ASK** if the dependency is already configured
2. **VERIFY** the package is available in Deno's import system
3. **DON'T** add code that references packages that don't exist
4. Check `deno.json` for configured imports

---

## Branch Naming Convention

**CRITICAL**: Branch names must follow strict naming conventions to trigger CI/CD workflows.

| Branch Type | Pattern | Example |
|-------------|---------|---------|
| Feature | `feat-[description]` | `feat-initial-structure` |
| Fix | `fix-[description]` | `fix-poller-reconnect` |
| Chore | `chore-[description]` | `chore-update-deps` |

**DO NOT USE:**
- `feature/` prefix (use `feat-` instead)
- Descriptive names without prefix (won't trigger builds)

---

## Git Rules

- **NEVER force push** — Always create new commits instead of amending and force pushing
- If a commit needs changes, create a new commit on top of the existing one
- Force pushing destroys history and causes problems for anyone else who has pulled the branch

---

## Security

- **API Keys**: Never log or expose API keys
- **Secrets**: Use environment variables, never hardcode
- **Network**: Services communicate via NetBird VPN
- **Auth**: Bridge uses API key auth for both PrivacyFlow and A0 plugin endpoints

---

## Environment Variables

| Variable | Purpose |
|---|---|
| `PF_API_BASE` | PrivacyFlow public backend API URL |
| `PF_API_KEY` | PrivacyFlow API key (for poll + send) |
| `A0_API_BASE` | Agent Zero Web UI endpoint |
| `A0_API_KEY` | A0 plugin endpoint API key (shared with bridge) |
| `POLL_INTERVAL_MS` | Poll frequency (default: 2000) |
| `PORT` | Bridge HTTP API port (default: 3005) |

Never commit `.env` files. Validate presence at startup, fail fast on missing vars.
