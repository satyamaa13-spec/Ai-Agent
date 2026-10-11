"""The agent loop: observe -> decide -> act, wrapped in a harness that enforces reliability rules
the model cannot be trusted to enforce on itself (approval gates, verification gate, loop guards)."""
import copy
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .browser import ToolError

SYSTEM_PROMPT = """You are an autonomous task worker operating a computer on behalf of a user. You complete the user's end goal by calling tools, observing the results, and deciding each next step yourself.

ENVIRONMENT
{context}

HOW TO WORK
1. Start with a 2-4 line plan, then act. Revise the plan when observations contradict it.
2. Discover information with tools before asking the user. Never invent values (amounts, dates, IDs): copy them from what you observed.
3. Use `remember` to pin facts you'll need later (source values, which step you're on). Old observations get truncated; your notes do not.
4. Errors are information. Read the message, fix the cause (e.g. a format rule), then retry with a change. Never repeat an identical failing call.
5. A write that returned an error may still have taken effect. Before retrying any write, re-read the target system to check its current state, to avoid duplicates.
6. Convert data into the format the target system asks for without changing its meaning. Read form field labels and hints for format rules BEFORE the first submit, so you don't waste a failed attempt.
7. Use `ask_user` when the request is ambiguous in a way that changes the outcome, or access/information is missing and cannot be found. Do not ask about things you can look up.
8. Some writes require user approval; this is enforced for you. If approval is denied, do not work around it: finish with status "blocked".
9. Before finishing, verify: re-read the final state of the target system and compare it with the source. Then call `finish` with evidence = concrete observed facts (record IDs, values exactly as the system shows them), not statements of effort. Use "done" only when verified, "blocked" if you need the user, "failed" if it cannot be done.
10. Every model call is metered. When several tool calls are independent (e.g. read the credentials file AND open the portal), make them together in ONE turn. Never batch calls that depend on each other's results. Don't re-open pages you already have.
Keep prose minimal; let tool calls do the work."""

FINISH_TOOL = {
    "name": "finish",
    "description": "End the task. Call only after verifying the outcome (or when blocked/failed).",
    "input_schema": {"type": "object", "required": ["status", "summary", "evidence"], "properties": {
        "status": {"type": "string", "enum": ["done", "blocked", "failed"]},
        "summary": {"type": "string", "description": "2-4 sentence plain-language summary for the user"},
        "evidence": {"type": "array", "items": {"type": "string"}, "description": "Observed facts proving the outcome"}}},
}

VERIFIER_PROMPT = """You are an independent QA verifier for an autonomous agent. You did not do the work.
TASK: {task}
AGENT'S CLAIM: {summary}
AGENT'S EVIDENCE: {evidence}

RAW OBSERVATIONS FROM THE SYSTEMS (most recent last):
{obs}

Decide whether the raw observations PROVE the task outcome was achieved: the right record exists in the target system, its values match the source data (allowing format conversion), and there are no duplicates. Attempted actions or error-free-looking responses are not proof. Reply with only JSON: {{"achieved": true|false, "reason": "<one sentence>"}}"""


@dataclass
class Result:
    status: str
    summary: str
    evidence: list = field(default_factory=list)
    verified: bool = False
    steps: int = 0
    memory: dict = field(default_factory=dict)
    llm_calls: int = 0


