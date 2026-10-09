#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Analyze metrics from agent execution logs
Useful for understanding performance and preparing data for DynamoDB

Reads the metrics_*.json files lib/strands_logger.py writes (one per run). Runs recorded by
older versions used ``claude_calls`` instead of ``model_calls`` and carried no per-call
token counts; they still load, with those fields shown as zero.

For a quick before/after comparison, ``--table`` prints one line per run plus the averages.
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path


def parse_timestamp(timestamp_str):
    """Parse timestamp string in format YYYYMMDD_HHMMSS"""
    try:
        return datetime.strptime(timestamp_str, "%Y%m%d_%H%M%S")
    except ValueError:
        print(f"Error: Invalid timestamp format. Use YYYYMMDD_HHMMSS (e.g., 20260304_003743)")
        sys.exit(1)


def load_metrics(metrics_dir="metrics", since_timestamp=None):
    """Load all metrics from JSON files in metrics directory, optionally filtered by timestamp"""
    metrics = []
    validations = {}
    metrics_path = Path(metrics_dir)

    if not metrics_path.exists():
        print(f"No metrics directory found: {metrics_dir}")
        return metrics, validations

    # Parse the since_timestamp if provided
    since_dt = parse_timestamp(since_timestamp) if since_timestamp else None

    # Load all metrics_*.json files
    for metrics_file in sorted(metrics_path.glob("metrics_*.json")):
        try:
            # Extract session_id from filename
            session_id = metrics_file.stem.replace("metrics_", "")

            # Filter by timestamp if provided
            if since_dt:
                file_dt = parse_timestamp(session_id)
                if file_dt < since_dt:
                    continue

            with open(metrics_file, 'r') as f:
                metric_data = json.load(f)
                metrics.append(metric_data)

                # Try to load corresponding validation file
                validation_file = metrics_path / f"validation_{session_id}.json"
                if validation_file.exists():
                    with open(validation_file, 'r') as vf:
                        validations[session_id] = json.load(vf)
        except Exception as e:
            print(f"Error loading {metrics_file}: {e}")

    return metrics, validations


# --- reading a run's record, tolerating older formats ----------------------------------------

def model_calls(m):
    """A run's per-model-call records (``model_calls``; older runs called them ``claude_calls``)."""
    return m.get('model_calls') or m.get('claude_calls') or []


def is_current(m):
    """True for runs recorded with per-call tokens, durations and failure accounting (schema 2 on)."""
    return m.get('schema_version', 1) >= 2


def warn_if_mixed(metrics):
    """Earlier runs lack those figures; without a note their zeros would pass as measurements."""
    old = [m for m in metrics if not is_current(m)]
    if old:
        print(f"\nNote: {len(old)} of {len(metrics)} runs were recorded before per-call accounting (no cache tokens, "
              f"tool durations or failed-tool counts; MCP errors counted as successes). Their zeros are not "
              f"measurements: compare only newer runs (--since).")


def tokens(m):
    """A run's token totals, with the cache fields that older runs lack."""
    t = m.get('total_tokens') or {}
    return {
        'input': t.get('input', 0),
        'output': t.get('output', 0),
        'total': t.get('total', 0),
        'cache_read': t.get('cache_read', 0),
        'cache_write': t.get('cache_write', 0),
    }


def _mean(values):
    values = list(values)
    return sum(values) / len(values) if values else 0


