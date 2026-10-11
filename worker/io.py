"""User-facing channel: logging, questions, approvals. Swap for Slack/web UI without touching the agent."""


class CLIIO:
    def __init__(self, auto_approve=False):
        self.auto_approve = auto_approve

    def log(self, kind, data):
        s = {"thought": lambda d: f"\n\U0001F4AD {d['text']}", "call": lambda d: f"  \u25B6 {d['name']}({d['input']})",
             "observation": lambda d: "    \u21B3 " + d["text"][:260].replace("\n", " \u23CE "),
             "note": lambda d: f"  \u26A0 {d['text']}", "memory": lambda d: f"  \U0001F4CC {d['key']} = {d['value']}"}.get(kind)
        if s:
            print(s(data), flush=True)

    def ask(self, question):
        return input(f"\n\u2753 Worker asks: {question}\n> ")

    def approve(self, description):
        if self.auto_approve:
            print(f"  \u2705 auto-approved: {description}")
            return True
        return input(f"\n\U0001F512 Approval needed: {description}\nApprove? [y/N] ").strip().lower() == "y"
