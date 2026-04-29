#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""PDF Extractor Demo Agent - Extracts data from PDFs on a remote desktop."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))

from lib import agent_common


def main():
    return agent_common.run_standard_agent(
        agent_dir=os.path.dirname(os.path.abspath(__file__)),
        description='PDF Extractor Demo Agent (Strands)',
        banner_title="PDF Extractor Demo Agent",
        banner_body=(
            "The agent extracts data from PDF documents\n"
            "on a remote Windows desktop using Firefox,\n"
            "OpenOffice Writer, and File Explorer."
        ),
        skill_filename="pdf_extractor-skill.json",
        skill_label="FIREFOX, OPENOFFICE WRITER, FILE EXPLORER SKILL",
    )


if __name__ == "__main__":
    sys.exit(main())
