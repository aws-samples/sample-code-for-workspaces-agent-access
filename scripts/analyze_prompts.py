#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Analyze agent logs and prompts to suggest improvements using Claude.
This script uses AI to review execution logs, metrics, and current prompts
to recommend specific improvements for better agent performance.
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import boto3

logger = logging.getLogger(__name__)

# Model used for log/prompt analysis. Override via env var for easy
# experimentation without editing the script.
ANALYSIS_MODEL_ID = os.environ.get(
    "ANALYSIS_MODEL_ID",
    "global.anthropic.claude-sonnet-4-6",
)


def load_log_file(log_path):
    """Load and return log file content"""
    try:
        with open(log_path, 'r') as f:
            return f.read()
    except Exception as e:
        print(f"Error loading log file {log_path}: {e}")
        return None


def load_metrics_file(metrics_path):
    """Load and return metrics JSON"""
    try:
        with open(metrics_path, 'r') as f:
            return json.load(f)
    except Exception as e:
        print(f"Error loading metrics file {metrics_path}: {e}")
        return None


def load_prompt_file(prompt_path):
    """Load and return prompt content"""
    try:
        with open(prompt_path, 'r') as f:
            return f.read()
    except Exception as e:
        print(f"Error loading prompt file {prompt_path}: {e}")
        return None


def load_skill_file(skill_path):
    """Load and return skill JSON"""
    try:
        with open(skill_path, 'r') as f:
            return json.load(f)
    except Exception as e:
        print(f"Error loading skill file {skill_path}: {e}")
        return None


def load_recommendations_file(recommendations_path):
    """Load and return recommendations markdown content"""
    try:
        with open(recommendations_path, 'r') as f:
            return f.read()
    except Exception as e:
        print(f"Error loading recommendations file {recommendations_path}: {e}")
        return None


