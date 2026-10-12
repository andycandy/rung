# Rung

Rung is an operations agent for a company's internal apps, built for the CentrAlign brief. It takes a request like "Please onboard Priya, she's joining Monday as a data analyst" and carries it out across HR, chat, SSO, e-mail, a shared Drive and LibreOffice. It checks each step against the app's real state afterwards.

https://github.com/user-attachments/assets/b62befc8-78db-41e4-9584-b05bce5f8cfd

Demo: one real run on Nemotron, 2:47, sped up 2× ([full-quality file](docs/rung-demo.mp4)). It covers a cold start, a warm run on learned API tools, a new task learned from a how-to document, and the chat API changing under it.

## Results

| | |
|---|---|
| End-to-end eval, 41 cases | 0 false successes, 38 passed |
| Replay of that run, no model | same 41 outcomes, 0 misses (tag `eval-run-1`) |
| Unknown-task search, 5 cases | 5 passed, 0 false successes |
| A chat step once its tool is learned | ~60 ms, vs ~20 s through the UI |
| Tests | 85 passing |

A false success is a run that reports a task as done when it isn't. An independent checker reads the apps' databases, which the agent can't access, and compares them with what the agent reported.

The 3 failures reported themselves as failures:
- the UI explorer couldn't use a redesigned chat UI;
- it couldn't disable a login in the Django admin, so the run was rolled back;
- the model read one request as "not a request", so nothing was done.

Per-case logs are in [eval/model_eval.log](eval/model_eval.log).

## How it works

The model reads and code decides. The model (Nemotron 3 Super on NVIDIA NIM) does four things:
- reads the request;
- proposes extra steps from evidence (how colleagues are set up, recent mail and chat);
- operates web UIs it hasn't seen before;
- drafts procedures from documents.

Code handles everything else:
- It looks people and dates up in HR, and asks the requester whenever something is ambiguous.
- It checks every proposed step against written policy and the app's real fields. Mail from outside the company never justifies a step.
- It re-reads app state after every action. If a required step fails, it undoes the steps already completed.

Each step uses the cheapest interface that works:
1. The change is already true, so the step is skipped.
2. A learned API tool runs.
3. A documented API or CLI runs: HR's OpenAPI, SMTP, LibreOffice.
4. The model operates the UI. Rung records the HTTP traffic, turns it into an API tool, tests it (do, verify, undo, verify) and keeps it for next time. If the API later changes (404, 410), the tool is dropped and relearned.

For a task it has no procedure for, Rung searches the Drive handbook and ops@ mail for a how-to, drafts a procedure, and waits for the requester's approval before running it.

## Run it

```bash
pip install -r requirements.txt          # Python 3.11+, Playwright Chromium, LibreOffice, pdftotext
python -m sandbox.seed
PYTHONHASHSEED=0 python -m sandbox.run_all &
export RUNG_MODEL=nim NVIDIA_API_KEY=nvapi-...

python -m rung run "Please onboard Priya, she's joining Monday as a data analyst"
python -m rung clock 2026-10-12T09:30:00+05:30 && python -m rung tick     # jump to the start date
python -m rung inbox                                                       # handle requests mailed to ops@
python -m rung events <run>                                                # full log of a run

python -m pytest -q tests
python -m eval.run_eval --random 7                                         # full eval on the live model, ~30 min
git checkout eval-run-1 && RUNG_MODEL=replay RUNG_REPLAY=eval/model_run.jsonl python -m eval.run_eval --random 7   # exact replay, no key needed
```

## Limitations

- One domain (people ops) and one model. Run-to-run variance hasn't been measured.
- The HR, chat and mail apps are purpose-built stand-ins. The SSO app is a stock Django admin. SMTP, LibreOffice and Chromium are real.
- The UI explorer fails 2 of its 11 benchmark tasks.
- If the model is down, requests escalate. There is no offline mode.

## Repository

| path | contents |
|---|---|
| `rung/` | the agent: understanding, grounding, planning, the interface ladder, tool learning, the UI explorer, the run model |
| `rung/llm/` | typed model calls; the model client, rate limit, recording and replay |
| `memory/` | procedures (YAML) and written policy |
| `sandbox/` | the test company: HR, chat, mail, SSO, Drive, failure injection |
| `eval/` | end-to-end eval, independent checker, layer benchmarks, results and recordings |
| `scripts/` | sample report and demo-video tooling |
| `docs/` | [DEVLOG](docs/DEVLOG.md) (design, every bug found, full results, background, future work), sample report, demo video |

## Background

Rung grew out of Jarvis, my personal assistant on Telegram, which runs on the same stack (Nemotron on NIM, SQLite, typed tool calls). Its patterns carried over: timed job claims, outside text treated as data, risk gates and batch undo. The code is new.

AI coding tools: Claude (architecture discussion and critique) and Claude Code in the Antigravity IDE for implementation, eval runs and the demo tooling. I directed the design, wrote the specs and reviewed the results.
