# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Domain Join request signing: put the SAML assertion in ``_meta``, *then* sign.

A domain-joined fleet has no streaming URL. The Agent Access MCP endpoint identifies the
desktop session from a SAML assertion and the stack ARN, carried in the JSON-RPC ``_meta``
of the requests. They go with every request. A live domain-joined fleet accepted these keys
and refused a request that carried an expired assertion with HTTP 400 "SAML assertion has
expired", so a client that keeps sending the assertion it started with stops working when
that assertion expires.

Order matters. The request body is covered by the SigV4 signature (the payload hash and
``Content-Length``), so ``_meta`` has to be in place *before* signing.
``mcp-proxy-for-aws``'s ``metadata=`` option adds it in an httpx request hook, and httpx
runs request hooks *after* the ``auth=`` flow that signs - the body changes after it was
signed and the declared ``Content-Length`` no longer matches. It also skips requests
without ``params`` (the first ``tools/list`` page, ``ping``). Here the injection runs
inside the auth flow, in front of the proxy's own signer, which then signs the body that
is actually sent.

Imports only ``httpx`` and ``json`` so the transport logic is testable in isolation.
"""

import json

import httpx

# _meta keys for a Domain Join session; a live fleet accepted these. (The public developer
# guide's example shows other names, saml_response / stack_arn, which were not tried.)
META_KEY_SAML_ASSERTION = "aws.agentaccess/workspacesApplicationsSamlAssertion"
META_KEY_STACK_ARN = "aws.agentaccess/workspacesApplicationsStackArn"


def build_meta(assertion, stack_arn):
    """Return the ``_meta`` entries that authenticate a Domain Join session."""
    return {META_KEY_SAML_ASSERTION: assertion, META_KEY_STACK_ARN: stack_arn}


def inject_meta(body, meta):
    """Return ``body`` with ``meta`` merged into ``params._meta``, or ``None`` to leave it alone.

    Only JSON-RPC *requests* (``method`` plus ``id``) are changed. Notifications and the
    client's answers to server requests pass through untouched, as does anything that is
    not a single JSON object. ``params`` is created when absent (``tools/list`` without a
    cursor, ``ping``); an existing ``_meta`` is kept and merged, with ``meta`` winning on a
    key clash; a ``_meta`` that is not an object is replaced.
    """
    try:
        message = json.loads(body)
    except ValueError:  # not JSON (or not UTF-8): nothing to inject into
        return None
    if not isinstance(message, dict) or "method" not in message or "id" not in message:
        return None

    params = message.get("params")
    if params is None:
        params = message["params"] = {}
    elif not isinstance(params, dict):
        return None
    existing = params.get("_meta")
    params["_meta"] = {**(existing if isinstance(existing, dict) else {}), **meta}
    return json.dumps(message, separators=(",", ":")).encode("utf-8")


def _with_body(request, body):
    """Return a copy of ``request`` that carries ``body`` and a matching ``Content-Length``."""
    headers = httpx.Headers(request.headers)
    headers.pop("content-length", None)  # httpx recomputes it from the new body
    return httpx.Request(
        request.method, request.url, headers=headers, content=body, extensions=request.extensions
    )


class DomainJoinAuth(httpx.Auth):
    """``httpx.Auth`` that adds the Domain Join ``_meta`` to each request, then signs it.

    ``inner`` is the SigV4 auth ``mcp-proxy-for-aws`` builds; it receives the request
    *after* ``_meta`` was added, so the signature covers the body that goes on the wire.
    """

    requires_request_body = True

    def __init__(self, inner, meta):
        if inner is None:
            raise ValueError("DomainJoinAuth needs the SigV4 auth to delegate signing to")
        self._inner = inner
        self._meta = dict(meta)

    def auth_flow(self, request):
        if request.method == "POST":
            body = inject_meta(request.content, self._meta)
            if body is not None:
                request = _with_body(request, body)
        return (yield from self._inner.auth_flow(request))

    def __repr__(self):  # never show the assertion
        return f"{type(self).__name__}(inner={self._inner!r}, meta=<{len(self._meta)} keys>)"