def compare_sessions_with_claude(before_metrics, after_metrics, recommendations, region='us-east-1'):
    """Use Claude to compare before/after sessions and evaluate recommendations"""

    # Extract key metrics for comparison
    def extract_key_metrics(m):
        return {
            "duration": m.get('duration_seconds', 0),
            "success": m.get('success', False),
            "iterations": m.get('iterations', 0),
            "total_tokens": m.get('total_tokens', {}).get('total', 0),
            "input_tokens": m.get('total_tokens', {}).get('input', 0),
            "output_tokens": m.get('total_tokens', {}).get('output', 0),
            "tool_calls": len(m.get('tool_calls', [])),
            "screenshots": sum(1 for t in m.get('tool_calls', []) if t.get('action') == 'screenshot'),
            "claude_calls": len(m.get('claude_calls', [])),
            "tool_success_rate": m.get('summary', {}).get('tool_success_rate', 0),
            "validation_accuracy": m.get('validation', {}).get('overall_accuracy', None),
            "prompt_versions": m.get('prompt_versions', {})
        }

    before = extract_key_metrics(before_metrics)
    after = extract_key_metrics(after_metrics)

    # Build comparison prompt
    comparison_prompt = f"""You are an expert AI prompt engineer evaluating the effectiveness of prompt optimizations for an autonomous agent.

## CONTEXT
An agent was optimized based on recommendations. Your task is to analyze whether the optimizations were successful and provide guidance on next steps.

## BEFORE OPTIMIZATION (Baseline)
Session: {before_metrics.get('session_id')}
Prompt Versions: System {before['prompt_versions'].get('system_prompt', {}).get('version', 'unknown')}, Task {before['prompt_versions'].get('task_prompt', {}).get('version', 'unknown')}

Performance:
- Duration: {before['duration']:.1f}s
- Success: {before['success']}
- Total Tokens: {before['total_tokens']:,} (Input: {before['input_tokens']:,}, Output: {before['output_tokens']:,})
- Screenshots: {before['screenshots']}
- Iterations: {before['iterations']}
- Claude API Calls: {before['claude_calls']}
- Tool Success Rate: {before['tool_success_rate']:.1f}%
- Validation Accuracy: {before['validation_accuracy']}%

## AFTER OPTIMIZATION
Session: {after_metrics.get('session_id')}
Prompt Versions: System {after['prompt_versions'].get('system_prompt', {}).get('version', 'unknown')}, Task {after['prompt_versions'].get('task_prompt', {}).get('version', 'unknown')}

Performance:
- Duration: {after['duration']:.1f}s ({((after['duration'] - before['duration']) / before['duration'] * 100):+.1f}% change)
- Success: {after['success']}
- Total Tokens: {after['total_tokens']:,} ({((after['total_tokens'] - before['total_tokens']) / before['total_tokens'] * 100):+.1f}% change)
- Screenshots: {after['screenshots']} ({after['screenshots'] - before['screenshots']:+d} change)
- Iterations: {after['iterations']} ({after['iterations'] - before['iterations']:+d} change)
- Claude API Calls: {after['claude_calls']} ({after['claude_calls'] - before['claude_calls']:+d} change)
- Tool Success Rate: {after['tool_success_rate']:.1f}% ({after['tool_success_rate'] - before['tool_success_rate']:+.1f}% change)
- Validation Accuracy: {after['validation_accuracy']}% ({(after['validation_accuracy'] or 0) - (before['validation_accuracy'] or 0):+.1f}% change)

## ORIGINAL RECOMMENDATIONS
{recommendations if recommendations else "No recommendations provided"}

## YOUR ANALYSIS TASK

Provide a comprehensive evaluation with these sections:

### 1. Overall Assessment
- Did the optimizations achieve their goals?
- Were there any unexpected regressions?
- Is quality maintained (validation accuracy)?

### 2. Metrics Analysis
For each key metric, analyze:
- **Duration**: Target was 50% reduction. Actual change: {((after['duration'] - before['duration']) / before['duration'] * 100):+.1f}%
- **Screenshots**: Target was 68% reduction (31 → 8-10). Actual: {before['screenshots']} → {after['screenshots']}
- **Tokens**: Target was 43% reduction. Actual change: {((after['total_tokens'] - before['total_tokens']) / before['total_tokens'] * 100):+.1f}%
- **Accuracy**: Must remain 100%. Actual: {before['validation_accuracy']}% → {after['validation_accuracy']}%

### 3. Recommendation Effectiveness
- Which recommendations were successfully implemented?
- Which had the expected impact?
- Which didn't work as expected?
- Were there unintended consequences?

### 4. Next Steps
Based on the results, recommend:
- **If successful**: Further optimizations to try
- **If partially successful**: Adjustments to make
- **If unsuccessful**: Whether to rollback or try different approach

### 5. Specific Action Items
Provide 3-5 concrete next steps with priority (High/Medium/Low)

## CRITICAL CONSTRAINTS
- Quality (validation accuracy) MUST be 100% - any reduction is unacceptable
- If accuracy dropped, recommend immediate rollback
- Focus on sustainable improvements, not one-time flukes
- Consider whether improvements are consistent or lucky

Please provide detailed, actionable analysis.
"""

    print("\n" + "=" * 80)
    print("🤖 Comparing sessions with Claude...")
    print("=" * 80)

    # Use Bedrock
    try:
        bedrock = boto3.client('bedrock-runtime', region_name=region)

        request_body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 8000,
            "temperature": 0.0,
            "messages": [{
                "role": "user",
                "content": comparison_prompt
            }]
        }

        response = bedrock.invoke_model(
            modelId=ANALYSIS_MODEL_ID,
            body=json.dumps(request_body)
        )

        response_body = json.loads(response['body'].read())
        analysis = response_body['content'][0]['text']
        return analysis

    except Exception as e:
        print(f"Error calling Bedrock: {e}")
        logger.error("Analysis failed", exc_info=False)
        return None


def find_latest_session(logs_dir="logs", metrics_dir="metrics"):
    """Find the most recent session ID based on log files"""
    log_path = Path(logs_dir)
    if not log_path.exists():
        return None

    log_files = list(log_path.glob("agent_*.log"))
    if not log_files:
        return None

    # Sort by modification time, most recent first
    log_files.sort(key=lambda x: x.stat().st_mtime, reverse=True)

    # Extract session ID from filename (agent_YYYYMMDD_HHMMSS.log)
    latest_log = log_files[0]
    session_id = latest_log.stem.replace("agent_", "")

    return session_id