def summary(m):
    """A run's summary numbers, worked out from its records where the summary lacks them."""
    s = m.get('summary') or {}
    calls = model_calls(m)
    tools = m.get('tool_calls') or []
    return {
        'total_model_calls': s.get('total_model_calls', s.get('total_claude_calls', len(calls))),
        'failed_model_calls': s.get('failed_model_calls', 0),
        'total_tool_calls': s.get('total_tool_calls', len(tools)),
        'failed_tool_calls': s.get('failed_tool_calls', sum(1 for t in tools if not t.get('success', True))),
        'total_screenshots': s.get('total_screenshots', sum(1 for t in tools if t.get('action') == 'screenshot' and t.get('success', True))),
        'avg_model_duration': s.get('avg_model_duration',
                                    s.get('avg_claude_duration', _mean(c.get('duration_seconds', 0) for c in calls))),
        'avg_tool_duration': s.get('avg_tool_duration', _mean(t.get('duration_seconds', 0) for t in tools)),
        'tokens_per_second': s.get('tokens_per_second', 0),
        'tool_success_rate': s.get('tool_success_rate',
                                   round(sum(1 for t in tools if t.get('success', True)) / len(tools) * 100, 2)
                                   if tools else 0),
    }


def _short(text, limit=160):
    """One line, cut at a word boundary with an ellipsis."""
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "…"


def print_table(metrics):
    """One line per run, then the means: the view to compare two batches of runs with."""
    if not metrics:
        print("No metrics to analyze")
        return

    header = ["Run", "OK", "Secs", "Try", "Model", "Tools", "Fail", "Shots",
              "Input", "Output", "CacheRd", "CacheWr", "Total"]
    rows, numbers = [], []
    for m in metrics:
        s, t = summary(m), tokens(m)
        n = {'ok': bool(m.get('success')), 'secs': m.get('duration_seconds', 0), 'try': m.get('attempts', 1) or 1,
             'model': s['total_model_calls'], 'tools': s['total_tool_calls'], 'fail': s['failed_tool_calls'],
             'shots': s['total_screenshots'], 'input': t['input'], 'output': t['output'],
             'cache_read': t['cache_read'], 'cache_write': t['cache_write'], 'total': t['total']}
        numbers.append(n)
        run_id = str(m.get('session_id', '?')) + ('' if is_current(m) else '*')
        rows.append([run_id, 'yes' if n['ok'] else 'NO', f"{n['secs']:.1f}", str(n['try']), str(n['model']),
                     str(n['tools']), str(n['fail']), str(n['shots']), f"{n['input']:,}", f"{n['output']:,}",
                     f"{n['cache_read']:,}", f"{n['cache_write']:,}", f"{n['total']:,}"])

    # Average the runs that carry the figures; earlier runs would drag the means toward zero.
    counted = [n for n, m in zip(numbers, metrics) if is_current(m)] or numbers
    label = f"mean of {len(counted)}"
    mean = lambda key: _mean(n[key] for n in counted)
    ok = sum(1 for n in counted if n['ok'])
    footer = [label, f"{ok}/{len(counted)}", f"{mean('secs'):.1f}", f"{mean('try'):.1f}", f"{mean('model'):.1f}",
              f"{mean('tools'):.1f}", f"{mean('fail'):.1f}", f"{mean('shots'):.1f}", f"{mean('input'):,.0f}",
              f"{mean('output'):,.0f}", f"{mean('cache_read'):,.0f}", f"{mean('cache_write'):,.0f}", f"{mean('total'):,.0f}"]

    widths = [max(len(header[i]), *(len(r[i]) for r in rows + [footer])) for i in range(len(header))]
    line = lambda cells: " ".join(c.ljust(w) if i < 2 else c.rjust(w) for i, (c, w) in enumerate(zip(cells, widths)))
    print("\n" + line(header))
    print("-" * len(line(header)))
    for r in rows:
        print(line(r))
    print("-" * len(line(header)))
    print(line(footer))

    if any(not is_current(m) for m in metrics):
        print("\n* recorded before per-call accounting: shown, but left out of the means (see --since)")
    if any(n['try'] > 1 for n in numbers):
        print("Try > 1: the run reconnected and replayed its task; its counts include every attempt.")
    for m in metrics:
        if not m.get('success') and m.get('error'):
            print(f"  {m.get('session_id')}: {_short(m['error'])}")


