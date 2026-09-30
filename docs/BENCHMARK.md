# Running the v2 benchmark on your laptop

This guide is for the teammate who runs the benchmark on a Windows 11 laptop. You run three models, one after the other, through the same pipeline the owner built. Everything below is typed in an **Ubuntu window inside Windows** (WSL2), except the few steps marked "in PowerShell".

```
  Windows 11 Home laptop
  ┌─────────────────────────────────────────────────────────────────────────┐
  │  PowerShell (admin) ── wsl --install, .wslconfig, powercfg              │
  │                                                                         │
  │  WSL2 · Ubuntu ──────────────────────────────────────────────────────┐  │
  │   ~/work/rag-email   (git clone, LF line endings)                    │  │
  │   make bench-doctor ─ bench-setup ─ bench-run ─ bench-report ─ package  │
  │        │                    │                                        │  │
  │        │            Docker Engine (inside Ubuntu)                    │  │
  │        │             └ Postgres, RabbitMQ, MinIO, API, workers        │  │
  │        │            guard-worker (a process of the kit)              │  │
  │        │            Ollama (Qwen, Llama) ── your RTX 4050, 6 GB      │  │
  │        ▼                                                             │  │
  │   OpenAI API (gpt-4o-mini)        Gemini API (embeddings, always)    │  │
  └─────────────────────────────────────────────────────────────────────────┘
```

## What you need

- **An OpenAI API key** with credit on the account (for `gpt-4o-mini`). It goes in `.env` as `BENCH_OPENAI_API_KEY`.
- **A Gemini API key** from Google AI Studio (https://aistudio.google.com/app/apikey). Every run uses it for embeddings, whichever model is under test. It goes in `.env` twice (see part C).
- **The Layer-1 classifier file, `l1_injection_clf_v1.joblib`, from the owner.** It is **not in git** (ADR-0012 decision 15: a dataset that went into its training declares no license, so the file is not redistributed, see `evaluation/mailguard_bench/pinned/NOTICE.md`). The owner sends it to you privately. It is 28 MB. Do not put it in a public link, a chat group or the repository. You place it in step D1.
- **Disk:** about 63 GB free on this laptop. The doctor warns below 25 GB: the Docker images, the two Ollama models (several GB each) and the run folders are the big parts.
- **A charger and a laptop that stays awake** for the whole run (part A, last step).
- **Time:** a full model is 9 configs x 550 cases = 4,950 case runs. A 7-case smoke run was measured on the owner's desktop (an RTX 3060 with 12 GB): about 3 s per case and config for `gpt-4o-mini` at concurrency 2, about 10 s for Qwen2.5-7B and about 9 s for Llama-3.1-8B at concurrency 1. That makes roughly **4 to 6 hours for `gpt-4o-mini`** and **12 to 15 hours for each local model** on that desktop, about 28 to 36 hours for the three. **This is an estimate from a 7-case smoke run, not a measurement of a full run**, and your laptop's GPU has 6 GB (part E), so the two local models will take longer here. The kit writes `evaluation/results/mailguard_bench/<RUN>/kit-log.jsonl`, one JSON line per finished step with its times. After your first model, the lines of that file are the real estimate for the next two; please send it back with the results (part F). A stopped run resumes where it stopped (D5), so the hours do not need to be in one sitting.
- **A decision from the owner:** which git branch to check out (part B) until the code is on `main`.
- **A decision from you:** the reader model of the "meaning" column (part C). Write it down before the first run.

A rule for the whole guide: **never paste the contents of `.env` anywhere**, not in chat, not in an email, not in a screenshot. The commands below print only the *names* of your keys, never their values.

---

## A. Windows setup (once)

### A1. Install WSL2 with Ubuntu

Docker Desktop's installation page lists Windows 11 Enterprise, Pro or Education (23H2 or newer) for its WSL 2 backend, and does not list Home. Your laptop runs Home, so this guide does **not** use Docker Desktop: it installs Docker Engine inside Ubuntu instead (ADR-0012 decision 16: Docker Engine inside WSL2 Ubuntu is this kit's primary route, and Docker Desktop with WSL integration stays an option only on the editions its page lists). (If you ever move to a Pro laptop, the native-Windows guide `docs/benchmark-windows-native.md` has the Docker Desktop route.)

In PowerShell, opened with "Run as administrator":

```powershell
wsl --install
```

