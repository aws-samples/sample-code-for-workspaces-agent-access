#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Retrieve the Replay Session recording of one agent session.

Replay Session records an agent's work on a stack with Replay Session enabled
(scripts/setup_replay_session.sh). After the MCP session ends, the service delivers
a video, a transcript and a report to the stack's S3 bucket. This script waits for
them, downloads them, and prints the transcript.

This repo's agents can do this for you with --save-replay. For any other agent, note
its MCP session ID (`get_session_info` returns it as `mcpSessionId`), then:

    python3 scripts/get_replay_session.py --session-id <id> --region us-east-1

Exit code 0 means the recording was downloaded.
"""

import argparse
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

try:
    from lib import replay_session
    from lib.replay_session import DeliveryBucket, RecordingNotDelivered, RecordingNotRetrieved
except ModuleNotFoundError as exc:
    sys.exit(f"ERROR: {exc}. Activate the venv that scripts/setup.sh creates (source venv/bin/activate) "
             "and run this again.")


def parse_session_id(value):
    if not replay_session.is_session_id(value):
        raise argparse.ArgumentTypeError(f"expected an MCP session ID (letters, digits, - and _), got {value!r}")
    return value


def parse_wait_seconds(value):
    seconds = int(value)
    if seconds < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return seconds


def parse_session_date(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {value!r}") from None


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Retrieve the Replay Session recording of an agent session.")
    parser.add_argument('--session-id', required=True, metavar='MCP_SESSION_ID', type=parse_session_id,
                        help='The MCP session ID (agents print it as "MCP server session: <id>")')
    parser.add_argument('--stack-name',
                        help='Stack with Replay Session enabled; its S3BucketArn is where the '
                             'recording is delivered (default: stack.name in scripts/config.json)')
    parser.add_argument('--bucket', help='S3 bucket the recording is delivered to (default: read from --stack-name)')
    parser.add_argument('--session-date', metavar='YYYY-MM-DD', type=parse_session_date,
                        help='UTC date of the session (the day it started, or the day after); that day '
                             'and the day before are searched (default: search yesterday and today)')
    parser.add_argument('--wait-seconds', type=parse_wait_seconds, default=replay_session.DEFAULT_WAIT_SECONDS,
                        metavar='S', help='How long to wait for the recording to be delivered '
                                          f'(default: {replay_session.DEFAULT_WAIT_SECONDS})')
    parser.add_argument('--region', default=os.environ.get('AWS_REGION') or os.environ.get('AWS_DEFAULT_REGION') or 'us-east-1',
                        help="The stack's region (default: $AWS_REGION, $AWS_DEFAULT_REGION or us-east-1)")
    parser.add_argument('--profile', metavar='PROFILE',
                        help='AWS profile for reading the stack and the bucket (default: your default credentials)')
    parser.add_argument('--output-dir', metavar='DIR',
                        help='Where to save the files (default: recordings/<session-id>/ in the repo)')
    args = parser.parse_args(argv)
    if not (args.stack_name or args.bucket):
        args.stack_name = replay_session.default_stack_name()
    if not (args.stack_name or args.bucket):
        parser.error("--stack-name or --bucket is required (no stack.name in scripts/config.json)")
    return args


def main(argv=None):
    args = parse_args(argv)
    delivery = DeliveryBucket(region=args.region, stack_name=args.stack_name, bucket=args.bucket,
                              profile=args.profile)
    try:
        replay_session.fetch_recording(delivery, args.session_id, session_date=args.session_date,
                                       wait_seconds=args.wait_seconds, output_dir=args.output_dir)
    except RecordingNotDelivered as exc:
        print(f"⚠️  {exc} Delivery can take longer for long sessions; run this command again later.")
        return 1
    except RecordingNotRetrieved as exc:
        print(f"⚠️  {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