def print_summary(metrics, validations):
    """Print summary of all runs"""
    if not metrics:
        print("No metrics to analyze")
        return

    print("\n" + "=" * 80)
    print(f"METRICS SUMMARY - {len(metrics)} runs")
    print("=" * 80)
    warn_if_mixed(metrics)

    for i, m in enumerate(metrics, 1):
        session_id = m['session_id']
        s, t = summary(m), tokens(m)
        print(f"\n--- Run {i}: {session_id} ---")
        print(f"Start: {m['start_time']}")
        print(f"Duration: {m['duration_seconds']}s")
        print(f"Success: {m['success']}")
        if m.get('error'):
            print(f"Error: {m['error']}")

        print(f"\nTokens:")
        print(f"  Input:       {t['input']:,}")
        print(f"  Output:      {t['output']:,}")
        print(f"  Cache read:  {t['cache_read']:,}")
        print(f"  Cache write: {t['cache_write']:,}")
        print(f"  Total:       {t['total']:,}")

        print(f"\nActivity:")
        print(f"  Iterations:    {m.get('iterations', 0)}")
        if m.get('attempts', 1) > 1:
            print(f"  Attempts:      {m['attempts']} (the task was replayed after a reconnect; counts include every attempt)")
        print(f"  Model calls:   {s['total_model_calls']}" + (f" ({s['failed_model_calls']} failed)" if s.get('failed_model_calls') else ""))
        print(f"  Tool calls:    {s['total_tool_calls']} ({s['failed_tool_calls']} failed)")
        print(f"  Screenshots:   {s['total_screenshots']}")

        print(f"\nPerformance:")
        print(f"  Avg model call duration: {s['avg_model_duration']}s")
        print(f"  Avg tool duration:       {s['avg_tool_duration']}s")
        print(f"  Tokens/second:           {s['tokens_per_second']}")
        print(f"  Tool success rate:       {s['tool_success_rate']}%")

        # Print validation results if available
        if session_id in validations:
            val = validations[session_id]
            print(f"\nValidation:")
            print(f"  Overall Accuracy: {val['overall_accuracy']}%")
            print(f"  Correct Fields:   {val['correct_fields']}/{val['total_fields']}")


