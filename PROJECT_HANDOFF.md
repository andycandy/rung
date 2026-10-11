# Rung — Agent Handoff & Progress Report

**Date / Timestamp:** 2026-10-11 ~19:55 IST  
**Workspace:** `c:\Users\Administrator\Desktop\Projects\Random Ahh\rung`  
**Remote Compute Host:** `ssh Anand@100.121.80.124` (WSL user: `anand`, path: `/home/anand/rung`)  
**Status:** Stopped cleanly per user instruction. All offline tests and evals passing 100%. Ready for NIM eval resumption or final commit/wrap-up.

---

## 1. What Antigravity Was Instructed With in the Beginning

### Initial Project Context & Handover
- Began with investigating Claude Code paired with NVIDIA NIM as backend (`/v1/messages` compatible endpoint). Audited latency (TTFT), token speeds, and tool-calling capabilities across NVIDIA NIM models.
- Took over the **Rung** project (`c:\Users\Administrator\Desktop\Projects\Random Ahh\rung`), an "AI employee" prototype built for the CentrAlign Founding Engineer take-home.
- Core thesis: **"AI decides *how*, code decides *whether*"**:
  - **Interface Ladder:** Already done → learned API tool → documented API/CLI → tool on probation → explore UI.
  - **Learning Interfaces:** Turn recorded UI traffic into typed HTTP tools after sandbox do-verify-undo-verify tests.
  - **Saga Run Model:** Checkpoints, step resume, compensation when required steps fail, scheduled deferred executions, run leases.
  - **Independent Eval Oracle:** Direct database verification independent of model output claims.

