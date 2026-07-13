#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""MCP Redirection Demo Agent - Drives forwarded MCP tools on a remote desktop.

Requires a fleet built with scripts/setup_mcp_redirection.sh (FORWARD_MCP_TOOLS
enabled), which installs the example `filesystem` and `fetch` MCP servers on the
Windows host. Those servers' tools appear to the agent as forwarded tools
(prefixed with `forwarded___`) alongside the usual desktop tools.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))

from lib import agent_common


def main():
    return agent_common.run_standard_agent(
        agent_dir=os.path.dirname(os.path.abspath(__file__)),
        description='MCP Redirection Demo Agent (Strands)',
        banner_title="MCP Redirection Demo Agent",
        banner_body=(
            "The agent exercises forwarded MCP tools\n"
            "(filesystem + fetch) running on a remote\n"
            "Windows desktop, then verifies the result\n"
            "on the desktop itself."
        ),
        skill_filename="mcp-redirection-skill.json",
        skill_label="MCP REDIRECTION SKILL",
    )


if __name__ == "__main__":
    sys.exit(main())
