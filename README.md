# PrivacyFlow Channel — Agent Zero Plugin

Bi-directional channel connecting [PrivacyFlow](https://privacyflow.ai) (Signal, SimpleX, Session) to [Agent Zero](https://agentzero.ai). Polls PrivacyFlow for incoming messages and sends agent responses back.

## How It Works

The plugin runs entirely inside Agent Zero using two extension hooks:

1. **`job_loop`** — Starts a background poller that fetches incoming messages from PrivacyFlow's public API every few seconds. Each message is forwarded to the appropriate Agent Zero context via `context.communicate()`.
2. **`process_chain_end`** — When the agent finishes processing, extracts the last response and sends it back to PrivacyFlow via the send API. Long messages are automatically split to fit messenger length limits.

No external bridge service, MCP server, or HTTP API required.

## Installation

1. Copy this plugin folder into your Agent Zero `plugins/` directory.
2. Set the required environment variables in your Agent Zero `.env` file.

## Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `PF_API_BASE` | Yes | PrivacyFlow public backend API URL |
| `PF_API_KEY` | Yes | PrivacyFlow API key for poll + send |
| `PF_APP_ID` | Yes | PrivacyFlow app ID (one per instance) |

## Supported Messengers

- **Signal** — 2000 char limit per message
- **SimpleX** — 8000 char limit per message
- **Session** — 2000 char limit per message

Messages exceeding the limit are automatically split at paragraph, line, or word boundaries.

## Architecture

```
PrivacyFlow Public API
        ↕
  pf_client.py (poll + send)
        ↕
  job_loop/_10_pf_poll.py  →  context.communicate(UserMessage)
        ↕
  Agent Zero processes message
        ↕
  process_chain_end/_50_pf_reply.py  →  send_message() back to PF
```

## License

MIT — see [LICENSE](./LICENSE).
