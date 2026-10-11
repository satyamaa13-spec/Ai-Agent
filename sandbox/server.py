"""Simulated company environment: a vendor portal and an internal ERP, with realistic friction.

Friction deliberately built in (so the worker's reliability is actually exercised):
  * portal requires login (unauthenticated requests redirect to the login page)
  * invoice list is NOT sorted by date; invoice numbers do not follow date order
  * portal shows "June 4, 2026" / "3,980.00"; ERP only accepts 2026-06-04 / 3980.00
  * ERP's first valid save is persisted but answers HTTP 500 (a timeout-after-commit)
  * ERP rejects duplicate invoice numbers with HTTP 409
"""
import html
import json
import re
import secrets
import threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

INVOICES = [
    {"id": "INV-1107", "vendor": "Globex Corp", "amount": "1,240.50", "currency": "USD",
     "issued": "April 2, 2026", "due": "May 2, 2026", "status": "Open"},
    {"id": "INV-0990", "vendor": "Globex Corp", "amount": "2,115.00", "currency": "USD",
     "issued": "January 15, 2026", "due": "February 14, 2026", "status": "Paid"},
    {"id": "INV-2001", "vendor": "Initech LLC", "amount": "560.00", "currency": "USD",
     "issued": "May 20, 2026", "due": "June 19, 2026", "status": "Open"},
    {"id": "INV-1042", "vendor": "Globex Corp", "amount": "3,980.00", "currency": "USD",
     "issued": "May 5, 2026", "due": "June 4, 2026", "status": "Open"},  # newest Globex invoice
]
USERS = {"ap_clerk": "sandbox-pass-123"}

# One-click test pack (admin page). Each entry exercises a different behaviour of the worker.
TESTPACK = [
    {"id": "INV-3001", "vendor": "Initech LLC", "amount": "800.00", "currency": "USD", "issued": "June 1, 2026", "due": "July 1, 2026", "status": "Open"},
    {"id": "INV-3002", "vendor": "Initech LLC", "amount": "950.00", "currency": "USD", "issued": "June 1, 2026", "due": "July 1, 2026", "status": "Open"},   # same date: ambiguous "latest"
    {"id": "INV-4001", "vendor": "Umbrella Co", "amount": "12,500.00", "currency": "USD", "issued": "June 3, 2026", "due": "July 3, 2026", "status": "Open"},  # over approval limit
    {"id": "INV-5001", "vendor": "Soylent GmbH", "amount": "2,300.00", "currency": "EUR", "issued": "June 5, 2026", "due": "July 5, 2026", "status": "Open"},  # EUR
    {"id": "CN-7001", "vendor": "Hooli Inc", "amount": "400.00", "currency": "USD", "issued": "June 8, 2026", "due": "July 8, 2026", "status": "Open"},      # id the ERP rejects
]

# Invoices added through the admin page persist across runs and restarts in this file.
CUSTOM_FILE = Path(__file__).parent / "custom_invoices.json"
BASE_IDS = {i["id"] for i in INVOICES}
INV_LOCK = threading.Lock()


def _save_custom():
    custom = [i for i in INVOICES if i["id"] not in BASE_IDS]
    if custom:
        CUSTOM_FILE.write_text(json.dumps(custom, indent=2))
    elif CUSTOM_FILE.exists():
        CUSTOM_FILE.unlink()


def _load_custom():
    if CUSTOM_FILE.exists():
        try:
            for i in json.loads(CUSTOM_FILE.read_text()):
                if i["id"] not in {x["id"] for x in INVOICES}:
                    INVOICES.append(i)
        except Exception:
            pass


_load_custom()


class World:
    def __init__(self, flaky=True):
        self.flaky = flaky
        self.flaky_fired = False
        self.bills = []
        self.sessions = set()
        self.lock = threading.Lock()


NAV = '<p><a href="/portal/invoices">Vendor Portal</a> | <a href="/erp/bills">Internal ERP</a></p>'


def page(title, body):
    return f"<html><head><title>{html.escape(title)}</title></head><body>{NAV}<h1>{html.escape(title)}</h1>{body}</body></html>"


