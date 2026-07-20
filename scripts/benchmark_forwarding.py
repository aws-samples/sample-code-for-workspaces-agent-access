#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Benchmark: compare agent performance WITH vs. WITHOUT forwarded MCP tools.

Both arms complete the same task on the same fleet (MCPForwarding fleet with
FORWARD_MCP_TOOLS enabled). The difference:

  - WITH:    Agent has access to forwarded filesystem + fetch tools (direct API calls)
  - WITHOUT: Forwarded tools are filtered out; agent must use desktop tools only
             (open Notepad, screenshot, read pixels, type, etc.)

The task is: "Read the contents of C:\\Users\\Public\\Documents\\hello.txt and
report back what it says."

This is achievable both ways — forwarded read_file is one call; the desktop-only
path requires Run dialog → Notepad → screenshot → OCR from the image.

Metrics captured per arm:
  - Wall-clock latency (seconds)
  - Total tokens (input + output)
  - Number of tool calls
  - Number of screenshots taken
  - Success (did the agent report the correct file contents?)

Usage:
    python3 scripts/benchmark_forwarding.py --streaming-url "$URL" --region us-east-1
    python3 scripts/benchmark_forwarding.py --streaming-url "$URL" --trials 3

Requires a fleet with FORWARD_MCP_TOOLS enabled (scripts/setup_mcp_forwarding.sh).
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from strands import Agent
from lib.model import create_model
from lib.mcp_client import create_mcp_client_factory, build_mcp_client
from lib.strands_logger import StrandsAgentLogger
from lib.screenshot_pruning_manager import ScreenshotPruningConversationManager


# The task both arms must complete. Designed to be achievable with or without
# forwarded tools, with a verifiable ground-truth answer.
TASK = (
    "Read the contents of C:\\Users\\Public\\Documents\\hello.txt and report "
    "exactly what the file says. Put the file contents between <RESULT> and "
    "</RESULT> tags in your final response."
)

SYSTEM_PROMPT_BASE = """\
You control a remote Windows desktop. You have access to desktop interaction
tools (screenshot, left_click, type_text, key, etc.).
{forwarded_section}
Your task is to read a specific file and report its contents.

Rules:
- Report the exact file contents between <RESULT> and </RESULT> tags.
- Minimize tool calls — be efficient.
- If you cannot read the file, report <RESULT>FAILED</RESULT>.
"""

FORWARDED_SECTION = """\
You also have access to forwarded MCP tools (prefixed with `forwarded___`)
that let you read/write files directly without opening applications. Prefer
these over desktop tools when available — they are faster and more reliable.
"""

NO_FORWARDED_SECTION = """\
You do NOT have access to any file-reading tools. To read a file, you must
open it in an application (e.g., Notepad via Run dialog) and read from the
screenshot.
"""

EXPECTED_CONTENT = "Hello from MCP filesystem server"


def create_parser():
    parser = argparse.ArgumentParser(description="Benchmark: forwarded tools vs desktop-only")
    parser.add_argument('--streaming-url', required=True, help='AppStream streaming URL')
    parser.add_argument('--model-id', default='global.anthropic.claude-sonnet-4-6')
    parser.add_argument('--region', default=os.environ.get('AWS_REGION', 'us-east-1'))
    parser.add_argument('--mcp-endpoint', default=None)
    parser.add_argument('--mcp-region', default=None)
    parser.add_argument('--mcp-profile', default=None)
    parser.add_argument('--trials', type=int, default=1,
                       help='Number of trials per arm (default: 1)')
    parser.add_argument('--output', default='reports/benchmark_forwarding.json',
                       help='Output JSON path')
    return parser


def run_single_trial(args, arm_name, use_forwarded):
    """Run one trial of the benchmark.

    For the WITHOUT arm, we filter forwarded tools by using the MCPClient's
    built-in tool_filters parameter (prefix exclusion).
    """
    from lib.agent_common import setup_signal_handler, print_handler
    from lib.mcp_client import build_mcp_client, _SanitizedMCPClient

    out_dir = Path(f"reports/benchmark_{arm_name}_{int(time.time())}")
    out_dir.mkdir(parents=True, exist_ok=True)

    logger = StrandsAgentLogger(
        log_dir=str(out_dir / "logs"),
        metrics_dir=str(out_dir / "metrics"),
        screenshots_dir=str(out_dir / "screenshots"),
        quiet_display=True,
    )

    system_prompt = SYSTEM_PROMPT_BASE.format(
        forwarded_section=FORWARDED_SECTION if use_forwarded else NO_FORWARDED_SECTION
    )
    logger.set_task_info(TASK[:200], args.model_id)

    model = create_model(args)
    mcp_factory = create_mcp_client_factory(args)
    conv_manager = ScreenshotPruningConversationManager()

    start_time = time.time()
    success = False
    error = None
    result = None

    max_retries = getattr(args, 'mcp_retries', 3)
    for attempt in range(1, max_retries + 1):
        try:
            mcp_client = build_mcp_client(mcp_factory, getattr(args, 'mcp_timeout', 180))

            # For the WITHOUT arm: wrap tools to exclude forwarded ones
            if not use_forwarded:
                original_load = mcp_client.load_tools

                async def filtered_load(**kwargs):
                    tools = await original_load(**kwargs)
                    return [t for t in tools if "forwarded" not in t.tool_name.lower()]

                mcp_client.load_tools = filtered_load

            agent = Agent(
                model=model,
                tools=[mcp_client],
                system_prompt=system_prompt,
                conversation_manager=conv_manager,
                hooks=[logger],
                callback_handler=print_handler,
            )

            result = agent(TASK)
            success = True
            break

        except Exception as e:
            error = str(e)
            if "closed" in error.lower() and attempt < max_retries:
                time.sleep(10 * attempt)
                continue
            break

    elapsed = time.time() - start_time
    metrics_file = logger.finalize(success, error, agent_result=result)

    # Load metrics
    metrics = {}
    if metrics_file and Path(metrics_file).exists():
        metrics = json.loads(Path(metrics_file).read_text())

    # Check correctness
    result_text = str(result) if result else ""
    if "<RESULT>" in result_text and "</RESULT>" in result_text:
        extracted = result_text.split("<RESULT>")[1].split("</RESULT>")[0].strip()
    else:
        extracted = result_text

    correct = EXPECTED_CONTENT.lower() in extracted.lower()

    # Count screenshots
    screenshot_count = sum(
        1 for tc in metrics.get("tool_calls", [])
        if "screenshot" in tc.get("tool_name", "").lower()
    )

    trial_result = {
        "arm": arm_name,
        "use_forwarded": use_forwarded,
        "success": success,
        "correct": correct,
        "extracted_answer": extracted[:200],
        "elapsed_seconds": round(elapsed, 1),
        "total_tokens": metrics.get("total_tokens", {}),
        "tool_call_count": len(metrics.get("tool_calls", [])),
        "screenshot_count": screenshot_count,
        "model_call_count": len(metrics.get("model_calls", [])),
        "metrics_file": str(metrics_file),
        "error": error,
    }

    return trial_result