def analyze_with_claude(log_content, metrics, system_prompt, task_prompt, skill_doc, region='us-east-1'):
    """Use Claude to analyze logs and suggest prompt improvements"""

    # Build analysis prompt for Claude
    analysis_prompt = f"""You are an expert AI prompt engineer analyzing the execution logs and prompts of an autonomous agent that controls a Windows desktop to extract data from PDFs and enter it into a form application.

Your task is to analyze the agent's performance and suggest specific, actionable improvements to the prompts.

## CURRENT SYSTEM PROMPT
```
{system_prompt}
```

## CURRENT TASK PROMPT
```
{task_prompt}
```

## SKILL DOCUMENTATION
```json
{json.dumps(skill_doc, indent=2) if skill_doc else "No skill documentation"}
```

## EXECUTION METRICS
- Duration: {metrics.get('duration_seconds', 0):.1f} seconds
- Success: {metrics.get('success', False)}
- Iterations: {metrics.get('iterations', 0)}
- Total tool calls: {metrics.get('summary', {}).get('total_tool_calls', 0)}
- Tool success rate: {metrics.get('summary', {}).get('tool_success_rate', 0):.1f}%
- Validation accuracy: {metrics.get('validation', {}).get('overall_accuracy', 'N/A')}%

## EXECUTION LOG (Last 15000 characters)
```
{log_content[-15000:]}
```

## YOUR ANALYSIS TASK

Please analyze the execution and provide specific, actionable improvements.

## OPTIMIZATION GOALS (in priority order):

1. **QUALITY FIRST** - Maintain or improve accuracy (validation should be 100%)
   - Zero errors in data extraction
   - Correct tool usage every time
   - Reliable completion of all tasks

2. **EFFICIENCY SECOND** - Reduce time and token usage WITHOUT compromising quality
   - Minimize unnecessary actions (redundant screenshots, extra clicks)
   - Reduce thinking/reasoning tokens (clearer instructions = less deliberation)
   - Optimize workflow (better ordering, fewer iterations)

3. **RELIABILITY THIRD** - Make the agent more robust
   - Handle edge cases gracefully
   - Recover from unexpected states
   - Consistent behavior across runs

## ANALYSIS SECTIONS:

1. **Performance Issues Identified**
   - What went wrong or could be improved?
   - Were there repeated mistakes or inefficiencies?
   - Did the agent follow instructions correctly?
   - Were there any tool usage errors?
   - Were there unnecessary actions that wasted time/tokens?

2. **System Prompt Improvements**
   - Specific additions, modifications, or removals
   - Better phrasing for clarity (reduces reasoning tokens)
   - Missing instructions or constraints
   - Provide exact text changes with before/after examples
   - Explain how each change maintains/improves quality while reducing time/tokens

3. **Task Prompt Improvements**
   - Clearer step-by-step instructions
   - Better ordering of steps (more efficient workflow)
   - Missing details or edge cases
   - Provide exact text changes with before/after examples
   - Explain how each change maintains/improves quality while reducing time/tokens

4. **Skill Documentation Improvements**
   - Missing information about the application
   - Better examples or clarifications
   - Suggest specific JSON structure changes
   - Focus on information that prevents errors and reduces trial-and-error

5. **Priority Ranking**
   - Rank your suggestions by expected impact (High/Medium/Low)
   - For each suggestion, estimate impact on:
     * Quality/Accuracy (maintain 100%)
     * Execution Time (reduce)
     * Token Usage (reduce)
     * Reliability (improve)

6. **Specific Metrics to Watch**
   - Which metrics should improve after implementing your suggestions?
   - What quality checks should remain at 100%?
   - What new issues might arise?

## CRITICAL CONSTRAINTS:

- DO NOT suggest changes that could reduce accuracy below 100%
- DO NOT sacrifice reliability for speed
- DO NOT remove safety checks or validation steps
- DO suggest removing redundant actions that don't improve quality
- DO suggest clearer instructions that reduce Claude's reasoning overhead
- DO suggest workflow optimizations that maintain quality while saving time

Please be specific and actionable. Provide exact text that should be added, removed, or modified.
Focus on improvements that maintain perfect quality while reducing execution time and token usage.
"""

    print("\n" + "=" * 80)
    print("🤖 Analyzing with Claude...")
    print("=" * 80)

    # Use Bedrock
    try:
        bedrock = boto3.client('bedrock-runtime', region_name=region)

        request_body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 8000,
            "temperature": 0.0,
            "messages": [{
                "role": "user",
                "content": analysis_prompt
            }]
        }

        response = bedrock.invoke_model(
            modelId=ANALYSIS_MODEL_ID,
            body=json.dumps(request_body)
        )

        response_body = json.loads(response['body'].read())
        analysis = response_body['content'][0]['text']
        return analysis

    except Exception as e:
        print(f"Error calling Bedrock: {e}")
        logger.error("Analysis failed", exc_info=False)
        return None


