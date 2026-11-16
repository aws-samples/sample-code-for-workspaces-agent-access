# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Find, wait for and download Replay Session recordings.

Replay Session records an agent's work on a stack with Replay Session enabled
(scripts/setup_replay_session.sh). After the MCP session ends, the service delivers
a video, a transcript and a report to the stack's S3 bucket, under a folder named
after the MCP session ID. Used by scripts/get_replay_session.py and by an agent's
--save-replay option.
"""

import json
import os
import re
import shlex
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import boto3
from boto3.exceptions import RetriesExceededError
from botocore.exceptions import BotoCoreError, ClientError
from s3transfer.exceptions import S3DownloadFailedError, S3ValidationError

from .mcp_client import _config

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
DELIVERY_PREFIX = "agentaccess/replay-session/"
TRANSCRIPT_FILE = "replay_session_transcript.json"
VIDEO_FILE = "replay_session_video.mp4"
REPORT_FILE = "replay_session_report.html"
ARTIFACTS = (TRANSCRIPT_FILE, VIDEO_FILE, REPORT_FILE)
FALLBACK_DIR = "replay_fallback_content/"
POLL_SECONDS = 15
DEFAULT_WAIT_SECONDS = 600
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
# Raised by s3.download_file and not a BotoCoreError or ClientError: retries used up on a cut-off
# download, the object changing while it downloads, and a range reply that doesn't match the request.
_DOWNLOAD_ERRORS = (RetriesExceededError, S3DownloadFailedError, S3ValidationError)


class RecordingNotRetrieved(Exception):
    """The recording, or the bucket it is delivered to, could not be read, or it has not been delivered yet."""


class RecordingNotDelivered(RecordingNotRetrieved):
    """Nothing complete arrived in time; delivery may still finish, so trying again later can work."""


@dataclass(frozen=True)
class DeliveryBucket:
    """Where recordings are delivered: an explicit bucket, or the S3BucketArn of a stack."""

    region: str
    stack_name: Optional[str] = None
    bucket: Optional[str] = None
    profile: Optional[str] = None


def is_session_id(value):
    """True for an MCP session ID; it names an S3 prefix and a local folder, so no path characters."""
    return bool(SESSION_ID_PATTERN.match(value))


def default_stack_name():
    """Return stack.name from scripts/config.json (the stack setup.sh deploys), or None."""
    stack = _config.get("stack")
    name = stack.get("name") if isinstance(stack, dict) else None
    return name if isinstance(name, str) and name else None


def candidate_days(session_date):
    """Return the UTC dates whose partitions can hold the session's recording.

    The service partitions by the session's start time: its first MCP request, in UTC. A given
    date is searched together with the day before, since a date taken when the session ID first
    came back can be a day after the start date just after midnight.
    """
    if session_date:
        return (session_date - timedelta(days=1), session_date)
    today = datetime.now(timezone.utc)
    return (today - timedelta(days=1), today)


def resolve_bucket(session, delivery):
    """Return the delivery bucket name: ``delivery.bucket``, or read from the stack's AgentAccessConfig."""
    if delivery.bucket:
        return delivery.bucket
    appstream = session.client("appstream")
    try:
        stacks = appstream.describe_stacks(Names=[delivery.stack_name]).get("Stacks", [])
    except appstream.exceptions.ResourceNotFoundException:
        raise RecordingNotRetrieved(f"Stack {delivery.stack_name} was not found in {session.region_name}. "
                                    "Check the region and the account you are signed in to.") from None
    arn = (stacks[0].get("AgentAccessConfig") or {}).get("S3BucketArn") if stacks else None
    if not arn:
        raise RecordingNotRetrieved(
            f"Stack {delivery.stack_name} has no S3BucketArn, so it has nowhere to deliver recordings. "
            "Run scripts/setup_replay_session.sh. If the stack does have one, "
            "your boto3 may be older than the version in requirements.txt.")
    return arn.split(":::", 1)[-1]


def find_recording(s3, bucket, session_id, days):
    """Return (folder_prefix, keys) for the session, searching each of the given UTC dates.

    Keys ending in "/" are folder markers (for example from the S3 console), not files, so
    they are left out.
    """
    for day in days:
        prefix = f"{DELIVERY_PREFIX}year={day:%Y}/month={day:%m}/day={day:%d}/{session_id}/"
        keys = []
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            keys += [obj["Key"] for obj in page.get("Contents", []) if not obj["Key"].endswith("/")]
        if keys:
            return prefix, keys
    return None, []


