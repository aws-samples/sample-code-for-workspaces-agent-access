# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Model provider creation for Bedrock (Converse API) and bedrock-mantle."""

import json
import os
import sys

from botocore.credentials import CredentialProvider
from strands.models.bedrock import BedrockModel
from strands.models.model import CacheConfig

from . import computer_tool


# Accepted regions for Bedrock calls.
ALLOWED_REGIONS = frozenset({
    "us-east-1", "us-east-2", "us-west-2", "ca-central-1",
    "eu-central-1", "eu-west-1", "eu-west-2", "eu-west-3",
    "ap-northeast-1", "ap-northeast-2", "ap-south-1",
    "ap-southeast-1", "ap-southeast-2",
})


def _supports_converse_images(model_id):
    """Return True if model_id works on bedrock-runtime Converse API with images."""
    lower = model_id.lower()
    return any(x in lower for x in ("anthropic", "claude", "amazon.nova", "nova-pro", "nova-lite", "nova-premier"))


_PLACEHOLDER_TOOL_SPEC = {
    "name": "noop", "description": "Does nothing. Never call this tool.",
    "inputSchema": {"json": {"type": "object", "properties": {}}},
}


class NativeComputerBedrockModel(BedrockModel):
    """BedrockModel that offers Anthropic's ``computer_20251124`` tool instead of the desktop function tools.

    The agent still registers the Agent Access tools and a ``computer`` tool that runs on them
    (``lib.computer_tool``); only the request to the model changes. The desktop function tools
    and the ``computer`` function spec are left out of ``toolConfig`` and the typed tool goes in
    ``additionalModelRequestFields``, next to the beta header. Other tools (forwarded MCP tools)
    are sent as usual. ``toolConfig`` must exist whenever the history holds tool calls, so a
    placeholder function tool stands in when nothing else is left; it is sent on every request,
    first one included, so the cached prefix never changes shape.
    """

    computer_version = computer_tool.DEFAULT_VERSION      # "20251124" or "20260801"

    def format_request(self, messages, tool_specs=None, system_prompt_content=None, tool_choice=None,
                       dynamic_trailing_blocks=0, **kwargs):
        kept = [spec for spec in (tool_specs or []) if not computer_tool.is_desktop_spec(spec["name"], self.computer_version)]
        request = super().format_request(
            messages, kept or [_PLACEHOLDER_TOOL_SPEC], system_prompt_content, tool_choice,
            dynamic_trailing_blocks, **kwargs)
        fields = dict(request.get("additionalModelRequestFields") or {})
        if self.computer_version == "20260801":
            fields["tools"] = [json.loads(json.dumps(computer_tool.TOOLSET_DEFINITION))]      # needs no beta header
        else:
            fields["tools"] = [dict(computer_tool.TOOL_DEFINITION)]
            betas = list(fields.get("anthropic_beta") or [])
            if computer_tool.COMPUTER_USE_BETA not in betas:
                betas.append(computer_tool.COMPUTER_USE_BETA)
            fields["anthropic_beta"] = betas
        request["additionalModelRequestFields"] = fields
        return request


class _SessionCredentials(CredentialProvider):
    """Expose one boto3 session's credentials to Strands' ``bedrock_mantle_config``.

    Used so ``--llm-profile`` also selects the identity that mints the short-term
    Bedrock key for bedrock-mantle models.
    """

    METHOD = "boto3-session"
    CANONICAL_NAME = "boto3-session"

    def __init__(self, session):
        super().__init__()
        self._session = session

    def load(self):
        return self._session.get_credentials()


