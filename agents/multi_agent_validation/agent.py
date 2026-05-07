#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Multi-Agent Application Validation — parallel agents on separate desktop sessions.

Each worker gets its own AppStream streaming session so agents don't
interfere with each other. Applications are distributed across workers
and validated simultaneously.

Usage:
    # Uses fleet/stack names from scripts/config.json by default.
    python3 agents/multi_agent_validation/agent.py

    # Override explicitly if needed:
    python3 agents/multi_agent_validation/agent.py \\
        --stack-name <STACK> --fleet-name <FLEET>
"""

import json
import os
import sys
import time
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Optional

import boto3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))

from strands import Agent
from strands.tools.mcp import MCPClient
from lib import agent_common, ScreenshotPruningConversationManager

_print_lock = threading.Lock()


def _worker_print(worker_id, msg):
    """Thread-safe print with worker prefix."""
    with _print_lock:
        sys.stdout.write(f"  [W{worker_id}] {msg}\n")
        sys.stdout.flush()


def _make_callback(worker_id):
    """Create a callback handler that prefixes output with worker ID."""
    line_buf = []

    def handler(**kwargs):
        if "data" not in kwargs:
            return
        data = kwargs["data"]
        line_buf.append(data)
        if "\n" in data:
            full = "".join(line_buf)
            for line in full.split("\n"):
                line = line.strip()
                if line:
                    _worker_print(worker_id, line)
            line_buf.clear()

    return handler


APPLICATIONS = [
    {"name": "Firefox", "search_term": "firefox",
     "interaction": "Click the address bar, type 'about:blank', press Enter",
     "success_criteria": "Browser window visible with address bar"},
    {"name": "Notepad++", "search_term": "notepad++",
     "interaction": "Click the editor area, type 'test', then Ctrl+Z to undo",
     "success_criteria": "Editor window with menu bar and text area visible"},
    {"name": "OpenOffice Calc", "search_term": "scalc",
     "interaction": "Click cell A1, type '123', press Enter, then Ctrl+Z",
     "success_criteria": "Spreadsheet grid with formula bar visible"},
    {"name": "OpenOffice Draw", "search_term": "sdraw",
     "interaction": "Click on the drawing canvas area",
     "success_criteria": "Drawing canvas with toolbars visible"},
    {"name": "OpenOffice Impress", "search_term": "simpress",
     "interaction": "Click on the slide editing area",
     "success_criteria": "Slide editing area with slide panel visible"},
    {"name": "OpenOffice Math", "search_term": "smath",
     "interaction": "Click the formula input area and type 'a + b'",
     "success_criteria": "Formula editor with rendering area visible"},
    {"name": "OpenOffice Start Center", "search_term": "soffice",
     "interaction": "Confirm the document type icons are visible",
     "success_criteria": "Start center with document type buttons visible"},
    {"name": "OpenOffice Writer", "search_term": "swriter",
     "interaction": "Click the document area, type 'test', then Ctrl+Z",
     "success_criteria": "Document editing area with formatting toolbar visible"},
]


@dataclass
class ValidationResult:
    app_name: str
    status: str = "NOT_RUN"
    duration_seconds: float = 0.0
    error: Optional[str] = None


def create_streaming_url(stack_name, fleet_name, region, user_id=None):
    """Create a fresh AppStream streaming URL for a worker."""
    client = boto3.client("appstream", region_name=region)
    resp = client.create_streaming_url(
        StackName=stack_name,
        FleetName=fleet_name,
        UserId=user_id or f"validator-{uuid.uuid4().hex[:8]}",
        Validity=3600,
    )
    return resp["StreamingURL"]


def scale_fleet(fleet_name, region, desired):
    """Update fleet desired capacity and wait for RUNNING state."""
    client = boto3.client("appstream", region_name=region)

    # Get current capacity
    fleet = client.describe_fleets(Names=[fleet_name])["Fleets"][0]
    current = fleet.get("ComputeCapacity", {}).get("DesiredInstances", 1)

    if current >= desired:
        running = fleet.get("ComputeCapacityStatus", {}).get("Running", 0)
        print(f"  Fleet already has {current} desired ({running} running)")
        return

    print(f"  Scaling fleet from {current} to {desired} instances...")
    client.update_fleet(
        Name=fleet_name,
        ComputeCapacity={"DesiredInstances": desired},
    )

    # Wait for fleet to have enough running instances
    for i in range(60):
        fleet = client.describe_fleets(Names=[fleet_name])["Fleets"][0]
        status = fleet.get("ComputeCapacityStatus", {})
        running = status.get("Running", 0)
        available = status.get("Available", 0)
        state = fleet.get("State", "UNKNOWN")
        if state == "RUNNING" and running >= desired:
            print(f"  ✓ Fleet has {running} running instances ({available} available)")
            return
        print(f"  Waiting for instances... ({running}/{desired} running, {available} available, state: {state})")
        time.sleep(15)

    print(f"  ⚠ Fleet has {running}/{desired} instances after timeout — proceeding anyway")


def build_task_prompt(app):
    return f"""Validate that {app['name']} is installed and functional on this Windows desktop.