def print_tool_breakdown(metrics):
    """Print breakdown of tool usage across all runs"""
    if not metrics:
        return

    print("\n" + "=" * 80)
    print("TOOL USAGE BREAKDOWN (Across All Runs)")
    print("=" * 80)

    # Aggregate tool data across all runs
    tool_counts = {}
    tool_durations = {}
    tool_failures = {}
    tool_errors = {}

    for m in metrics:
        for tool in m.get('tool_calls') or []:
            action = tool['action']
            duration = tool.get('duration_seconds', 0)

            if action not in tool_counts:
                tool_counts[action] = 0
                tool_durations[action] = []
                tool_failures[action] = 0
                tool_errors[action] = []

            tool_counts[action] += 1
            tool_durations[action].append(duration)
            if not tool.get('success', True):
                tool_failures[action] += 1
                if tool.get('error'):
                    tool_errors[action].append(tool['error'])

    if not tool_counts:
        print("\nNo tool calls recorded.")
        return

    # Print aggregate statistics
    print(f"\n{'Action':<20} {'Count':<8} {'Success':<10} {'Avg Time':<10} {'Min':<8} {'Max':<8} {'Std Dev':<10}")
    print("-" * 95)

    for action in sorted(tool_counts.keys()):
        count = tool_counts[action]
        durations = tool_durations[action]
        failures = tool_failures[action]
        success_rate = ((count - failures) / count * 100) if count > 0 else 0

        avg_time = sum(durations) / len(durations)
        min_time = min(durations)
        max_time = max(durations)

        # Calculate standard deviation
        variance = sum((d - avg_time) ** 2 for d in durations) / len(durations)
        std_dev = variance ** 0.5

        print(f"{action:<20} {count:<8} {success_rate:<9.1f}% {avg_time:<10.3f} {min_time:<8.3f} {max_time:<8.3f} {std_dev:<10.3f}")

        # Show errors if any
        if tool_errors[action]:
            unique_errors = {}
            for error in tool_errors[action]:
                unique_errors[error] = unique_errors.get(error, 0) + 1

            print(f"  Errors ({failures} total):")
            for error, error_count in unique_errors.items():
                print(f"    - {error} ({error_count}x)")

    # Print summary by tool with peaks
    print("\n" + "=" * 80)
    print("TOOL PERFORMANCE SUMMARY")
    print("=" * 80)

    for action in sorted(tool_counts.keys()):
        count = tool_counts[action]
        durations = tool_durations[action]
        failures = tool_failures[action]
        success_rate = ((count - failures) / count * 100) if count > 0 else 0

        avg_time = sum(durations) / len(durations)
        min_time = min(durations)
        max_time = max(durations)

        print(f"\n{action}:")
        print(f"  Total calls:    {count}")
        print(f"  Success rate:   {success_rate:.1f}% ({count - failures}/{count})")
        print(f"  Avg duration:   {avg_time:.3f}s")
        print(f"  Shortest call:  {min_time:.3f}s")
        print(f"  Longest call:   {max_time:.3f}s")
        print(f"  Duration range: {max_time - min_time:.3f}s")

        if tool_errors[action]:
            print(f"  Errors: {len(tool_errors[action])} failures")
            unique_errors = {}
            for error in tool_errors[action]:
                unique_errors[error] = unique_errors.get(error, 0) + 1
            for error, error_count in unique_errors.items():
                print(f"    - {error} ({error_count}x)")

    # Print per-run breakdown
    print("\n" + "=" * 80)
    print("PER-RUN TOOL BREAKDOWN")
    print("=" * 80)

    for i, m in enumerate(metrics, 1):
        print(f"\n--- Run {i}: {m['session_id']} ---")

        # Count tool actions for this run
        run_tool_counts = {}
        run_tool_durations = {}
        run_tool_failures = {}

        for tool in m.get('tool_calls') or []:
            action = tool['action']
            run_tool_counts[action] = run_tool_counts.get(action, 0) + 1
            run_tool_durations[action] = run_tool_durations.get(action, 0) + tool.get('duration_seconds', 0)
            if not tool.get('success', True):
                run_tool_failures[action] = run_tool_failures.get(action, 0) + 1

        print(f"\n{'Action':<20} {'Count':<10} {'Total Time':<15} {'Avg Time':<15} {'Failures'}")
        print("-" * 80)

        for action in sorted(run_tool_counts.keys()):
            count = run_tool_counts[action]
            total_time = run_tool_durations[action]
            avg_time = total_time / count
            failures = run_tool_failures.get(action, 0)

            print(f"{action:<20} {count:<10} {total_time:<15.3f} {avg_time:<15.3f} {failures}")


