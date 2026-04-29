#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Paint Demo Agent - Draws in MS Paint on a remote desktop."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))

from lib import agent_common


def main():
    return agent_common.run_standard_agent(
        agent_dir=os.path.dirname(os.path.abspath(__file__)),
        description='Paint Demo Agent (Strands)',
        banner_title="Paint Drawing Demo Agent",
        banner_body=(
            "The agent opens a paint application on\n"
            "a remote Windows desktop and creates\n"
            "artwork using mouse and keyboard tools."
        ),
        skill_filename="ms-paint-skill.json",
        skill_label="MS PAINT SKILL",
    )


if __name__ == "__main__":
    sys.exit(main())
