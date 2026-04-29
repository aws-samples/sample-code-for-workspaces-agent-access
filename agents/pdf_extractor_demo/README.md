# PDF Extractor Demo Agent

Demonstrates autonomous PDF text extraction on a remote Windows desktop via DCV using Firefox, OpenOffice Writer, and File Explorer.

## What It Does

The agent opens Mozilla Firefox, downloads an AWS whitepaper PDF, locates the Amazon Bedrock section, copies the descriptive text, and saves it to a file on the Desktop using OpenOffice Writer.

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
