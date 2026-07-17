# MCP Tool Forwarding Compatibility Tester

Verify that the MCP servers you set up for tool forwarding on Amazon WorkSpaces
Applications work, before you bake them into a fleet image.

When forwarding is enabled, the WorkSpaces Applications MCP server (the "service")
launches the servers in your config and forwards their tools to your agent. A server
that runs fine locally can still fail to forward: it may start too slowly, list zero
tools, or ship a config the service rejects. This tool reproduces how the service
launches and queries your servers, then reports, per server, what works and what to fix.

For the forwarding feature itself, see the [WorkSpaces Applications MCP server docs](https://docs.aws.amazon.com/appstream2/latest/developerguide/agent-access-mcp-server.html#agent-access-mcp-tool-forwarding-details).

## Install

Requires Python 3.10+. Install it on the WorkSpaces Applications image builder, where
your MCP server is installed.

On the image builder, in PowerShell:

```powershell
py -m pip install "https://github.com/aws-samples/sample-code-for-workspaces-agent-access/archive/refs/heads/main.zip#subdirectory=utils/mcpforwardingtester"
```

For local development, from the repo root, in your terminal:

```bash
pip install ./utils/mcpforwardingtester
```

## Usage

On the image builder, in PowerShell, validate the deployed config before you create
the image. No path is needed; it defaults to the location the service reads:

```powershell
py -m mcp_forwarding_tester config
# defaults to C:\ProgramData\NICE\dcv\mcp_server_redirection_config.json
```

Fix anything it reports, re-run until the verdict is `OK`, then create the image.

Other forms, also in PowerShell on the image builder:

```powershell
# Point at a specific config file
py -m mcp_forwarding_tester config C:\ProgramData\NICE\dcv\mcp_server_redirection_config.json

# Test a single server ad hoc (order of --arg is preserved)
py -m mcp_forwarding_tester server --command C:/Python312/python.exe --arg C:/McpServers/my_server.py

# Opt-in: invoke a tool
py -m mcp_forwarding_tester server --command C:/McpServers/my_server.exe --call search --args-json '{"q": "hello"}'

# Opt-in: smoke-call every tool that needs no required arguments
py -m mcp_forwarding_tester config --probe-tools

# Machine-readable output (for a build gate)
py -m mcp_forwarding_tester config --json
```

Locally (macOS, Linux, or WSL), swap the Windows `py` launcher for `python`, e.g.
`python -m mcp_forwarding_tester server --command ./my-server`.

Exit codes: `0` = no failures (warnings allowed), `1` = at least one FAIL, `2` = usage
or config-load error.

> **Run latency checks on the image builder.** The ~5 s `tools/list` budget only
> reflects real cold-start cost on an instance, not on a fast laptop. See
> [Slow servers & the 5 s budget](#slow-servers--the-5-s-budget).

## What it checks

Each check is a requirement your server must meet to forward successfully.

| Check | What must be true |
| --- | --- |
| `config.encoding` | The config file is UTF-8 **without a BOM**. Windows editors (Notepad, some PowerShell `Set-Content` calls) add one by default, and the service rejects it. |
| `config.command` / `config.ignored-keys` | A server entry uses only `command` and `args`. **`env`/`cwd` are ignored** (a common mistake), so it warns. |
| `spawn` | The server launches from `command` + `args` over stdio. |
| `initialize` | The MCP handshake completes. |
| `protocol-version` | Reports the negotiated MCP protocol version (see fidelity note). |
| `tools-list` | **The server returns ≥1 tool.** A server that lists none is dropped; if no server lists any, forwarding yields no tools. |
| `tool-names` | Tool names stay unambiguous once namespaced as `forwarded___<server>___<tool>`. |
| `tools-list-latency` | **Listing tools finishes within ~5 s.** Over the budget the tools are dropped and your agent sees none. |
| `initialize-latency` | Spawn + `initialize` time (WARN-only; a slow start makes `tools/list` more likely to exceed its budget). |
| `call:<tool>` | Opt-in: a tool call returns a non-error result within ~5 s. |
| `aggregate.tools` | At least one tool total across all servers. |

## Slow servers & the 5 s budget

Listing tools (and each tool call) is time-boxed at **5 seconds**, with no setting to
raise it. A server whose first listing is slow (cold interpreter startup, heavy imports,
antivirus scanning, or a network call at startup) is cut off, and the agent sees **no
tools**. A warm retry then works, which can mask the problem.

To fix a slow server: cut startup work, pre-warm it once so imports and AV scans are
cached, disable telemetry, and make sure any startup network call succeeds fast.

## Setting env vars: use a wrapper

A server entry has only `command` and `args`, so you cannot set environment variables in
the config. Launch the server through a wrapper script that sets the env and execs it;
the launched process inherits the wrapper's environment. On Windows, point `command` at
`cmd.exe /c <wrapper>` (a `.cmd`/`.bat` cannot be exec'd directly), and start the wrapper
with `@echo off`, or shell echoing will corrupt the JSON-RPC stdout.

```bat
REM launch-myserver.cmd
@echo off
set "MY_ENV_VAR=value"
"C:\path\to\python.exe" "C:\path\to\myserver.py" %*
```

```json
{
  "mcpServers": {
    "myserver": {
      "command": "C:/Windows/System32/cmd.exe",
      "args": ["/c", "C:/path/to/launch-myserver.cmd"]
    }
  }
}
```

## Fidelity note

This tool uses the official Python `mcp` SDK to approximate the service's client. The one
difference worth checking is the negotiated **MCP protocol version** (the tool prints it):
if your server accepts only one version, confirm it matches what the service negotiates.

## Development

```bash
pip install -e '.[test]'
pytest
```
