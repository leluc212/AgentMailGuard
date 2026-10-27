# Guard Playground

A local web app that shows AgentMailGuard to the thesis committee: pick a case or type an email, compare **C0** (no guard) with **C3** (full guard), and read the decision layer by layer. Spec: `.kiro/specs/guard-playground/`.

**Security note:** the Playground has **no login**. It listens on **127.0.0.1 only**, so other machines cannot reach it. Never publish port 3010 on another address.

## Start

| Case | Command | Mode | Cost |
|---|---|---|---|
| (b) Without Docker, Replay only | `powershell -ExecutionPolicy Bypass -File scripts\playground\run_playground.ps1` | Replay | 0 |
| (b) Without Docker, with Live | `powershell -ExecutionPolicy Bypass -File scripts\playground\run_playground.ps1 -Live` | Replay + Live | ~0.005 USD per gpt-4o-mini run |
| (a) Docker, Replay + Live | `docker compose up -d guard-playground` | Replay + Live | as above |
| (a) Docker, Replay only | `$env:PLAYGROUND__LIVE_ENABLED="false"; docker compose up -d guard-playground` | Replay | 0 |

Then open **http://127.0.0.1:3010/guard**. Stop with Ctrl+C, or `docker compose stop guard-playground`.

- Live keys come from `.env`: `BENCH_OPENAI_API_KEY` and `BENCH_OPENROUTER_API_KEY`. The launcher prints only "set" or "not set", never the values.
- Docker mounts every data folder read-only and needs no image rebuild. If the image is older than the current commit, run `docker compose build guard-playground` first, and never during a benchmark run.

## Pages

- **`/guard`** has three parts:
  - the toolbar: Mode, Draft model, Compare/Single, the presets and Run;
  - the case list on the left, with ★ marking the Demo set;
  - "Your own email": sample buttons, Subject, Body, Advanced KB chunks, and a file drop zone.
- **Results** at the bottom of `/guard`: the benchmark numbers at a glance, with a "Full results" link.
- **`/guard/results`**: the three KPI cards, the six black-and-white figures (each with a PDF link) and the provenance line.
- **Start tour** walks 5 prepared steps. N and P keys move between steps.
- **Presenter mode** gives bigger text, hides the case list and collapses the guarded prompts.

## Replay store

`data/replay/` holds 72 recorded runs: 12 Demo-set cases × {C0, C3} × 3 models, generated on 2026-10-26.

- **Regenerate** (paid API calls): `.venv\Scripts\python.exe scripts\playground\build_replay.py --model all`.
- **Preview the plan** without calling anything: add `--dry-run`.
- **Suggest Demo-set cases** from the benchmark results: `--list-candidates`.

A pair that fails keeps its old file.

## Fallback plan for the committee demo

Fallback order: **Live → Replay → screen recording**.

- **Live → Replay:** switch when a Live run shows an error, or has not finished after **60 seconds**. Either press "Show the recorded REPLAY result instead" or switch the Mode to Replay.
- **Replay → recording:** switch when `/guard` does not load within **30 seconds**, or the laptop cannot be used.

### Screen recording

- Record the full Demo tour (5 steps, C0 vs C3) no later than **24 hours before the demo**.
- Keep the primary copy on the presenter's laptop and at least one backup on another device or a USB stick.
- Recording files over 50 MB stay outside the git repository. Never commit them.
- Fill in the record below:

| Item | Value |
|---|---|
| Primary copy (path on laptop) | _to fill_ |
| Backup copy (device / path) | _to fill_ |
| Recorded on (YYYY-MM-DD) | _to fill_ |
| Playground commit (`git rev-parse --short HEAD`) | _to fill_ |

### Pre-demo checklist (done no later than 24 h before the demo)

- [ ] (a) or (b) with Live: one Live run each with gpt-4o-mini, Qwen and Llama completes.
- [ ] (b) Replay only: `/guard` loads on the demo laptop and the tour runs all 5 steps.
- [ ] Browser check passes on the demo laptop (Playground running): `.venv\Scripts\python.exe scripts\playground\ui_check.py` prints `0 problem(s)`. It checks the 1280×720 layout, the tour, presenter mode and the Results figures, and makes no API call.
- [ ] The primary copy of the screen recording plays from start to end.
- [ ] The backup copy plays from start to end.
- [ ] The commit in the table above equals `git rev-parse --short HEAD` on the demo laptop.

## Tests

`.venv\Scripts\python.exe -m pytest tests/unit -k guard_playground -q`. These tests make no network call and need no API key.
