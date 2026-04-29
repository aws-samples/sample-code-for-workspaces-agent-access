#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Agent Creator - Interactive CLI that interviews the user about their
target application, then generates a skill JSON and scaffolds an agent directory.
"""

import argparse
import json
import os
import signal
import sys
import textwrap

from strands import Agent
from strands.models.bedrock import BedrockModel

# Add parent directories to path to import lib
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))

BANNER = r"""
  Workspace Agent Creator

  Create an agent for automating a Windows
  desktop application via DCV.

  Answer a few questions about your app,
  and the tool will generate everything.
"""


def interview():
    """Interactive interview to gather application details from the user."""
    print(BANNER)
    print("  " + "─" * 40 + "\n")

    questions = [
        ("app_name", "What application do you want to automate? (e.g., Notepad, Excel, Paint)"),
        ("app_launch", "How do you launch it? (e.g., 'search for notepad in Start menu', 'double-click desktop icon')"),
        ("tasks", "What tasks should the agent perform? (describe the workflow in a few sentences)"),
        ("ui_layout", "Describe the UI layout (e.g., 'toolbar at top, canvas in center, sidebar on left')"),
        ("key_tools", "What are the main tools/buttons the agent will use? (comma-separated)"),
        ("shortcuts", "Any important keyboard shortcuts? (e.g., 'Ctrl+S save, Ctrl+Z undo')"),
        ("gotchas", "Any known gotchas or tricky behaviors? (or 'none')"),
        ("save_workflow", "How does the user save their work? (e.g., 'Ctrl+S opens Save As dialog')"),
    ]

    answers = {}
    for key, question in questions:
        print(f"  {question}")
        answer = input("  > ").strip()
        if not answer and key == "app_name":
            print("  App name is required.")
            answer = input("  > ").strip()
        answers[key] = answer
        print()

    return answers


def build_system_prompt():
    """Build the system prompt for the skill creator agent."""
    agent_dir = os.path.dirname(os.path.abspath(__file__))
    root_dir = os.path.join(agent_dir, '../..')

    # Load the skill creator knowledge
    skill_md_path = os.path.join(root_dir, "skills/workspace-skill-creator/SKILL.md")
    schema_path = os.path.join(root_dir, "skills/workspace-skill-creator/references/skill-schema.md")

    skill_knowledge = ""
    for path in [skill_md_path, schema_path]:
        try:
            with open(path, 'r') as f:
                content = f.read()
            if content.startswith('---'):
                end = content.find('---', 3)
                if end != -1:
                    content = content[end + 3:].strip()
            skill_knowledge += content + "\n\n"
        except Exception as e:
            print(f"  Warning: Could not load {path}: {e}")

    # Load the paint skill as a reference example
    paint_skill_path = os.path.join(root_dir, "agents/paint_demo/skills/ms-paint-skill.json")
    paint_example = ""
    try:
        with open(paint_skill_path, 'r') as f:
            paint_example = f.read()
    except Exception:
        pass

    # Load the paint demo system prompt as a reference for generated agent prompts
    paint_prompt_path = os.path.join(root_dir, "agents/paint_demo/prompts/system_prompt.md")
    paint_prompt_example = ""
    try:
        with open(paint_prompt_path, 'r') as f:
            paint_prompt_example = f.read()
    except Exception:
        pass

    return f"""You are a skill creator agent. Your job is to generate two things based on
the user's description of a Windows desktop application:

1. A SKILL JSON file — a comprehensive guide for a computer use agent to interact with the application
2. An AGENT SYSTEM PROMPT — instructions for the agent that will use the skill
3. A TASK PROMPT — a default task the agent should perform

## Skill Creator Knowledge

{skill_knowledge}

## Reference Example: MS Paint Skill JSON

This is a complete, well-structured skill. Use it as your template for structure, depth, and style:

```json
{paint_example}
```

## Reference Example: Agent System Prompt

This is the system prompt used by the paint demo agent. Generate a similar one for the new application:

```markdown
{paint_prompt_example}
```

