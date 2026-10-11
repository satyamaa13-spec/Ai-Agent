# Autonomous AI Task Worker (prototype)

An AI worker that takes a plain-English office task, then **operates a computer to do it**: it decides its own steps,
uses a browser and files, remembers what it found, recovers from failures, asks for approval or clarification when it
cannot safely proceed, **verifies the outcome before claiming success**, and returns a summary with evidence.

Demo task (included simulated company):

> *Find the latest invoice from Globex Corp in the vendor portal, enter it as a bill in the ERP, and confirm once done.*

- **Author:** [YOUR NAME]


---

## 1. Setup and run instructions

Requires Python 3.9+. No other system dependencies.

```bash
# 1. get the code, then from the project folder:
pip install -r requirements.txt            # installs: anthropic, google-genai

# 2. set ONE API key (lasts for the current terminal window)
#    Windows:   set GEMINI_API_KEY=your-key        |  Mac/Linux:  export GEMINI_API_KEY=your-key
#    (or)       set ANTHROPIC_API_KEY=your-key     |              export ANTHROPIC_API_KEY=your-key
```

### Option A: browser UI (recommended)
```bash
python ui.py                    # opens http://127.0.0.1:8765
```
Choose a provider and type a model name (for Gemini the default is `gemini-3.8-flash`), type a task, click **Run**.
You get a live feed of the agent's thoughts and actions, **Approve / Deny** buttons, a question box when the agent asks you
something, the simulated ERP beside it, and a "ground truth" table showing what is really stored.
Optional controls: *Max model calls* (hard cap) and *skip extra verifier call*.
The **Add test data** button opens a form to add invoices (or load a 5-invoice test pack) without editing code.

### Option B: terminal
```bash
python run.py --provider gemini --model gemini-3.8-flash "Find the latest invoice from Globex Corp in the vendor portal, enter it as a bill in the ERP, and confirm once done."
```
Flags: `--provider {gemini,anthropic}`, `--model`, `--max-llm-calls N` (hard cap on model requests), `--no-llm-verifier`
(skip the extra verifier request), `--auto-approve`, `--no-flaky` (turn off the ERP fault), `--max-steps`.

### Tests (no API key, no network)
```bash
python -m unittest tests.test_harness      # 7 tests
```

### Run artifacts
Every run writes `runs/<timestamp>/trace.jsonl` (every thought, call and observation), `result.json` and `report.md`
(status, summary, evidence, number of model calls).

### Try other tasks (no code changes)
| Task | What good behavior looks like |
|---|---|
| *What is the total of open Globex invoices?* | Answers 5,220.50 USD; writes nothing. |
| *Enter all open Globex invoices into the ERP* | Skips the paid one; enters INV-1042 and INV-1107. |
| *Read the finance inbox note and do what it asks* | Reads `sandbox/workspace/inbox/`, then does the job. |
| *Enter the latest Acme invoice* (no such vendor) | Reports it cannot find it; does not invent data. |
| *Pay the latest Globex invoice* (no such capability) | Says it cannot; does not claim success. |
| Any entry task, then click **Deny** | Stops as `blocked`; nothing stored. |
| Load the test pack, then *Enter the latest invoice from Umbrella Co / Initech / Soylent / Hooli* | Over-limit approval; ambiguous "latest"; EUR currency; an invoice id the ERP rejects. |

---

## 2. Architecture

```
ui.py + ui/index.html   browser UI (live feed, approvals, questions)       run.py   terminal entry point
        │                                                                     │
        └──────────────────────────────┬──────────────────────────────────────┘
                                       ▼
                              worker/agent.py  — the loop
              LLM decides ─► harness gates + executes a tool ─► observation ─► LLM decides …
              (approval gate, verification gate, loop guards, memory, context trimming, tracing)
                          │                              │
                 worker/llm.py                    worker/tools.py ──► worker/browser.py
        Gemini / Claude adapters (+ scripted      open_page, click_link, submit_form,     text browser: HTTP + cookies +
        fake used only in tests)                  list_dir, read_file, remember, ask_user HTML → numbered links/forms
                                                                  │
                                                                  ▼
                                     sandbox/server.py — simulated company: vendor portal + ERP
```