class Agent:
    def __init__(self, llm, tools, memory, io, context="", max_steps=40, run_dir=None, keep_full=6,
                 max_llm_calls=None, llm_verifier=True):
        self.llm, self.tools, self.memory, self.io = llm, tools, memory, io
        self.context, self.max_steps, self.keep_full = context, max_steps, keep_full
        self.run_dir = Path(run_dir) if run_dir else None
        if self.run_dir:
            self.run_dir.mkdir(parents=True, exist_ok=True)
        self.unverified_write = False
        self.verify_rounds = 0
        self.observations = []   # (tool, args, text) raw, never truncated: evidence for the verifier
        self.fail_counts, self.consec_fail = {}, 0
        self.approved = set()
        self.max_llm_calls, self.llm_verifier, self.llm_calls = max_llm_calls, llm_verifier, 0
        if hasattr(llm, "notify"):
            llm.notify = lambda m: self._emit("note", text=m)

    # ------------------------------------------------------------ utilities
    def _emit(self, kind, **data):
        if self.run_dir:
            with open(self.run_dir / "trace.jsonl", "a") as f:
                f.write(json.dumps({"t": time.time(), "kind": kind, **data}, default=str) + "\n")
        self.io.log(kind, data)

    def _system(self):
        mem = "\n".join(f"- {k}: {v}" for k, v in self.memory.items()) or "(empty)"
        return SYSTEM_PROMPT.format(context=self.context) + f"\n\nWORKING MEMORY (your pinned notes)\n{mem}"

    def _compact(self, messages):
        """Trim OLD tool observations so context stays bounded; recent ones stay intact."""
        msgs = copy.deepcopy(messages)
        idx = [i for i, m in enumerate(msgs) if m["role"] == "user" and isinstance(m["content"], list)]
        for i in idx[:-self.keep_full]:
            for b in msgs[i]["content"]:
                if b.get("type") == "tool_result" and len(b["content"]) > 300:
                    b["content"] = b["content"][:300] + " ...[older observation trimmed; rely on working memory]"
        return msgs

    # ------------------------------------------------------------ execution
    def _execute(self, call):
        name, args = call["name"], call["input"]
        tool = self.tools.get(name)
        if not tool:
            return f"ERROR: unknown tool {name!r}. Available: {sorted(self.tools)}", False
        if tool.risk:
            risk = tool.risk(args)
            if risk and risk[1] not in self.approved:
                reason, sig = risk
                if not self.io.approve(reason):
                    self._emit("note", text=f"approval denied: {reason}")
                    return f"DENIED: the user did not approve this action ({reason}). Do not retry or work around it; finish with status 'blocked'.", False
                self.approved.add(sig)
        try:
            res = tool.fn(args)
        except ToolError as e:
            return f"ERROR: {e}", False
        except Exception as e:  # tool bugs must never crash the loop
            return f"ERROR: tool crashed: {type(e).__name__}: {e}", False
        if res.wrote:
            self.unverified_write = True      # an error response does NOT mean nothing was written
        elif res.read and res.ok:
            self.unverified_write = False
        return res.text, res.ok

    def _budget_left(self):
        return not self.max_llm_calls or self.llm_calls < self.max_llm_calls

    def _verify(self, task, inp):
        if not self.llm_verifier or not self._budget_left():
            return {"achieved": None, "reason": "LLM verifier skipped (disabled or model-call budget used up)"}
        self.llm_calls += 1
        obs = "\n\n".join(f"[{t}({json.dumps(a)[:150]})]\n{x[:1500]}" for t, a, x in self.observations[-10:])
        prompt = VERIFIER_PROMPT.format(task=task, summary=inp.get("summary"), evidence=inp.get("evidence"), obs=obs)
        raw = self.llm.judge(prompt)
        m = re.search(r"\{.*\}", raw, re.S)
        try:
            return json.loads(m.group(0))
        except Exception:
            return {"achieved": None, "reason": "verifier output unparseable"}

    def _handle_finish(self, task, inp):
        """Returns (accepted: bool, message_for_agent_if_rejected, verified: bool)."""
        if inp.get("status") != "done":
            return True, "", False
        if self.unverified_write:
            return False, ("REJECTED: you sent a state-changing request and have not re-read the system since. "
                           "Re-open the relevant page to confirm the real state, then finish."), False
        if self.verify_rounds >= 2:
            return True, "", False
        self.verify_rounds += 1
        v = self._verify(task, inp)
        self._emit("note", text=f"independent verifier: {v}")
        if v.get("achieved") is False:
            return False, f"REJECTED by independent verifier: {v.get('reason')}. Fix the problem or finish as failed/blocked.", False
        return True, "", v.get("achieved") is True

    # ----------------------------------------------------------------- loop
    def run(self, task):
        schemas = [t.spec() for t in self.tools.values()] + [FINISH_TOOL]
        messages = [{"role": "user", "content": f"Task: {task}"}]
        for step in range(1, self.max_steps + 1):
            if not self._budget_left():
                r = Result("failed", f"Model-call budget ({self.max_llm_calls}) reached before the task finished.", [], False, step - 1, dict(self.memory), self.llm_calls)
                self._save(task, r)
                return r
            self.llm_calls += 1
            blocks = self.llm.step(self._system(), self._compact(messages), schemas)
            messages.append({"role": "assistant", "content": blocks})
            for b in blocks:
                if b["type"] == "text" and b["text"].strip():
                    self._emit("thought", text=b["text"].strip())
            calls = [b for b in blocks if b["type"] == "tool_use"]
            if not calls:
                messages.append({"role": "user", "content": "No tool was called. Continue the task, or call `finish`."})
                continue
            results = []
            for c in calls:
                self._emit("call", name=c["name"], input=c["input"])
                if c["name"] == "finish":
                    ok, msg, verified = self._handle_finish(task, c["input"])
                    if ok:
                        r = Result(c["input"]["status"], c["input"]["summary"], c["input"].get("evidence", []), verified, step, dict(self.memory), self.llm_calls)
                        self._save(task, r)
                        return r
                    self._emit("note", text=msg)
                    results.append({"type": "tool_result", "tool_use_id": c["id"], "content": msg, "is_error": True})
                    continue
                text, ok = self._execute(c)
                self.observations.append((c["name"], c["input"], text))
                self._emit("observation", text=text)
                if c["name"] == "remember":
                    self._emit("memory", key=c["input"]["key"], value=c["input"]["value"])
                key = c["name"] + json.dumps(c["input"], sort_keys=True)
                self.consec_fail = 0 if ok else self.consec_fail + 1
                self.fail_counts[key] = 0 if ok else self.fail_counts.get(key, 0) + 1
                if self.fail_counts[key] >= 3:
                    text += "\n[HARNESS] This exact call has failed 3 times. Change approach, ask the user, or finish as blocked/failed."
                elif self.consec_fail >= 5:
                    text += "\n[HARNESS] 5 consecutive failures. Reconsider your approach or use ask_user."
                results.append({"type": "tool_result", "tool_use_id": c["id"], "content": text, "is_error": not ok})
            messages.append({"role": "user", "content": results})
        r = Result("failed", f"Step budget ({self.max_steps}) exhausted before the task finished.", [], False, self.max_steps, dict(self.memory), self.llm_calls)
        self._save(task, r)
        return r

    def _save(self, task, r):
        if not self.run_dir:
            return
        (self.run_dir / "result.json").write_text(json.dumps({"task": task, **r.__dict__}, indent=2))
        ev = "\n".join(f"- {e}" for e in r.evidence) or "- (none)"
        (self.run_dir / "report.md").write_text(
            f"# Task report\n\n**Task:** {task}\n\n**Status:** {r.status} (independently verified: {r.verified}, steps: {r.steps}, model calls: {r.llm_calls})\n\n"
            f"## Summary\n{r.summary}\n\n## Evidence\n{ev}\n\nFull action trace: `trace.jsonl`\n")
