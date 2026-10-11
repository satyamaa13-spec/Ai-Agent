#!/usr/bin/env python3
"""CLI: python run.py "<natural-language task>" [--auto-approve] [--no-flaky] [--model ...]"""
import argparse
import time
from pathlib import Path
from urllib.parse import urlparse

from sandbox.server import start_server
from worker.agent import Agent
from worker.browser import Browser
from worker.io import CLIIO
from worker.llm import AnthropicLLM, GeminiLLM
from worker.tools import build_tools

ROOT = Path(__file__).parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("task")
    ap.add_argument("--auto-approve", action="store_true", help="auto-approve gated actions (demo only)")
    ap.add_argument("--no-flaky", action="store_true", help="disable the ERP's timeout-after-commit fault")
    ap.add_argument("--provider", choices=["anthropic", "gemini"], default="anthropic")
    ap.add_argument("--model")
    ap.add_argument("--max-steps", type=int, default=40)
    ap.add_argument("--max-llm-calls", type=int, help="hard cap on model requests (stops the run when reached)")
    ap.add_argument("--no-llm-verifier", action="store_true", help="skip the extra verifier request (saves 1 call)")
    args = ap.parse_args()

    srv, world, base = start_server(flaky=not args.no_flaky)
    context = (f"Vendor portal (invoices, login required): {base}/portal/invoices\n"
               f"Internal ERP (bills): {base}/erp/bills\n"
               "Workspace files (credentials, inbox notes) via list_dir/read_file.")
    memory, io = {}, CLIIO(args.auto_approve)
    browser = Browser({urlparse(base).netloc})
    tools = build_tools(browser, ROOT / "sandbox" / "workspace", memory, io)
    run_dir = ROOT / "runs" / time.strftime("%Y%m%d-%H%M%S")
    result = Agent((GeminiLLM if args.provider == "gemini" else AnthropicLLM)(args.model), tools, memory, io, context, args.max_steps, run_dir,
                   max_llm_calls=args.max_llm_calls, llm_verifier=not args.no_llm_verifier).run(args.task)

    print(f"\n{'=' * 60}\nSTATUS: {result.status}  (verified: {result.verified}, steps: {result.steps}, model calls: {result.llm_calls})\n{result.summary}")
    print("Evidence:\n" + "\n".join(f"  - {e}" for e in result.evidence))
    print(f"\nGround truth in ERP database (not visible to the agent): {world.bills}\nReport: {run_dir}/report.md")


if __name__ == "__main__":
    main()
