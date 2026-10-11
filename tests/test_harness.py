"""Harness tests. The LLM is scripted (no reasoning) so these verify the *machinery*: tools, sandbox faults,
approval gate, verification gate, and loop behaviour. Ground truth is read from the sandbox DB."""
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlparse

from sandbox.server import start_server
from worker.agent import Agent
from worker.browser import Browser
from worker.llm import ScriptedLLM
from worker.tools import build_tools

WS = Path(__file__).parent.parent / "sandbox" / "workspace"


class IO:
    def __init__(self, approve=True):
        self.approve_answer, self.approvals, self.logs = approve, [], []

    def log(self, kind, data): self.logs.append((kind, data))
    def ask(self, q): return "n/a"
    def approve(self, d): self.approvals.append(d); return self.approve_answer


def build(script, flaky=True, approve=True, verdict='{"achieved": true, "reason": "ok"}'):
    srv, world, base = start_server(flaky=flaky)
    mem, io = {}, IO(approve)
    tools = build_tools(Browser({urlparse(base).netloc}), WS, mem, io)
    agent = Agent(ScriptedLLM(script(base), verdict), tools, mem, io, run_dir=tempfile.mkdtemp(), max_steps=20)
    return srv, world, io, agent


FIXED = {"vendor": "Globex Corp", "invoice_number": "INV-1042", "amount": "3980.00", "currency": "USD", "due_date": "2026-06-04"}


def happy(base):
    return [
        ("read creds", [("read_file", {"path": "notes/credentials.txt"})]),
        ("open portal", [("open_page", {"url": f"{base}/portal/invoices"})]),                    # redirected to login
        ("log in", [("submit_form", {"form_index": 0, "values": {"username": "ap_clerk", "password": "sandbox-pass-123"}})]),
        ("open newest", [("open_page", {"url": f"{base}/portal/invoice/INV-1042"})]),
        ("pin", [("remember", {"key": "invoice", "value": "INV-1042 3,980.00 due June 4, 2026"})]),
        ("erp form", [("open_page", {"url": f"{base}/erp/bills/new"})]),
        ("first try, portal formatting", [("submit_form", {"form_index": 0, "values": {**FIXED, "amount": "3,980.00", "due_date": "June 4, 2026"}})]),  # 422
        ("fix formats", [("submit_form", {"form_index": 0, "values": FIXED})]),                   # saved, but HTTP 500
        ("finish too early", [("finish", {"status": "done", "summary": "saved", "evidence": ["x"]})]),  # must be rejected
        ("verify", [("open_page", {"url": f"{base}/erp/bills"})]),
        ("finish", [("finish", {"status": "done", "summary": "Bill INV-1042 entered.", "evidence": ["ERP list shows INV-1042 3980.00 USD due 2026-06-04"]})]),
    ]


class HarnessTests(unittest.TestCase):
    def test_recovers_from_validation_error_and_commit_then_500(self):
        srv, world, io, agent = build(happy)
        r = agent.run("enter newest Globex invoice")
        srv.shutdown(); srv.server_close()
        self.assertEqual(r.status, "done")
        self.assertTrue(r.verified)
        self.assertEqual(world.bills, [FIXED])                  # exactly one bill, correct values (no duplicate)
        self.assertEqual(len(io.approvals), 1)                  # 3,980 > limit -> asked once, cached for the retry
        self.assertTrue(any("REJECTED: you sent" in d["text"] for k, d in io.logs if k == "note"))  # premature finish blocked
        self.assertIn("invoice", r.memory)

    def test_denied_approval_blocks_write(self):
        def script(base):
            return [("open", [("open_page", {"url": f"{base}/erp/bills/new"})]),
                    ("try", [("submit_form", {"form_index": 0, "values": FIXED})]),
                    ("stop", [("finish", {"status": "blocked", "summary": "Approval denied.", "evidence": []})])]
        srv, world, io, agent = build(script, approve=False)
        r = agent.run("enter bill")
        srv.shutdown(); srv.server_close()
        self.assertEqual(r.status, "blocked")
        self.assertEqual(world.bills, [])

    def test_verifier_can_reject_a_false_claim(self):
        def script(base):
            return [("claim", [("finish", {"status": "done", "summary": "done", "evidence": []})]),
                    ("give up", [("finish", {"status": "failed", "summary": "could not", "evidence": []})])]
        srv, world, io, agent = build(script, verdict='{"achieved": false, "reason": "no bill exists"}')
        r = agent.run("enter bill")
        srv.shutdown(); srv.server_close()
        self.assertEqual(r.status, "failed")
        self.assertTrue(any("independent verifier" in d["text"] for k, d in io.logs if k == "note"))

    def test_bad_inputs_are_observations_not_crashes_and_sandbox_is_enforced(self):
        def script(base):
            return [("bad host", [("open_page", {"url": "http://evil.example.com/"})]),
                    ("bad path", [("read_file", {"path": "../../etc/passwd"})]),
                    ("bad form", [("submit_form", {"form_index": 9})]),
                    ("stop", [("finish", {"status": "failed", "summary": "n/a", "evidence": []})])]
        srv, world, io, agent = build(script)
        agent.run("x")
        srv.shutdown(); srv.server_close()
        errs = [d["text"] for k, d in io.logs if k == "observation"]
        self.assertTrue(all(e.startswith("ERROR") for e in errs[:3]), errs)


