# Replay Session Demo Agent

Demonstrates **Replay Session** on Amazon WorkSpaces Applications. Replay Session
records an agent's work on a stack: before each desktop action, the service
captures the screen together with the exact tool call and its arguments. After
the session ends, it delivers a reviewable audit trail to **your own S3
bucket**:

| File | What it is |
|---|---|
| `replay_session_video.mp4` | Screen recording of the session, one frame per action |
| `replay_session_transcript.json` | Each action (tool and arguments) with an AI-generated description of its intent, plus a session summary |
| `replay_session_report.html` | Self-contained report that plays the video next to a searchable transcript |

This demo runs a short Notepad task on a recorded stack, then closes the session,
waits for the recording to arrive, downloads it, and prints the transcript.

## Prerequisites

- A stack with Replay Session enabled. Starting from the stack and fleet that
  `scripts/setup.sh` deploys, run:

  ```bash
  ./scripts/setup_replay_session.sh --region us-east-1
  ```

  It creates a private S3 bucket for recordings and updates the stack's
  `AgentAccessConfig` with `ReplaySessionEnabled: true` and `S3BucketArn`. Pass
  `--kms` to also create a customer managed KMS key for the captured frames.
- Your AWS account enabled for Replay Session. If it is not, updating the stack
  fails with `Replay Session is not supported for this account.`
- Credentials that can read the stack (`appstream:DescribeStacks`) and the
  recordings (`s3:ListBucket` and `s3:GetObject` on the delivery prefix), in
  addition to the permissions the other demos need.

## Run

```bash
source venv/bin/activate
STREAMING_URL=$(scripts/streaming_url.sh)
python3 agents/replay_session_demo/agent.py \
    --streaming-url "$STREAMING_URL" \
    --expire-session-on-exit
```

The agent looks up the delivery bucket from the stack's `S3BucketArn`. The
stack defaults to `stack.name` in `scripts/config.json`, the one `setup.sh`
deploys; pass `--stack-name <name>` for another stack, or `--bucket <name>` to
skip the lookup.
`--expire-session-on-exit` also ends the desktop session when the agent
disconnects, so the fleet instance is freed.

## What It Does

1. Calls `get_session_info` and reports the `mcpSessionId`. The recording is
   delivered to a folder named after this ID.
2. Opens Notepad, types a short note, and saves it to
   `C:\Users\Public\Documents\replay_session_demo.txt`.
3. Confirms the saved file on screen with a screenshot.
4. Closes the MCP session. Closing it is what starts delivery of the recording.
5. Polls the bucket until the three files appear under
   `agentaccess/replay-session/year=YYYY/month=MM/day=DD/<mcpSessionId>/`
   (typically about a minute for a short session), downloads them to
   `agents/replay_session_demo/recordings/<mcpSessionId>/`, and prints the
   session summary and each recorded action.

Open `recordings/<mcpSessionId>/replay_session_report.html` in a browser to play
the video alongside the transcript. Keep the three files in the same folder; the
report links to the transcript next to it.

## Finding a Recording Yourself

```bash
BUCKET=<your-recordings-bucket>
# all sessions recorded on a day (the date is the session's start date, in UTC)
aws s3 ls s3://$BUCKET/agentaccess/replay-session/year=2026/month=10/day=22/

# one session's files, by MCP session ID
aws s3 cp s3://$BUCKET/agentaccess/replay-session/year=2026/month=10/day=22/<mcpSessionId>/ \
    ./replay/ --recursive
```

## Structure

```
replay_session_demo/
├── agent.py          # Runs the task, then retrieves the recording
├── prompts/
│   ├── system_prompt.md  # Desktop guidance for a recorded stack
│   └── task_prompt.md    # Short Notepad workflow to record
├── skills/
│   └── replay-session-skill.json  # Working on a recorded stack
├── logs/             # Runtime logs
├── metrics/          # Performance metrics
├── recordings/       # Downloaded Replay Session files
└── screenshots/      # Screenshots captured during execution
```

## Notes

- **Recordings contain everything on screen and everything the agent types.**
  Keep the recordings bucket private, scope who can read the
  `agentaccess/replay-session/` prefix, and set a lifecycle rule that matches
  your retention needs. The task prompt tells the agent never to type secrets.
- **Delivered files use the bucket's default encryption.** A `KmsKeyArn` on the
  stack protects the captured frames while they are processed; it is not
  applied to the delivered files. To encrypt recordings with your own key, set
  SSE-KMS as the bucket's default encryption.
- **Capture is fail-closed.** If a frame cannot be recorded, the action is
  rejected and the tool returns an error starting with `Replay Session enabled
  but action blocked`. When the message names a KMS error, fix the key policy
  of the stack's `KmsKeyArn`.
- **A `replay_fallback_content/` folder** in the recording means processing
  failed; it holds the raw frames and per-action metadata instead of the video.
- If no recording arrives, confirm the stack's `ReplaySessionEnabled` is `true`
  and that the session started after it was enabled; changes apply to new
  sessions only.
