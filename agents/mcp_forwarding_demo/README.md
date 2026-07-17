# MCP Tool Forwarding Demo Agent

Demonstrates **MCP tool forwarding** on Amazon WorkSpaces
Applications. The agent uses MCP servers running *on the Windows host* —
exposed with a `forwarded___` prefix — to read/write files and fetch web
content, then confirms the result on the desktop.

Unlike the other demos (which drive GUI apps via screenshots and clicks), this
one shows the agent calling structured, headless tools that the fleet forwards
from the host.

## Prerequisites

This demo requires a fleet built with MCP tool forwarding enabled. Build one with:

```bash
./scripts/setup_mcp_forwarding.sh --region us-east-1
```

That script installs the example `filesystem` and `fetch` MCP servers
(`mcp_servers/`) onto a Windows image and creates a fleet + stack with
`FORWARD_MCP_TOOLS: ENABLED`. When the fleet is running, `tools/list` returns
both the usual desktop tools and the forwarded ones.

## What It Does

1. Discovers the `forwarded___` tools and identifies which server each belongs to.
2. Lists `C:\Users\Public\Documents` and reads the seeded `hello.txt` (forwarded filesystem).
3. Fetches `https://example.com` and extracts its text (forwarded fetch).
4. Writes `mcp_forwarding_report.txt` combining the above, then reads it back to verify (forwarded filesystem).
5. Opens the report in Notepad and takes one screenshot as visual confirmation (desktop tools).

## Run

```bash
source venv/bin/activate
STREAMING_URL=$(aws appstream create-streaming-url \
  --stack-name MCPForwardingStack --fleet-name MCPForwarding \
  --user-id test --validity 3600 \
  --query StreamingURL --output text)
python3 agents/mcp_forwarding_demo/agent.py --streaming-url "$STREAMING_URL"
```

> The streaming URL must be for the forwarding-enabled fleet/stack
> (`MCPForwarding` / `MCPForwardingStack` — the defaults created by
> `setup_mcp_forwarding.sh`, which prints this exact command on completion).
> `scripts/streaming_url.sh` reads the *standard* fleet/stack from
> `config.json`, so it will not expose forwarded tools unless you point
> `config.json` at the forwarding fleet.

## Structure

```
mcp_forwarding_demo/
├── agent.py          # Agent orchestrator (standard pipeline)
├── prompts/
│   ├── system_prompt.md  # Desktop + forwarded-tool guidance
│   └── task_prompt.md    # Deterministic filesystem + fetch workflow
├── skills/
│   └── mcp-forwarding-skill.json  # How to use the forwarded tools
├── logs/             # Runtime logs
├── metrics/          # Performance metrics
└── screenshots/      # Screenshots captured during execution
```

## Notes

- The forwarded `filesystem` server is sandboxed to `C:\Users\Public\Documents`.
- The forwarded `fetch` server calls the public internet from the host, so the
  fleet needs outbound network access for Step 3.
- If the agent reports no `forwarded___` tools, the streaming URL is for a
  non-forwarding fleet, or the stack is missing `FORWARD_MCP_TOOLS: ENABLED`.
