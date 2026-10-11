# Rung — handoff (2026-10-11, ~18:15 IST)

For whoever picks this up next: you, or a local coding agent such as Claude Code or Codex. Read this, then `README.md`.

## 1. What this is
This is a prototype for the CentrAlign Founding Engineer take-home: an "AI employee" that turns short company requests into verified work across a sandbox company. The thesis is **"AI decides *how*, code decides *whether*"**. In more detail:
- **The interface ladder.** For each step it tries, in order: already done → learned API tool → documented API or CLI → tool on probation → explore the UI. The model never decides whether a step succeeded; a read of the real system state does.
- **Learning interfaces.** It turns recorded UI traffic into typed HTTP tools. A tool is promoted only after a do → verify → undo → verify sandbox test, and it's demoted and re-learned when it breaks.
- **A saga run model.** Checkpoints, resume, compensation when a required step fails, scheduled steps, and a run lease.
- **Precedent mining** (how peers are set up, chat history, mail), with written policy always beating precedent and suspicious instructions flagged.
- **An independent eval oracle** that reads the app databases directly.

## 2. State of the code
- **Commits.** Everything is committed up to the last commit, apart from the one exception below.
- **Last full eval that passed:** commit `8d79f37`, **24/24, 0 false-success** (`eval/last_run.log`). That run predates the iteration-2 work below.
- **Iteration 2, all written since that run** (in the final commit `iteration 2 (WIP)`), driven by two independent critiques plus a red-team of about 35 attacks:
  - Requester authorization: internal domain, active in HR, authority rules in `memory/policies.yaml` under `requesters`.
  - Email channel hardening: SMTP From can be spoofed, so destructive tasks get a one-time code mailed to the requester's address in PeopleHub.
  - A real date parser (`rung/dates.py`). An unclear date gets a question, and offboarding never defaults to today.
  - Negation detection, multiple people in one request split into child runs, and case-insensitive names and emails.
  - Answer validation, with bad answers re-asked. `tick` isolates each run so one failure can't block the others. When HR is down the run fails closed (escalated).
  - The welcome email and pack list the channels that actually succeeded (late binding).
  - Trusted mail precedent is scoped to the hire's role. Asks inside the request go through policy, and refusals are reported back.
  - **A stock Django admin "Acme SSO" app (port 7104), whose UI I didn't write.** It is wired into the onboarding and offboarding procedures.
  - **A generic explorer.** It fills forms, picks select options and presses verb buttons, with a goal check after every action. It handles Django's two-step user creation, and the promoter correctly declines to build a tool because Django has no JSON API.
  - **Procedures drafted from prose.** For an unknown task the requester points at a Drive SOP; the model drafts a procedure, it's validated (`rung/procedures.py`, with errors fed back), a human approves it, and it's saved.
  - Model-call recording and replay (`RUNG_RECORD`, and `RUNG_MODEL=replay`).
- **The eval run in flight when I stopped** (`var/eval.log`, started before the last few edits): A1–B5.3 and C1–C4 passed, but **B5.2 FAILS**. This is a regression from the generic explorer. UI variant 2 labels the username field "Handle", which isn't in the synonym list, and the old "assign the last unmatched field by elimination" rule was lost in the rewrite. The run correctly ends **compensated, with no false success**. **Fix:** in `HeuristicExplorer.fill_form`, after token matching, if exactly one text input and one wanted field are left unmatched, pair them.
- **Not yet verified end to end:**
  - the R1–R10 red-team regression cases;
  - SSO checks inside every onboarding case;
  - `tests/test_procedures.py::test_unknown_task_learns_procedure_from_sop_with_approval`;
  - the default-authority policy change.

## 3. Next steps, in order
1. **Fix B5.2** (elimination rule above), then run `python -m pytest -q tests` and `python -m eval.run_eval --random 7`. Fix failures, then update the README results table with honest numbers.
2. **Run with a real model** on a machine that can reach NIM:
   ```
   RUNG_MODEL=nim NVIDIA_API_KEY=... RUNG_RECORD=eval/model_run.jsonl python -m eval.run_eval --model
   ```
   Put the offline-vs-model comparison in the README. Commit the recording so reviewers can replay it.
3. **README:** add the new pieces and a three-line thesis at the top, and keep the limitations section honest.
4. **Demo:** a `scripts/demo.sh` that seeds, starts the servers, then does a cold run, a warm run and a tick. Commit a sample `docs/sample_report.md`. A short screen recording is optional.
5. Still open from the critiques:
   - the promoter's sandbox test runs in the live workspace (should be a staging tenant);
   - side effects outside an app's state projection aren't seen;
   - the Chatter probes are hand-written for that app;
   - the generated-company test only changes names, not structure.

## 4. Gotchas
- **Restarting the sandbox.** Don't use `pkill -f sandbox.run_all`, because it kills your own shell. Use:
  ```
  for p in $(ps -eo pid,comm,args | awk '$2 ~ /python/ && /sandbox.run_all/ {print $1}'); do kill $p; done; python -m sandbox.run_all &
  ```
  Restart after any edit to `sandbox/*`, since the servers load that code once at startup.
- **Resetting.** `python -m eval.run_eval` reseeds everything. By hand:
  ```
  rm -rf var/agent.db var/learned var/sessions var/discovery var/runs && python -m sandbox.seed
  ```
- **Timezone.** `TZ` must be `Asia/Kolkata`, because tzlocal rejects `Asia/Calcutta`. `rung/dates.py` sets this.
- **Dependencies:** Python 3.11+, Playwright Chromium, LibreOffice (`soffice`), `pdftotext`, and the packages in `requirements.txt` plus `dateparser` and `django`.
- **Restricted network on the build machine:** no model APIs, no Docker registry, no Go proxy. pip, npm and GitHub work.

## 5. Map
`rung/` holds the core:
- `ladder.py`: the interface ladder
- `promoter.py`: traffic → tool synthesis, sandbox test, promotion
- `runtime.py`: runs, saga, scheduling
- `understand.py` and `derive.py`: understanding and plan building
- `policy.py`: the policy gate
- `precedent.py`: precedent mining
- `procedures.py`: validation and SOP drafting
- `dates.py`: date parsing
- `inbox.py`: the email front door
- `apps/*`: one adapter per app
- `explore/*`: the browser and the explorers
- `llm/*`: model clients and typed calls

Elsewhere:
- `memory/`: procedures and policies, as YAML data
- `sandbox/`: the fake company, including the Django SSO app
- `eval/`: scenarios, the oracle and results
- `tests/`: unit tests and FakeModel tests