## Output Format

You MUST respond with EXACTLY three fenced blocks, in this order, with no other text outside them:

1. The skill JSON (fenced with ```json)
2. The agent system prompt markdown (fenced with ```system_prompt)
3. The task prompt markdown (fenced with ```task_prompt)

Do not include any explanation, commentary, or text outside these three blocks.
Make the skill comprehensive — cover every tool, menu, and interaction pattern the agent might need.
Make the system prompt specific to the application and task.
Make the task prompt a concrete, step-by-step task the agent should perform.
"""


def build_task_prompt(answers):
    """Build the task prompt from interview answers."""
    return f"""Create a skill and agent for the following Windows desktop application:

Application: {answers['app_name']}
How to launch: {answers['app_launch']}
Tasks to automate: {answers['tasks']}
UI Layout: {answers['ui_layout']}
Key tools/buttons: {answers['key_tools']}
Keyboard shortcuts: {answers['shortcuts']}
Known gotchas: {answers['gotchas']}
Save workflow: {answers['save_workflow']}

Generate the skill JSON, system prompt, and task prompt now."""


def parse_output(text):
    """Parse the three fenced blocks from the agent's output."""
    blocks = {}

    # Extract JSON skill
    json_start = text.find('```json')
    if json_start != -1:
        json_end = text.find('```', json_start + 7)
        if json_end != -1:
            blocks['skill'] = text[json_start + 7:json_end].strip()

    # Extract system prompt
    sp_start = text.find('```system_prompt')
    if sp_start != -1:
        sp_end = text.find('```', sp_start + 16)
        if sp_end != -1:
            blocks['system_prompt'] = text[sp_start + 16:sp_end].strip()

    # Extract task prompt
    tp_start = text.find('```task_prompt')
    if tp_start != -1:
        tp_end = text.find('```', tp_start + 14)
        if tp_end != -1:
            blocks['task_prompt'] = text[tp_start + 14:tp_end].strip()

    return blocks


def scaffold_agent(app_name, blocks, root_dir):
    """Create the agent directory structure with generated files."""
    # Normalize agent directory name
    agent_slug = app_name.lower().replace(' ', '_').replace('-', '_')
    agent_slug = ''.join(c for c in agent_slug if c.isalnum() or c == '_')
    agent_dir = os.path.join(root_dir, f"agents/{agent_slug}_demo")

    # Create directories
    for subdir in ['prompts', 'skills', 'logs', 'metrics', 'screenshots']:
        os.makedirs(os.path.join(agent_dir, subdir), exist_ok=True)

    # Write skill JSON
    skill_path = os.path.join(agent_dir, f"skills/{agent_slug}-skill.json")
    if 'skill' in blocks:
        try:
            # Validate and pretty-print
            parsed = json.loads(blocks['skill'])
            with open(skill_path, 'w') as f:
                json.dump(parsed, f, indent=2)
            print(f"  ✓ Skill: {skill_path}")
        except json.JSONDecodeError as e:
            # Write raw if JSON is invalid
            with open(skill_path, 'w') as f:
                f.write(blocks['skill'])
            print(f"  ⚠ Skill written (invalid JSON — needs manual fix): {skill_path}")

    # Write system prompt
    if 'system_prompt' in blocks:
        sp_path = os.path.join(agent_dir, "prompts/system_prompt.md")
        with open(sp_path, 'w') as f:
            f.write(blocks['system_prompt'])
        print(f"  ✓ System prompt: {sp_path}")

    # Write task prompt
    if 'task_prompt' in blocks:
        tp_path = os.path.join(agent_dir, "prompts/task_prompt.md")
        with open(tp_path, 'w') as f:
            f.write(blocks['task_prompt'])
        print(f"  ✓ Task prompt: {tp_path}")

    # Copy agent.py from paint_demo as a template and patch it
    paint_agent = os.path.join(root_dir, "agents/paint_demo/agent.py")
    new_agent = os.path.join(agent_dir, "agent.py")
    try:
        with open(paint_agent, 'r') as f:
            agent_code = f.read()

        # Patch the agent code for the new application
        agent_code = agent_code.replace('paint_demo', f'{agent_slug}_demo')
        agent_code = agent_code.replace('Paint Demo Agent', f'{app_name} Agent')
        agent_code = agent_code.replace('Paint Drawing Demo Agent', f'{app_name} Automation Agent')
        agent_code = agent_code.replace('ms-paint-skill.json', f'{agent_slug}-skill.json')
        agent_code = agent_code.replace('MS PAINT SKILL', f'{app_name.upper()} SKILL')
        agent_code = agent_code.replace(
            'a remote Windows desktop and creates\nartwork using mouse and keyboard tools.',
            f'a remote Windows desktop to automate\n{app_name} tasks.'
        )

        with open(new_agent, 'w') as f:
            f.write(agent_code)
        print(f"  ✓ Agent: {new_agent}")
    except Exception as e:
        print(f"  ⚠ Could not generate agent.py: {e}")

    # Write .gitkeep files for empty dirs
    for subdir in ['logs', 'metrics', 'screenshots']:
        gitkeep = os.path.join(agent_dir, subdir, '.gitkeep')
        if not os.path.exists(gitkeep):
            with open(gitkeep, 'w') as f:
                pass

    return agent_dir


def streaming_handler(**kwargs):
    """Print tokens as they stream in so the user sees progress."""
    if "data" in kwargs:
        sys.stdout.write(kwargs["data"])
        sys.stdout.flush()


def load_file(path):
    """Load a file's contents, returning empty string on failure."""
    try:
        with open(path, 'r') as f:
            return f.read()
    except Exception:
        return ""


def find_skill_file(skills_dir):
    """Find the first JSON skill file in a skills directory."""
    try:
        for f in os.listdir(skills_dir):
            if f.endswith('-skill.json') or f.endswith('_skill.json'):
                return os.path.join(skills_dir, f)
        # Fallback: any .json file
        for f in os.listdir(skills_dir):
            if f.endswith('.json'):
                return os.path.join(skills_dir, f)
    except Exception:
        pass
    return None


def build_update_system_prompt(current_skill, current_system_prompt, current_task_prompt, analysis_report):
    """Build the system prompt for the update mode."""
    return f"""You are an agent optimizer. You have a prompt analysis report from a real agent execution
that identifies performance issues and recommends specific improvements. Your job is to apply
those recommendations to the agent's skill JSON, system prompt, and task prompt.

## Current Skill JSON
```json
{current_skill}
```

## Current System Prompt
```markdown
{current_system_prompt}
```

## Current Task Prompt
```markdown
{current_task_prompt}
```

## Prompt Analysis Report
{analysis_report}

## Your Task

Apply the HIGH PRIORITY and MEDIUM PRIORITY recommendations from the analysis report to produce
updated versions of all three files. Focus on:

1. Screenshot batching — add strict rules about when to screenshot and when NOT to
2. Coordinate planning — add specific coordinate reference systems if recommended
3. Tool state management — add checklists for verifying tool state before drawing
4. Error recovery — add cause/prevention/recovery patterns for identified failure modes
5. Task prompt phasing — restructure into explicit phases with "NO screenshots between steps"

Preserve everything that's working well. Only change what the analysis identifies as problematic.
Bump the version number in the skill JSON.

## Output Format

You MUST respond with EXACTLY three fenced blocks, in this order, with no other text outside them:

1. The updated skill JSON (fenced with ```json)
2. The updated system prompt markdown (fenced with ```system_prompt)
3. The updated task prompt markdown (fenced with ```task_prompt)

Do not include any explanation, commentary, or text outside these three blocks.
"""


def run_update(args):
    """Analyze the latest run and update an agent's skill and prompts."""
    import subprocess

    agent_target = args.update
    root_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '../..')

    # Resolve agent directory
    if not os.path.isabs(agent_target):
        agent_target = os.path.join(root_dir, agent_target)

    if not os.path.isdir(agent_target):
        print(f"  ✗ Agent directory not found: {agent_target}")
        return 1

    # Step 1: Run analysis (unless a report is explicitly provided)
    if args.analysis:
        analysis_path = args.analysis
        if not os.path.isabs(analysis_path):
            analysis_path = os.path.join(root_dir, analysis_path)
        print(f"  Using existing analysis: {analysis_path}\n")
    else:
        print("  Step 1: Analyzing latest run...\n")
        analyze_script = os.path.join(root_dir, "scripts/analyze_prompts.py")
        result = subprocess.run(
            [sys.executable, analyze_script, "--agent", agent_target],
            cwd=root_dir
        )
        if result.returncode != 0:
            print("\n  ✗ Analysis failed. Make sure the agent has been run at least once.")
            return 1

        # Find the report that was just generated
        reports_dir = os.path.join(root_dir, "reports")
        try:
            reports = sorted([
                f for f in os.listdir(reports_dir)
                if f.startswith('prompt_analysis_') and f.endswith('.md')
            ], reverse=True)
            if reports:
                analysis_path = os.path.join(reports_dir, reports[0])
            else:
                print("  ✗ Analysis ran but no report was generated.")
                return 1
        except Exception:
            print("  ✗ Could not read reports directory.")
            return 1

    print(f"\n  Step 2: Updating agent from analysis...\n")
    print(f"  Agent: {agent_target}")
    print(f"  Analysis: {analysis_path}")
    print(f"  Model: {args.model_id}")
    print("  " + "─" * 40 + "\n")

    # Load current files
    skill_path = find_skill_file(os.path.join(agent_target, "skills"))
    current_skill = load_file(skill_path) if skill_path else ""
    current_system_prompt = load_file(os.path.join(agent_target, "prompts/system_prompt.md"))
    current_task_prompt = load_file(os.path.join(agent_target, "prompts/task_prompt.md"))
    analysis_report = load_file(analysis_path)

    if not current_skill:
        print(f"  ⚠ No skill file found in {agent_target}/skills/")
    if not current_system_prompt:
        print(f"  ✗ No system prompt found")
        return 1
    if not analysis_report:
        print(f"  ✗ Could not load analysis report: {analysis_path}")
        return 1

    # Build prompts and run
    system_prompt = build_update_system_prompt(
        current_skill, current_system_prompt, current_task_prompt, analysis_report
    )

    model = BedrockModel(model_id=args.model_id, region_name=args.region)

    def signal_handler(sig, frame):
        print("\n\n  ⚠️  Interrupted")
        sys.exit(1)
    signal.signal(signal.SIGINT, signal_handler)

    # Progress indicator — print a dot for every ~100 tokens instead of full output
    token_count = [0]
    def progress_handler(**kwargs):
        if "data" in kwargs:
            token_count[0] += 1
            if token_count[0] % 100 == 0:
                sys.stdout.write(".")
                sys.stdout.flush()

    print("  Applying analysis recommendations... (this may take a minute)\n  ", end="")

    try:
        agent = Agent(
            model=model,
            system_prompt=system_prompt,
            callback_handler=progress_handler,
        )
        result = agent("Apply the analysis recommendations and generate the updated files now.")
        output_text = str(result)
    except KeyboardInterrupt:
        print("\n\n  ⚠️  Interrupted")
        return 1
    except Exception as e:
        print(f"\n  ✗ Error: {e}")
        return 1

    # Parse and write updated files
    print("\n\n  " + "─" * 40)
    print("  Writing updated files...\n")

    blocks = parse_output(output_text)
    if not blocks:
        print("  ✗ Could not parse output. Raw output saved to /tmp/agent_update_output.txt")
        with open('/tmp/agent_update_output.txt', 'w') as f:
            f.write(output_text)
        return 1

    if 'skill' in blocks and skill_path:
        try:
            parsed = json.loads(blocks['skill'])
            with open(skill_path, 'w') as f:
                json.dump(parsed, f, indent=2)
            print(f"  ✓ Updated skill: {skill_path}")
        except json.JSONDecodeError:
            with open(skill_path, 'w') as f:
                f.write(blocks['skill'])
            print(f"  ⚠ Skill written (invalid JSON — needs manual fix): {skill_path}")

    if 'system_prompt' in blocks:
        sp_path = os.path.join(agent_target, "prompts/system_prompt.md")
        with open(sp_path, 'w') as f:
            f.write(blocks['system_prompt'])
        print(f"  ✓ Updated system prompt: {sp_path}")

    if 'task_prompt' in blocks:
        tp_path = os.path.join(agent_target, "prompts/task_prompt.md")
        with open(tp_path, 'w') as f:
            f.write(blocks['task_prompt'])
        print(f"  ✓ Updated task prompt: {tp_path}")

    print(f"\n  " + "─" * 40)
    print(f"  Done! Agent updated at: {agent_target}")
    print()
    return 0


