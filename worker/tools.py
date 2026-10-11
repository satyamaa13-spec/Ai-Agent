"""Tool registry. Every tool returns a ToolResult; the agent never needs to know what a tool does internally."""
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Tuple

from .browser import Browser, ToolError


@dataclass
class ToolResult:
    text: str
    ok: bool = True      # False => the attempt failed (bad HTTP status, etc.)
    wrote: bool = False  # a state-changing request was SENT (even if the response was an error)
    read: bool = False   # a read-only look at external state happened


@dataclass
class Tool:
    name: str
    description: str
    schema: dict
    fn: Callable[[dict], ToolResult]
    risk: Optional[Callable[[dict], Optional[Tuple[str, str]]]] = None  # -> (reason, signature) if approval needed

    def spec(self):
        return {"name": self.name, "description": self.description, "input_schema": self.schema}


def _obj(props, required):
    return {"type": "object", "properties": props, "required": required}


def build_tools(browser: Browser, workspace: Path, memory: dict, io, approval_limit: float = 2500.0):
    root = workspace.resolve()

    def safe(p):
        full = (root / p).resolve()
        if root != full and root not in full.parents:
            raise ToolError("Path escapes the workspace.")
        return full

    def page_result(snap, write=False):
        return ToolResult(snap.render(), ok=snap.status < 400, wrote=write, read=not write)

    def open_page(a):
        return page_result(browser.open(a["url"]))

    def click_link(a):
        return page_result(browser.click(int(a["index"])))

    def submit_form(a):
        idx = int(a["form_index"])
        method = browser.snap.forms[idx]["method"] if browser.snap and 0 <= idx < len(browser.snap.forms) else None
        return page_result(browser.submit(idx, a.get("values") or {}), write=(method == "post"))

    def submit_risk(a):
        """Generic, task-agnostic approval rules for state-changing requests."""
        try:
            form = browser.snap.forms[int(a["form_index"])]
        except Exception:
            return None
        if form["method"] != "post":
            return None
        vals = {f["name"]: f["value"] for f in form["fields"]}
        vals.update({k: str(v) for k, v in (a.get("values") or {}).items()})
        for k, v in vals.items():
            if re.search(r"amount|total|price", k, re.I):
                try:
                    n = float(re.sub(r"[^\d.]", "", v))
                except ValueError:
                    continue
                if n > approval_limit:
                    return f"Submit {form['action']} with {k}={v} (above the {approval_limit:,.0f} auto-approval limit)", f"{form['action']}|{n}"
        if re.search(r"delete|remove|pay|transfer|cancel", form["action"], re.I):
            return f"Irreversible-looking action: POST {form['action']}", f"{form['action']}|{sorted(vals.items())}"
        return None

    def list_dir(a):
        d = safe(a.get("path", "."))
        if not d.is_dir():
            raise ToolError("Not a directory.")
        return ToolResult("\n".join(sorted(str(p.relative_to(root)) + ("/" if p.is_dir() else "") for p in d.rglob("*"))) or "(empty)", read=True)

    def read_file(a):
        f = safe(a["path"])
        if not f.is_file():
            raise ToolError("No such file.")
        return ToolResult(f.read_text(errors="replace")[:6000], read=True)

    def remember(a):
        memory[a["key"]] = a["value"]
        return ToolResult(f"Saved note '{a['key']}'.")

    def ask_user(a):
        return ToolResult(f"User answered: {io.ask(a['question'])}")

    S = lambda d: {"type": "string", "description": d}
    tools = [
        Tool("open_page", "Open a URL in the browser. Returns page text, numbered links and numbered forms.",
             _obj({"url": S("Absolute or relative URL")}, ["url"]), open_page),
        Tool("click_link", "Follow a numbered link from the current page.",
             _obj({"index": {"type": "integer"}}, ["index"]), click_link),
        Tool("submit_form", "Submit a numbered form on the current page. 'values' overrides field values by field name; "
             "unspecified fields keep their defaults. POST submissions change state and may require user approval.",
             _obj({"form_index": {"type": "integer"}, "values": {"type": "object", "additionalProperties": {"type": "string"}}},
                  ["form_index"]), submit_form, risk=submit_risk),
        Tool("list_dir", "List files in the workspace (default: all).", _obj({"path": S("Relative path")}, []), list_dir),
        Tool("read_file", "Read a text file from the workspace.", _obj({"path": S("Relative path")}, ["path"]), read_file),
        Tool("remember", "Pin a fact in working memory. Memory is re-shown every step even after old observations are trimmed.",
             _obj({"key": S("Short name"), "value": S("The fact")}, ["key", "value"]), remember),
        Tool("ask_user", "Ask the user a question when you cannot safely proceed (ambiguity, missing info/access).",
             _obj({"question": S("A specific question")}, ["question"]), ask_user),
    ]
    return {t.name: t for t in tools}
