# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Command-line entry point for the MCP tool forwarding compatibility tester."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from . import contract, report
from .config import ConfigError, ForwardingConfig, ServerSpec, load_config
from .runner import CallRequest, RunOptions, run_config


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp-forwarding-test",
        description=(
            "Verify that MCP servers work with MCP tool forwarding on WorkSpaces "
            "Applications. Reproduces how the service launches servers and "
            "lists/calls their tools."
        ),
    )
    sub = parser.add_subparsers(dest="mode", required=True)

    # Shared options for both subcommands.
    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--call",
            action="append",
            default=[],
            metavar="TOOL",
            help="invoke this tool (opt-in active test). Repeatable. Pair with "
            "--args-json positionally in order.",
        )
        p.add_argument(
            "--args-json",
            action="append",
            default=[],
            metavar="JSON",
            help="JSON object of arguments for the matching --call (by order).",
        )
        p.add_argument(
            "--probe-tools",
            action="store_true",
            help="smoke-call every tool whose input schema has no required args.",
        )
        p.add_argument("--json", action="store_true", help="emit a machine-readable report.")

    p_config = sub.add_parser(
        "config",
        help="test all servers in a forwarding config (mcp_server_redirection_config.json)",
    )
    p_config.add_argument(
        "path",
        nargs="?",
        default=None,
        help=f"path to the config (default: {contract.default_config_path()})",
    )
    add_common(p_config)

    p_server = sub.add_parser("server", help="test a single ad-hoc server")
    p_server.add_argument("--command", required=True, help="server executable")
    p_server.add_argument(
        "--arg",
        action="append",
        default=[],
        dest="args",
        metavar="ARG",
        help="one argument to the server command (repeatable, order preserved).",
    )
    p_server.add_argument(
        "--name", default="adhoc", help="logical server name for namespacing (default: adhoc)"
    )
    add_common(p_server)

    return parser


def _parse_calls(call_names: list[str], args_jsons: list[str]) -> list[CallRequest]:
    calls: list[CallRequest] = []
    for i, name in enumerate(call_names):
        arguments: dict | None = None
        if i < len(args_jsons):
            try:
                arguments = json.loads(args_jsons[i])
            except json.JSONDecodeError as exc:
                raise SystemExit(
                    f"--args-json #{i + 1} for --call {name} is not valid JSON: {exc}"
                )
            if not isinstance(arguments, dict):
                raise SystemExit(f"--args-json #{i + 1} must be a JSON object")
        calls.append(CallRequest(tool=name, arguments=arguments))
    return calls


def _load(ns: argparse.Namespace) -> ForwardingConfig:
    if ns.mode == "config":
        path = ns.path or contract.default_config_path()
        return load_config(path)
    # ad-hoc single server
    spec = ServerSpec(name=ns.name, command=ns.command, args=list(ns.args))
    return ForwardingConfig(servers=[spec], source="<command-line>")


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    ns = parser.parse_args(argv)

    try:
        config = _load(ns)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return report.EXIT_USAGE

    opts = RunOptions(
        calls=_parse_calls(ns.call, ns.args_json),
        probe_tools=ns.probe_tools,
    )

    result = asyncio.run(run_config(config, opts))

    if ns.json:
        report.render_json(result)
    else:
        report.render_text(result)

    return report.exit_code(result)


if __name__ == "__main__":
    raise SystemExit(main())