def print_claude_reasoning_analysis(metrics):
    """Analyze the model's reasoning patterns and tool selection"""
    if not metrics:
        return

    print("\n" + "=" * 80)
    print("MODEL REASONING ANALYSIS")
    print("=" * 80)

    # Aggregate model call data
    total_calls = 0
    stop_reasons = {}
    total_input_tokens = 0
    total_output_tokens = 0
    total_tokens = 0
    token_usage = []
    durations = []
    tool_selections = {}

    # Track tokens by tool - store all token values for each tool
    tool_input_tokens = {}
    tool_output_tokens = {}
    tool_total_tokens = {}

    for m in metrics:
        for call in model_calls(m):
            if call.get('failed'):
                continue   # an attempt that raised made no progress; it is listed in the run's failed_model_calls
            total_calls += 1
            stop_reason = call.get('stop_reason', 'unknown')
            stop_reasons[stop_reason] = stop_reasons.get(stop_reason, 0) + 1

            input_tok = call.get('input_tokens', 0)
            output_tok = call.get('output_tokens', 0)
            total_tok = call.get('total_tokens', 0)

            total_input_tokens += input_tok
            total_output_tokens += output_tok
            total_tokens += total_tok

            token_usage.append(total_tok)
            durations.append(call.get('duration_seconds', 0))

            # Track tool selections and their token usage
            tools_used = call.get('tools_used', [])
            for tool in tools_used:
                tool_selections[tool] = tool_selections.get(tool, 0) + 1

                # Track token usage for this tool
                if tool not in tool_input_tokens:
                    tool_input_tokens[tool] = []
                    tool_output_tokens[tool] = []
                    tool_total_tokens[tool] = []

                tool_input_tokens[tool].append(input_tok)
                tool_output_tokens[tool].append(output_tok)
                tool_total_tokens[tool].append(total_tok)

    print(f"\nTotal model calls: {total_calls}")
    if total_calls and not total_tokens:
        print("  (per-call token counts were not recorded for these runs)")

    print(f"\nTotal Token Usage (All Runs):")
    print(f"  Input tokens:   {total_input_tokens:>12,}")
    print(f"  Output tokens:  {total_output_tokens:>12,}")
    print(f"  Total tokens:   {total_tokens:>12,}")
    print(f"  Input/Output:   {total_input_tokens/total_output_tokens:.1f}:1" if total_output_tokens > 0 else "  Input/Output:   N/A")

    print(f"\nStop Reason Distribution:")
    for reason, count in sorted(stop_reasons.items(), key=lambda x: x[1], reverse=True):
        percentage = (count / total_calls * 100) if total_calls > 0 else 0
        print(f"  {reason:<15} {count:>5} calls ({percentage:>5.1f}%)")

    # Tool selection statistics
    if tool_selections:
        print(f"\nTool Selection by the model:")
        print(f"  (Tools the model chose to use in responses)")
        name_width = max(20, *(len(t) for t in tool_selections)) + 1
        for tool, count in sorted(tool_selections.items(), key=lambda x: x[1], reverse=True):
            print(f"  {tool:<{name_width}} {count:>5} times")

    # Token usage by tool
    if tool_total_tokens:
        print(f"\nToken Usage by Tool Selection:")
        print(f"  (Tokens used in API calls where the model selected each tool)")
        name_width = max(20, *(len(t) for t in tool_total_tokens)) + 1
        print(f"\n{'Tool':<{name_width}} {'Count':<8} {'Avg Input':<12} {'Avg Output':<12} {'Avg Total':<12} {'Min Total':<12} {'Max Total':<12}")
        print("-" * (name_width + 90))

        for tool in sorted(tool_total_tokens.keys(), key=lambda t: sum(tool_total_tokens[t]) / len(tool_total_tokens[t]), reverse=True):
            count = len(tool_total_tokens[tool])
            avg_input = sum(tool_input_tokens[tool]) / count
            avg_output = sum(tool_output_tokens[tool]) / count
            avg_total = sum(tool_total_tokens[tool]) / count
            min_total = min(tool_total_tokens[tool])
            max_total = max(tool_total_tokens[tool])

            print(f"{tool:<{name_width}} {count:<8} {avg_input:<12,.0f} {avg_output:<12,.0f} {avg_total:<12,.0f} {min_total:<12,} {max_total:<12,}")

        print(f"\nInsights:")
        print(f"  - Higher token counts may indicate more complex decision-making")
        print(f"  - Input tokens grow as conversation history accumulates")
        print(f"  - Output tokens reflect the model's response complexity for that tool")

    # Token usage statistics per call
    if token_usage:
        avg_tokens = sum(token_usage) / len(token_usage)
        min_tokens = min(token_usage)
        max_tokens = max(token_usage)

        print(f"\nToken Usage per Call:")
        print(f"  Average:  {avg_tokens:>10,.0f} tokens")
        print(f"  Minimum:  {min_tokens:>10,} tokens")
        print(f"  Maximum:  {max_tokens:>10,} tokens")
        print(f"  Range:    {max_tokens - min_tokens:>10,} tokens")

    # Duration statistics
    if durations:
        avg_duration = sum(durations) / len(durations)
        min_duration = min(durations)
        max_duration = max(durations)

        print(f"\nModel Call Duration:")
        print(f"  Average:  {avg_duration:>8.3f}s")
        print(f"  Shortest: {min_duration:>8.3f}s")
        print(f"  Longest:  {max_duration:>8.3f}s")
        print(f"  Range:    {max_duration - min_duration:>8.3f}s")

    # Per-run breakdown
    print("\n" + "=" * 80)
    print("PER-RUN MODEL ANALYSIS")
    print("=" * 80)

    for i, m in enumerate(metrics, 1):
        print(f"\n--- Run {i}: {m['session_id']} ---")

        run_stop_reasons = {}
        run_tool_selections = {}
        calls = model_calls(m)
        for call in calls:
            reason = call.get('stop_reason', 'unknown')
            run_stop_reasons[reason] = run_stop_reasons.get(reason, 0) + 1

            tools_used = call.get('tools_used', [])
            for tool in tools_used:
                run_tool_selections[tool] = run_tool_selections.get(tool, 0) + 1

        t = tokens(m)
        print(f"Total calls: {len(calls)}")
        print(f"Tokens: {t['input']:,} input + {t['output']:,} output = {t['total']:,} total")
        print(f"Stop reasons:")
        for reason, count in sorted(run_stop_reasons.items()):
            print(f"  {reason}: {count}")

        if run_tool_selections:
            print(f"Tools selected by the model:")
            for tool, count in sorted(run_tool_selections.items(), key=lambda x: x[1], reverse=True):
                print(f"  {tool}: {count}")


