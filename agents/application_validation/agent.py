#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Application Validation Agent - Validates desktop applications on a remote desktop."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))

from lib import agent_common


def main():
    return agent_common.run_standard_agent(
        agent_dir=os.path.dirname(os.path.abspath(__file__)),
        description='Application Validation Agent (Strands)',
        banner_title="Application Validation Agent",
        banner_body=(
            "The agent opens and validates desktop\n"
            "applications on a remote Windows desktop."
        ),
        skill_filename="application-validation-skill.json",
        skill_label="APPLICATION VALIDATION SKILL",
        # Stays on stateful MCP, so the repo keeps a stateful sample (quickstart.py and generic_cua are stateless).
        mcp_protocol=agent_common.MCP_PROTOCOL_STATEFUL,
    )


if __name__ == "__main__":
    sys.exit(main())