Restart the laptop when it asks. The first time Ubuntu starts, a window opens, unpacks for a minute, and asks for a Linux user name and a password. Choose any; you need the password for `sudo` below. `wsl --install` installs the Ubuntu distribution, and sets new installs to WSL 2.

Check it (PowerShell):

```powershell
wsl --version
wsl -l -v
```

`wsl -l -v` must show Ubuntu with `VERSION 2`. If it shows `1`, run `wsl --set-version Ubuntu 2`.

### A2. Check that systemd runs

The current Ubuntu from `wsl --install` runs systemd by default. Docker and Ollama run as systemd services, so check (Ubuntu window):

```bash
ps -p 1 -o comm=
```

It must print `systemd`. If it does not, open the file `sudo nano /etc/wsl.conf`, add these two lines, save, and run `wsl --shutdown` in PowerShell, then open Ubuntu again:

```ini
[boot]
systemd=true
```

### A3. Give WSL enough memory

By default WSL 2 gets half of the laptop's RAM. The stack (Postgres, RabbitMQ, MinIO, the workers, a reranker model) and a local model on the CPU side need more. On a 24 GB laptop, try this. The numbers are a starting point, not a measurement. Create the file `%UserProfile%\.wslconfig` (for example `notepad $env:UserProfile\.wslconfig` in PowerShell):

```ini
[wsl2]
memory=16GB
processors=16
swap=8GB
```

Apply it by closing every Ubuntu window and running, in PowerShell:

```powershell
wsl --shutdown
```

Open Ubuntu again and check with `free -h`: the total should be close to 16 GB. The doctor fails if Docker sees less than 8 GB.

### A4. Install Docker Engine inside Ubuntu

These are Docker's own instructions for Ubuntu (https://docs.docker.com/engine/install/ubuntu/), typed in the Ubuntu window:

```bash
sudo apt update
sudo apt install ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

sudo tee /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF

sudo apt update
sudo apt install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

Let your user run Docker without `sudo` (Docker's "post-installation steps"):

```bash
sudo groupadd docker          # it may already exist; that is fine
sudo usermod -aG docker $USER
newgrp docker
sudo systemctl enable docker.service containerd.service
docker run --rm hello-world
docker compose version
```

`docker compose version` must say `v2.x`. Close the Ubuntu window and open a new one once, so that every new shell has the group.

### A5. Install the other tools

```bash
sudo apt install -y make git curl
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Open a new Ubuntu window, then check `uv --version` and `make --version`. (`uv` installs the right Python by itself.)

### A6. Keep the laptop awake

A sleeping laptop stops the run, and Docker and WSL come back in a confused state. **Plug in the charger and leave it plugged in.** In PowerShell, tell Windows never to sleep or hibernate on AC power:

```powershell
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /change disk-timeout-ac 0
```

Microsoft's reference page for `powercfg /change` says the value is in minutes. It does not spell out that `0` means "never", but that is what Windows does. You can check what is now set with `powercfg /query`, and the same settings are in Windows Settings under System, Power & battery. The screen may turn off; that is harmless. Also set what closing the lid does (Control Panel, Power Options, "Choose what closing the lid does") to "Do nothing" while plugged in, or leave the lid open.

---

## B. Get the project (once)

Everything here is in the Ubuntu window. **Put the repository in the Linux file system, in `~/work`. Never under `/mnt/c`**: that is the Windows disk seen from Linux, it is slow, and Windows would change the line endings.

```bash
git config --global core.autocrlf input
mkdir -p ~/work && cd ~/work
git clone https://github.com/leluc212/AgentMailGuard.git rag-email
cd rag-email
git checkout <BRANCH>        # the owner tells you the branch name; after the merge it is main
```

`core.autocrlf input` means: never convert line endings when files are checked out. The repository also has a `.gitattributes` file that keeps every text file LF on every machine. If the repository is private, the owner has to give your GitHub account access, and GitHub asks you to sign in on `git clone`.