def main():
    parser = argparse.ArgumentParser(description='Workspace Agent Creator')
    parser.add_argument('--model-id', default='us.anthropic.claude-sonnet-4-6',
                       help='Bedrock model ID (default: us.anthropic.claude-sonnet-4-6)')
    parser.add_argument('--no-prompt', action='store_true',
                       help='Skip interview and read from stdin as JSON')
    parser.add_argument('--update', metavar='AGENT_DIR',
                       help='Update an existing agent based on a prompt analysis report (e.g., agents/paint_demo)')
    parser.add_argument('--analysis', metavar='REPORT_PATH',
                       help='Path to prompt analysis report (default: most recent in reports/)')
    parser.add_argument('--region', default=os.environ.get('AWS_DEFAULT_REGION', os.environ.get('AWS_REGION', 'us-west-2')),
                       help='AWS region for Bedrock (default: us-west-2)')
    args = parser.parse_args()

    # Route to update mode
    if args.update:
        return run_update(args)

    agent_dir = os.path.dirname(os.path.abspath(__file__))
    root_dir = os.path.join(agent_dir, '../..')

    os.system('clear 2>/dev/null || cls 2>/dev/null || true')

    # Step 1: Interview
    if args.no_prompt:
        answers = json.load(sys.stdin)
    else:
        answers = interview()

    app_name = answers.get('app_name', 'Unknown')
    print(f"  Generating skill and agent for: {app_name}")
    print(f"  Model: {args.model_id}")
    print("  " + "─" * 40 + "\n")

    # Step 2: Build prompts
    system_prompt = build_system_prompt()
    task_prompt = build_task_prompt(answers)

    # Step 3: Run the agent
    model = BedrockModel(
        model_id=args.model_id,
        region_name=args.region
    )

    # Handle Ctrl-C
    def signal_handler(sig, frame):
        print("\n\n  ⚠️  Interrupted")
        sys.exit(1)
    signal.signal(signal.SIGINT, signal_handler)

    print("  Generating... (this may take a minute)\n")

    try:
        agent = Agent(
            model=model,
            system_prompt=system_prompt,
            callback_handler=streaming_handler,
        )

        result = agent(task_prompt)
        output_text = str(result)

    except KeyboardInterrupt:
        print("\n\n  ⚠️  Interrupted")
        return 1
    except Exception as e:
        print(f"\n  ✗ Error: {e}")
        return 1

    # Step 4: Parse and scaffold
    print("\n  " + "─" * 40)
    print("  Scaffolding agent directory...\n")

    blocks = parse_output(output_text)
    if not blocks:
        print("  ✗ Could not parse agent output. Raw output saved to /tmp/skill_creator_output.txt")
        with open('/tmp/skill_creator_output.txt', 'w') as f:
            f.write(output_text)
        return 1

    agent_path = scaffold_agent(app_name, blocks, root_dir)

    print(f"\n  " + "─" * 40)
    print(f"  Done! Agent created at: {agent_path}")
    print(f"\n  To run it:")
    print(f"  python3 {os.path.relpath(os.path.join(agent_path, 'agent.py'), root_dir)} --streaming-url \"<URL>\"")
    print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
