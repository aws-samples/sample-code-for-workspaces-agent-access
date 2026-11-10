# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""SigV4 signing of MCP requests (``httpx2.Auth``)."""

import boto3
import httpx2
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest


class SigV4HttpAuth(httpx2.Auth):
    """``httpx2.Auth`` that signs each MCP request with SigV4.

    The credentials are read on the first request, and then refreshed by botocore when they expire.
    Reading them on the first request (not here) means a bad ``--mcp-profile`` or missing credentials
    fails inside the connection retry loop, which prints the formatted error.
    For Domain Join, :class:`~lib.domain_join_auth.DomainJoinAuth` wraps this and adds the SAML
    ``_meta`` first, since the signature covers the body.
    """

    requires_request_body = True

    def __init__(self, service, region, profile=None):
        self._profile = profile
        self._credentials = None
        self._service = service
        self._region = region

    def _get_credentials(self):
        if self._credentials is None:
            session = boto3.Session(profile_name=self._profile) if self._profile else boto3.Session()
            credentials = session.get_credentials()
            if credentials is None:
                raise RuntimeError("No AWS credentials found for MCP signing (check aws sts get-caller-identity).")
            self._credentials = credentials
        return self._credentials

    def auth_flow(self, request):
        # 'connection' is not part of the signature the service computes; signing it breaks the match.
        headers = {k: v for k, v in request.headers.items() if k.lower() != "connection"}
        aws_request = AWSRequest(method=request.method, url=str(request.url), data=request.content, headers=headers)
        SigV4Auth(self._get_credentials().get_frozen_credentials(), self._service, self._region).add_auth(aws_request)
        request.headers.update(dict(aws_request.headers))
        yield request
