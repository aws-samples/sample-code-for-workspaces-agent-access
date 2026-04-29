#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""
Analyze metrics from agent execution logs
Useful for understanding performance and preparing data for DynamoDB
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


def print_summary(metrics, validations):
    """Print summary of all runs"""
    if not metrics:
        print("No metrics to analyze")
        return
    
    print("\n" + "=" * 80)
    print(f"METRICS SUMMARY - {len(metrics)} runs")
    print("=" * 80)
    
    for i, m in enumerate(metrics, 1):
        session_id = m['session_id']
        print(f"\n--- Run {i}: {session_id} ---")
        print(f"Start: {m['start_time']}")
        print(f"Duration: {m['duration_seconds']}s")
        print(f"Success: {m['success']}")
        if m.get('error'):
            print(f"Error: {m['error']}")
        
        print(f"\nTokens:")
        print(f"  Input:  {m['total_tokens']['input']:,}")
        print(f"  Output: {m['total_tokens']['output']:,}")
        print(f"  Total:  {m['total_tokens']['total']:,}")
        
        print(f"\nActivity:")
        print(f"  Iterations:    {m['iterations']}")
        print(f"  Claude calls:  {m['summary']['total_claude_calls']}")
        print(f"  Tool calls:    {m['summary']['total_tool_calls']}")
        
        print(f"\nPerformance:")
        print(f"  Avg Claude duration: {m['summary']['avg_claude_duration']}s")
        print(f"  Avg tool duration:   {m['summary']['avg_tool_duration']}s")
        print(f"  Tokens/second:       {m['summary']['tokens_per_second']}")
        print(f"  Tool success rate:   {m['summary']['tool_success_rate']}%")
        
        # Print validation results if available
        if session_id in validations:
            val = validations[session_id]
            print(f"\nValidation:")
            print(f"  Overall Accuracy: {val['overall_accuracy']}%")
            print(f"  Correct Fields:   {val['correct_fields']}/{val['total_fields']}")
            
            if val['pdfs_validated']:
                print(f"\n  Per-PDF Results:")
                for pdf in val['pdfs_validated']:
                    print(f"    {pdf['filename']}: {pdf['accuracy']}% ({pdf['correct']}/{pdf['total']})")


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
        for tool in m['tool_calls']:
            action = tool['action']
            duration = tool['duration_seconds']
            
            if action not in tool_counts:
                tool_counts[action] = 0
                tool_durations[action] = []
                tool_failures[action] = 0
                tool_errors[action] = []
            
            tool_counts[action] += 1
            tool_durations[action].append(duration)
            if not tool['success']:
                tool_failures[action] += 1
                if tool.get('error'):
                    tool_errors[action].append(tool['error'])
    
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
        
        for tool in m['tool_calls']:
            action = tool['action']
            run_tool_counts[action] = run_tool_counts.get(action, 0) + 1
            run_tool_durations[action] = run_tool_durations.get(action, 0) + tool['duration_seconds']
            if not tool['success']:
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
    """Analyze Claude's reasoning patterns and tool selection"""
    if not metrics:
        return
    
    print("\n" + "=" * 80)
    print("CLAUDE REASONING ANALYSIS")
    print("=" * 80)
    
    # Aggregate Claude call data
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
        for call in m['claude_calls']:
            total_calls += 1
            stop_reason = call['stop_reason']
            stop_reasons[stop_reason] = stop_reasons.get(stop_reason, 0) + 1
            
            input_tok = call['input_tokens']
            output_tok = call['output_tokens']
            total_tok = call['total_tokens']
            
            total_input_tokens += input_tok
            total_output_tokens += output_tok
            total_tokens += total_tok
            
            token_usage.append(total_tok)
            durations.append(call['duration_seconds'])
            
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
    
    print(f"\nTotal Claude API calls: {total_calls}")
    
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
        print(f"\nTool Selection by Claude:")
        print(f"  (Tools Claude chose to use in responses)")
        for tool, count in sorted(tool_selections.items(), key=lambda x: x[1], reverse=True):
            print(f"  {tool:<20} {count:>5} times")
    
    # Token usage by tool
    if tool_total_tokens:
        print(f"\nToken Usage by Tool Selection:")
        print(f"  (Tokens used in API calls where Claude selected each tool)")
        print(f"\n{'Tool':<20} {'Count':<8} {'Avg Input':<12} {'Avg Output':<12} {'Avg Total':<12} {'Min Total':<12} {'Max Total':<12}")
        print("-" * 110)
        
        for tool in sorted(tool_total_tokens.keys(), key=lambda t: sum(tool_total_tokens[t]) / len(tool_total_tokens[t]), reverse=True):
            count = len(tool_total_tokens[tool])
            avg_input = sum(tool_input_tokens[tool]) / count
            avg_output = sum(tool_output_tokens[tool]) / count
            avg_total = sum(tool_total_tokens[tool]) / count
            min_total = min(tool_total_tokens[tool])
            max_total = max(tool_total_tokens[tool])
            
            print(f"{tool:<20} {count:<8} {avg_input:<12,.0f} {avg_output:<12,.0f} {avg_total:<12,.0f} {min_total:<12,} {max_total:<12,}")
        
        print(f"\nInsights:")
        print(f"  - Higher token counts may indicate more complex decision-making")
        print(f"  - Input tokens grow as conversation history accumulates")
        print(f"  - Output tokens reflect Claude's response complexity for that tool")
    
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
        
        print(f"\nClaude API Call Duration:")
        print(f"  Average:  {avg_duration:>8.3f}s")
        print(f"  Shortest: {min_duration:>8.3f}s")
        print(f"  Longest:  {max_duration:>8.3f}s")
        print(f"  Range:    {max_duration - min_duration:>8.3f}s")
    
    # Per-run breakdown
    print("\n" + "=" * 80)
    print("PER-RUN CLAUDE ANALYSIS")
    print("=" * 80)
    
    for i, m in enumerate(metrics, 1):
        print(f"\n--- Run {i}: {m['session_id']} ---")
        
        run_stop_reasons = {}
        run_tool_selections = {}
        for call in m['claude_calls']:
            reason = call['stop_reason']
            run_stop_reasons[reason] = run_stop_reasons.get(reason, 0) + 1
            
            tools_used = call.get('tools_used', [])
            for tool in tools_used:
                run_tool_selections[tool] = run_tool_selections.get(tool, 0) + 1
        
        print(f"Total calls: {len(m['claude_calls'])}")
        print(f"Tokens: {m['total_tokens']['input']:,} input + {m['total_tokens']['output']:,} output = {m['total_tokens']['total']:,} total")
        print(f"Stop reasons:")
        for reason, count in sorted(run_stop_reasons.items()):
            print(f"  {reason}: {count}")
        
        if run_tool_selections:
            print(f"Tools selected by Claude:")
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
    total_tokens = sum(m['total_tokens']['total'] for m in metrics)
    total_iterations = sum(m['iterations'] for m in metrics)
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
    """Export metrics in a format ready for DynamoDB"""
    if not metrics:
        print("No metrics to export")
        return
    
    # Transform metrics for DynamoDB
    dynamodb_items = []
    
    for m in metrics:
        session_id = m['session_id']
        item = {
            "session_id": session_id,
            "start_time": m['start_time'],
            "end_time": m.get('end_time'),
            "duration_seconds": m['duration_seconds'],
            "model_id": m.get('model_id'),
            "success": m['success'],
            "error": m.get('error'),
            "iterations": m['iterations'],
            "total_tokens": m['total_tokens']['total'],
            "input_tokens": m['total_tokens']['input'],
            "output_tokens": m['total_tokens']['output'],
            "total_claude_calls": m['summary']['total_claude_calls'],
            "total_tool_calls": m['summary']['total_tool_calls'],
            "avg_claude_duration": m['summary']['avg_claude_duration'],
            "avg_tool_duration": m['summary']['avg_tool_duration'],
            "tokens_per_second": m['summary']['tokens_per_second'],
            "tool_success_rate": m['summary']['tool_success_rate'],
        }
        
        # Add validation data if available
        if session_id in validations:
            val = validations[session_id]
            item["validation_accuracy"] = val['overall_accuracy']
            item["validation_correct_fields"] = val['correct_fields']
            item["validation_total_fields"] = val['total_fields']
            item["validation_json"] = json.dumps(val)
        
        # Store detailed data as JSON strings for DynamoDB
        item["tool_calls_json"] = json.dumps(m['tool_calls'])
        item["claude_calls_json"] = json.dumps(m['claude_calls'])
        
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
  
  # Analyze metrics since a specific timestamp
  python scripts/analyze_metrics.py --since 20260304_003743
  
  # Specify custom metrics directory
  python scripts/analyze_metrics.py --dir custom_metrics/
        """
    )
    parser.add_argument('--dir', default='metrics', help='Metrics directory (default: metrics)')
    parser.add_argument('--since', help='Only analyze runs from this timestamp onwards (format: YYYYMMDD_HHMMSS)')
    parser.add_argument('--export', default='metrics_for_dynamodb.json', help='Output file for DynamoDB export')
    
    args = parser.parse_args()
    
    if args.since:
        print(f"Loading metrics from: {args.dir}/ (since {args.since})")
    else:
        print(f"Loading metrics from: {args.dir}/")
    
    metrics, validations = load_metrics(args.dir, args.since)
    
    if not metrics:
        print("No metrics found")
        return 1
    
    print_summary(metrics, validations)
    print_tool_breakdown(metrics)
    print_claude_reasoning_analysis(metrics)
    print_aggregate_stats(metrics, validations)
    export_for_dynamodb(metrics, validations, args.export)
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