def print_comparison(results):
    """Print a summary table comparing the two arms."""
    with_results = [r for r in results if r["use_forwarded"]]
    without_results = [r for r in results if not r["use_forwarded"]]

    def avg(lst, key):
        vals = [r[key] for r in lst if r[key] is not None]
        return round(sum(vals) / len(vals), 1) if vals else "N/A"

    def avg_tokens(lst):
        totals = [r["total_tokens"].get("total", 0) for r in lst if r.get("total_tokens")]
        return round(sum(totals) / len(totals)) if totals else "N/A"

    print("\n" + "=" * 70)
    print("BENCHMARK RESULTS: Forwarded Tools vs. Desktop-Only")
    print("=" * 70)
    print(f"Task: Read hello.txt and report contents")
    print(f"Model: {results[0].get('model_id', 'claude-sonnet-4-6')}")
    print(f"Trials per arm: {len(with_results)}")
    print()
    print(f"{'Metric':<25} {'WITH forwarded':<20} {'WITHOUT (desktop)':<20} {'Δ'}")
    print("-" * 70)
    print(f"{'Latency (avg sec)':<25} {avg(with_results, 'elapsed_seconds'):<20} {avg(without_results, 'elapsed_seconds'):<20}")
    print(f"{'Total tokens (avg)':<25} {avg_tokens(with_results):<20} {avg_tokens(without_results):<20}")
    print(f"{'Tool calls (avg)':<25} {avg(with_results, 'tool_call_count'):<20} {avg(without_results, 'tool_call_count'):<20}")
    print(f"{'Screenshots (avg)':<25} {avg(with_results, 'screenshot_count'):<20} {avg(without_results, 'screenshot_count'):<20}")
    print(f"{'Correct answers':<25} {sum(1 for r in with_results if r['correct'])}/{len(with_results):<17} {sum(1 for r in without_results if r['correct'])}/{len(without_results)}")
    print("=" * 70)

    # Compute speedup/savings if both have numeric results
    try:
        with_lat = avg(with_results, 'elapsed_seconds')
        without_lat = avg(without_results, 'elapsed_seconds')
        if isinstance(with_lat, (int, float)) and isinstance(without_lat, (int, float)) and with_lat > 0:
            print(f"\nForwarded tools are {without_lat/with_lat:.1f}x faster")
        with_tok = avg_tokens(with_results)
        without_tok = avg_tokens(without_results)
        if isinstance(with_tok, (int, float)) and isinstance(without_tok, (int, float)) and with_tok > 0:
            print(f"Forwarded tools use {without_tok/with_tok:.1f}x fewer tokens")
    except (TypeError, ZeroDivisionError):
        pass

    print()


def main():
    parser = create_parser()
    args = parser.parse_args()

    print("=" * 70)
    print("MCP Tool Forwarding Benchmark")
    print("=" * 70)
    print(f"Model: {args.model_id}")
    print(f"Trials per arm: {args.trials}")
    print(f"Task: Read hello.txt")
    print()

    results = []

    for trial in range(1, args.trials + 1):
        print(f"\n--- Trial {trial}/{args.trials}: WITH forwarded tools ---")
        r = run_single_trial(args, "with_forwarded", use_forwarded=True)
        r["trial"] = trial
        results.append(r)
        print(f"  Result: correct={r['correct']}, {r['elapsed_seconds']}s, "
              f"{r['tool_call_count']} tools, {r['screenshot_count']} screenshots")

        print(f"\n--- Trial {trial}/{args.trials}: WITHOUT forwarded tools (desktop only) ---")
        r = run_single_trial(args, "without_forwarded", use_forwarded=False)
        r["trial"] = trial
        results.append(r)
        print(f"  Result: correct={r['correct']}, {r['elapsed_seconds']}s, "
              f"{r['tool_call_count']} tools, {r['screenshot_count']} screenshots")

    # Save results
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(results, indent=2))
    print(f"\nRaw results saved to: {output_path}")

    print_comparison(results)

    return 0


if __name__ == "__main__":
    sys.exit(main())
