# Migration Guide: Beta → Public Preview (MCP Server)

This guide helps beta customers migrate from the local agentic client (RPM) to the remote Agent Access MCP Server.

## What's Changing

| | Beta (local client) | Public Preview (MCP Server) |
|---|---|---|
| Transport | Local RPM binary via stdio | Remote HTTP endpoint via SigV4 |
| Setup | Extract RPM, install system libraries | `pip install mcp-proxy-for-aws` |
| Connection | `--streaming-url "<URL>"` | `--streaming-url "<URL>"` (endpoint auto-configured) |
| Dependencies | RPM + system libs (libva, libX11, etc.) | Python only |
| Platforms | Linux x86_64 only | Any platform with Python 3.10+ |

## What Stays the Same

- Your fleet, stack, and streaming URLs
- Your agent code (prompts, skills, task logic)
- Your Bedrock model and region configuration
- Your `--streaming-url` argument

## Migration Steps

### 1. Run the migration script

```bash
./scripts/migrate_to_mcp.sh
```

This script:
- Installs `mcp-proxy-for-aws`
- Verifies connectivity to the MCP server
- Optionally removes the local RPM and extracted client

### 2. Update custom agents (if applicable)

If you built custom agents that directly use `StdioServerParameters` / `stdio_client`, replace the transport:

**Before:**
```python
from mcp.client.stdio import StdioServerParameters, stdio_client

server_params = StdioServerParameters(
    command="agentic-client/usr/libexec/dcvviewer-headless/dcvviewer-headless",
    args=["--streaming-url", streaming_url],
    env=env,
)
mcp_client = MCPClient(lambda: stdio_client(server_params))
```

**After:**
```python
from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client
import os

def mcp_factory():
    return aws_iam_streamablehttp_client(
        endpoint=os.environ["MCP_ENDPOINT"],
        aws_service=os.environ["AWS_SERVICE_NAME"],
        # Sign for the runtime's region. MCP is deployed per-region.
        aws_region=os.environ.get("AWS_REGION", "us-west-2"),
        headers={
            "X-Amzn-AgentAccess-Streaming-Session-Url": streaming_url,
        },
    )
mcp_client = MCPClient(mcp_factory)
```

Set `MCP_ENDPOINT` and `AWS_SERVICE_NAME`  (refer to config.json) in your runtime environment — both are required.

Or if using our framework, just use `agent_common.create_mcp_client_factory(args)` — it reads the endpoint from `config.json` automatically and picks the signing region from `AWS_REGION`.

### 3. Clean up (optional)

After verifying the MCP server works, run the migration script with cleanup:
```bash
./scripts/migrate_to_mcp.sh --remove-local
```

## Troubleshooting

- `401 Unauthorized` — your AWS credentials can't sign requests to the MCP service. Check IAM permissions.
- `403 Forbidden` — check that your IAM credentials have the required permissions for the MCP endpoint.
- `400 Bad Request` — the streaming URL is missing or expired.
- `isError: True` / `tools field required` — the MCP server connected but the desktop session isn't ready yet. Wait a moment and retry.
- `mcp-proxy-for-aws not found` — run `pip install mcp-proxy-for-aws==1.4.0`

## FAQ

**Do I need to redeploy my fleet?**
Yes. Your existing fleet and stack work requires new agentic support features.

**Do I need to change my prompts or skills?**
No. The agent logic is unchanged — only the transport layer is different.

**Does the MCP server support all the same tools?**
Yes. The same screenshot, click, type, key, scroll tools are available through both transports.
