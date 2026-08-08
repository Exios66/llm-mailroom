#!/usr/bin/env python3
"""
Phase 10: Agent-by-agent local model cutover utility.

Usage:
  python cutover.py --list                    # Show all agents and their current provider/model
  python cutover.py --agent sorter --provider ollama --model qwen3:7b
  python cutover.py --all --provider ollama --model qwen3:7b
  python cutover.py --agent sorter --provider openrouter --model openai/gpt-4o   # cut back
  python cutover.py --validate --agent sorter  # Run fixture tests for one agent against current config
"""

import argparse
import subprocess
import sys
from pathlib import Path
import yaml

CONFIG_PATH = Path(__file__).resolve().parent / "config" / "taxonomy.yaml"

LOCAL_MODEL_MAP = {
    "qwen": {
        "models": ["qwen3:7b", "qwen3:14b", "qwen2.5:14b", "qwen2.5:32b"],
        "description": "Qwen — strong structured output, good for legal text extraction",
    },
    "llama": {
        "models": ["llama3.1:8b", "llama3.1:70b", "llama3.2:3b"],
        "description": "Llama 3.1 — solid general-purpose, reliable structured output",
    },
    "mistral": {
        "models": ["mistral:7b", "mistral-nemo:12b", "mixtral:8x7b"],
        "description": "Mistral/Mixtral — fast, good instruction following",
    },
    "deepseek": {
        "models": ["deepseek-r1:8b", "deepseek-r1:14b"],
        "description": "DeepSeek-R1 — reasoning model, strong for complex legal analysis",
    },
    "phi": {
        "models": ["phi4:14b"],
        "description": "Phi-4 — strong document understanding, compact",
    },
    "gemma": {
        "models": ["gemma2:9b", "gemma2:27b"],
        "description": "Gemma 2 — Google's open models, good instruction following",
    },
    "command_r": {
        "models": ["command-r:35b", "command-r-plus:104b"],
        "description": "Command R — Cohere's models, strong at RAG and extraction",
    },
}


def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
    print(f"Config updated: {CONFIG_PATH}")


def list_agents(cfg):
    agents = cfg.get("agents", {})
    print(f"\n{'Agent':<35s} {'Provider':<15s} {'Model':<30s}")
    print("-" * 80)
    for name, agt in agents.items():
        print(f"{name:<35s} {agt.get('provider', '?'):<15s} {agt.get('model', '?'):<30s}")
    print()


def list_local_models():
    print("\nAvailable local models (Ollama):")
    for family, info in LOCAL_MODEL_MAP.items():
        print(f"  {family}:")
        for m in info["models"]:
            print(f"    - {m}")
        print(f"    ({info['description']})\n")


def cutover_agent(cfg, agent_name, provider, model):
    agents = cfg.get("agents", {})
    if agent_name not in agents:
        print(f"Agent '{agent_name}' not found. Available: {list(agents.keys())}")
        return
    old = agents[agent_name]
    agents[agent_name]["provider"] = provider
    agents[agent_name]["model"] = model
    cfg["agents"] = agents
    save_config(cfg)
    print(f"Cutover: {agent_name}: {old['provider']}/{old['model']} -> {provider}/{model}")


def cutover_all(cfg, provider, model):
    for name in cfg.get("agents", {}):
        old = cfg["agents"][name]
        cfg["agents"][name]["provider"] = provider
        cfg["agents"][name]["model"] = model
        print(f"  {name}: {old['provider']}/{old['model']} -> {provider}/{model}")
    save_config(cfg)


def validate_agent(agent_name):
    print(f"\nValidating {agent_name}...")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", f"tests/test_agents/", "-v", "-k", agent_name, "--no-header"],
        capture_output=True, text=True,
    )
    print(result.stdout)
    if result.returncode != 0:
        print(result.stderr)
    return result.returncode == 0


def recommend_cutover_order():
    print("""
Recommended agent-by-agent cutover order (least risky first):

  1. sorter                       (classification — low accuracy sensitivity)
  2. compliance_specialist        (structured forms, predictable format)
  3. correspondence_specialist    (narrative text, moderate complexity)
  4. corporate_records_specialist (hierarchical data, moderate complexity)
  5. contracts_specialist          (complex extraction, high accuracy needed)
  6. due_diligence_specialist     (nuance/risk detection, highest sensitivity)
  7. reporter                     (summarization)
  8. boss                         (adjudication/analysis)

For each agent:
  - Run: python cutover.py --agent <name> --provider ollama --model qwen3:7b
  - Validate: python cutover.py --validate --agent <name>
  - If validation passes, move to next agent.
  - If validation fails, revert: python cutover.py --agent <name> --provider openrouter --model openai/gpt-4o
""")


def main():
    parser = argparse.ArgumentParser(description="Mailroom local model cutover utility")
    parser.add_argument("--list", action="store_true", help="List all agents and their configs")
    parser.add_argument("--list-models", action="store_true", help="List available local models")
    parser.add_argument("--recommend", action="store_true", help="Show recommended cutover order")
    parser.add_argument("--agent", help="Agent name to cut over")
    parser.add_argument("--all", action="store_true", help="Cut over all agents at once")
    parser.add_argument("--provider", help="Target provider (ollama, openrouter, vllm, generic)")
    parser.add_argument("--model", help="Target model name")
    parser.add_argument("--validate", action="store_true", help="Run tests for the specified agent")
    args = parser.parse_args()

    if args.list:
        list_agents(load_config())
        return

    if args.list_models:
        list_local_models()
        return

    if args.recommend:
        recommend_cutover_order()
        return

    if args.validate:
        if not args.agent:
            print("Specify --agent to validate")
            return
        success = validate_agent(args.agent)
        sys.exit(0 if success else 1)

    if args.agent or args.all:
        if not args.provider or not args.model:
            print("Specify --provider and --model")
            return
        cfg = load_config()
        if args.all:
            cutover_all(cfg, args.provider, args.model)
        else:
            cutover_agent(cfg, args.agent, args.provider, args.model)
        return

    parser.print_help()


if __name__ == "__main__":
    main()