Steps:
1. Take a screenshot to see the current desktop state
2. Press the Windows key to open the Start Menu
3. Type '{app['search_term']}' and click the best matching result
4. Wait for the application to fully open (up to 15 seconds)
5. Dismiss any non-essential dialogs (update prompts, registration, recovery)
6. Take a screenshot to verify the app opened
7. Perform this interaction: {app['interaction']}
8. Close the application with Alt+F4 (click 'Don't Save' if prompted)

Success criteria: {app['success_criteria']}

End your response with exactly one of:
  RESULT: PASS
  RESULT: FAIL
  RESULT: NOT_FOUND"""


def validate_app_batch(apps_batch, streaming_url, args, model, system_prompt, worker_id):
    """Validate a batch of apps sequentially on a single dedicated session."""
    from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client

    results = []
    endpoint = args.mcp_endpoint
    mcp_profile = getattr(args, 'mcp_profile', None)
    callback = _make_callback(worker_id)

    _worker_print(worker_id, f"Starting — {len(apps_batch)} app(s) to validate")

    # Sign MCP requests for the caller's runtime region (MCP is deployed
    # per-region). Respect --mcp-region override if set, and fall back to
    # AWS_REGION, then args.region.
    mcp_region = agent_common.resolve_mcp_region(args)
    # Substitute {region} in the endpoint template with the signing region
    # so the request hits the correct regional deployment.
    endpoint = endpoint.replace("{region}", mcp_region)

    # One MCP client per worker, reused across the entire app batch.
    # Creating a new session per app would waste AppStream startup time
    # and trigger server-side session binding conflicts.
    mcp_client = None
    last_err = None
    for attempt in range(3):
        try:
            def mcp_factory():
                return aws_iam_streamablehttp_client(
                    endpoint=endpoint,
                    aws_service=agent_common.DEFAULT_MCP_SERVICE,
                    aws_region=mcp_region,
                    aws_profile=mcp_profile,
                    headers={
                        "X-Amzn-AgentAccess-Streaming-Session-Url": streaming_url,
                    },
                )

            mcp_client = MCPClient(mcp_factory, startup_timeout=args.mcp_timeout)
            break
        except Exception as e:
            last_err = e
            if attempt < 2:
                wait = 15 * (attempt + 1)
                _worker_print(worker_id, f"  Connection failed, retrying in {wait}s...")
                time.sleep(wait)

    if mcp_client is None:
        _worker_print(worker_id, f"⚠ Could not connect after 3 attempts: {last_err}")
        for app in apps_batch:
            results.append(ValidationResult(app_name=app["name"], status="ERROR", error=str(last_err)[:120]))
        return results

    for app in apps_batch:
        result = ValidationResult(app_name=app["name"])
        start = time.time()
        _worker_print(worker_id, f"▶ Validating {app['name']}...")

        try:
            conv_manager = ScreenshotPruningConversationManager() if not args.no_screenshot_pruning else None

            agent = Agent(
                model=model,
                tools=[mcp_client],
                system_prompt=system_prompt,
                conversation_manager=conv_manager,
                callback_handler=callback,
            )

            output = str(agent(build_task_prompt(app)) or "")

            if "RESULT: PASS" in output.upper():
                result.status = "PASS"
            elif "RESULT: NOT_FOUND" in output.upper():
                result.status = "NOT_FOUND"
            elif "RESULT: FAIL" in output.upper():
                result.status = "FAIL"
            else:
                result.status = "PASS"

        except Exception as e:
            result.status = "ERROR"
            result.error = str(e)[:120]
            _worker_print(worker_id, f"⚠ Error: {result.error}")

        result.duration_seconds = round(time.time() - start, 1)
        results.append(result)

        icon = {"PASS": "✓", "FAIL": "✗", "NOT_FOUND": "?", "ERROR": "⚠"}.get(result.status, "?")
        _worker_print(worker_id, f"{icon} {result.app_name}: {result.status} ({result.duration_seconds}s)")

    _worker_print(worker_id, f"Done — {len(results)} app(s) validated")
    return results


def distribute_apps(apps, num_workers):
    """Distribute apps across workers as evenly as possible."""
    batches = [[] for _ in range(num_workers)]
    for i, app in enumerate(apps):
        batches[i % num_workers].append(app)
    return [b for b in batches if b]  # Remove empty batches


def print_report(results, total_time, num_workers):
    print("\n" + "=" * 60)
    print("  MULTI-AGENT VALIDATION REPORT")
    print("=" * 60)

    passed = sum(1 for r in results if r.status == "PASS")
    failed = sum(1 for r in results if r.status == "FAIL")
    not_found = sum(1 for r in results if r.status == "NOT_FOUND")
    errors = sum(1 for r in results if r.status == "ERROR")

    print(f"\n  Total: {len(results)} | ✓ Pass: {passed} | ✗ Fail: {failed} | ? Not Found: {not_found} | ⚠ Error: {errors}")
    print(f"  Workers: {num_workers} | Total time: {total_time:.1f}s\n")

    print(f"  {'Application':<25} {'Status':<12} {'Time':>8}")
    print(f"  {'─' * 25} {'─' * 12} {'─' * 8}")

    for r in results:
        icon = {"PASS": "✓", "FAIL": "✗", "NOT_FOUND": "?", "ERROR": "⚠", "NOT_RUN": "-"}.get(r.status, "?")
        print(f"  {r.app_name:<25} {icon} {r.status:<10} {r.duration_seconds:>6.1f}s")
        if r.error:
            print(f"  {'':>25} └─ {r.error[:60]}")

    print("\n" + "=" * 60)


def main():
    parser = agent_common.create_base_parser('Multi-Agent Application Validation')
    parser.add_argument('--workers', type=int, default=3,
                       help='Number of parallel workers, each gets its own desktop session (default: 3)')
    parser.add_argument('--apps', nargs='*', default=None,
                       help='Specific apps to validate (default: all)')
    # --stack-name and --fleet-name come from the base parser.
    args = parser.parse_args()

    agent_dir = os.path.dirname(os.path.abspath(__file__))
    region = args.region

    # We need stack + fleet to create per-worker sessions. Fall back to
    # values from scripts/config.json so the common case doesn't require flags.
    stack_name = args.stack_name or agent_common._config.get("stack", {}).get("name")
    fleet_name = args.fleet_name or agent_common._config.get("fleet", {}).get("name")

    if not stack_name or not fleet_name:
        parser.error(
            "Could not determine stack or fleet name.\n"
            "  Provide them via --stack-name and --fleet-name, or set\n"
            "  'stack.name' and 'fleet.name' in scripts/config.json."
        )

    if not agent_common._is_remote_mcp(args):
        parser.error("Multi-agent validation requires an MCP endpoint (configure in scripts/config.json)")

    agent_common.print_banner(
        "Multi-Agent Application Validation",
        "Validates desktop applications in parallel.\n"
        "Each worker gets its own desktop session.",
        args.model_id, args,
    )

    # Filter apps
    apps = APPLICATIONS
    if args.apps:
        names_lower = [a.lower() for a in args.apps]
        apps = [a for a in APPLICATIONS if a["name"].lower() in names_lower]
        if not apps:
            print(f"  No matching apps. Available: {', '.join(a['name'] for a in APPLICATIONS)}")
            return 1

    num_workers = min(args.workers, len(apps))
    batches = distribute_apps(apps, num_workers)

    print(f"  Applications: {len(apps)}")
    print(f"  Workers: {num_workers} (each with its own desktop session)")
    print(f"  Stack: {stack_name}")
    print(f"  Fleet: {fleet_name}")
    print()

    # Scale fleet up if needed
    print(f"  Checking fleet capacity...")
    scale_fleet(fleet_name, region, num_workers)

    print(f"  Creating {num_workers} streaming sessions...")
    session_tag = uuid.uuid4().hex[:6]
    worker_urls = []
    for i in range(num_workers):
        user_id = f"validator-{session_tag}-{i+1}"
        url = create_streaming_url(stack_name, fleet_name, region, user_id=user_id)
        worker_urls.append(url)
        apps_in_batch = ", ".join(a["name"] for a in batches[i])
        print(f"    Worker {i+1} ({user_id}): {apps_in_batch}")
        print(f"    URL: {url}")
    print()
    print(f"  Waiting 30s for desktop sessions to initialize...")
    time.sleep(30)

    # Load system prompt and model
    system_prompt = agent_common.load_prompt(os.path.join(agent_dir, "prompts/system_prompt.md"))
    model = agent_common.create_model(args)

    # Run workers in parallel
    print(f"  Starting parallel validation...\n")
    start_time = time.time()
    all_results = []

    with ThreadPoolExecutor(max_workers=num_workers) as executor:
        futures = {}
        for i, (batch, url) in enumerate(zip(batches, worker_urls)):
            f = executor.submit(validate_app_batch, batch, url, args, model, system_prompt, i + 1)
            futures[f] = i

        for future in as_completed(futures):
            try:
                batch_results = future.result()
                all_results.extend(batch_results)
            except Exception as e:
                worker_id = futures[future]
                for app in batches[worker_id]:
                    all_results.append(ValidationResult(app_name=app["name"], status="ERROR", error=str(e)[:120]))

    total_time = time.time() - start_time

    # Sort by original app order
    app_order = {a["name"]: i for i, a in enumerate(apps)}
    all_results.sort(key=lambda r: app_order.get(r.app_name, 999))

    print_report(all_results, total_time, num_workers)

    # Save report
    report_path = os.path.join(agent_dir, "metrics", f"validation_{time.strftime('%Y%m%d_%H%M%S')}.json")
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, 'w') as f:
        json.dump({
            "timestamp": time.strftime('%Y-%m-%dT%H:%M:%S'),
            "total_time_seconds": round(total_time, 1),
            "workers": num_workers,
            "results": [
                {"app": r.app_name, "status": r.status, "duration": r.duration_seconds, "error": r.error}
                for r in all_results
            ],
        }, f, indent=2)
    print(f"\n📊 Report: {report_path}")

    passed = sum(1 for r in all_results if r.status == "PASS")
    return 0 if passed == len(all_results) else 1


if __name__ == "__main__":
    sys.exit(main())