def admin_page(msg=""):
    e = lambda x: html.escape(str(x))
    rows = "".join(f'<tr><td>{e(i["id"])}</td><td>{e(i["vendor"])}</td><td>{e(i["amount"])} {e(i["currency"])}</td>'
                   f'<td>{e(i["issued"])}</td><td>{e(i["due"])}</td><td>{e(i["status"])}</td>'
                   f'<td>{"built-in" if i["id"] in BASE_IDS else "added"}</td></tr>' for i in INVOICES)
    note = f'<p class="error">{e(msg)}</p>' if msg else ""
    return page("Admin: test data (not visible to the agent)", note + """<form method="post" action="/admin/invoices">
<label>Invoice id (INV-1234 style) <input name="id"></label>
<label>Vendor <input name="vendor"></label>
<label>Amount (e.g. 1,875.25) <input name="amount"></label>
<label>Currency <select name="currency"><option value="USD">USD</option><option value="EUR">EUR</option></select></label>
<label>Issued (e.g. June 10, 2026) <input name="issued"></label>
<label>Due (e.g. July 10, 2026) <input name="due"></label>
<label>Status <select name="status"><option value="Open">Open</option><option value="Paid">Paid</option></select></label>
<button type="submit">Add invoice</button></form>
<form method="post" action="/admin/testpack"><button type="submit">Load test pack (5 invoices)</button></form>
<form method="post" action="/admin/reset"><button type="submit">Remove all added invoices</button></form>
<table><tr><th>Id</th><th>Vendor</th><th>Amount</th><th>Issued</th><th>Due</th><th>Status</th><th>Source</th></tr>""" + rows + "</table>")


def bill_form(error=None, v=None):
    v = v or {}
    err = f'<p class="error">ERROR: {html.escape(error)}</p>' if error else ""
    val = lambda k: html.escape(v.get(k, ""))
    return err + f"""<form method="post" action="/erp/bills">
<label>Vendor <input name="vendor" value="{val('vendor')}"></label>
<label>Invoice number <input name="invoice_number" value="{val('invoice_number')}"></label>
<label>Amount (digits and decimal point only) <input name="amount" value="{val('amount')}"></label>
<label>Currency <select name="currency"><option value="USD">USD</option><option value="EUR">EUR</option></select></label>
<label>Due date (YYYY-MM-DD) <input name="due_date" value="{val('due_date')}"></label>
<button type="submit">Save bill</button></form>"""