### Initial Assigned Directives
1. Pick up from [`HANDOFF.md`](file:///c:/Users/Administrator/Desktop/Projects/Random%20Ahh/rung/HANDOFF.md) and [`AGENT_HANDOVER.md`](file:///c:/Users/Administrator/Desktop/Projects/Random%20Ahh/AGENT_HANDOVER.md) (Iteration 2 WIP).
2. Fix the **B5.2 generic explorer regression** (elimination pairing rule in `HeuristicExplorer`).
3. Fix unit test failures (`tests/`).
4. Run the 34-case offline evaluation suite (`python -m eval.run_eval --random 7`) and achieve 100% pass with 0 false-success claims.
5. Benchmark against NVIDIA NIM (`nvidia/nemotron-3-super-120b-a12b`), recording model calls to `eval/model_run.jsonl`.
6. Update documentation / `README.md` and create demo scripts.

### Operational Boundaries & Rules
- **Global Rule 1:** Zero unapproved environment modifications on local or remote hosts.
- **Global Rule 3:** Offload heavy backend compute, test suites, and Playwright workloads to the secondary remote compute node (`ssh Anand@100.121.80.124`, remote WSL user: `anand`).
- **Global Rule 4:** Do not touch or send input to user interactive terminals (specifically PID 15084 running `claude`).
- **User Specific Agreement:** The user approved installing `libreoffice-writer`, `libreoffice-calc`, and `poppler-utils` on the remote WSL node, with the strict agreement to use `uv` and remove all installed packages and virtual environments upon task completion.

---

## 2. What Was Accomplished So Far

### A. Environment Provisioning & Remote Execution
- Cloned/synced workspace to `/home/anand/rung` on the remote compute host.
- Set up isolated Python virtual environment via `uv`, installed project dependencies and Playwright Chromium.
- Installed `libreoffice-writer`, `libreoffice-calc`, and `poppler-utils` via apt (approved) for headless document rendering and PDF text extraction.
- Created `remote_exec.py` helper to run remote commands over SSH without PowerShell quote-mangling issues.
- Started background mock sandbox services (ports 7101: HR, 7102: Chatter, 7103: Mail, 7104: SSO Django).

### B. Bug Fixes & Code Hardening
Six bugs were diagnosed and resolved:

1. **B5.2 Generic Explorer Elimination Pairing ([`rung/explore/explorer.py`](file:///c:/Users/Administrator/Desktop/Projects/Random%20Ahh/rung/rung/explore/explorer.py)):**
   - Added elimination pairing rule in `HeuristicExplorer.fill_form`: after token matching, if exactly one text input and one non-boolean wanted field remain unmatched, they are paired. Fix verified: B5.2 and B5.3 pass cleanly.
2. **SQLite Database Locking & Concurrency ([`sandbox/seed.py`](file:///c:/Users/Administrator/Desktop/Projects/Random%20Ahh/rung/sandbox/seed.py)):**
   - Added `django.db.connections.close_all()` in `seed()` and `seed_sso()` before removing `SANDBOX` directory, and added `os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"`. Solved `sqlite3.OperationalError: attempt to write a readonly database`.
3. **LLM Explorer Fallback in SSO ([`rung/apps/sso.py`](file:///c:/Users/Administrator/Desktop/Projects/Random%20Ahh/rung/rung/apps/sso.py)):**
   - Added fallback to `HeuristicExplorer` when `LLMExplorer` gives up or exhausts step budget in SSO, preventing step failure and saga compensation.
4. **FakeModel Test Prompt Extraction ([`tests/fake_model.py`](file:///c:/Users/Administrator/Desktop/Projects/Random%20Ahh/rung/tests/fake_model.py)):**
   - Restricted regex word extraction strictly inside `<data>...</data>` instead of searching the entire user prompt.
5. **Timezone Drift Bug / Glibc `tzset()` ([`sandbox/sso/settings.py`](file:///c:/Users/Administrator/Desktop/Projects/Random%20Ahh/rung/sandbox/sso/settings.py), [`rung/dates.py`](file:///c:/Users/Administrator/Desktop/Projects/Random%20Ahh/rung/rung/dates.py), [`sandbox/common.py`](file:///c:/Users/Administrator/Desktop/Projects/Random%20Ahh/rung/sandbox/common.py)):**
   - Django Admin lacked `TIME_ZONE="Asia/Kolkata"`, defaulting to `America/Chicago` (CDT, UTC-5) which triggered glibc `time.tzset()` during `django.setup()`. This warped Python's process-level timezone backwards by 10.5 hours. During morning runs (10:00 AM IST), timestamps shifted into the previous calendar day, breaking `not_before` in scheduled offboarding (`C6`) and date agreement in `R10`.
   - Fixed by setting `TIME_ZONE = "Asia/Kolkata"` in Django settings, ensuring `time.tzset()` runs on initialization, and normalizing sandbox clock to IST.
6. **Dynamic Reseeded Company Requester ([`eval/run_eval.py`](file:///c:/Users/Administrator/Desktop/Projects/Random%20Ahh/rung/eval/run_eval.py)):**
   - Replaced hardcoded requester in case `D1` with dynamic department head email on randomized company schemas (`--random 7`).

---

## 3. Test & Evaluation Results

### Unit Tests
```bash
python -m pytest -q tests
```
- **Result:** **10 passed in 44.8s (100%)**

### Offline Evaluation Suite
```bash
python -m eval.run_eval --random 7
```
- **Result:** **34 passed / 34 total (100%), 0 false-success claims, 284.2s wall time**
- **Artifact:** Synced locally to [`eval/results.json`](file:///c:/Users/Administrator/Desktop/Projects/Random%20Ahh/rung/eval/results.json).

### Live NVIDIA NIM Benchmark
```bash
RUNG_MODEL=nim NVIDIA_API_KEY=... RUNG_RECORD=eval/model_run.jsonl python -m eval.run_eval --model --random 7
```
- **Model:** `nvidia/nemotron-3-super-120b-a12b`
- **Result:** 76 live model calls recorded to [`eval/model_run.jsonl`](file:///c:/Users/Administrator/Desktop/Projects/Random%20Ahh/rung/eval/model_run.jsonl). Halted cleanly per user instruction.

---

## 4. Current Workspace State

```text
 M eval/results.json       (Synced: 34/34 eval results)
 M eval/run_eval.py        (Fixed: D1 dynamic requester)
 M rung/apps/sso.py        (Fixed: LLM explorer fallback)
 M rung/dates.py           (Fixed: tzset handling)
 M rung/explore/explorer.py(Fixed: B5.2 elimination rule)
 M sandbox/common.py       (Fixed: sandbox clock IST normalization)
 M sandbox/seed.py         (Fixed: SQLite connection closing & async safety)
 M sandbox/sso/settings.py (Fixed: Django TIME_ZONE Asia/Kolkata)
 M tests/fake_model.py     (Fixed: data block scoping)
?? eval/model_run.jsonl    (Synced: 76 recorded NIM model calls)
```

---

## 5. Next Steps

1. **Resume NIM Benchmark:** Run `--model` to completion if full model-vs-offline comparison is needed.
2. **Update README.md:** Fill in final results table with 34/34 pass metrics and write three-line thesis statement.
3. **Generate Demo Script:** Run `scripts/demo.sh` to output sample report.
4. **Environment Cleanup:** Purge `libreoffice-writer`, `libreoffice-calc`, and `poppler-utils` on remote WSL node and remove virtual environment `.venv`.