def wait_for_recording(s3, bucket, session_id, days, wait_seconds):
    """Poll until all three artifacts (or fallback content) are delivered.

    Returns (prefix, keys, is_fallback), or None if nothing arrived within wait_seconds. Polls
    at least once, so wait_seconds=0 checks without waiting.

    Fallback content is uploaded in parallel with no marker for the last file, so it counts as
    delivered once two polls in a row list the same files, or when the wait runs out.
    """
    print(f"⏳ Waiting for the Replay Session recording of {session_id} in s3://{bucket}/{DELIVERY_PREFIX} ...")
    deadline = time.monotonic() + wait_seconds
    previous_keys = None
    while True:
        prefix, keys = find_recording(s3, bucket, session_id, days)
        out_of_time = time.monotonic() >= deadline
        is_fallback = _delivered(prefix, keys, previous_keys, out_of_time)
        if is_fallback is not None:
            return prefix, keys, is_fallback
        if out_of_time:
            return None
        previous_keys = set(keys)
        time.sleep(POLL_SECONDS)


def _delivered(prefix, keys, previous_keys, out_of_time):
    """Whether one listing of the session's folder is a delivered recording: False for all three
    artifacts, True for fallback content that stopped changing since ``previous_keys`` (or any when
    out of time), None when it isn't delivered yet."""
    names = {key[len(prefix):] for key in keys} if prefix else set()
    if all(name in names for name in ARTIFACTS):
        return False
    if any(name.startswith(FALLBACK_DIR) for name in names) and (set(keys) == previous_keys or out_of_time):
        return True
    return None


def download(s3, bucket, prefix, keys, target):
    """Download each key under target, keeping its path below the session's folder."""
    root = os.path.realpath(target)
    for key in keys:
        path = os.path.realpath(os.path.join(target, key[len(prefix):]))
        if not path.startswith(root + os.sep):
            print(f"⚠️  Skipped {key}: its name points outside {target}")
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        s3.download_file(bucket, key, path)


def print_transcript_safely(path):
    """Print the transcript; a file that can't be read is reported, since it is already downloaded.

    The transcript is input from the bucket, so an unexpected shape (AttributeError, TypeError)
    is reported the same way as an unreadable file.
    """
    try:
        print_transcript(path)
    except (OSError, ValueError, AttributeError, TypeError) as exc:
        print(f"⚠️  Could not read the transcript at {path}: {exc}")


def print_transcript(path):
    with open(path, encoding="utf-8") as f:
        transcript = json.load(f)
    print("\n=== Session summary ===")
    print(transcript.get("session_summary", ""))
    print("\n=== Recorded actions ===")
    for frame in transcript.get("frames", []):
        intent = (frame.get("inferred") or {}).get("intent", "")
        arguments = json.dumps(frame.get("arguments", {}))
        # the transcript comes from the bucket: a frame without a number still prints
        print(f"  {frame.get('frame_number') or '':>3}  {frame.get('timestamp_iso')}  "
              f"{frame.get('tool_name')} {arguments}")
        if intent:
            print(f"       intent: {intent}")


def report_download(target, fallback):
    """Print where the recording is and what is in it."""
    print(f"✓ Recording downloaded to {target}")
    if fallback:
        print("⚠️  Processing failed for this session: raw frames and per-action metadata are in "
              f"{os.path.join(target, FALLBACK_DIR)}. The service marks no last file, so a file still "
              "uploading may be missing; fetch it again later to be sure.")
    transcript = os.path.join(target, TRANSCRIPT_FILE)
    if os.path.exists(transcript):
        print_transcript_safely(transcript)
    report = os.path.join(target, REPORT_FILE)
    if os.path.exists(report):
        print(f"\n🎬 Open the report to play the video with the transcript:\n    {report}")


def fetch_recording(delivery, session_id, session_date=None, wait_seconds=DEFAULT_WAIT_SECONDS,
                    output_dir=None):
    """Wait for one session's recording, download it, and print the transcript.

    Raises RecordingNotDelivered if nothing complete arrives in time, and RecordingNotRetrieved if
    the session ID is malformed, the bucket can't be found, or AWS or the disk fails.
    """
    if not is_session_id(session_id):
        raise RecordingNotRetrieved(f"{session_id!r} is not an MCP session ID (letters, digits, - and _).")
    try:
        _download_recording(delivery, session_id, session_date, wait_seconds, output_dir)
    except (BotoCoreError, ClientError, OSError, *_DOWNLOAD_ERRORS) as exc:
        raise RecordingNotRetrieved(f"Could not retrieve the recording of {session_id}: {exc}") from exc


