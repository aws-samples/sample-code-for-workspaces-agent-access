# PDF Extractor Demo Agent

Demonstrates autonomous PDF text extraction on a remote Windows desktop using Firefox and Notepad.

## What It Does

The agent opens Mozilla Firefox, navigates to an AWS whitepaper PDF, locates the Amazon Bedrock section, reads the text, and saves it to a file using Notepad.

## Run

```bash
python3 agents/pdf_extractor_demo/agent.py --streaming-url "<YOUR_APPSTREAM_STREAMING_URL>"
```

## Structure

```
pdf_extractor_demo/
├── agent.py          # Agent orchestrator
├── prompts/
│   ├── system_prompt.md  # Desktop control + app tips
│   └── task_prompt.md    # PDF extraction instructions
├── logs/             # Runtime logs
├── metrics/          # Performance metrics
└── screenshots/      # Screenshots captured during execution
```
