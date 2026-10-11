#!/usr/bin/env python3
"""Local web UI:  python ui.py   ->  http://127.0.0.1:8765
Type a task, watch the agent work live, approve/deny gated actions, see the simulated ERP beside it."""
import argparse
import json
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from sandbox.server import start_server
from worker.agent import Agent
from worker.browser import Browser
from worker.llm import AnthropicLLM, GeminiLLM, ScriptedLLM
from worker.tools import build_tools

ROOT = Path(__file__).parent
WORKSPACE = ROOT / "sandbox" / "workspace"
LOCK = threading.Lock()


class Cancelled(Exception):
    pass


class WebIO:
    """Same interface as CLIIO, but questions/approvals are answered through the browser."""

    def __init__(self, auto_approve=False):
        self.events, self.pending, self.auto_approve, self.cancelled = [], None, auto_approve, False
        self._ev, self._answer = threading.Event(), None

    def log(self, kind, data):
        if self.cancelled:
            raise Cancelled()
        self.events.append({"kind": kind, **data})

    def _wait(self, pending):
        self._ev.clear()
        self._answer, self.pending = None, pending
        self._ev.wait()
        self.pending = None
        if self.cancelled:
            raise Cancelled()
        return self._answer

    def ask(self, question):
        self.events.append({"kind": "ask", "question": question})
        a = str(self._wait({"type": "ask", "text": question}))
        self.events.append({"kind": "answer", "text": a})
        return a

    def approve(self, description):
        if self.auto_approve:
            self.events.append({"kind": "note", "text": f"auto-approved: {description}"})
            return True
        self.events.append({"kind": "approval", "text": description})
        ok = bool(self._wait({"type": "approve", "text": description}))
        self.events.append({"kind": "note", "text": "approved by user" if ok else "DENIED by user"})
        return ok

    def answer(self, value):
        self._answer = value
        self._ev.set()

    def cancel(self):
        self.cancelled = True
        self._ev.set()


class Job:
    def __init__(self):
        self.id = 0
        self.status = "idle"
        self.io = WebIO()
        self.srv = self.world = self.base = self.run_dir = None

    def new_sandbox(self):
        if self.srv:
            self.srv.shutdown()
            self.srv.server_close()
        self.srv, self.world, self.base = start_server(flaky=True)


JOB = Job()
JOB.new_sandbox()

FIXED = {"vendor": "Globex Corp", "invoice_number": "INV-1042", "amount": "3980.00", "currency": "USD", "due_date": "2026-06-04"}


def replay_script(base):
    """NOT AI: a fixed script so the UI can be previewed without an API key."""
    return [
        ("[replay] reading credentials", [("read_file", {"path": "notes/credentials.txt"})]),
        ("[replay] opening portal", [("open_page", {"url": f"{base}/portal/invoices"})]),
        ("[replay] logging in", [("submit_form", {"form_index": 0, "values": {"username": "ap_clerk", "password": "sandbox-pass-123"}})]),
        ("[replay] opening newest invoice", [("open_page", {"url": f"{base}/portal/invoice/INV-1042"})]),
        ("[replay] pinning facts", [("remember", {"key": "invoice", "value": "INV-1042, 3,980.00 USD, due June 4, 2026"})]),
        ("[replay] opening ERP form", [("open_page", {"url": f"{base}/erp/bills/new"})]),
        ("[replay] first attempt with portal formatting", [("submit_form", {"form_index": 0, "values": {**FIXED, "amount": "3,980.00", "due_date": "June 4, 2026"}})]),
        ("[replay] fixing formats", [("submit_form", {"form_index": 0, "values": FIXED})]),
        ("[replay] verifying in ERP", [("open_page", {"url": f"{base}/erp/bills"})]),
        ("[replay] done", [("finish", {"status": "done", "summary": "Bill INV-1042 entered (scripted replay, not AI).",
                                       "evidence": ["ERP list shows INV-1042 3980.00 USD due 2026-06-04"]})]),
    ]


def start_job(task, provider, model, auto_approve, max_calls=None, verifier=True):
    with LOCK:
        if JOB.status == "running":
            return False, "A run is already in progress."
        JOB.new_sandbox()
        JOB.id += 1
        JOB.io = io = WebIO(auto_approve)
        JOB.status = "running"
        JOB.run_dir = ROOT / "runs" / time.strftime("%Y%m%d-%H%M%S")
        base, run_dir = JOB.base, JOB.run_dir
    context = (f"Vendor portal (invoices, login required): {base}/portal/invoices\n"
               f"Internal ERP (bills): {base}/erp/bills\nWorkspace files (credentials, inbox notes) via list_dir/read_file.")

    def work():
        try:
            memory = {}
            tools = build_tools(Browser({urlparse(base).netloc}), WORKSPACE, memory, io)
            llm = ScriptedLLM(replay_script(base)) if provider == "replay" else (GeminiLLM if provider == "gemini" else AnthropicLLM)(model or None)
            res = Agent(llm, tools, memory, io, context, 40, run_dir, max_llm_calls=max_calls, llm_verifier=verifier).run(task)
            io.events.append({"kind": "result", **res.__dict__})
            JOB.status = "finished"
        except Cancelled:
            io.events.append({"kind": "error", "text": "Stopped by user."})
            JOB.status = "stopped"
        except Exception as e:
            io.events.append({"kind": "error", "text": f"{type(e).__name__}: {e}"})
            JOB.status = "failed"

    threading.Thread(target=work, daemon=True).start()
    return True, ""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        data = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            data = (ROOT / "ui" / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif u.path == "/api/state":
            since = int(parse_qs(u.query).get("since", ["0"])[0])
            evs = JOB.io.events
            self._json({"run_id": JOB.id, "status": JOB.status, "events": evs[since:], "next": len(evs),
                        "pending": JOB.io.pending, "sandbox_url": JOB.base, "bills": list(JOB.world.bills),
                        "run_dir": str(JOB.run_dir) if JOB.run_dir else None})
        else:
            self.send_error(404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if self.path == "/api/run":
            ok, err = start_job(body.get("task", "").strip(), body.get("provider", "gemini"), body.get("model", ""), bool(body.get("auto_approve")),
                                int(body["max_calls"]) if body.get("max_calls") else None, bool(body.get("verifier", True)))
            self._json({"ok": ok, "error": err}, 200 if ok else 409)
        elif self.path == "/api/answer":
            JOB.io.answer(body.get("value"))
            self._json({"ok": True})
        elif self.path == "/api/stop":
            JOB.io.cancel()
            self._json({"ok": True})
        else:
            self.send_error(404)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    url = f"http://127.0.0.1:{a.port}"
    print(f"Worker UI running at {url}  (Ctrl+C to stop)")
    if not a.no_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