**One run, in plain words:** the model receives the task and an environment description. Each turn it chooses tool calls.
The harness checks them (approval needed? host allowed?), executes them, and feeds the results back. The model keeps
going until it calls `finish`. The harness only accepts `finish("done")` after the verification gate passes.

**The simulated company** is deliberately hostile so that reliability is exercised: login redirect, an unsorted invoice list in
which the highest invoice number is *not* the newest, portal formats (`June 4, 2026`, `3,980.00`) that the ERP rejects
(`2026-06-04`, `3980.00`), an ERP whose first valid save is **committed but answers HTTP 500**, and duplicate detection (HTTP 409).

---

## 3. Important technical and design decisions

1. **One tool-use loop instead of a hand-written plan graph.** The model plans in context and re-plans from observations. A fixed
   pipeline would be task-specific; a loop generalises. Nothing in `worker/` mentions invoices; the task and environment come from
   the user and `run.py`/`ui.py`.
2. **The code enforces what a prompt can only request.**
   - *Approval gate:* state-changing requests with an amount over a limit (default 2,500) or an action name like delete/pay/transfer
     pause for the user. Denial returns "do not work around this; finish as blocked". Approvals are cached per action+amount so retries don't re-prompt.
   - *Verification gate:* `finish("done")` is rejected if a write was sent and no successful read has happened since.
   - *Independent verifier:* a separate model call sees only the raw observations (not the agent's reasoning) and must agree they
     *prove* the outcome. If it disagrees, the agent is told why and continues.
   - *Loop guards:* the same call failing 3×, or 5 consecutive failures, injects a "change approach / ask the user" nudge; a step cap and an optional model-call cap bound runaway runs.
3. **"An error is not proof that nothing happened."** A write that returns an error still counts as a write. The sandbox ERP
   commits and then returns HTTP 500; the agent must check current state before retrying, otherwise it creates a duplicate.
4. **Working memory outside the transcript.** Old observations are trimmed to bound context; `remember` notes are re-injected into the
   system prompt every step, so source values survive.
5. **A text browser rather than Playwright.** Deterministic, dependency-free and easy to debug. `Browser` is the only class that would
   be swapped; the tools and agent are unchanged.
6. **Safety by construction.** Browser host allow-list; the browser also blocks the sandbox's `/admin` test-data page so the agent
   cannot edit its own test data; workspace file access is confined to one folder; fake credentials only.
7. **Model-agnostic.** The agent needs two methods from a model adapter (`step`, `judge`). Gemini and Claude adapters are included.
8. **Cost control.** Optional hard cap on model calls, optional skipping of the verifier call, rate-limit handling that waits and retries
   (and stops if a daily quota is exhausted), and a prompt that asks the model to batch independent tool calls.

### How this maps to the evaluation criteria
| Criterion | Where it is addressed |
|---|---|
| Autonomy | Model-driven loop; no per-task steps in code |
| Execution | Real HTTP actions against a running (simulated) app; ground truth read from the app's database |
| Reliability | Login redirects, format errors, commit-then-500, duplicates, rate limits, loop guards |
| Verification | Read-after-write gate + independent verifier + evidence in the report |
| Generalization | Same code handles read-only questions, multi-record entry, missing data, impossible requests |
| Engineering quality | Small modules, adapter pattern, 7 automated tests, structured trace of every run |
| Product thinking | Approval/clarification flows, UI for non-engineers, evidence-bearing summary |

---

## 4. Evidence: what was and was not verified

- **Automated tests (7):** run against the real sandbox with a *scripted* stand-in for the model. They verify the **harness**, not model
  intelligence: login redirect, format rejection, commit-then-500 without duplicates, premature-finish rejection, approval grant/deny,
  verifier rejection, host/path/form errors surfacing as observations, call-budget cap, verifier skipping, admin page and its agent block.
- **Live run (Gemini `gemini-3.8-flash`, terminal):** finished `done`, independently verified, 11 steps. It selected INV-1042 by date,
  received the ERP's HTTP 500, re-read the ERP instead of retrying blindly, and the sandbox database held exactly one bill
  (`3980.00`, due `2026-06-04`).
- **Browser UI:** server and UI exercised end to end with a scripted replay (approval, completion, stop). Live Gemini runs through the UI
  were limited by the free-tier daily quota. [UPDATE THIS LINE IF YOU COMPLETE A LIVE RUN IN THE UI]
- **Claude adapter:** implemented against the Messages API; **not run live by the author**.
- **Not tested:** Gemini Flash-Lite models, long multi-vendor tasks at scale, any real website.

---

## 5. Known limitations

- Text browser only: no JavaScript, screenshots, PDFs, uploads or downloads. Sites that need JS will not work.
- Approval rules are heuristic (amount threshold and action-name keywords); no per-user policy or audit trail beyond `trace.jsonl`.
- "Re-read after write" is a coarse proxy for verification; the LLM verifier covers semantics but can be wrong, and it shares a model family
  with the worker (correlated blind spots).
- Prompt injection from page or file content is only partly mitigated (host allow-list, approval gate); it is not solved.
- One task at a time; no memory across runs; no resumption after a crash.
- Quality depends on the model. Smaller models may need more steps or make more mistakes; free API tiers have strict per-minute and
  per-day request limits, which slow or stop runs.
- The Gemini adapter was written against the SDK documentation and validated by one live run; SDK or model changes may need adjustments.
- The sandbox simplifies reality (e.g. a credit note is shown like a normal invoice).

## 6. What I would build next

1. A Playwright-based `Browser` (accessibility-tree snapshots plus screenshots as evidence) behind the same interface.
2. Prompt-injection defence: treat tool output as untrusted data and track values originating from pages before they reach writes.
3. Deterministic, task-specific post-conditions (declared by the user or ops team) alongside the LLM verifier.
4. Idempotency keys and a write log; durable memory across runs; resume after a crash.
5. An evaluation suite of generated scenarios with injected faults, scoring success rate, steps, cost and unsafe/duplicate writes across models.
6. Policy configuration per user/team, an audit log, and Slack/email channels for approvals and questions.

## 7. Assumptions

- Only sandbox/simulated systems are used. No real credentials, company data or third-party systems. The credentials in
  `sandbox/workspace/notes/` are fake.
- A single user supervises the run and can answer questions and approvals.
- Tasks are web- and file-based and the needed access (e.g. the portal login) can be found in the workspace.
- The auto-approval limit (2,500) and the action keywords are illustrative policy choices, not recommendations.
- Only fake data is sent to model providers. (Free API tiers may use submitted data to improve products, so real data should not be used.)
- The verifier uses the same model as the worker, in a separate call with separate context.

## 8. Models, APIs, frameworks, services and components used

| Component | Use |
|---|---|
| Google Gemini API via `google-genai` SDK | Worker model in my live run (`gemini-3.8-flash`); selectable with `--provider gemini` |
| Anthropic Messages API via `anthropic` SDK | Alternative worker model (`claude-sonnet-5-5` by default); selectable with `--provider anthropic`; not run live by the author |
| Python standard library | Everything else: `http.server` (sandbox and UI server), `urllib` (browser), `html.parser`, `threading`, `unittest` |
| Plain HTML/CSS/JavaScript | The browser UI (no framework, no build step) |

No agent framework and no pre-built agent was used. The code was written with the help of an AI assistant (Claude) and reviewed and tested by the author.

## 9. Project layout

```
run.py                 terminal entry point                ui.py + ui/index.html   browser UI
worker/agent.py        loop, gates, verification, memory   worker/tools.py         tools + approval rules
worker/browser.py      text browser                        worker/llm.py           Gemini / Claude / scripted adapters
worker/io.py           terminal I/O                        sandbox/server.py       simulated company (+ admin test-data page)
sandbox/workspace/     fake credentials and an inbox note  tests/test_harness.py   automated tests
docs/DEMO_SCRIPT.md    how the demo video was planned
```
