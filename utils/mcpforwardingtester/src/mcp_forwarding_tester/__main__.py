# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Enable ``python -m mcp_forwarding_tester``."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