def _open_delivery(delivery):
    """Return (s3 client, bucket name) for the delivery bucket, read with the delivery's profile and region."""
    session = boto3.Session(profile_name=delivery.profile, region_name=delivery.region)
    return session.client("s3"), resolve_bucket(session, delivery)


def _download_recording(delivery, session_id, session_date, wait_seconds, output_dir):
    s3, bucket = _open_delivery(delivery)
    delivered = wait_for_recording(s3, bucket, session_id, candidate_days(session_date), wait_seconds)
    if delivered is None:
        raise RecordingNotDelivered(f"No complete recording of {session_id} after {wait_seconds}s.")
    prefix, keys, fallback = delivered
    target = output_dir or os.path.join(REPO_ROOT, "recordings", session_id)
    download(s3, bucket, prefix, keys, target)
    report_download(target, fallback)


def save_recordings(delivery, sessions, wait_seconds=DEFAULT_WAIT_SECONDS):
    """Download the recordings of an agent run's MCP sessions, newest first. Returns True if all arrived.

    ``sessions`` are ``mcp_client.ServerSession`` records. The sessions that ran a tool call share
    one wait of ``wait_seconds``, so a later one gets the time an earlier one didn't use. A session
    that ran none (a connection attempt that was retried) may have nothing to record, so it is checked
    once without waiting. A recording that is missing or can't be read is reported with the command
    to fetch it later, and so are the ones not saved yet when Ctrl-C stops the wait.
    """
    if not sessions:
        print("⚠️  No MCP session was opened, so there is no recording to save.")
        return False
    deadline = time.monotonic() + wait_seconds
    pending = list(reversed(sessions))
    saved = True
    try:
        while pending:
            session = pending[0]
            wait = max(0, int(deadline - time.monotonic())) if session.ran_tools else 0
            saved = _save_one(delivery, session, wait) and saved
            pending.pop(0)
    except KeyboardInterrupt:
        _report_stopped(delivery, pending)
        return False
    return saved


def _save_one(delivery, session, wait_seconds):
    """Fetch one recording; on failure print why and the command to fetch it later. Returns True if saved.

    A session that ran no tool call may never get a recording, so an undelivered one is reported as
    a note, not a failure.
    """
    try:
        fetch_recording(delivery, session.session_id, session_date=session.first_seen, wait_seconds=wait_seconds)
        return True
    except RecordingNotRetrieved as exc:
        if not session.ran_tools and isinstance(exc, RecordingNotDelivered):
            note = (f"ℹ️  No recording of {session.session_id}. It ran no tool call (for example a "
                    "connection attempt that was retried), so it may have recorded nothing.")
        else:
            note = f"⚠️  Could not save the recording of {session.session_id}: {exc}"
        print(f"{note}\n   Fetch it later with: {retry_command(delivery, session)}")
        return False


def _report_stopped(delivery, sessions):
    """Ctrl-C stopped the wait or a download: print the command for each recording not saved yet."""
    print("\n⚠️  Stopped before the recording was saved. Fetch it later with:")
    for session in sessions:
        print(f"   {retry_command(delivery, session)}")


def check_delivery(delivery):
    """Check, before the agent runs, that the delivery bucket can be found and listed.

    Raises RecordingNotRetrieved with the reason, so a wrong stack, region or credentials are
    reported before the task rather than after it.
    """
    try:
        s3, bucket = _open_delivery(delivery)
        s3.list_objects_v2(Bucket=bucket, Prefix=DELIVERY_PREFIX, MaxKeys=1)
    except (BotoCoreError, ClientError) as exc:
        raise RecordingNotRetrieved(
            f"Could not read the recordings bucket of {_delivery_destination(delivery)}: {exc}. "
            "Check the region and the AWS profile.") from exc


def _delivery_destination(delivery):
    """The bucket or stack recordings are delivered to, for messages."""
    return f"bucket {delivery.bucket}" if delivery.bucket else f"stack {delivery.stack_name} ({delivery.region})"


def retry_command(delivery, session):
    """The scripts/get_replay_session.py command that fetches this session's recording (a ``ServerSession``)."""
    words = ["python3", "scripts/get_replay_session.py", "--session-id", session.session_id,
             "--session-date", f"{session.first_seen:%Y-%m-%d}", "--region", delivery.region]
    if delivery.bucket:
        words += ["--bucket", delivery.bucket]
    elif delivery.stack_name != default_stack_name():
        words += ["--stack-name", delivery.stack_name]
    if delivery.profile:
        words += ["--profile", delivery.profile]
    return shlex.join(words)