class BudgetTests(unittest.TestCase):
    def test_hard_call_budget_stops_run_and_counts_calls(self):
        srv, world, io, agent = build(happy)
        agent.max_llm_calls = 4
        r = agent.run("enter newest Globex invoice")
        srv.shutdown(); srv.server_close()
        self.assertEqual(r.status, "failed")
        self.assertEqual(r.llm_calls, 4)
        self.assertIn("budget", r.summary)

    def test_skipping_llm_verifier_saves_a_call_but_keeps_read_after_write_gate(self):
        srv, world, io, agent = build(happy)
        agent.llm_verifier = False
        r = agent.run("enter newest Globex invoice")
        srv.shutdown(); srv.server_close()
        self.assertEqual(r.status, "done")
        self.assertFalse(r.verified)               # no independent LLM verdict
        self.assertEqual(r.llm_calls, 11)          # 11 scripted turns, 0 judge calls
        self.assertTrue(any("REJECTED: you sent" in d["text"] for k, d in io.logs if k == "note"))
        self.assertEqual(len(world.bills), 1)


class AdminTests(unittest.TestCase):
    def setUp(self):
        import sandbox.server as sv
        self.sv, self._file = sv, sv.CUSTOM_FILE
        sv.CUSTOM_FILE = Path(tempfile.mkdtemp()) / "custom.json"
        sv.INVOICES[:] = [i for i in sv.INVOICES if i["id"] in sv.BASE_IDS]

    def tearDown(self):
        self.sv.INVOICES[:] = [i for i in self.sv.INVOICES if i["id"] in self.sv.BASE_IDS]
        self.sv.CUSTOM_FILE = self._file

    def _post(self, base, path, data=None):
        from urllib.parse import urlencode
        from urllib.request import urlopen
        return urlopen(base + path, urlencode(data or {}).encode()).read().decode()

    def test_admin_adds_loads_pack_resets_and_agent_is_blocked(self):
        srv, world, base = start_server()
        self._post(base, "/admin/invoices", {"id": "INV-9001", "vendor": "Test Co", "amount": "1.00", "currency": "USD",
                                             "issued": "June 1, 2026", "due": "July 1, 2026", "status": "Open"})
        self.assertIn("INV-9001", self._post(base, "/admin/testpack"))
        self.assertTrue(any(i["id"] == "INV-4001" for i in self.sv.INVOICES))
        self.assertTrue(self.sv.CUSTOM_FILE.exists())
        b = Browser({urlparse(base).netloc})
        from worker.browser import ToolError
        with self.assertRaises(ToolError):
            b.open(base + "/admin")
        self._post(base, "/admin/reset")
        self.assertEqual({i["id"] for i in self.sv.INVOICES}, self.sv.BASE_IDS)
        srv.shutdown(); srv.server_close()


if __name__ == "__main__":
    unittest.main()