def print_aggregate_stats(metrics, validations):
    """Print aggregate statistics across all runs"""
    if not metrics:
        return

    print("\n" + "=" * 80)
    print("AGGREGATE STATISTICS")
    print("=" * 80)

    total_duration = sum(m['duration_seconds'] for m in metrics)
    total_tokens = sum(tokens(m)['total'] for m in metrics)
    total_iterations = sum(m.get('iterations', 0) for m in metrics)
    successful_runs = sum(1 for m in metrics if m['success'])

    print(f"\nOverall:")
    print(f"  Total runs:        {len(metrics)}")
    print(f"  Successful runs:   {successful_runs} ({successful_runs/len(metrics)*100:.1f}%)")
    print(f"  Total duration:    {total_duration:.1f}s ({total_duration/60:.1f} minutes)")
    print(f"  Total tokens:      {total_tokens:,}")
    print(f"  Total iterations:  {total_iterations}")
    print(f"  Avg duration/run:  {total_duration/len(metrics):.1f}s")
    print(f"  Avg tokens/run:    {total_tokens//len(metrics):,}")

    # Validation aggregate
    if validations:
        total_accuracy = sum(v['overall_accuracy'] for v in validations.values())
        avg_accuracy = total_accuracy / len(validations)
        perfect_runs = sum(1 for v in validations.values() if v['overall_accuracy'] == 100.0)

        print(f"\nValidation:")
        print(f"  Runs with validation: {len(validations)}")
        print(f"  Average accuracy:     {avg_accuracy:.1f}%")
        print(f"  Perfect runs (100%):  {perfect_runs} ({perfect_runs/len(validations)*100:.1f}%)")


