# Running the v2 benchmark natively on Windows (Docker Desktop)

**Read this first.** Docker Desktop's installation page (https://docs.docker.com/desktop/setup/install/windows-install/, read 2026-09-30) lists, for every backend, Windows 11 **Enterprise, Pro or Education**, version 23H2 or newer (Windows 10 22H2 for the same three editions). Windows **Home is not listed**. A note further down the same page says "Windows Home or Education editions only allow you to run Linux containers", which contradicts the lists, so this project does not rely on Home with Docker Desktop.

- **On Windows 11 Home, use the WSL2 route in `docs/BENCHMARK.md`.** It installs Docker Engine inside Ubuntu and needs no Docker Desktop. It is the kit's primary route (ADR-0012 decision 16); this guide is the alternative for the editions Docker Desktop lists.
- **On Windows 11 Pro, Enterprise or Education**, you may follow this guide instead: Docker Desktop, and everything typed in PowerShell.

**What was not exercised on Windows.** The kit and this guide were written and tested on Linux. Nothing below has been run on Windows by the author. The commands are taken from the tools' own documentation, which is cited for each one. Where I am guessing, the text says so. The list of what to watch is at the end.

The steps of the benchmark itself, and what the kit does in each, are the same as in `docs/BENCHMARK.md` (parts C to F); this guide only replaces the setup (its part A and B) and the way to start the commands.

**The Layer-1 classifier is not in git.** You need the file `l1_injection_clf_v1.joblib` from the owner (ADR-0012 decision 15: a dataset that went into its training declares no license, so the file is not redistributed; `evaluation/mailguard_bench/pinned/NOTICE.md` says why). The owner sends it to you privately, never by a public link, and you place it in section 2. Never commit, push or share it.

**How long it takes** (an estimate from the owner's 7-case smoke run on the owner's desktop, an RTX 3060 with 12 GB, not a measurement of a full run): a full model is 9 configs x 550 cases; about 3 s per case and config for `gpt-4o-mini`, 10 s for Qwen2.5-7B and 9 s for Llama-3.1-8B, so roughly 4 to 6 hours for `gpt-4o-mini` and 12 to 15 hours for each local model on that desktop, and longer on a 6 GB laptop GPU. See "What you need" in `docs/BENCHMARK.md`.

---

## 1. Install the tools

### Docker Desktop

Download and install Docker Desktop for Windows from the page above. Use the **WSL 2 backend** (the default). The page requires WSL 2.1.5 or newer, 8 GB of RAM, and hardware virtualization turned on in the BIOS/UEFI. To turn WSL 2 on and get a recent WSL, in PowerShell as administrator, then restart:

```powershell
wsl --install
wsl --version
```

Give Docker enough memory: create `%UserProfile%\.wslconfig` with the lines below (the size is a suggestion for a 24 GB laptop, not a measurement), then run `wsl --shutdown` and start Docker Desktop again. The kit's doctor fails below 8 GB.

```ini
[wsl2]
memory=16GB
processors=16
swap=8GB
```