class Handler(BaseHTTPRequestHandler):
    world: World = None

    def log_message(self, *a):
        pass

    def _send(self, code, body, headers=None):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for k, val in (headers or {}).items():
            self.send_header(k, val)
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, loc, headers=None):
        self.send_response(302)
        self.send_header("Location", loc)
        self.send_header("Content-Length", "0")
        for k, val in (headers or {}).items():
            self.send_header(k, val)
        self.end_headers()

    def _authed(self):
        m = re.search(r"sid=(\w+)", self.headers.get("Cookie", ""))
        return bool(m and m.group(1) in self.world.sessions)

    # ---------------------------------------------------------------- GET
    def do_GET(self):
        u = urlparse(self.path)
        path, q = u.path.rstrip("/") or "/", parse_qs(u.query)
        w = self.world
        if path == "/":
            return self._redirect("/portal/invoices")
        if path == "/admin":
            return self._send(200, admin_page())
        if path == "/portal/login":
            return self._send(200, page("Vendor Portal Login", '<form method="post" action="/portal/login">'
                '<label>Username <input name="username"></label><label>Password <input type="password" name="password"></label>'
                '<button type="submit">Log in</button></form>'))
        if path.startswith("/portal") and not self._authed():
            return self._redirect("/portal/login")
        if path == "/portal/invoices":
            vendor = (q.get("vendor", [""])[0]).lower()
            rows = "".join(
                f'<tr><td><a href="/portal/invoice/{i["id"]}">{i["id"]}</a></td><td>{i["vendor"]}</td>'
                f'<td>{i["issued"]}</td><td>{i["amount"]} {i["currency"]}</td></tr>'
                for i in INVOICES if vendor in i["vendor"].lower())
            return self._send(200, page("Invoices", '<form method="get" action="/portal/invoices"><label>Vendor filter '
                f'<input name="vendor" value="{html.escape(vendor)}"></label><button type="submit">Search</button></form>'
                f"<table><tr><th>Invoice</th><th>Vendor</th><th>Issued</th><th>Amount</th></tr>{rows}</table>"))
        m = re.fullmatch(r"/portal/invoice/([\w-]+)", path)
        if m:
            inv = next((i for i in INVOICES if i["id"] == m.group(1)), None)
            if not inv:
                return self._send(404, page("Not found", "<p>No such invoice.</p>"))
            body = "".join(f"<p>{k.title()}: {html.escape(v)}</p>" for k, v in
                           [("Invoice number", inv["id"]), ("Vendor", inv["vendor"]), ("Amount", inv["amount"]),
                            ("Currency", inv["currency"]), ("Issue date", inv["issued"]), ("Due date", inv["due"]), ("Status", inv["status"])])
            return self._send(200, page(f"Invoice {inv['id']}", body))
        if path == "/erp/bills":
            with w.lock:
                rows = "".join(f'<tr><td>{b["invoice_number"]}</td><td>{html.escape(b["vendor"])}</td><td>{b["amount"]} {b["currency"]}</td>'
                               f'<td>{b["due_date"]}</td></tr>' for b in w.bills) or "<tr><td colspan=4>No bills yet</td></tr>"
            return self._send(200, page("ERP - Bills", '<p><a href="/erp/bills/new">New bill</a></p>'
                f"<table><tr><th>Invoice</th><th>Vendor</th><th>Amount</th><th>Due</th></tr>{rows}</table>"))
        if path == "/erp/bills/new":
            return self._send(200, page("ERP - New bill", bill_form()))
        return self._send(404, page("Not found", "<p>404</p>"))

    # --------------------------------------------------------------- POST
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        form = {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode()).items()}
        path = urlparse(self.path).path.rstrip("/")
        w = self.world
        if path.startswith("/admin"):
            with INV_LOCK:
                if path == "/admin/invoices":
                    inv = {k: form.get(k, "").strip() for k in ("id", "vendor", "amount", "currency", "issued", "due", "status")}
                    missing = [k for k in ("id", "vendor", "amount", "issued", "due") if not inv[k]]
                    if missing:
                        return self._send(422, admin_page(f"Missing: {', '.join(missing)}"))
                    if any(i["id"] == inv["id"] for i in INVOICES):
                        return self._send(409, admin_page(f"Invoice id {inv['id']} already exists."))
                    INVOICES.append(inv)
                elif path == "/admin/testpack":
                    have = {i["id"] for i in INVOICES}
                    INVOICES.extend(dict(t) for t in TESTPACK if t["id"] not in have)
                elif path == "/admin/reset":
                    INVOICES[:] = [i for i in INVOICES if i["id"] in BASE_IDS]
                else:
                    return self._send(404, page("Not found", "<p>404</p>"))
                _save_custom()
            return self._redirect("/admin")
        if path == "/portal/login":
            if USERS.get(form.get("username")) == form.get("password"):
                sid = secrets.token_hex(8)
                w.sessions.add(sid)
                return self._redirect("/portal/invoices", {"Set-Cookie": f"sid={sid}; Path=/"})
            return self._send(401, page("Vendor Portal Login", '<p class="error">Invalid username or password.</p>'
                '<form method="post" action="/portal/login"><label>Username <input name="username"></label>'
                '<label>Password <input type="password" name="password"></label><button type="submit">Log in</button></form>'))
        if path == "/erp/bills":
            err = None
            if not form.get("vendor", "").strip():
                err = "Vendor is required."
            elif not re.fullmatch(r"INV-\d+", form.get("invoice_number", "")):
                err = "Invoice number must look like INV-1234."
            elif not re.fullmatch(r"\d+(\.\d{1,2})?", form.get("amount", "")):
                err = "Amount must contain only digits and an optional decimal point (no commas or currency symbols)."
            elif not re.fullmatch(r"\d{4}-\d{2}-\d{2}", form.get("due_date", "")):
                err = "Due date must be formatted YYYY-MM-DD."
            if err:
                return self._send(422, page("ERP - New bill", bill_form(err, form)))
            with w.lock:
                if any(b["invoice_number"] == form["invoice_number"] for b in w.bills):
                    return self._send(409, page("ERP - Conflict", f'<p class="error">Bill {form["invoice_number"]} already exists.</p>'))
                w.bills.append({k: form.get(k, "") for k in ("vendor", "invoice_number", "amount", "currency", "due_date")})
                if w.flaky and not w.flaky_fired:
                    w.flaky_fired = True  # committed, but the response is lost
                    return self._send(500, page("Server error", "<p>Internal Server Error: upstream timeout.</p>"))
            return self._send(200, page("ERP - Bill saved", f'<p>Bill {form["invoice_number"]} saved.</p><p><a href="/erp/bills">Back to bills</a></p>'))
        return self._send(404, page("Not found", "<p>404</p>"))


def start_server(flaky=True, port=0):
    world = World(flaky)
    handler = type("H", (Handler,), {"world": world})
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, world, f"http://127.0.0.1:{srv.server_address[1]}"


if __name__ == "__main__":
    srv, _, base = start_server(port=8000)
    print("Sandbox running at", base, "(Ctrl+C to stop)")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        srv.shutdown()
