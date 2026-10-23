#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Replay Session Demo Agent - Records an agent session and retrieves its audit trail.

Requires a stack with Replay Session enabled (scripts/setup_replay_session.sh), so
every desktop action the agent takes is captured. After the agent finishes, this
script closes the MCP session (which starts delivery), waits for the recording to
arrive in the stack's S3 bucket, downloads it, and prints the transcript.
"""

import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))

import boto3

from lib import agent_common
from lib.mcp_client import close_mcp_clients
from lib.retry import run_agent_with_retry

AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
DELIVERY_PREFIX = "agentaccess/replay-session/"
ARTIFACTS = (
    "replay_session_transcript.json",
    "replay_session_video.mp4",
    "replay_session_report.html",
)
FALLBACK_DIR = "replay_fallback_content/"


class _SessionIdCapture(logging.Filter):
    """Record the MCP session ID the transport receives (it names the recording's folder).

    Attached as a logger filter, not a handler: logger filters see each record before any
    handler runs, and the shared library's handler rewrites the message when it prints it.
    """

    MARKER = "Received session ID: "

    def __init__(self):
        super().__init__()
        self.session_id = None

    def filter(self, record):
        message = record.getMessage()
        if self.MARKER in message:
            self.session_id = message.split(self.MARKER, 1)[1].strip()
        return True


def capture_session_id():
    """Attach the capture filter to the MCP transport logger and return it."""
    capture = _SessionIdCapture()
    transport_logger = logging.getLogger("mcp.client.streamable_http")
    transport_logger.setLevel(logging.INFO)
    transport_logger.addFilter(capture)
    return capture


def default_stack_name():
    """Return stack.name from scripts/config.json (the stack setup.sh deploys), or None."""
    path = os.path.join(AGENT_DIR, "..", "..", "scripts", "config.json")
    try:
        with open(path) as config_file:
            raw = re.sub(r"(?m)^\s*//.*$", "", config_file.read())
        name = json.loads(raw).get("stack", {}).get("name")
    except (OSError, ValueError, AttributeError):
        return None
    return name if isinstance(name, str) and name else None


def add_replay_arguments(parser):
    group = parser.add_argument_group("Replay Session")
    group.add_argument('--stack-name',
                       help='Stack with Replay Session enabled; its S3BucketArn is where the '
                            'recording is delivered (default: stack.name in scripts/config.json)')
    group.add_argument('--bucket',
                       help='S3 bucket the recording is delivered to (default: read from --stack-name)')
    group.add_argument('--aws-profile', metavar='PROFILE',
                       help='AWS profile for reading the stack and the bucket (default: your default credentials)')
    group.add_argument('--appstream-endpoint', metavar='URL',
                       help='WorkSpaces Applications API endpoint for the stack lookup '
                            '(default: the regional endpoint)')
    group.add_argument('--wait-seconds', type=int, default=600, metavar='S',
                       help='How long to wait for the recording after the session closes (default: 600)')
    group.add_argument('--no-fetch', action='store_true', default=False,
                       help='Run the task only; do not wait for or download the recording')


def resolve_bucket(session, args):
    """Return the delivery bucket name from --bucket or the stack's AgentAccessConfig, or None."""
    if args.bucket:
        return args.bucket
    if not args.stack_name:
        return None
    appstream = session.client("appstream", endpoint_url=getattr(args, "appstream_endpoint", None))
    try:
        stacks = appstream.describe_stacks(Names=[args.stack_name]).get("Stacks", [])
    except appstream.exceptions.ResourceNotFoundException:
        print(f"\n⚠️  Stack {args.stack_name} was not found in {session.region_name}. "
              "Check --region, or pass --bucket.")
        return None
    arn = (stacks[0].get("AgentAccessConfig") or {}).get("S3BucketArn") if stacks else None
    if not arn:
        print(f"\n⚠️  Stack {args.stack_name} has no S3BucketArn, so it has nowhere to deliver "
              "recordings. Run scripts/setup_replay_session.sh, or pass --bucket.")
        return None
    return arn.split(":::", 1)[-1]


def find_recording(s3, bucket, session_id, start_utc):
    """Return (folder_prefix, keys) for the session, searching the start date and the next day."""
    for day in (start_utc, start_utc + timedelta(days=1)):
        prefix = f"{DELIVERY_PREFIX}year={day:%Y}/month={day:%m}/day={day:%d}/{session_id}/"
        keys = []
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            keys += [obj["Key"] for obj in page.get("Contents", [])]
        if keys:
            return prefix, keys
    return None, []


def wait_for_recording(s3, bucket, session_id, start_utc, wait_seconds):
    """Poll until all three artifacts (or fallback content) are delivered, or time runs out."""
    print(f"\n⏳ Waiting for the Replay Session recording in s3://{bucket}/{DELIVERY_PREFIX} ...")
    deadline = time.monotonic() + wait_seconds
    prefix, keys = None, []
    while time.monotonic() < deadline:
        prefix, keys = find_recording(s3, bucket, session_id, start_utc)
        names = {key[len(prefix):] for key in keys} if prefix else set()
        if all(name in names for name in ARTIFACTS):
            return prefix, keys, False
        if any(name.startswith(FALLBACK_DIR) for name in names):
            # fallback content is written after processing has given up, so the set is final
            return prefix, keys, True
        time.sleep(15)
    return prefix, keys, None


def download(s3, bucket, prefix, keys, session_id):
    target = os.path.join(AGENT_DIR, "recordings", session_id)
    for key in keys:
        path = os.path.join(target, key[len(prefix):])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        s3.download_file(bucket, key, path)
    return target


def print_transcript(path):
    with open(path, encoding="utf-8") as f:
        transcript = json.load(f)
    print("\n=== Session summary ===")
    print(transcript.get("session_summary", ""))
    print("\n=== Recorded actions ===")
    for frame in transcript.get("frames", []):
        intent = (frame.get("inferred") or {}).get("intent", "")
        arguments = json.dumps(frame.get("arguments", {}))
        print(f"  {frame.get('frame_number'):>3}  {frame.get('timestamp_iso')}  "
              f"{frame.get('tool_name')} {arguments}")
        if intent:
            print(f"       intent: {intent}")


def fetch_recording(args, session_id, start_utc):
    """Wait for the recording, download it, and print the transcript. Returns True on success."""
    session = boto3.Session(profile_name=args.aws_profile, region_name=args.region)
    bucket = resolve_bucket(session, args)
    if not bucket:
        if not (args.bucket or args.stack_name):
            print("\n⚠️  No delivery bucket: pass --stack-name or --bucket to retrieve the recording.")
        return False
    s3 = session.client("s3")

    prefix, keys, fallback = wait_for_recording(s3, bucket, session_id, start_utc, args.wait_seconds)
    if fallback is None:
        print(f"\n⚠️  No complete recording after {args.wait_seconds}s. Delivery can take longer for "
              "long sessions; list the prefix later:")
        print(f"    aws s3 ls s3://{bucket}/{DELIVERY_PREFIX} --recursive | grep {session_id}")
        return False

    target = download(s3, bucket, prefix, keys, session_id)
    print(f"\n✓ Recording downloaded to {target}")
    if fallback:
        print("⚠️  Processing failed for this session: raw frames and per-action metadata are in "
              f"{os.path.join(target, FALLBACK_DIR)}")
    transcript = os.path.join(target, "replay_session_transcript.json")
    if os.path.exists(transcript):
        print_transcript(transcript)
    report = os.path.join(target, "replay_session_report.html")
    if os.path.exists(report):
        print(f"\n🎬 Open the report to play the video with the transcript:\n    {report}")
    return True


def main():
    parser = agent_common.create_base_parser('Replay Session Demo Agent (Strands)')
    add_replay_arguments(parser)
    args = parser.parse_args()
    agent_common.resolve_session_auth(parser, args)
    if not (args.stack_name or args.bucket):
        args.stack_name = default_stack_name()
    if not args.no_fetch and not (args.stack_name or args.bucket):
        parser.error("--stack-name or --bucket is required to retrieve the recording "
                     "(or pass --no-fetch to run the task only)")

    agent_common.print_banner(
        "Replay Session Demo Agent",
        "The agent does a short Notepad task on a\n"
        "stack with Replay Session enabled, then\n"
        "retrieves the recording of what it did.",
        args.model_id, args,
    )

    setup = agent_common.setup_standard_agent(
        args, AGENT_DIR,
        skill_filename="replay-session-skill.json",
        skill_label="REPLAY SESSION SKILL",
        task_prompt_path=os.path.join(AGENT_DIR, "prompts/task_prompt.md"),
    )

    capture = capture_session_id()
    start_utc = datetime.now(timezone.utc)
    success, error, result = run_agent_with_retry(
        args, setup["mcp_factory"], setup["model"],
        setup["system_prompt"], setup["task_prompt"],
        setup["agent_logger"], setup["conv_manager"],
    )

    # Closing the MCP session (HTTP DELETE) is what starts delivery of the recording.
    close_mcp_clients()

    if success and not args.no_fetch:
        if capture.session_id:
            fetch_recording(args, capture.session_id, start_utc)
        else:
            print("\n⚠️  The MCP session ID was not captured, so the recording cannot be located automatically.")

    return agent_common.finalize_and_exit(setup["agent_logger"], success, error, result)


if __name__ == "__main__":
    sys.exit(main())
