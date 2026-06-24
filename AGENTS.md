# Agent Coding Standards — privacyflow-agent-zero-channel

Agent Zero plugin that connects PrivacyFlow (Signal, SimpleX, Session) to Agent Zero. Uses `job_loop` extension to poll PrivacyFlow for incoming messages and `process_chain_end` extension to send agent responses back. No external service required — the plugin runs inside Agent Zero with direct access to `AgentContext` and `context.communicate()`.

---

## Architecture

Pure A0 plugin. No bridge service, no MCP server, no HTTP API. The plugin:

1. **`job_loop` extension** (`_10_pf_poll.py`): Starts a background asyncio task that polls `GET /api/v1/messages/poll` every few seconds. For each message, finds or creates an `AgentContext`, stores routing metadata in `context.data['pf_routing']`, and calls `context.communicate(UserMessage(...))` directly.
2. **`process_chain_end` extension** (`_50_pf_reply.py`): When the agent finishes processing, extracts the last response from `context.log.logs` (type=="response"), splits it for messenger length limits, and sends it back via `POST /api/v1/messages/send`.

Configured for **one appId per instance**. Context mapping is in-memory via `context.data['pf_routing']['mapping_key']`. No agent profile or prompt modifications.

---

## Project Structure

```
privacyflow-agent-zero-channel/
├── plugin.yaml                                # Plugin manifest (A0 runtime)
├── index.yaml                                 # Plugin index metadata (a0-plugins)
├── README.md
├── LICENSE
├── helpers/
│   ├── pf_client.py                           # PrivacyFlow API client (poll, send, auth, health)
│   └── message_splitter.py                     # Split long messages for messenger limits
├── extensions/python/
│   ├── job_loop/
│   │   └── _10_pf_poll.py                      # Background poller → context.communicate()
│   └── process_chain_end/
│       └── _50_pf_reply.py                     # Extract response → send via PF API
├── .gitea/workflows/
│   └── lint_and_check.yaml                     # CI/CD (lint + syntax check)
├── .env.example
└── AGENTS.md
```

---

## PrivacyFlow Public API

The plugin integrates with `privacyflow-public-backend-api`. The public API exposes exactly 4 endpoints:

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/v1/health` | GET | Health check |
| `/api/v1/auth/verify` | GET | Validate API key, return appIds |
| `/api/v1/messages/poll` | GET | Poll incoming messages from `poll:{appId}` Redis queue |
| `/api/v1/messages/send` | POST | Send outgoing messages to `outgoing-{messenger}` BullMQ queues |

**PolledMessage**: `{appId, messenger, contactId, messageId, content, timestamp, groupId?, isGroupMessage}`

**SendMessageInput**: `{appId, contactId, message, messenger, groupId?}`

No group creation, member management, or app info endpoints. Groups are created in the dashboard. The API is a message relay — poll and send. That's it.

A success response (200/202) from the send endpoint guarantees messages are in the BullMQ queue.

---

## Code Style

### Python

- Use explicit type annotations for function parameters and return types
- Prefer `async/await` over `.then()` chains
- File naming: `snake_case.py`
- Export functions individually, not as default exports
- Use `from helpers.*` imports (not `from python.helpers.*`)

### Naming
- `camelCase` — not used in Python; use `snake_case` for functions, variables, parameters
- `PascalCase` — classes
- `SCREAMING_SNAKE_CASE` — module-level constants
- Prefix booleans with `is`/`has`/`can`

### Error Handling

- Always handle errors explicitly — never silent failures
- Log with bracketed service prefix:
  ```python
  PrintStyle.error(f"[pf_channel] Failed to poll messages: {error}")
  PrintStyle.info(f"[pf_channel] ✅ Forwarded message to context {context.id}")
  ```
- Use structured logging with emojis for visibility (✅ ❌ 🔄 📦 🚨)
- Validate environment variables at startup, fail fast on missing vars

---

## Testing Requirements

### 1. Always Test Before Claiming Done

- **NEVER** claim something is "done" or "complete" without running tests
- **ALWAYS** run tests to verify they actually pass
- If tests fail, fix issues before marking task complete

### 2. Test Commands

```bash
# Syntax check all Python files
find . -name '*.py' -exec python3 -m py_compile {} \;
```

### 3. Test Quality Standards

- Run each test suite before submitting
- Check for resource leaks (TCP connections, file handles)
- Clean up test data after each test
- Handle async operations properly (close connections, await promises)

---

## Communication

### Status Reporting

- **ACCURATE**: Only report what you've verified works
- **HONEST**: If something is broken, say so
- **DETAILED**: Provide specific test results (pass/fail counts, error messages)

---

## Third-Party Dependencies

Before adding code that uses third-party libraries:

1. **ASK** if the dependency is already configured
2. **VERIFY** the package is installed in the A0 environment
3. **DON'T** add code that references packages that don't exist

---

## Branch Naming Convention

**CRITICAL**: Branch names must follow strict naming conventions to trigger CI/CD workflows.

| Branch Type | Pattern | Example |
|-------------|---------|---------|
| Feature | `feat-[description]` | `feat-initial-structure` |
| Fix | `fix-[description]` | `fix-poller-reconnect` |
| Chore | `chore-[description]` | `chore-update-deps` |

---

## Git Rules

- **NEVER force push** — Always create new commits instead of amending and force pushing
- If a commit needs changes, create a new commit on top of the existing one

---

## Security

- **API Keys**: Never log or expose API keys
- **Secrets**: Use environment variables, never hardcode
- **Network**: Services communicate via NetBird VPN
- **Auth**: Plugin uses PF API key for PrivacyFlow poll + send

---

## Environment Variables

| Variable | Purpose |
|---|---|
| `PF_API_BASE` | PrivacyFlow public backend API URL |
| `PF_API_KEY` | PrivacyFlow API key (for poll + send) |
| `PF_APP_ID` | PrivacyFlow app ID (one per instance) |

Never commit `.env` files. Validate presence at startup, fail fast on missing vars.
