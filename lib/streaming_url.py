# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Read which stack a WorkSpaces Applications streaming URL was created for.

A streaming URL carries its parameters as URL-safe base64 JSON in the ``parameters`` query
parameter. ``catalogSource`` names the stack (``stack/<name>``) and ``region`` is the stack's Region,
which WorkSpaces Applications adds for accounts with agent access. The Agent Access MCP server reads
the same two fields, the same way, to authorize the session, and refuses a URL without a stack.
"""

import base64
import json
import re
from urllib.parse import parse_qs, urlparse

from .saml_assertion import REGION_PATTERN, STACK_NAME_PATTERN

_STACK_PREFIX = "stack/"
_REGION = re.compile(REGION_PATTERN)
_STACK_NAME = re.compile(STACK_NAME_PATTERN)


def stack_of(streaming_url):
    """Return ``(region, stack_name)`` of the stack ``streaming_url`` was created for.

    Raises ValueError when the URL has no readable parameters, names no stack (an image builder
    URL, for example) or has no valid Region. The message never contains the URL, which grants
    access to the desktop.
    """
    values = parse_qs(urlparse(streaming_url).query).get("parameters")
    if not values:
        raise ValueError("the streaming URL has no parameters")
    encoded = values[0]
    try:
        params = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    except ValueError:
        # from None: the decoder's message can quote part of the URL
        raise ValueError("the streaming URL's parameters can't be read") from None
    if not isinstance(params, dict):
        raise ValueError("the streaming URL's parameters can't be read")
    catalog_source = params.get("catalogSource")
    region = params.get("region")
    if not isinstance(catalog_source, str) or not catalog_source.startswith(_STACK_PREFIX):
        raise ValueError("the streaming URL is not for a stack (Replay Session records stacks, not image builders)")
    name = catalog_source[len(_STACK_PREFIX):]
    if not _STACK_NAME.fullmatch(name):
        raise ValueError("the streaming URL names no valid stack")
    if not isinstance(region, str) or not _REGION.fullmatch(region):
        raise ValueError("the streaming URL has no valid Region")
    return region, name
