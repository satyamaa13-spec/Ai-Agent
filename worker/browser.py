"""A small text 'browser': HTTP + cookies + HTML -> numbered links/forms snapshot the model can act on.

No JavaScript. Deliberately simple: the Browser class is the only thing that would be swapped for
Playwright in a real deployment; the tools above it would not change.
"""
import re
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import HTTPCookieProcessor, Request, build_opener


class ToolError(Exception):
    """Raised when a tool call is invalid or could not be performed (nothing was sent)."""


class _Parser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title, self.text, self.links, self.forms = "", [], [], []
        self._in_title, self._skip, self._href, self._ltext, self._form, self._select = False, 0, None, [], None, None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag in ("script", "style"):
            self._skip += 1
        elif tag == "a" and a.get("href"):
            self._href, self._ltext = a["href"], []
        elif tag == "form":
            self._form = {"method": (a.get("method") or "get").lower(), "action": a.get("action", ""), "fields": []}
            self.forms.append(self._form)
        elif tag in ("input", "textarea") and self._form is not None and a.get("name") and a.get("type") not in ("submit", "button"):
            self._form["fields"].append({"name": a["name"], "type": a.get("type", "text"), "value": a.get("value", "")})
        elif tag == "select" and self._form is not None and a.get("name"):
            self._select = {"name": a["name"], "type": "select", "value": "", "options": []}
            self._form["fields"].append(self._select)
        elif tag == "option" and self._select is not None:
            v = a.get("value", "")
            self._select["options"].append(v)
            if "selected" in a or not self._select["value"]:
                self._select["value"] = v
        elif tag in ("br", "p", "tr", "li", "h1", "h2", "div", "label"):
            self.text.append("\n")
        elif tag in ("td", "th"):
            self.text.append(" | ")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag == "a" and self._href is not None:
            self.links.append(("".join(self._ltext).strip(), self._href))
            self.text.append(f" [link {len(self.links) - 1}]")
            self._href = None
        elif tag == "form":
            self._form = None
        elif tag == "select":
            self._select = None

    def handle_data(self, data):
        if self._skip:
            return
        if self._in_title:
            self.title += data
        else:
            self.text.append(data)
            if self._href is not None:
                self._ltext.append(data)


class Snapshot:
    def __init__(self, url, status, body):
        p = _Parser()
        p.feed(body)
        self.url, self.status, self.title, self.links, self.forms = url, status, p.title.strip(), p.links, p.forms
        lines = [re.sub(r"[ \t]+", " ", l).strip() for l in "".join(p.text).splitlines()]
        self.text = "\n".join(l for l in lines if l)

    def render(self, max_chars=3500):
        out = [f"HTTP {self.status} | {self.url} | title: {self.title}", "", self.text[:max_chars]]
        if self.links:
            out += ["", "LINKS:"] + [f"  [{i}] {t or '(no text)'} -> {h}" for i, (t, h) in enumerate(self.links)]
        for i, f in enumerate(self.forms):
            fields = ", ".join(
                f'{x["name"]}({x["type"]}{"; options=" + "/".join(x["options"]) if x.get("options") else ""}'
                f'{"; default=" + repr(x["value"]) if x["value"] else ""})' for x in f["fields"])
            out.append(f"FORM [{i}] {f['method'].upper()} {f['action'] or '(same page)'} fields: {fields}")
        return "\n".join(out)


class Browser:
    def __init__(self, allowed_hosts, blocked_prefixes=("/admin",)):
        self.allowed = set(allowed_hosts)
        self.blocked = tuple(blocked_prefixes)  # e.g. the sandbox's test-data admin page: off-limits to the agent
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))
        self.snap = None

    def _check(self, url):
        parsed = urlparse(url)
        if parsed.path.startswith(self.blocked):
            raise ToolError(f"Blocked: {parsed.path} is an administrative area the worker must not use.")
        host = parsed.netloc
        if host not in self.allowed:
            raise ToolError(f"Blocked: {host!r} is not an allowed host. Allowed: {sorted(self.allowed)}")

    def _fetch(self, url, data=None):
        self._check(url)
        req = Request(url, data=data.encode() if data is not None else None, method="POST" if data is not None else "GET")
        try:
            r = self.opener.open(req, timeout=10)
            status, body, final = r.status, r.read().decode(errors="replace"), r.geturl()
        except HTTPError as e:
            status, body, final = e.code, e.read().decode(errors="replace"), e.geturl()
        except URLError as e:
            raise ToolError(f"Network error: {e.reason}")
        self._check(final)
        self.snap = Snapshot(final, status, body)
        return self.snap

    def open(self, url):
        return self._fetch(urljoin(self.snap.url, url) if self.snap else url)

    def click(self, index):
        if not self.snap or not (0 <= index < len(self.snap.links)):
            raise ToolError(f"No link [{index}] on the current page.")
        return self._fetch(urljoin(self.snap.url, self.snap.links[index][1]))

    def submit(self, form_index, values):
        if not self.snap or not (0 <= form_index < len(self.snap.forms)):
            raise ToolError(f"No form [{form_index}] on the current page.")
        form = self.snap.forms[form_index]
        data = {f["name"]: f["value"] for f in form["fields"]}
        unknown = set(values) - set(data)
        if unknown:
            raise ToolError(f"Unknown field(s) {sorted(unknown)}. Form fields are: {sorted(data)}")
        data.update({k: str(v) for k, v in values.items()})
        url = urljoin(self.snap.url, form["action"] or self.snap.url)
        if form["method"] == "post":
            return self._fetch(url, urlencode(data))
        return self._fetch(url.split("?")[0] + "?" + urlencode(data))
