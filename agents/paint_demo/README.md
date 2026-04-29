# Paint Demo Agent

Demonstrates autonomous drawing in MS Paint on a remote Windows desktop via DCV.

## What It Does

The agent opens MS Paint and creates a simple landscape drawing (sky, sun, ground, house, tree) using mouse clicks, drag operations, and paint tools.

## Run

```bash
python3 agents/paint_demo/agent.py --streaming-url "<YOUR_APPSTREAM_STREAMING_URL>"
```

## Structure

```
paint_demo/
├── agent.py          # Agent orchestrator
├── prompts/
│   ├── system_prompt.md  # Desktop control + paint tips
│   └── task_prompt.md    # Drawing instructions
├── logs/             # Runtime logs
├── metrics/          # Performance metrics
└── screenshots/      # Screenshots captured during execution
```