def export_for_dynamodb(metrics, validations, output_file="metrics_for_dynamodb.json"):
    """Export metrics in a format ready for DynamoDB

    The ``*_claude_*`` field names are kept as they were so an existing table schema still
    fits; they hold the model-call figures whichever model ran.
    """
    if not metrics:
        print("No metrics to export")
        return

    # Transform metrics for DynamoDB
    dynamodb_items = []

    for m in metrics:
        session_id = m['session_id']
        s, t = summary(m), tokens(m)
        item = {
            "session_id": session_id,
            "start_time": m['start_time'],
            "end_time": m.get('end_time'),
            "duration_seconds": m['duration_seconds'],
            "model_id": m.get('model_id'),
            "success": m['success'],
            "error": m.get('error'),
            "iterations": m.get('iterations', 0),
            "schema_version": m.get('schema_version', 1),
            "attempts": m.get('attempts', 1),
            "active_seconds": m.get('active_seconds'),
            "total_tokens": t['total'],
            "input_tokens": t['input'],
            "output_tokens": t['output'],
            "cache_read_tokens": t['cache_read'],
            "cache_write_tokens": t['cache_write'],
            "total_claude_calls": s['total_model_calls'],
            "total_tool_calls": s['total_tool_calls'],
            "failed_tool_calls": s['failed_tool_calls'],
            "total_screenshots": s['total_screenshots'],
            "avg_claude_duration": s['avg_model_duration'],
            "avg_tool_duration": s['avg_tool_duration'],
            "tokens_per_second": s['tokens_per_second'],
            "tool_success_rate": s['tool_success_rate'],
        }

        # Add validation data if available
        if session_id in validations:
            val = validations[session_id]
            item["validation_accuracy"] = val['overall_accuracy']
            item["validation_correct_fields"] = val['correct_fields']
            item["validation_total_fields"] = val['total_fields']
            item["validation_json"] = json.dumps(val)

        # Store detailed data as JSON strings for DynamoDB
        item["tool_calls_json"] = json.dumps(m.get('tool_calls') or [])
        item["claude_calls_json"] = json.dumps(model_calls(m))

        dynamodb_items.append(item)

    with open(output_file, 'w') as f:
        json.dump(dynamodb_items, f, indent=2)

    print(f"\n✓ Exported {len(dynamodb_items)} items to {output_file}")
    print(f"  Ready for DynamoDB batch write")


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description='Analyze agent metrics and validation results',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Analyze all metrics
  python scripts/analyze_metrics.py

  # One line per run (steps, tokens, failures) plus averages, e.g. to compare two batches
  python scripts/analyze_metrics.py --dir agents/application_validation/metrics --since 20260304_003743 --table

  # Analyze metrics since a specific timestamp
  python scripts/analyze_metrics.py --since 20260304_003743

  # Specify custom metrics directory
  python scripts/analyze_metrics.py --dir custom_metrics/
        """
    )
    parser.add_argument('--dir', default='metrics', help='Metrics directory (default: metrics)')
    parser.add_argument('--since', help='Only analyze runs from this timestamp onwards (format: YYYYMMDD_HHMMSS)')
    parser.add_argument('--table', action='store_true',
                        help='Print one line per run plus the averages, instead of the full report')
    parser.add_argument('--export', default=None, metavar='FILE',
                        help='Output file for the DynamoDB export (default: metrics_for_dynamodb.json, '
                             'written by the full report; with --table only when given)')

    args = parser.parse_args()

    if args.since:
        print(f"Loading metrics from: {args.dir}/ (since {args.since})")
    else:
        print(f"Loading metrics from: {args.dir}/")

    metrics, validations = load_metrics(args.dir, args.since)

    if not metrics:
        print("No metrics found")
        return 1

    if args.table:
        print_table(metrics)
        if args.export:
            export_for_dynamodb(metrics, validations, args.export)
        return 0

    print_summary(metrics, validations)
    print_tool_breakdown(metrics)
    print_claude_reasoning_analysis(metrics)
    print_aggregate_stats(metrics, validations)
    export_for_dynamodb(metrics, validations, args.export or 'metrics_for_dynamodb.json')

    return 0


if __name__ == "__main__":
    sys.exit(main())
