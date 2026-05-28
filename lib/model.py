# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Model provider creation for Bedrock (Converse API) and bedrock-mantle."""

import os
import sys

from strands.models.bedrock import BedrockModel


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

        if getattr(args, 'computer_use_tool', False) and ("anthropic" in model_id.lower() or "claude" in model_id.lower()):
            model_kwargs["additional_request_fields"] = {
                "anthropic_beta": ["computer-use-2025-11-24"],
            }

        if getattr(args, 'llm_profile', None):
            model_kwargs["boto_session"] = boto3.Session(
                profile_name=args.llm_profile, region_name=args.region)
        else:
            model_kwargs["region_name"] = args.region

        return BedrockModel(**model_kwargs)

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

        if not api_key:
            try:
                from aws_bedrock_token_generator import BedrockTokenGenerator
                import boto3 as _boto3

                session = _boto3.Session(
                    profile_name=getattr(args, 'llm_profile', None),
                    region_name=args.region,
                )
                credentials = session.get_credentials().get_frozen_credentials()
                generator = BedrockTokenGenerator()
                api_key = generator.get_token(credentials=credentials, region=args.region)
                sys.stdout.write("  Auto-generated short-term Bedrock API key\n")
                sys.stdout.flush()
            except ImportError:
                raise RuntimeError(
                    f"Model {model_id!r} requires bedrock-mantle (OpenAI-compatible endpoint).\n"
                    "  Install aws-bedrock-token-generator to auto-generate a key:\n"
                    "    pip install aws-bedrock-token-generator\n"
                    "  Or set AWS_BEARER_TOKEN_BEDROCK / --bedrock-api-key manually."
                )
            except Exception as e:
                raise RuntimeError(
                    f"Failed to auto-generate Bedrock API key: {e}\n"
                    "  Set AWS_BEARER_TOKEN_BEDROCK in the environment or pass --bedrock-api-key."
                ) from e

        mantle_url = f"https://bedrock-mantle.{args.region}.api.aws/v1"
        sys.stdout.write(f"  Model provider: bedrock-mantle ({mantle_url})\n")
        sys.stdout.flush()

        return _MantleModel(
            client_args={"base_url": mantle_url, "api_key": api_key},
            model_id=model_id,
        )