(Source: https://learn.microsoft.com/en-us/windows/wsl/wsl-config, read 2026-09-30.)

### Git for Windows

Download it from https://gitforwindows.org/ (read 2026-09-30; the page links to the latest release and says nothing about line endings). The repository's `.gitattributes` keeps every text file LF on every machine, so the installer's line-ending choice does not matter for this repository's files. Still, pick the choice that does not convert files on checkout if the installer offers one (I did not read the installer's own wording), and set this once, in PowerShell:

```powershell
git config --global core.autocrlf input
```

(`core.autocrlf input` means no conversion on checkout; https://git-scm.com/docs/git-config, read 2026-09-30.)

### uv

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

or `winget install --id=astral-sh.uv -e`. Then open a **new** PowerShell and check `uv --version`. (Source: https://docs.astral.sh/uv/getting-started/installation/, read 2026-09-30.) `uv` installs the Python the project needs.

### make (optional)

The Makefile's `bench-*` targets need `make`. On Windows, `winget install ezwinports.make` installs one (the package exists in the winget catalogue, version 4.4.1 when I looked on 2026-09-30). **The Makefile's other targets use shell features (`case`, `test`), so `make` also needs a `sh` on the PATH, for example the one that comes with Git for Windows; I did not test this.** You do not need `make` at all: section 3 gives the `python -m` commands that every `make bench-*` target runs.

### Ollama (only for the two local models)

Install Ollama for Windows from https://ollama.com/download. Its documentation lists Windows 10 22H2 or newer, Home or Pro, and NVIDIA driver 551.61 or newer if you have an NVIDIA card (https://github.com/ollama/ollama/blob/main/docs/windows.mdx, read 2026-09-30). Ollama serves on `http://localhost:11434` and the `ollama` command works in PowerShell. Your RTX 4050 has 6 GB, so a 7B or 8B model partly runs on the CPU; see part E of `docs/BENCHMARK.md` for what to write down. Pull the two models once (`ollama pull qwen2.5:7b-instruct`, `ollama pull llama3.1:8b`). **Before each local-model run, load the model** (`ollama run qwen2.5:7b-instruct "Reply with OK"`): the runner refuses a model that Ollama has not loaded, and the keep-alive below keeps it loaded for 30 minutes after the last call.

Set Ollama's settings as Windows environment variables, as Ollama's FAQ describes (quit Ollama from the taskbar; in Settings search for "environment variables", choose "Edit environment variables for your account"; add the variables; start Ollama again from the Start menu):

| Variable | Value |
|---|---|
| `OLLAMA_KEEP_ALIVE` | `30m` |
| `OLLAMA_CONTEXT_LENGTH` | `32768` (the owner may give you a smaller number for 6 GB) |

The containers reach Ollama on your machine through the name `host.docker.internal`, which Docker resolves to the host (https://docs.docker.com/desktop/features/networking/networking-how-tos/, read 2026-09-30 from the docs source). Keep `BENCH_OLLAMA_BASE_URL` **unset**: the host processes then use `http://localhost:11434/v1`, and the kit's stack settings rewrite it to `host.docker.internal` for the containers. If a container cannot reach Ollama (part G of `docs/BENCHMARK.md`), the owner's runbook (section 9.8, step 2) says to set `OLLAMA_HOST=0.0.0.0` the same way; that opens Ollama to every network interface of your machine, which is your decision. I did not test this path.

---

## 2. Get the project and configure it

In PowerShell. Keep the repository on a normal local drive, in a short path (for example `C:\work\rag-email`), not in OneDrive or another synced folder.

```powershell
mkdir C:\work; cd C:\work
git clone https://github.com/leluc212/AgentMailGuard.git rag-email
cd rag-email
git checkout <BRANCH>          # the owner gives you the branch name; after the merge it is main
Copy-Item .env.example .env
notepad .env
```

Set exactly the lines of `docs/BENCHMARK.md` part C. Save the file as UTF-8 (not "UTF-8 with BOM") with Unix or Windows line endings; either reads fine. **Never share `.env`.**

Check that nothing is exported (this prints nothing when you are clear, and never a value):

```powershell
Get-ChildItem Env: | Where-Object { $_.Name -match '^(LLM__|EMBEDDING__|RETRIEVAL__|SUMMARIZATION__|ROUTING__|BENCH_SUMMARIZER_MODEL)' } | Select-Object -ExpandProperty Name
```

If a name is printed, remove it: `Remove-Item Env:NAME` for this window, and delete it from your user environment variables (Settings, "Edit environment variables for your account") so it does not come back.

Check the two old lines are not in `.env`:

```powershell
Select-String -Path .env -Pattern '^(SUMMARIZATION__SUMMARIZER_MODEL|ROUTING__CONFIGURED_CONSUMERS)=' | ForEach-Object { $_.LineNumber }
```

No output means you are clear. Choose and write down the reader model as in part C of `docs/BENCHMARK.md`.

Now the classifier the owner sent you. Save it, for example, in Downloads, copy it into the repository, and check its fingerprint:

```powershell
Copy-Item "$env:USERPROFILE\Downloads\l1_injection_clf_v1.joblib" evaluation\mailguard_bench\pinned\
(Get-FileHash evaluation\mailguard_bench\pinned\l1_injection_clf_v1.joblib -Algorithm SHA256).Hash
```

The hash must be `8fc1cbe74a599ab870a10ca5ff43f4a6d80b3e2273e36c7ed163c637a1d40103` (PowerShell prints the letters in upper case; that is the same number). Any other value means it is not the pinned file: ask the owner again. Git ignores that exact path, so a commit cannot pick it up. (Get-FileHash uses SHA256 by default: https://learn.microsoft.com/powershell/module/microsoft.powershell.utility/get-filehash, read 2026-10-01.)

---

## 3. The commands, without `make`

Every `make bench-*` target runs a `python -m` command under one overlay: AgentMailGuard at its pinned commit, installed on top of this repository's environment. In PowerShell, set three variables for the window, and put the overlay in a variable so the commands stay short:

```powershell
$repo = (Get-Location).Path
$env:MAILGUARD_DIR = Join-Path (Split-Path $repo -Parent) "AgentMailGuard-bench"
$env:MAILGUARD_COMMIT = "1a3ef62b7368703c22c3f90111abdde0678d5617"
$env:MAILGUARD_ARTIFACTS = Join-Path $repo "evaluation\mailguard_bench\pinned"
$mg = @("run", "--project", ".", "--with-editable", $env:MAILGUARD_DIR, "python", "-m")
```

The guard at the pinned commit (once; this is what `make mailguard-worktree` does, and after the single-repository merge the guard is in `agentmailguard\` and this step goes away, with `$env:MAILGUARD_DIR` set to that folder):

```powershell
git fetch origin feature/mailguard-defense-stack
git worktree add --detach $env:MAILGUARD_DIR $env:MAILGUARD_COMMIT
git -C $env:MAILGUARD_DIR rev-parse HEAD     # must print the commit above
```

Then, in the same window (the variables last only for this window: set them again in a new one):

```powershell
# the doctor: fix every FAIL it prints (add --model-profile qwen2.5-7b for a local model)
uv @mg evaluation.mailguard_bench.kit.doctor --model-profile gpt-4o-mini --reader <your reader model>

# once: checks the guard and the pinned inputs (the classifier too), runs the guard smoke, brings the stack up (slow the first time)
uv @mg evaluation.mailguard_bench.kit.campaign setup

# one model, completely, then the next (gpt-4o-mini, then qwen2.5-7b, then llama-3.1-8b-local)
uv @mg evaluation.mailguard_bench.kit.campaign run --model-profile gpt-4o-mini --run 2026-10-02-gpt4omini-live --concurrency 2
ollama run qwen2.5:7b-instruct "Reply with OK"
uv @mg evaluation.mailguard_bench.kit.campaign run --model-profile qwen2.5-7b --run 2026-10-02-qwen25-live --concurrency 1
ollama run llama3.1:8b "Reply with OK"
uv @mg evaluation.mailguard_bench.kit.campaign run --model-profile llama-3.1-8b-local --run 2026-10-02-llama31-local-live --concurrency 1

# a small trial first, under a throwaway name (then delete its folder)
uv @mg evaluation.mailguard_bench.kit.campaign run --model-profile gpt-4o-mini --run trial-gpt --configs C0,C0T,C7 --limit 5

# the meaning column, for a finished run (the two LLM__ variables are for this command only; remove them afterwards)
$env:LLM__PROVIDER = "openai"; $env:LLM__OPENAI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
uv @mg evaluation.mailguard_bench.kit.campaign report --run 2026-10-02-gpt4omini-live --reader <your reader model>
Remove-Item Env:LLM__PROVIDER, Env:LLM__OPENAI_BASE_URL

# the results zip
uv @mg evaluation.mailguard_bench.kit.campaign package --run 2026-10-02-gpt4omini-live
```

`--configs` takes a comma-separated list and defaults to the v2 list; `--limit` runs only the first N cases of each config. To resume a stopped run, run the same `run` command again. The rules of `docs/BENCHMARK.md` part D apply: one model completely before the next; while a run is going do not edit `.env`, rebuild the images or commit; keep the laptop plugged in and awake (set "never sleep" on power under Settings, System, Power & battery, or with `powercfg /change standby-timeout-ac 0`, which Microsoft's reference describes as taking minutes; I did not read that `0` means never).

After every `git pull` run `kit.campaign setup` again: the images are labelled with the commit they were built from, and `run` refuses containers built from another commit than your checkout (or a checkout with a modified tracked file).

If you installed `make`, `make bench-doctor`, `make bench-setup` and `make bench-run MODEL=gpt-4o-mini RUN=2026-10-02-gpt4omini-live` do the same as the commands above, with the three variables set by the Makefile itself.

---

## 4. Send the results

`package` writes `bench-results-<RUN>.zip` in the repository folder, with `raw/` and `kit-log.jsonl`, and refuses to write it if a key is in any file. Send the zip to the owner. To commit the run to a branch instead (the `raw/` folder stays out of git on purpose):

```powershell
git switch -c bench/2026-10-02-gpt4omini-live
git add evaluation/results/mailguard_bench/2026-10-02-gpt4omini-live
git commit -m "bench: results of 2026-10-02-gpt4omini-live"
git push -u origin bench/2026-10-02-gpt4omini-live
```

---

## What was not exercised on Windows

- The whole kit run: the `campaign` commands, the doctor and the guard-worker have only been run on Linux. The kit stops the guard-worker with a signal chosen for the operating system (`kit/system.py`); that choice has not been tried on Windows.
- Docker Desktop and the stack on Windows: the compose file is used as on Linux, with `host.docker.internal` from `extra_hosts`.
- Containers reaching Ollama on the Windows host (above), and the `OLLAMA_HOST=0.0.0.0` fallback.
- `make` from `ezwinports.make` with this Makefile's shell constructs.
- Path handling: run folders and the `uv --with-editable` overlay with Windows paths.
- Durations: there are no measured times for this laptop (the estimate at the top is from the owner's desktop); `evaluation/results/mailguard_bench/<RUN>/kit-log.jsonl` will show them.

If something fails here and works in the WSL2 route, the WSL2 route is the reference. Send the owner the doctor's output and the last lines of `kit-log.jsonl` (never `.env`).

---

## Sources (read 2026-09-30, except where a line says 2026-10-01)

- Docker Desktop for Windows, system requirements and install: https://docs.docker.com/desktop/setup/install/windows-install/ (source file: https://raw.githubusercontent.com/docker/docs/main/content/manuals/desktop/setup/install/windows-install.md)
- Docker Desktop networking, `host.docker.internal`: https://docs.docker.com/desktop/features/networking/networking-how-tos/ (source file in github.com/docker/docs, `content/manuals/desktop/features/networking/networking-how-tos.md`)
- WSL install and `.wslconfig`: https://learn.microsoft.com/en-us/windows/wsl/install and https://learn.microsoft.com/en-us/windows/wsl/wsl-config
- Git for Windows: https://gitforwindows.org/ ; `core.autocrlf`: https://git-scm.com/docs/git-config
- uv: https://docs.astral.sh/uv/getting-started/installation/
- `ezwinports.make` (winget package): https://github.com/microsoft/winget-pkgs/tree/master/manifests/e/ezwinports/make
- PowerShell `Get-FileHash`: https://learn.microsoft.com/powershell/module/microsoft.powershell.utility/get-filehash (source file read 2026-10-01 in github.com/MicrosoftDocs/PowerShell-Docs)
- Ollama for Windows and its FAQ: https://github.com/ollama/ollama/blob/main/docs/windows.mdx and https://github.com/ollama/ollama/blob/main/docs/faq.mdx
- `powercfg /change`: https://learn.microsoft.com/en-us/windows-hardware/design/device-experiences/powercfg-command-line-options