def create_model(args):
    """Create a model provider from parsed args.

    For Anthropic/Claude models: uses BedrockModel (bedrock-runtime, Converse API).
    For all other models: uses OpenAIModel (bedrock-mantle, Chat Completions API).
    """
    import boto3

    if args.region not in ALLOWED_REGIONS:
        raise ValueError(
            f"region {args.region!r} not in allow-list. "
            f"Permitted: {sorted(ALLOWED_REGIONS)}"
        )

    model_id = args.model_id

    if _supports_converse_images(model_id):
        model_kwargs = {"model_id": model_id}
        if getattr(args, 'prompt_cache', False):
            # "auto": a rolling cache point at the end of the conversation plus one after the system
            # prompt; tools_ttl adds one after the tool schemas, so the tools stay cached when only
            # the prompt changes. Three of the four cache points Bedrock allows.
            model_kwargs["cache_config"] = CacheConfig(strategy="auto", tools_ttl=True)
        if getattr(args, 'max_tokens', None):
            model_kwargs["max_tokens"] = args.max_tokens

        native = getattr(args, 'native_computer_tool', False)
        is_claude = "anthropic" in model_id.lower() or "claude" in model_id.lower()
        if native and not is_claude:
            raise ValueError("--native-computer-tool needs a Claude model; "
                             f"{model_id!r} only works with the desktop function tools")
        version = getattr(args, 'computer_tool_version', computer_tool.DEFAULT_VERSION)
        if version not in computer_tool.VERSIONS:
            raise ValueError(f"unknown computer tool version {version!r}; choose one of {sorted(computer_tool.VERSIONS)}")
        if native and version == "20251124":
            model_kwargs["additional_request_fields"] = {
                "anthropic_beta": ["computer-use-2025-11-24"],
            }

        if getattr(args, 'llm_profile', None):
            model_kwargs["boto_session"] = boto3.Session(
                profile_name=args.llm_profile, region_name=args.region)
        else:
            model_kwargs["region_name"] = args.region

        effort = getattr(args, 'effort', None)
        if effort and is_claude:
            fields = dict(model_kwargs.get("additional_request_fields") or {})
            fields["thinking"] = {"type": "adaptive"}
            fields["output_config"] = {"effort": effort}
            model_kwargs["additional_request_fields"] = fields
        if not native:
            return BedrockModel(**model_kwargs)
        model = NativeComputerBedrockModel(**model_kwargs)
        model.computer_version = version
        return model

    else:
        from strands.models.openai import OpenAIModel
        from strands.types.exceptions import ContextWindowOverflowException
        import logging as _logging

        _logging.getLogger("strands.models.openai").setLevel(_logging.ERROR)

        class _MantleModel(OpenAIModel):
            """OpenAIModel subclass that maps bedrock-mantle payload errors to
            ContextWindowOverflowException so Strands calls reduce_context()."""

            async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
                try:
                    async for event in super().stream(messages, tool_specs, system_prompt, **kwargs):
                        yield event
                except Exception as e:
                    if "length limit exceeded" in str(e).lower():
                        raise ContextWindowOverflowException(str(e)) from e
                    raise

        api_key = (
            getattr(args, 'bedrock_api_key', None)
            or os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
        )

        if api_key:
            # Caller-supplied key: pass it straight through. This path targets the
            # /v1 base path, which serves most bedrock-mantle models. Models served
            # from /openai/v1 (for example openai.gpt-5.*) need the SDK to pick the
            # path - omit the key and let bedrock_mantle_config mint one instead.
            mantle_url = f"https://bedrock-mantle.{args.region}.api.aws/v1"
            sys.stdout.write(f"  Model provider: bedrock-mantle ({mantle_url})\n")
            sys.stdout.flush()
            return _MantleModel(
                client_args={"base_url": mantle_url, "api_key": api_key},
                model_id=model_id,
            )

        # No key supplied: Strands mints a fresh short-term Bedrock key from the AWS
        # credential chain for every request (so long runs outlive any single key)
        # and picks the right base path for the model.
        mantle_config = {"region": args.region}
        if getattr(args, 'llm_profile', None):
            mantle_config["credentials_provider"] = _SessionCredentials(
                boto3.Session(profile_name=args.llm_profile, region_name=args.region))

        sys.stdout.write(
            f"  Model provider: bedrock-mantle ({args.region}, short-term keys minted per request)\n")
        sys.stdout.flush()
        return _MantleModel(bedrock_mantle_config=mantle_config, model_id=model_id)