The 550 benchmark cases come with the clone, so you do **not** run `make mailguard-cases`, and you never run `make mailguard-prep` (the classifier is the owner's file, step D1):

- `evaluation/datasets/mailguard/cases.jsonl` and `manifest.json`: the 550 benchmark cases (committed, with their license notices in `evaluation/mailguard_bench/pinned/NOTICE.md`).

The Layer-1 classifier, `evaluation/mailguard_bench/pinned/l1_injection_clf_v1.joblib`, is **not in git**: you get it from the owner and place it in step D1. `evaluation/mailguard_bench/pinned/SHA256SUMS` lists the fingerprints of all of them, the classifier's included, and `NOTICE.md` next to it says where the data came from, under which licenses, and why the classifier is not in git.

---

## C. Configure (once)

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

Keep it to exactly these lines. **Edit the line that is already in the file; do not add a second line with the same name.** `<...>` is where you paste your own value.

```dotenv
BENCH_OPENAI_API_KEY=<your OpenAI key>          # gpt-4o-mini only
LLM__OPENAI_API_KEY=<your Gemini API key>       # embeddings for EVERY run
EMBEDDING__MOCK=false
EMBEDDING__MODEL_NAME=gemini-embedding-001
EMBEDDING__DIMENSION=1536
EMBEDDING__BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
EMBEDDING__API_KEY=<the same Gemini key>
RETRIEVAL__RETRIEVAL_TIMEOUT_MS=3000
RETRIEVAL__CATEGORY_FILTER_ENABLED=false        # the benchmark only: each case files its documents under its own category
LLM__TIMEOUT_S=60
```

(`BENCH_OPENAI_API_KEY` is a new line: add it. The others already exist in `.env.example`, some with other values.)

Two lines must **not** be in `.env`. A fresh copy of `.env.example` has neither, but check (this prints nothing when you are clear):

```bash
grep -nE '^(SUMMARIZATION__SUMMARIZER_MODEL|ROUTING__CONFIGURED_CONSUMERS)=' .env | cut -d= -f1
```

And none of these may be **exported in your shell** (a variable exported in the shell beats the `.env` file, so the Gemini key could end up as the OpenAI key of a run). This must print nothing:

```bash
env | grep -E '^(LLM__|EMBEDDING__|RETRIEVAL__|SUMMARIZATION__|ROUTING__|BENCH_SUMMARIZER_MODEL)' | cut -d= -f1
```

If it prints names, find the `export` in `~/.bashrc` or `~/.profile`, remove it and open a new window.

To see that your key lines are there without showing a value:

```bash
grep -nE '^(BENCH_OPENAI_API_KEY|LLM__OPENAI_API_KEY|EMBEDDING__API_KEY)=.' .env | cut -d= -f1
```

**Never share `.env`.** The kit never prints a key, and `make bench-package` refuses to write the results zip if a key of yours appears in any file of the run.

### Choose the reader model now

The "meaning" column of the report (part D) asks a *reader model* to judge what each attack draft would do. You choose that model, and it may **not** be one of the benchmarked models (`gpt-4o-mini`, `qwen2.5:7b-instruct`, `llama3.1:8b`, or the Gemma test profile). The rubric and the reader are fixed before the first run: changing either after you have seen a result means new runs. So: pick one, and write it down with today's date in a file outside the repository (for example `~/reader.txt`) before you start. The owner's decision is in `docs/adr/0012-post-review-v2-done-fixes-and-main.md`, decision 7; `make bench-doctor` refuses a benchmarked model.

The reader is reached through the `LLM__*` settings of the process that runs it. To use a Gemini model with the Gemini key you already have, the report step needs `LLM__PROVIDER=openai` and `LLM__OPENAI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai` as well (part D shows where). Ask the owner if you want a different provider.

---

## D. Check, set up, run

### D1. Put the guard and the classifier in place (once)

```bash
make mailguard-worktree
```

`make mailguard-worktree` puts the guard (AgentMailGuard) next to the repository, in `../AgentMailGuard-bench`, at the exact commit the benchmark pins (`1a3ef62b7368703c22c3f90111abdde0678d5617`); it does nothing if the folder is already there at that commit. After the owner merges everything into one repository, the guard is inside it (`agentmailguard/`) and this line is no longer needed. `make bench-setup` (D3) only checks the guard, it does not create it, and the doctor (D2) fails on a missing guard.

Now the classifier. The owner sends you `l1_injection_clf_v1.joblib` privately (it is not in git, see "What you need"). Save it on Windows, for example in Downloads, copy it into the repository folder (replace `<your Windows name>`), and check its fingerprint:

```bash
cp /mnt/c/Users/<your Windows name>/Downloads/l1_injection_clf_v1.joblib evaluation/mailguard_bench/pinned/
sha256sum evaluation/mailguard_bench/pinned/l1_injection_clf_v1.joblib
```

The second command must print `8fc1cbe74a599ab870a10ca5ff43f4a6d80b3e2273e36c7ed163c637a1d40103`; any other number means this is not the pinned file, so ask the owner again (the doctor refuses it too). The file stays in that folder: git ignores that exact path, so `git status` does not show it and a commit cannot pick it up. **Never commit, push or share it.** Once the file is there, the Makefile reads the classifier from that folder by itself (`MAILGUARD_ARTIFACTS` defaults to it), so you export nothing. If you ever exported `MAILGUARD_ARTIFACTS` yourself, unset it; the doctor prints the folder it found.

### D2. The doctor

```bash
make bench-doctor
```

It prints one line per check, `ok`, `WARN` or `FAIL`, and under every WARN and FAIL a line that starts with `fix:`. Fix every `FAIL`, then run it again until it exits cleanly. It checks, among other things: that you are in WSL2 and not under `/mnt/c`, that no file has Windows line endings, Docker (command, daemon, Compose v2, 8 GB of memory), disk space (inside Ubuntu, and on the Windows C: drive that holds Ubuntu's virtual disk), `uv`, Python 3.12, that `.env` has each key (by name), that nothing is exported in the shell, the pinned guard and the inputs in git, the classifier you placed (its sha256; a missing file is a `FAIL` that says to ask the owner) and the folder the runs will read it from, scikit-learn 1.9.1 (the classifier was made with it), that the ports the stack needs are free, and the reader model.

For a model you are about to run, name it, and the doctor also adds that model's key and, for a local model, the Ollama checks (it answers, it has the model, the containers can reach it, the model is loaded) and the GPU check (part E):

```bash
uv run python -m evaluation.mailguard_bench.kit.doctor --model-profile qwen2.5-7b --reader <your reader model>
```

The doctor changes nothing. It runs only read-only Docker commands.

### D3. Set up (once)

```bash
make bench-setup
```

`make bench-setup` checks the guard is at the pinned commit, checks the pinned inputs (the classifier included), runs the guard's smoke test (no model calls) and brings the stack up and waits until every container is healthy. **The first time is slow and needs the network**: the images are built and the reranker model is downloaded into them. Later runs do not download anything. The images are labelled with the commit of your checkout. **Run `make bench-setup` again after every `git pull` (or any change to the repository)**: `make bench-run` refuses, and names the services, when a container was built from another commit than your checkout, or when a tracked file is modified, because the containers and the runner would then be different programs under one commit.

### D4. One model at a time

Run the models **in this order, and finish one completely (all its configs, the retry pass, the reports) before you start the next**:

1. `gpt-4o-mini`
2. `qwen2.5-7b` (local, part E first)
3. `llama-3.1-8b-local` (local, part E first)

A `RUN` is the name of the results folder. Use one name per model, with today's date, for example:

```bash
make bench-run MODEL=gpt-4o-mini RUN=2026-10-02-gpt4omini-live CONCURRENCY=2
```

`CONCURRENCY=2` is for the API model; give the two local models `CONCURRENCY=1` (one GPU answers one request at a time). **Before each local-model run, load the model** (`ollama run <model> "Reply with OK"`, part E5): the runner refuses a model that Ollama has not loaded. The configs default to the v2 list `C0, C0T, C1` to `C7` (`C0` is rag-email alone; `C0T` the guard's reply template with no layer active; `C1` to `C5` one detection layer each (L1, L2, L3, L3b, L4) together with the policy layer L5; `C6` the policy layer alone; `C7` the full guard); `CONFIGS=C0,C7` runs only those, and `LIMIT=5` runs only the first five cases of each config.

**Do a small trial first**, under a throwaway name, and read what it says:

```bash
make bench-run MODEL=gpt-4o-mini RUN=trial-gpt CONFIGS=C0,C0T,C7 LIMIT=5 CONCURRENCY=1
```

It costs a few calls. If it ends without a `FAIL`, delete the trial folder (`rm -r evaluation/results/mailguard_bench/trial-gpt`): a trial folder is never a result. `docs/demo-runbook.md` section 9.9 step 5 explains what a good preflight looks like (for example that cases reach drafting and that retrieval did not fall back to lexical).

What the run does for you, in the order of the owner's runbook (section 9.9, steps 3 to 7): it writes the model's container settings and recreates the four model-calling containers with them; for each config it switches which process drafts (the `ai-worker` container for `C0`, the guard-worker, a process on your machine, for every other config), waits until it is ready, runs the cases, and stops it; then it makes one retry pass over the configs that left errors, and builds the reports.

While a run is going:

- **Do not** run `make up`, `docker compose up` or `docker compose down`, edit `.env`, rebuild the images or commit to the repository. Every config records a fingerprint of these, and a resume under other settings stops.
- Keep the laptop plugged in and awake (part A6).
- Close other heavy programs.

### D5. Resume

If something stops (Ctrl+C, a power cut, sleep, a network failure), run **the same command again**. Each case is written as it finishes, a rerun skips the cases it has, and it retries cases recorded as errors. The kit log says which configs are finished, so they are not started again. Ctrl+C stops the guard-worker cleanly and prints the command that resumes.

### D6. What the kit logs

- `evaluation/results/mailguard_bench/<RUN>/kit-log.jsonl`: one line per step, with its status and times.
- `evaluation/results/mailguard_bench/<RUN>/raw/guard-worker.<config>.log`: the guard-worker's output for a config.
- `evaluation/results/mailguard_bench/<RUN>/raw/`: one row per case (this folder holds attack emails and drafts; it is never committed, only zipped in part F).

Nothing the kit prints or logs contains a key.

### D7. Reports and the meaning column

The run builds `report.md` itself. The meaning column needs your reader model. Run it for a finished model like this (the two `LLM__` words before `make` apply to that one command only; nothing is exported):

```bash
LLM__PROVIDER=openai LLM__OPENAI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai \
  make bench-report RUN=2026-10-02-gpt4omini-live READER=<your reader model>
```

The report explains its numbers in a table at the top: the guard's rate of successful attacks (Guard ASR) is the one the owner's target is judged on, the pipeline rate counts attacks that triage already stopped, and the meaning-based rate is a second reading of the same drafts (quote both side by side).

---

## E. The local models (Qwen and Llama)

Both run on Ollama inside Ubuntu, using your RTX 4050. **The laptop's GPU has 6 GB.** A 7B or 8B model in 4 bit is about 4.7 to 5 GB before its context, so part of the model will run on the CPU. That is expected and is recorded, but it makes the numbers and the speed different from the owner's 12 GB desktop: say so when you send results, and expect these two runs to be slow.

### E1. The GPU driver (Windows, not Ubuntu)

Install the current NVIDIA driver on **Windows** (from nvidia.com or GeForce Experience). NVIDIA's WSL guide says it is the only driver you need and that you must not install a Linux display driver inside WSL: Windows hands the GPU to Ubuntu. Check in Ubuntu:

```bash
nvidia-smi
```

It must print your RTX 4050. (If the shell cannot find it, it lives at `/usr/lib/wsl/lib/nvidia-smi`.)

### E2. Install Ollama

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

### E3. Keep-alive and context (runbook section 9.8, step 2)

With a keep-alive of `0`, Ollama unloads the model after every call, and each call then pays the reload; and Ollama's default context is 4096 tokens (its FAQ). The owner's runs use a keep-alive of 30 minutes and a context of 32768. Set both:

```bash
sudo systemctl edit ollama
```

In the editor that opens, type these lines between the comment markers, save and leave:

```ini
[Service]
Environment="OLLAMA_KEEP_ALIVE=30m"
Environment="OLLAMA_CONTEXT_LENGTH=32768"
```

Then `sudo systemctl daemon-reload && sudo systemctl restart ollama`. The owner will tell you if a smaller context should be used on 6 GB (the design is to size it to the measured prompts, never to truncate one); use that number if the owner gives one.

### E4. Make Ollama reachable from the containers (runbook section 9.9, step 2)

Ollama listens on `127.0.0.1` only. Inside a container `localhost` is the container itself, so the containers reach the laptop's Ollama through the name `host.docker.internal`, which Docker Engine resolves to the **docker0 address** (the Docker bridge, normally `172.17.0.1`). Nothing listens there yet, and **the bridge address only exists while Docker runs**, so start Docker first. There are two ways to fix it. Use **one** of them: route 1 if you have `sudo` and want it permanent, route 2 if you prefer to leave Ollama alone. The doctor's `ollama` check tells you whether the containers can now reach it.

**Route 1: the systemd override (runbook 9.9 step 2; needs `sudo`).** Ollama listens on the bridge address, and only there:

```bash
BRIDGE_IP=$(ip -4 -o addr show docker0 | awk '{print $4}' | cut -d/ -f1)   # 172.17.0.1 normally
sudo mkdir -p /etc/systemd/system/ollama.service.d
printf '[Unit]\nAfter=docker.service\nWants=docker.service\n\n[Service]\nEnvironment="OLLAMA_HOST=%s:11434"\n' "$BRIDGE_IP" \
  | sudo tee /etc/systemd/system/ollama.service.d/bridge.conf
sudo systemctl daemon-reload && sudo systemctl restart ollama
systemctl show ollama -p Environment      # OLLAMA_HOST=<bridge ip>:11434, beside the two lines of E3
curl -s "http://$BRIDGE_IP:11434/api/version"
```

Then two more things:

1. In `.env`, add `BENCH_OLLAMA_BASE_URL=http://<the bridge ip>:11434/v1` (the address `echo $BRIDGE_IP` prints).
2. The `ollama` command finds the server through the same address. In every Ubuntu window where you use `ollama pull`, `ollama run`, `ollama ps` or `ollama show`, first run `export OLLAMA_HOST="$BRIDGE_IP:11434"` (set `BRIDGE_IP` first as above, or type the address).

`localhost:11434` no longer answers after this. That is intended: the guard-worker, the runner and the containers all use the bridge address. To undo it later: `sudo rm /etc/systemd/system/ollama.service.d/bridge.conf && sudo systemctl daemon-reload && sudo systemctl restart ollama`, then remove `BENCH_OLLAMA_BASE_URL` from `.env` and `unset OLLAMA_HOST`.

**Route 2: a forwarder bound to the docker0 address only (no changes to Ollama).** `socat` copies what arrives on the docker0 address to Ollama on `127.0.0.1`. Ollama, the `ollama` command and `localhost:11434` stay exactly as they are, and you leave `BENCH_OLLAMA_BASE_URL` unset (the kit's stack settings turn `localhost` into `host.docker.internal` for the containers). Install it once, then start it **after Docker is up**:

```bash
sudo apt install -y socat
BRIDGE_IP=$(ip -4 -o addr show docker0 | awk '{print $4}' | cut -d/ -f1)
nohup socat TCP-LISTEN:11434,bind=$BRIDGE_IP,reuseaddr,fork TCP:127.0.0.1:11434 >/dev/null 2>&1 &
curl -s "http://$BRIDGE_IP:11434/api/version"
```

The general form is `socat TCP-LISTEN:11434,bind=<docker0 address>,reuseaddr,fork TCP:127.0.0.1:11434`: the forwarder is bound to the docker0 address only, so it is not open on your other network interfaces. It must keep running for the whole run: it dies when Ubuntu shuts down (`wsl --shutdown`, a restart), so start it again before resuming, and stop it when you are done with `pkill -f 'socat TCP-LISTEN:11434'`.

### E5. Pull the models, and load the one you are about to run

```bash
ollama pull qwen2.5:7b-instruct
ollama pull llama3.1:8b
```

**Before each local-model run, load the model**, in the shell where the `ollama` command reaches the server (route 1: with `OLLAMA_HOST` exported as in E4):

```bash
ollama run qwen2.5:7b-instruct "Reply with OK"
ollama ps
```

The live runner **refuses a model that Ollama has not loaded** (`<model> is not loaded on the Ollama at ...`): it reads the model's real context length from the loaded model and records it with the run. The keep-alive of E3 keeps the model loaded for 30 minutes after the last call; if you stop for longer, or start a different model, load it again before the run or the resume. The doctor's `ollama model loaded` line warns when it is not.

`ollama ps` shows, in the `PROCESSOR` column, where the loaded model sits: `100% GPU`, `100% CPU`, or a split such as `48%/52% CPU/GPU`. On 6 GB expect a split. **Write the split down with the run**: a CPU share means the run's latencies are not comparable with the desktop's. Run Llama **after** the Qwen run is finished (one 6 GB GPU cannot hold both): stop Qwen, then load Llama:

```bash
ollama stop qwen2.5:7b-instruct
ollama run llama3.1:8b "Reply with OK"
ollama ps
```

### E6. Record the server state with each local run

The run's settings record the endpoint and the model but not Ollama's own settings. The runbook asks you to save them next to each local run (replace `<RUN>` and `<model>`):

```bash
R=evaluation/results/mailguard_bench/<RUN>
{ ollama --version; ollama show <model>; ollama ps; systemctl show ollama -p Environment; } > $R/ollama-state.txt
```

Run it after the model has answered its first call, so `ollama ps` shows the model.

Then load the model, check the machine for it, and start the run:

```bash
ollama run qwen2.5:7b-instruct "Reply with OK"
uv run python -m evaluation.mailguard_bench.kit.doctor --model-profile qwen2.5-7b --reader <your reader model>
make bench-run MODEL=qwen2.5-7b RUN=2026-10-02-qwen25-live CONCURRENCY=1
```

and, when it is completely done, the same for Llama (load it first):

```bash
ollama run llama3.1:8b "Reply with OK"
make bench-run MODEL=llama-3.1-8b-local RUN=2026-10-02-llama31-local-live CONCURRENCY=1
```

Do a small trial first for each local model too (D4), after loading it: `make bench-run MODEL=qwen2.5-7b RUN=trial-qwen CONFIGS=C0,C0T,C7 LIMIT=5 CONCURRENCY=1`. A local model answers slowly (about 10 s per case on the owner's desktop), so the trial is a few minutes, not seconds.

---

## F. Send the results back

When a model's run is complete:

```bash
make bench-package RUN=2026-10-02-gpt4omini-live
```

This writes `bench-results-2026-10-02-gpt4omini-live.zip` in the repository folder: the whole run folder including `raw/` and `kit-log.jsonl`, and never a key (it refuses to write the zip if a key appears in a file). Send the zip to the owner by the route the owner names. Copy it to Windows if you need to attach it: `cp bench-results-*.zip /mnt/c/Users/<your Windows name>/Desktop/`.

Or commit the run to a branch and push it, as the owner does. `raw/` stays out of git on purpose (it holds attack emails), so it is only in the zip:

```bash
git switch -c bench/2026-10-02-gpt4omini-live
git add evaluation/results/mailguard_bench/2026-10-02-gpt4omini-live
git commit -m "bench: results of 2026-10-02-gpt4omini-live"
git push -u origin bench/2026-10-02-gpt4omini-live
```

Include in your message: which model each run used, the reader model you chose and when, the `ollama ps` split of each local run, and any `WARN` the doctor printed.

---

### F1. After the last model

When every model's run is finished and sent: delete `.env.stack` (it holds your keys: `rm .env.stack`), and if you keep using the stack for anything but this benchmark, set `RETRIEVAL__CATEGORY_FILTER_ENABLED` back to `true` in `.env` (or delete that line) and run `make up`, so that searches are filtered by category again (runbook section 9.9, step 8).

## G. When something goes wrong

| What you see | What it means, and what to do |
|---|---|
| Doctor: `line endings: N tracked text files have CRLF` | The clone was made by Git for Windows, or under `/mnt/c`. Clone again inside Ubuntu, in `~/work` (part B). |
| `permission denied while trying to connect to the Docker daemon socket` | You are not in the `docker` group yet: `sudo usermod -aG docker $USER`, close the Ubuntu window and open a new one. |
| `Cannot connect to the Docker daemon` | Docker is not running. `sudo systemctl start docker`. If it says there is no systemd, part A2. |
| `port is already allocated` or doctor `ports: in use by another program` | Another program holds a port the stack needs (Postgres 5433, RabbitMQ 5672 and 15672, MinIO 9010 and 9011, Prometheus 9090, Grafana 3002, the API 8000, the review UI 3001, the guard-worker 8014). Find it with `ss -ltnp \| grep :<port>` inside Ubuntu, or `netstat -ano \| findstr :<port>` in PowerShell, and stop it. |
| Builds or containers are killed, `docker memory` fails | WSL has too little memory: part A3, then `wsl --shutdown`. |
| `429` from Gemini (`RESOURCE_EXHAUSTED`) | Google applies limits per project, not per key, and shows yours in AI Studio. Every case's knowledge documents and every query that needs retrieval call the embedding model in every run. The runner waits and retries; if cases are still recorded as errors, run the same command again later. Raise the limit on your Google project if it keeps happening. |
| `429` from OpenAI | OpenAI shows your limits under Settings, Organization, Limits. Retrying with waits is what the runner does; run the same command again to retry error rows. An account with no credit also fails on every call: check your balance. |
| Many rows with `retrieval_degraded` true | The Gemini embedding call ran out of its 3000 ms budget or quota. Check the Gemini limits, then rerun. |
| The laptop slept or the lid closed | The run stopped. Part A6, then run the same command again. If Docker looks stuck, `wsl --shutdown` in PowerShell, open Ubuntu, `docker ps`, and run the command again. |
| A container cannot reach Ollama (`connection refused`) | Nothing listens on the docker0 address. Route 1 of E4: `systemctl show ollama -p Environment`, and Docker must have started before Ollama. Route 2: the `socat` forwarder is not running (it stops when Ubuntu restarts), start it again. A timeout means a firewall drops traffic from Docker's network to port 11434. |
| The runner says `<model> is not loaded on the Ollama at ...` | Ollama has not loaded that model (it unloaded it after the keep-alive, or you started another one). Run `ollama run <model> "Reply with OK"` (E5), then the same `make bench-run` command again. |
| Doctor: `L1 classifier: ... is missing` or `has sha256 ...` | The classifier file is not in `evaluation/mailguard_bench/pinned/`, or it is another file. It is not in git, so `git checkout` cannot bring it back: ask the owner for `l1_injection_clf_v1.joblib` and place it as in D1. |
| Doctor: `ollama: ... it does not answer` for the docker0 address | The containers cannot reach your Ollama yet: do route 1 or route 2 of E4. |
| `the shell sets ...` or `.env` problems named by setting | Part C: unset the exported variable, or fix the `.env` line. The messages name the setting, never its value. |
| The runner refuses to start and names a missing or extra drafting consumer | A process is on when it should be off (`ai-worker` versus the guard-worker). Do not start things by hand; run the same `make bench-run` command again. |
| `make: command not found`, `uv: command not found` | Part A5, then open a new window. |

If you are stuck, send the owner the output of `make bench-doctor` and the last lines of `kit-log.jsonl`. Both are safe to share; `.env` is not.

---

## Sources

External facts in this guide come from these pages, all read on 2026-09-30:

- Docker Desktop for Windows, system requirements: https://docs.docker.com/desktop/setup/install/windows-install/ (source file read at https://raw.githubusercontent.com/docker/docs/main/content/manuals/desktop/setup/install/windows-install.md). Its WSL 2 lists name Enterprise, Pro or Education; a note on the same page says Home and Education "only allow you to run Linux containers", which is why the owner's decision is to treat Home as not supported and use Engine inside Ubuntu.
- Install WSL (`wsl --install`, `wsl -l -v`, `wsl --set-version`): https://learn.microsoft.com/en-us/windows/wsl/install
- systemd in WSL: https://learn.microsoft.com/en-us/windows/wsl/systemd
- `.wslconfig` and `wsl.conf` (memory default 50%, `wsl --shutdown`): https://learn.microsoft.com/en-us/windows/wsl/wsl-config
- Docker Engine on Ubuntu, apt repository: https://docs.docker.com/engine/install/ubuntu/
- Docker post-installation steps (docker group): https://docs.docker.com/engine/install/linux-postinstall/
- NVIDIA driver for WSL 2: https://docs.nvidia.com/cuda/wsl-user-guide/index.html
- Ollama on Linux, FAQ (context length, keep-alive, `OLLAMA_HOST`, `ollama ps`): https://github.com/ollama/ollama/blob/main/docs/linux.mdx and https://github.com/ollama/ollama/blob/main/docs/faq.mdx
- `powercfg /change`: https://learn.microsoft.com/en-us/windows-hardware/design/device-experiences/powercfg-command-line-options
- uv installation: https://docs.astral.sh/uv/getting-started/installation/
- `core.autocrlf`: https://git-scm.com/docs/git-config (text read in https://github.com/git/git/blob/master/Documentation/config/core.adoc)
- Gemini rate limits: https://ai.google.dev/gemini-api/docs/rate-limits
- OpenAI rate limits: https://developers.openai.com/api/docs/guides/rate-limits

The run times in "What you need" are the owner's own, from a 7-case smoke run on 2026-09-30/10-01 on the owner's desktop (RTX 3060, 12 GB); they are not from a page. The decisions cited are in `docs/adr/0012-post-review-v2-done-fixes-and-main.md` (15: the classifier is not redistributed; 16: Docker Engine inside WSL2 is the primary route). `socat`'s option syntax (`TCP-LISTEN`, `bind=`, `reuseaddr`, `fork`): its manual page, `man socat`, after `sudo apt install socat`.

The procedure itself is the owner's runbook, `docs/demo-runbook.md` section 9.9 (and 9.8 for the local models); this guide is the same steps, run by the kit.