def save_analysis(analysis, output_file, session_id=None, agent_dir=None):
    """Save analysis to a markdown file"""
    try:
        with open(output_file, 'w') as f:
            f.write("# Agent Prompt Analysis\n\n")
            f.write(f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            if session_id:
                f.write(f"**Session ID:** {session_id}\n\n")
            if agent_dir:
                f.write(f"**Agent Directory:** {agent_dir}\n\n")
            f.write("---\n\n")
            f.write(analysis)

        print(f"\n✓ Analysis saved to: {output_file}")
        return True
    except Exception as e:
        print(f"Error saving analysis: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description='Analyze agent logs and suggest prompt improvements using Claude',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Analyze the most recent session from default logs
  python scripts/analyze_prompts.py

  # Analyze agent logs
  python scripts/analyze_prompts.py --agent agents/paint_demo

  # Compare before/after optimization
  python scripts/analyze_prompts.py --compare --agent agents/paint_demo \\
    --before 20260304_190033 --after 20260304_200000 \\
    --recommendations reports/prompt_analysis_20260304_191820.md
        """
    )

    parser.add_argument('--agent', help='Agent directory path (e.g., agents/paint_demo). Uses logs/, metrics/, prompts/, skills/ subdirectories')
    parser.add_argument('--compare', action='store_true', help='Compare before/after sessions to evaluate optimization effectiveness')
    parser.add_argument('--before', help='Before session ID (required for --compare)')
    parser.add_argument('--after', help='After session ID (required for --compare)')
    parser.add_argument('--recommendations', help='Path to recommendations file (optional for --compare)')
    parser.add_argument('--session', help='Session ID to analyze (default: most recent, not used with --compare)')
    parser.add_argument('--logs-dir', help='Logs directory (overrides --agent)')
    parser.add_argument('--metrics-dir', help='Metrics directory (overrides --agent)')
    parser.add_argument('--prompts-dir', help='Prompts directory (overrides --agent)')
    parser.add_argument('--skills-dir', help='Skills directory (overrides --agent)')
    parser.add_argument('--output', help='Output filename (default: prompt_analysis_YYYYMMDD_HHMMSS.md or comparison_YYYYMMDD_HHMMSS.md, saved to reports/)')
    parser.add_argument('--region', default='us-east-1', help='AWS region for Bedrock (default: us-east-1)')

    args = parser.parse_args()

    # Validate comparison mode arguments
    if args.compare:
        if not args.before or not args.after:
            print("Error: --compare requires both --before and --after session IDs")
            return 1

    # Determine base directories
    if args.agent:
        # Use agent directory structure
        base_dir = args.agent
        logs_dir = args.logs_dir or f"{base_dir}/logs"
        metrics_dir = args.metrics_dir or f"{base_dir}/metrics"
        prompts_dir = args.prompts_dir or f"{base_dir}/prompts"
        skills_dir = args.skills_dir or f"{base_dir}/skills"
    else:
        # Use root-level directories
        logs_dir = args.logs_dir or 'logs'
        metrics_dir = args.metrics_dir or 'metrics'
        prompts_dir = args.prompts_dir or 'prompts'
        skills_dir = args.skills_dir or 'skills'

    # Create reports directory if it doesn't exist
    reports_dir = 'reports'
    os.makedirs(reports_dir, exist_ok=True)

    # COMPARISON MODE
    if args.compare:
        print(f"Comparison Mode: Evaluating optimization effectiveness")
        print(f"Before: {args.before}")
        print(f"After: {args.after}")

        # Load before metrics
        before_metrics_path = f"{metrics_dir}/metrics_{args.before}.json"
        print(f"Loading before metrics: {before_metrics_path}")
        before_metrics = load_metrics_file(before_metrics_path)
        if not before_metrics:
            print(f"Error: Could not load before metrics")
            return 1

        # Load after metrics
        after_metrics_path = f"{metrics_dir}/metrics_{args.after}.json"
        print(f"Loading after metrics: {after_metrics_path}")
        after_metrics = load_metrics_file(after_metrics_path)
        if not after_metrics:
            print(f"Error: Could not load after metrics")
            return 1

        # Load recommendations if provided
        recommendations = None
        if args.recommendations:
            print(f"Loading recommendations: {args.recommendations}")
            recommendations = load_recommendations_file(args.recommendations)
            if not recommendations:
                print(f"Warning: Could not load recommendations (continuing anyway)")

        # Compare with Claude
        analysis = compare_sessions_with_claude(
            before_metrics,
            after_metrics,
            recommendations,
            args.region
        )

        if not analysis:
            print("Error: Comparison failed")
            return 1

        # Generate output filename
        if args.output:
            output_file = f"{reports_dir}/{args.output}"
        else:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_file = f"{reports_dir}/comparison_{timestamp}.md"

        # Save comparison report
        try:
            with open(output_file, 'w') as f:
                f.write("# Prompt Optimization Comparison Report\n\n")
                f.write(f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
                f.write(f"**Before Session:** {args.before}\n")
                f.write(f"**After Session:** {args.after}\n")
                if args.agent:
                    f.write(f"**Agent Directory:** {args.agent}\n")
                if args.recommendations:
                    f.write(f"**Recommendations:** {args.recommendations}\n")
                f.write("\n---\n\n")
                f.write(analysis)

            print(f"\n✓ Comparison report saved to: {output_file}")
            print("\n" + "=" * 80)
            print("📊 COMPARISON RESULTS")
            print("=" * 80)
            print(analysis)
            return 0
        except Exception as e:
            print(f"Error saving comparison report: {e}")
            return 1

    # STANDARD ANALYSIS MODE
    # Generate output filename with timestamp
    if args.output:
        output_file = f"{reports_dir}/{args.output}"
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_file = f"{reports_dir}/prompt_analysis_{timestamp}.md"

    # Find session to analyze
    session_id = args.session
    if not session_id:
        session_id = find_latest_session(logs_dir, metrics_dir)
        if not session_id:
            print(f"Error: No sessions found in {logs_dir}. Run the agent first to generate logs.")
            return 1
        print(f"Analyzing most recent session: {session_id}")
    else:
        print(f"Analyzing session: {session_id}")

    # Load log file
    log_path = f"{logs_dir}/agent_{session_id}.log"
    print(f"Loading log: {log_path}")
    log_content = load_log_file(log_path)
    if not log_content:
        print(f"Error: Could not load log file")
        return 1

    # Load metrics file
    metrics_path = f"{metrics_dir}/metrics_{session_id}.json"
    print(f"Loading metrics: {metrics_path}")
    metrics = load_metrics_file(metrics_path)
    if not metrics:
        print(f"Error: Could not load metrics file")
        return 1

    # Load prompts
    system_prompt_path = f"{prompts_dir}/system_prompt.md"
    print(f"Loading system prompt: {system_prompt_path}")
    system_prompt = load_prompt_file(system_prompt_path)
    if not system_prompt:
        print(f"Error: Could not load system prompt")
        return 1

    task_prompt_path = f"{prompts_dir}/task_prompt.md"
    print(f"Loading task prompt: {task_prompt_path}")
    task_prompt = load_prompt_file(task_prompt_path)
    if not task_prompt:
        print(f"Error: Could not load task prompt")
        return 1

    # Load skill documentation (optional) - try common skill file patterns
    skill_doc = None
    for skill_name in ['ms-paint-skill.json', 'skill.json']:
        skill_path = f"{skills_dir}/{skill_name}"
        skill_doc = load_skill_file(skill_path)
        if skill_doc:
            print(f"Loading skill: {skill_path}")
            break
    if not skill_doc:
        print(f"Warning: Could not load skill documentation (continuing anyway)")

    # Analyze with Claude
    analysis = analyze_with_claude(
        log_content,
        metrics,
        system_prompt,
        task_prompt,
        skill_doc,
        args.region
    )

    if not analysis:
        print("Error: Analysis failed")
        return 1

    # Print analysis to console
    print("\n" + "=" * 80)
    print("📊 ANALYSIS RESULTS")
    print("=" * 80)
    print(analysis)

    # Save to file
    if save_analysis(analysis, output_file, session_id, args.agent):
        print(f"\n✓ Complete! Review the analysis in {output_file}")
        return 0
    else:
        return 1


if __name__ == "__main__":
    sys.exit(main())
