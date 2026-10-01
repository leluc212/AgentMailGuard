# Running the v2 benchmark on your laptop

This guide is for the teammate who runs the benchmark on a Windows 11 laptop. You run three models, one after the other, through the same pipeline the owner built. **All three are cloud models** (owner decision 2026-10-01, ADR-0014): `gpt-4o-mini` through the OpenAI API, and Qwen2.5-7B and Llama-3.1-8B through OpenRouter, each pinned to one provider. Nothing runs on your GPU. Everything below is typed in an **Ubuntu window inside Windows** (WSL2), except the few steps marked "in PowerShell".

```
  Windows 11 Home laptop
   PowerShell (admin) ── wsl --install, .wslconfig, powercfg
   WSL2 · Ubuntu
    ~/work/rag-email   (git clone, LF line endings; the guard is inside it, in agentmailguard/)
    make bench-doctor ─ bench-setup ─ bench-canary ─ bench-run ─ bench-report ─ bench-package
         │
         ├─ Docker Engine (inside Ubuntu): Postgres, RabbitMQ, MinIO, the API and the workers
         ├─ guard-worker (a process the kit starts and stops)
         │
         ├──▶ every LLM role, one model per run
         │      OpenAI API ───── gpt-4o-mini ............................ BENCH_OPENAI_API_KEY
         │      OpenRouter API ─ Qwen2.5-7B, pinned to Phala ............ BENCH_OPENROUTER_API_KEY
         │                     └ Llama-3.1-8B, pinned to CoreWeave, bf16   (fallbacks off)
         └──▶ embeddings: one endpoint of your choice, 1536 wide ......... EMBEDDING__API_KEY
```

## What you need

- **An OpenAI API key** with credit on the account (for `gpt-4o-mini`). It goes in `.env` as `BENCH_OPENAI_API_KEY`. Check that the account covers one model run as listed under "What one model run needs" below.
- **An OpenRouter API key with credit**, for Qwen2.5-7B and Llama-3.1-8B. It goes in `.env` as `BENCH_OPENROUTER_API_KEY`. Part E sets it up (credit, a per-key limit, the privacy settings that must allow the two pinned providers).
- **An embedding endpoint of your choice** and its key, `EMBEDDING__API_KEY` (part C): for example your OpenAI key with `text-embedding-3-small`, or a Gemini key with `gemini-embedding-001`. It must accept the `dimensions` parameter (part C; OpenAI's `text-embedding-ada-002` does not). Use the same one for all three models. A free Gemini key is not enough (part C).
- **The Layer-1 classifier file, `l1_injection_clf_v1.joblib`, from the owner.** It is **not in git** (ADR-0012 decision 15: a dataset that went into its training declares no license, so the file is not redistributed, see `evaluation/mailguard_bench/pinned/NOTICE.md`). The owner sends it to you privately. It is 28 MB. Do not put it in a public link, a chat group or the repository. You place it in step D1.
- **Disk:** the doctor warns below 25 GB free: the Docker images and the run folders are the big parts.
- **A charger and a laptop that stays awake** for the whole run (part A, last step).
- **Time, an estimate:** a full model is 9 configs x 550 cases = 4,950 case runs. For `gpt-4o-mini`, about 3 s per case and config at concurrency 2 (the owner's 7-case smoke run), so roughly **4 to 6 hours**. The two OpenRouter models were **not measured**: plan 4 to 8 hours each. The meaning column (D7) reads up to about 3,600 drafts per model, about 1 to 3 hours each. That is about 15 to 25 hours for everything: **plan an overnight window**, not a working day. The kit writes `evaluation/results/mailguard_bench/<RUN>/kit-log.jsonl`, one JSON line per finished step with its times: after your first model, its lines are the real estimate for the next two; please send it back with the results (part F). A stopped run resumes where it stopped (D5), so the hours do not need to be in one sitting.
- **A schedule.** The steps between two models need you at the keyboard; they cannot run unattended overnight. **The day before (Thursday):** the keys, OpenRouter credit (it can take up to an hour to show, E1), billing on the Google project if your embedding or your reader is a Gemini model (part C), and that your accounts cover "What one model run needs" (below). Also update your clone (part B). **Friday morning:** parts A to D3, then the doctor and the small trial for `gpt-4o-mini` (D2, D4), then its run, while you are there to watch its first configs. **After it:** the credit check, canary, probe and trial for Qwen2.5-7B (E2, E3, D4), then its run; then the same for Llama-3.1-8B, whose run can go overnight once its first configs look right. The meaning column of a finished model (`make bench-report`, D7, no Docker) may run while the next model runs: it reads another run folder. The one exception: a Gemini reader together with a Gemini embedding share one Google project's quota, so then build the meaning columns after the last run.
- **Cost, an estimate** (ADR-0014): about $1.5 to $2 per full `gpt-4o-mini` run, about $1 for Qwen2.5-7B and $2 for Llama-3.1-8B (E1). The embedding and the reader come on top, at their providers' prices. Leave room for the trials and the retry passes.
- **What one model run needs** (owner decision 2026-10-01, ADR-0014): check that your accounts cover it. Estimates from the code's calls per case and the owner's v1 run's measured tokens per call; none of the three Friday models has been measured on the full v2 run:
  - `gpt-4o-mini` (OpenAI): **about 8,000 to 11,000 requests** per run (9 configs x 550 cases: at most one reply per case, the triage model when the rules and the classifier are unsure, and the guard's model calls in `C1`, `C2`, `C4`, `C5` and `C7`), about 4 to 8 million input and 1 to 1.5 million output tokens, about $1.5 to $2.
  - Qwen2.5-7B and Llama-3.1-8B (OpenRouter): the same requests and about the same tokens per run, about $1 and $2 of credit (E1).
  - The embedding: **about 6,000 to 10,000 requests per model run**, so about 18,000 to 30,000 for the three: the 567 knowledge documents of the 100 RAG cases in each of the 9 configs (about 5,100 requests, one per document) plus one query per drafted email that retrieves (up to about 4,950), about 1 to 1.5 million tokens per model run.
  - The reader (D7): up to about 3,600 reads per model.
  - **If a limit or a credit runs out anyway**, the run stops cleanly by itself (`STOP`, E4), whatever the provider: a used-up quota, a prepaid balance at zero, a spend limit or a daily request cap of OpenAI, OpenRouter or Gemini, the embedding included. Nothing is lost: rerun **the same command** later (the next day for a daily cap); it resumes and retries the rows the stop left as errors (D5). Other models may run in between: each has its own `RUN`. Not when the next model uses the same limit (D5): the embedding's quota, or an OpenAI balance or spend limit when your embedding is on the same OpenAI account.
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

The current Ubuntu from `wsl --install` runs systemd by default. Docker runs as a systemd service, so check (Ubuntu window):

```bash
ps -p 1 -o comm=
```

It must print `systemd`. If it does not, open the file `sudo nano /etc/wsl.conf`, add these two lines, save, and run `wsl --shutdown` in PowerShell, then open Ubuntu again:

```ini
[boot]
systemd=true
```

### A3. Give WSL enough memory

By default WSL 2 gets half of the laptop's RAM. The stack (Postgres, RabbitMQ, MinIO, the workers, a reranker model) needs more. On a 24 GB laptop, try this. The numbers are a starting point, not a measurement. Create the file `%UserProfile%\.wslconfig` (for example `notepad $env:UserProfile\.wslconfig` in PowerShell):

```ini
[wsl2]
memory=16GB
processors=16
swap=8GB

[general]
instanceIdleTimeout=-1
```

`instanceIdleTimeout=-1` keeps Ubuntu running when no Ubuntu window is open. Without it, WSL shuts an idle Ubuntu down (Microsoft's default is after 15 seconds; https://learn.microsoft.com/en-us/windows/wsl/wsl-config, section `[general]`, read 2026-10-01), and Docker, the stack and a run stop with it (A6).

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

Two more things stop an overnight run:

- **Windows Update restarts.** Pause updates before the run: Start, Settings, Windows Update, "Pause updates" (up to 35 days). While updates are paused the device does not restart by itself to install them.
- **Closing the Ubuntu window** ends the commands running in it. Start each long run inside `tmux` (`sudo apt install -y tmux`, then `tmux`; detach with Ctrl+B then D, come back with `tmux attach`), so a closed window or a dropped terminal does not stop the run.
- **WSL stopping Ubuntu.** Microsoft documents that WSL shuts a distribution down about 8 seconds after its last shell window closes, unless the `[general]` setting of A3 is in place; it does not say whether a `tmux` session alone keeps Ubuntu running, and this guide has not been tried on Windows. So, for the whole run: have `instanceIdleTimeout=-1` in `.wslconfig` (A3), **keep one Ubuntu window open** (minimized is fine), and after you detach from `tmux`, check in PowerShell that `wsl --list --running` still lists Ubuntu. If a run stops anyway, resume it (D5).

---

## B. Get the project (once)

Everything here is in the Ubuntu window. **Put the repository in the Linux file system, in `~/work`. Never under `/mnt/c`**: that is the Windows disk seen from Linux, it is slow, and Windows would change the line endings.

```bash
git config --global core.autocrlf input
mkdir -p ~/work && cd ~/work
git clone https://github.com/leluc212/AgentMailGuard.git rag-email
cd rag-email
git checkout main            # the benchmark lives on main
```

**Already cloned before 2026-10-01** (for the earlier local-route kit)? The cloud route reaches `main` when the owner merges it. Before Friday, update your clone and check that it has the cloud route:

```bash
cd ~/work/rag-email
git switch main && git pull
make help | grep bench-canary     # must print a line
ls docs/adr/0014*                 # must list ADR-0014
```

If either prints nothing, the merge has not happened yet: ask the owner. After the pull, run `make bench-setup` again (D3).

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

Set these values. **Edit the line of each name that is already in the file; do not add a second line with the same name**, and leave the rest of the file as it is. `<...>` is where you paste your own value.

```dotenv
BENCH_OPENAI_API_KEY=<your OpenAI key>              # gpt-4o-mini
BENCH_OPENROUTER_API_KEY=<your OpenRouter key>      # Qwen2.5-7B and Llama-3.1-8B
EMBEDDING__MOCK=false
EMBEDDING__MODEL_NAME=text-embedding-3-small        # the embedding: your choice, see below
EMBEDDING__DIMENSION=1536
EMBEDDING__BASE_URL=https://api.openai.com/v1
EMBEDDING__API_KEY=<the key of that embedding endpoint>
RETRIEVAL__RETRIEVAL_TIMEOUT_MS=3000
RETRIEVAL__CATEGORY_FILTER_ENABLED=false            # the benchmark only: each case files its documents under its own category
LLM__TIMEOUT_S=60
LLM__OPENAI_API_KEY=<your reader's key>             # the meaning reader (below, and D7); the three runs use the BENCH_ keys
```

(`BENCH_OPENAI_API_KEY` and `BENCH_OPENROUTER_API_KEY` are new lines: add them. The others already exist in `.env.example`, some with other values. For a reader served by Anthropic, put its key in `LLM__ANTHROPIC_API_KEY` instead of `LLM__OPENAI_API_KEY`.)

### The embedding: your choice, the same for all three models

The embedding model turns the case documents and the queries into vectors. **You choose it** (ADR-0014), with four rules:

- any OpenAI-compatible `/embeddings` endpoint, with its own key in `EMBEDDING__API_KEY` (never an LLM key);
- `EMBEDDING__DIMENSION=1536`, and the endpoint must **accept the `dimensions` parameter** and return 1536 numbers: the kit sends `dimensions: 1536` with every request, also to a model that is 1536 wide anyway. OpenAI documents the parameter for `text-embedding-3` and later models (`text-embedding-3-small` and `-large`); Google's endpoint took it for `gemini-embedding-001` (checked 2026-09-29). A model that rejects it, such as OpenAI's `text-embedding-ada-002` (1536 wide, but older than the parameter), fails every embedding call. Use one of the two examples below. `make bench-run` makes **one embedding call first**, with these settings and the services' own embedder, before the stack is touched and before any model call, and refuses to start unless it returns one vector of 1536 numbers (D4): an endpoint that rejects or ignores `dimensions`, a wrong key or model, or a used-up quota stops it there. A per-minute rate limit (HTTP 429) is waited out once (at most a minute) and the call is sent again; only a second one stops it. 1536 is the width of the database column; another width needs a database migration, so the kit refuses it;
- `EMBEDDING__MOCK=false`: the benchmark never uses the fake embedder;
- **the same embedding model for all three models**: a different one changes what retrieval finds, and the three runs could no longer be compared. Every run records it (`embedding` in its meta, `manifest.json` and the report's "Run setup" section), and a resumed run refuses another one.

Two worked examples (only the four lines that differ):

```dotenv
# OpenAI text-embedding-3-small (1536 wide, and it accepts `dimensions`), with your OpenAI key
EMBEDDING__MODEL_NAME=text-embedding-3-small
EMBEDDING__BASE_URL=https://api.openai.com/v1
EMBEDDING__API_KEY=<your OpenAI key>
EMBEDDING__DIMENSION=1536
```

```dotenv
# Gemini gemini-embedding-001 at 1536 (Google returns 3072 unless asked for fewer), with a Gemini API key
EMBEDDING__MODEL_NAME=gemini-embedding-001
EMBEDDING__BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
EMBEDDING__API_KEY=<your Gemini API key>
EMBEDDING__DIMENSION=1536
```

A run embeds about 6,000 to 10,000 times ("What one model run needs"), in bursts of up to about 150 a minute at concurrency 2. A free Gemini key (1,000 requests a day when Google last published the number) runs out inside the first config: with Gemini, link billing to the project first and read its limits in AI Studio. With OpenAI the account's limits apply (platform.openai.com, Settings, Organization, Limits). If the embedding's quota runs out during a run, the run stops (E4) and the same command resumes it later.

Two lines must **not** be in `.env`. A fresh copy of `.env.example` has neither, but check (this prints nothing when you are clear):

```bash
grep -nE '^(SUMMARIZATION__SUMMARIZER_MODEL|ROUTING__CONFIGURED_CONSUMERS)=' .env | cut -d= -f1
```

And none of these may be **exported in your shell** (a variable exported in the shell beats the `.env` file, so another endpoint's key could end up as the LLM key of a run). This must print nothing:

```bash
env | grep -E '^(LLM__|EMBEDDING__|RETRIEVAL__|SUMMARIZATION__|ROUTING__|BENCH_SUMMARIZER_MODEL)' | cut -d= -f1
```

If it prints names, find the `export` in `~/.bashrc` or `~/.profile`, remove it and open a new window.

To see that your key lines are there without showing a value:

```bash
grep -nE '^(BENCH_OPENAI_API_KEY|BENCH_OPENROUTER_API_KEY|EMBEDDING__API_KEY|LLM__OPENAI_API_KEY)=.' .env | cut -d= -f1
```

**Never share `.env`.** The kit never prints a key, and `make bench-package` refuses to write the results zip if a key of yours appears in any file of the run.

### Choose the reader model now

The "meaning" column of the report (part D) asks a *reader model* to judge what each attack draft would do. You choose that model, and it may **not** be one of the benchmarked models (`gpt-4o-mini`, `qwen/qwen-2.5-7b-instruct`, `meta-llama/llama-3.1-8b-instruct`, any spelling of them such as `qwen2.5:7b-instruct` or `llama3.1:8b`, or the Gemma test profile). The rubric and the reader are fixed before the first run: changing either after you have seen a result means new runs. So: pick one, and write it down with today's date in a file outside the repository (for example `~/reader.txt`) before you start. The owner's decision is in `docs/adr/0012-post-review-v2-done-fixes-and-main.md`, decision 7; `make bench-doctor` refuses a benchmarked model.

The reader is reached through the `LLM__*` settings of the process that runs it, and its endpoint is your choice too: any OpenAI-compatible endpoint (`LLM__PROVIDER=openai`, `LLM__OPENAI_BASE_URL=<its URL>`), or `anthropic` (`LLM__ANTHROPIC_API_KEY`). Put the reader's key in `.env` as `LLM__OPENAI_API_KEY` (only the report step reads it: `make bench-run` gives every process the model profile's key instead), and give the provider and the URL on the report command line only (D7). For example a Gemini model: `LLM__PROVIDER=openai LLM__OPENAI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai`, with a Gemini key as `LLM__OPENAI_API_KEY`. A Gemini reader shares that Google project's quota with a Gemini embedding.

---

## D. Check, set up, run

### D1. Check the guard and put the classifier in place (once)

```bash
make mailguard-worktree
```

The guard (AgentMailGuard) comes with the clone, in `agentmailguard/`. `make mailguard-worktree` only checks that this folder is exactly the commit the benchmark pins (`915cb1e2b86395e4389673deb9914308cc39627b`) and unmodified, and prints `ok AgentMailGuard subtree ... @ 915cb1e...`; it changes nothing. `make bench-setup` (D3) checks the same, and the doctor (D2) fails on a guard that is not at the pin.

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

For a model you are about to run, name it: the doctor then requires that model's key (`BENCH_OPENROUTER_API_KEY` for the two OpenRouter profiles), and skips the Ollama and GPU checks, which only a local model needs (appendix L). The `embedding` line checks your four embedding lines by name, the width 1536 and `EMBEDDING__MOCK=false`, and names the model and the endpoint's host:

```bash
make bench-doctor MODEL=qwen2.5-7b-openrouter READER=<your reader model>
uv run python -m evaluation.mailguard_bench.kit.doctor --model-profile qwen2.5-7b-openrouter --reader <your reader model>
```

(The two lines are the same check; the second runs without `make`.)

The doctor changes nothing. It runs only read-only Docker commands.

### D3. Set up (once)

```bash
make bench-setup
```

`make bench-setup` checks the guard is at the pinned commit, checks the pinned inputs (the classifier included), runs the guard's smoke test (no model calls) and brings the stack up and waits until every container is healthy. **The first time is slow and needs the network**: the images are built and the reranker model is downloaded into them. Later runs do not download anything. The images are labelled with the commit of your checkout. **Run `make bench-setup` again after every `git pull` (or any change to the repository)**: `make bench-run` refuses, and names the services, when a container was built from another commit than your checkout, or when a tracked file is modified, because the containers and the runner would then be different programs under one commit.

### D4. One model at a time

Run the models **in this order, and finish one completely (all its configs and its retry pass; the kit then builds its reports) before you start the next**. The one exception: a run that stopped on a used-up quota, credit or daily cap (`STOP`, D5). The next model may run while you wait for that limit, and the stopped run resumes with its same command afterwards. Its meaning column (D7) may come later, also while the next model runs ("A schedule" in "What you need"):

1. `gpt-4o-mini`
2. `qwen2.5-7b-openrouter` (part E: credit check and canary first)
3. `llama-3.1-8b-openrouter` (part E: credit check and canary first)

A `RUN` is the name of the results folder. Use one name per model, with today's date, for example:

```bash
make bench-run MODEL=gpt-4o-mini RUN=2026-10-02-gpt4omini-live CONCURRENCY=2
make bench-run MODEL=qwen2.5-7b-openrouter RUN=2026-10-02-qwen25-openrouter-live CONCURRENCY=2
make bench-run MODEL=llama-3.1-8b-openrouter RUN=2026-10-02-llama31-openrouter-live CONCURRENCY=2
```

`CONCURRENCY=2` for all three; never more. The configs default to the v2 list `C0, C0T, C1` to `C7` (`C0` is rag-email alone; `C0T` the guard's reply template with no layer active; `C1` to `C5` one detection layer each (L1, L2, L3, L3b, L4) together with the policy layer L5; `C6` the policy layer alone; `C7` the full guard); `CONFIGS=C0,C7` runs only those, `LIMIT=5` runs only the first five cases of each config, and `CASE_IDS=<id>,<id>,...` runs only those cases of each config (a trial, below; its `RUN` must start with `trial`).

**Do a small trial first**, under a throwaway name, and read what it says. It runs one LLMail attack, one benign email and one RAG case, so it also uploads (and embeds) the RAG case's knowledge documents:

```bash
make bench-run MODEL=gpt-4o-mini RUN=trial-gpt CONFIGS=C0,C0T,C7 CASE_IDS=attack-llmail-01d16d4e4af9,benign-llmailfp-0,attack-prag-hotpotqa-5a76e88555429972597f13f4 CONCURRENCY=1
```

It costs a few calls. `CASE_IDS` only picks cases the config runs anyway; the pinned case file and the full run's selection are not changed, and the kit refuses it unless `RUN` starts with `trial`. Do the same for each OpenRouter model before its run, after its canary (`RUN=trial-qwen` and `RUN=trial-llama`, with its `MODEL=`), and run the report of one trial with your reader (`make bench-report RUN=trial-gpt READER=<your reader model>`, with the `LLM__` words of D7) so the reader's endpoint is tried once.

**A trial passes only if all of these hold.** Ending without a `FAIL` is not enough: a case that failed is only counted.

- every `ok <config>:` line says `0 error` (the line starts with `ok` even when it counts errors);
- there is no `WARN ... still have error rows` line;
- after the trial's `make bench-report`, every `MEANING OK` line says `0 errors`;
- the per-case lines below show `ok` for every case, at least one case with `drafting: True` in each config (triage stops some emails before drafting; that is normal; if none drafted, run another trial with `LIMIT=10` in place of `CASE_IDS`, under a new trial name), and no `degraded: True`.

This prints one line per case of the trial (no key, no email text):

```bash
RUN=trial-gpt
for c in C0 C0T C7; do python3 -c "
import json, sys
latest = {}
for line in open(sys.argv[1]):
    row = json.loads(line); latest[row['case_id']] = row
for row in latest.values():
    res = row['result'] or {}; p = res.get('pipeline') or {}; err = row['error'] or {}
    print(sys.argv[2], row['case_id'], row['status'], err.get('kind') or '', p.get('job_state'), 'drafting:', p.get('reached_drafting'), 'retrieved:', len(res.get('retrieved') or []), 'rerank:', p.get('rerank_applied'), 'degraded:', p.get('retrieval_degraded'))
" evaluation/results/mailguard_bench/$RUN/raw/$c.jsonl $c; done
```

If the trial passes, delete its folder (`rm -r evaluation/results/mailguard_bench/trial-gpt`): a trial folder is never a result. If it does not, send the owner its lines and the kit's output.

**What the trial does and does not test.** Its RAG case uploads six knowledge documents (five poisoned, one clean; each chunk of the case is its own document), so the trial embeds documents through the knowledge-worker; whether that email is then routed to retrieval is triage's call (`retrieved:` in its line). A trial is three cases of three configs, not the run's mix: watch `C0`'s lines when they reach the RAG cases of the real run (case ids starting `attack-prag-` or `attack-seedrag-`, the last 100 of each config): each must start with `ok`. If they start with `error`, press Ctrl+C, check the embedding (part C, and the embedding rows of G), and run the same command again. `LIMIT=5` instead runs the first five cases of each config, all LLMail attacks: no benign email and no RAG case. (The owner's own preflight, `docs/demo-runbook.md` section 9.9 step 5, runs all nine configs; this trial runs three.)

What the run does for you, in the order of the owner's runbook (section 9.9, steps 3 to 7): it writes the model's container settings, makes the one embedding call of part C and refuses to go on unless it returns a 1536-wide vector, and recreates the four model-calling containers with the settings (it prints the model, for an OpenRouter model the pinned provider, and the embedding); for each config it switches which process drafts (the `ai-worker` container for `C0`, the guard-worker, a process on your machine, for every other config), waits until it is ready, runs the cases, stops it, and saves the containers' log lines of that config; then it makes one retry pass over the configs that left errors, and builds the reports. If a quota or a credit runs out, or the route of an OpenRouter model stops serving, the whole run stops (part E4).

While a run is going:

- **Do not** run `make up`, `docker compose up` or `docker compose down`, edit `.env`, rebuild the images or commit to the repository. Every config records a fingerprint of these, and a resume under other settings stops.
- Keep the laptop plugged in and awake (part A6).
- Close other heavy programs.

### D5. Resume

If something stops (Ctrl+C, a power cut, sleep, a network failure, a `STOP` of the route), run **the same command again**. Each case is written as it finishes, a rerun skips the cases it has, and it retries cases recorded as errors. The kit log says which configs are finished, so they are not started again. Ctrl+C stops the guard-worker cleanly and prints the command that resumes. A resume refuses to mix settings: the same model, pin, embedding, guard commit and images, or a new `RUN`.

**A quota or a credit that runs out stops the run by itself, for every provider** (owner decision 2026-10-01): OpenAI's used-up quota, prepaid balance, spend limit or daily request cap, OpenRouter's credit or key limit, a Gemini daily or spend limit, the embedding's as well as the model's. The runner prints `STOP <config>: quota_exhausted: ...` (or `no_credit`), ending with the case and the call that hit it (`Embedding request failed` is the embedding's account; `LLM request failed` or `guard LLM stage` the model's), the kit stops the campaign, and the cases not run yet stay unrun. Restore the credit or wait for the limit (a daily cap resets the next day), then run **the same command** again: it resumes, and the rows the stop left as errors are retried. Another model's run may come in between (its own `RUN` and `make bench-run`); the stopped run's settings are rendered again the same way when you come back to it, so the resume is accepted. Not when the limit that stopped it is one the next model also uses: the embedding's quota, or an OpenAI balance or spend limit when your embedding is on the same OpenAI account. The next model's `make bench-run` then stops at its embedding check with the same message, before any model call. A per-minute rate limit is not a stop: the services back off and retry it.

### D6. What the kit logs

- `evaluation/results/mailguard_bench/<RUN>/kit-log.jsonl`: one line per step, with its status and times.
- `evaluation/results/mailguard_bench/<RUN>/raw/guard-worker.<config>.log`: the guard-worker's output for a config.
- `evaluation/results/mailguard_bench/<RUN>/raw/services.<config>.log`: the triage-worker's and the ai-worker's log lines while that config ran. Their `llm_inference` lines say which provider served each call (triage, the summarizer, and `C0`'s replies, which no result row records).
- `evaluation/results/mailguard_bench/<RUN>/raw/`: one row per case (this folder holds attack emails and drafts; it is never committed, only zipped in part F).

Nothing the kit prints or logs contains a key.

### D7. Reports and the meaning column

The run builds `report.md` itself; its "Run setup" section names the model, the pinned provider and the providers that served the calls, and the embedding. The meaning column needs your reader model. Run it for a finished model like this, with the reader's key in `.env` as `LLM__OPENAI_API_KEY` (part C) and its provider and URL given on the command line (the two `LLM__` words before `make` apply to that one command only; nothing is exported). With a Gemini reader:

```bash
LLM__PROVIDER=openai LLM__OPENAI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai \
  make bench-report RUN=2026-10-02-gpt4omini-live READER=<your reader model>
```

For a reader on another OpenAI-compatible endpoint, put that endpoint's URL in `LLM__OPENAI_BASE_URL`.

**Check that every `MEANING OK` line says `0 errors`.** `make bench-report` ends without a `FAIL` even when the reader failed some reads (a wrong key or model name, a timeout), and the report then gives the meaning-based rate over fewer drafts, with a note that `N of M scored attacks have no current verdict`. The kit's meaning step passes `--retry-errors` (owner decision 2026-10-01), so **running the same `make bench-report` command again**, once the cause is fixed, reads those drafts again and rebuilds the report. A used-up reader quota (a Gemini reader shares the quota of a Gemini embedding project) or a reader account without credit stops the step at once with `STOP meaning: quota_exhausted: ...` (or `no_credit`); restore it, then run the same `make bench-report` command again, unchanged. The line also names `--retry-errors`: that is for the step run by hand, below; the kit already passes it. The step by hand, which the kit runs for you (the same two `LLM__` words as above, your `RUN` and your reader):

```bash
LLM__PROVIDER=openai LLM__OPENAI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai \
  uv run --project . --with-editable agentmailguard python -m evaluation.mailguard_bench.meaning \
  --run-dir evaluation/results/mailguard_bench/2026-10-02-gpt4omini-live --reader-model <your reader model> --retry-errors
make bench-report RUN=2026-10-02-gpt4omini-live
```

The second line rebuilds `report.md` with the new verdicts; it calls no model.

The report explains its numbers in a table at the top: the guard's rate of successful attacks (Guard ASR) is the one the owner's target is judged on, the pipeline rate counts attacks that triage already stopped, and the meaning-based rate is a second reading of the same drafts (quote both side by side).

---

## E. The OpenRouter route (Qwen2.5-7B and Llama-3.1-8B)

The two open-weight models are served by OpenRouter, a router in front of many hosting providers. The benchmark pins **one provider per model, with fallbacks off** (ADR-0014): every call of a run must be served by that provider, or it is an error, never a result. The pins were chosen from OpenRouter's public listing of 2026-10-01: Qwen2.5-7B has one provider, Phala (precision not stated); for Llama-3.1-8B, CoreWeave at `bf16` is the only provider that lists structured outputs. These are **not** the 4-bit local builds of the v1 runs, so the numbers are not comparable with v1's on serving; the report says so.

| `MODEL=` | Model id | Pinned provider | Price per 1M tokens (in / out) |
|---|---|---|---|
| `qwen2.5-7b-openrouter` | `qwen/qwen-2.5-7b-instruct` | Phala, precision not filtered | $0.10 / $0.20 |
| `llama-3.1-8b-openrouter` | `meta-llama/llama-3.1-8b-instruct` | CoreWeave, `bf16` | $0.22 / $0.22 |

### E1. The account and the key

1. Create the key at https://openrouter.ai/settings/keys, **with a credit limit on the key** (about $10 to $15 covers both models and their reruns; the estimate per full run is about $1 for Qwen and $2 for Llama). Put it in `.env` as `BENCH_OPENROUTER_API_KEY`.
2. Buy credit at https://openrouter.ai/settings/credits. OpenRouter charges a fee on purchases and says credit can take up to an hour to show.
3. At https://openrouter.ai/settings/privacy, make sure **no setting excludes Phala or CoreWeave** (an "allowed providers" list without them, or an "ignored providers" list with them). A request no provider may serve fails with HTTP 404 on every call. Both providers state that they do not train on prompts and do not retain them. Turn on no guardrail on the key: the benchmark sends prompt-injection emails on purpose.

### E2. Check the credit before every OpenRouter run

The 2026-09-29 run of the owner stopped on HTTP 402 "Insufficient credits". Before each `make bench-run` of an OpenRouter model, open https://openrouter.ai/settings/credits and the key's page, and check that the balance and the key's remaining limit cover the run. (For OpenAI, check the balance the same way: an OpenAI account out of balance answers HTTP 429 with `insufficient_quota`, not 402, and the run stops on it like on a 402, E4.)

### E3. The canary, once before each OpenRouter run

```bash
make bench-canary MODEL=qwen2.5-7b-openrouter
make mailguard-probe MODEL=qwen2.5-7b-openrouter
```

and the same two lines with `MODEL=llama-3.1-8b-openrouter` before the Llama run. Each makes **one** live call, under a cent:

- `bench-canary` sends one strict-JSON request through rag-email's own pinned client and prints three checks: `strict_json` (the answer is valid for the schema), `provider_match` (the pinned provider served it, on the first attempt) and `captured`. It saves the request and the response, without the key, to `evaluation/results/mailguard_bench/canary/<profile>.json`, a folder git ignores. It exits 1 and says why when a check fails: 401 is the key, 402 the credit or the key's limit, 404 the privacy settings of E1, 502 or 503 the provider being down. **`strict_json` FAIL while `provider_match` is `ok`** means the pinned provider served the call but did not keep to the JSON schema: do not start that model's run. Send `evaluation/results/mailguard_bench/canary/<profile>.json` to the owner and wait for their decision (you may not switch providers, E4).
- `mailguard-probe` makes one call of the guard's judge through the guard's own client, and checks the same pin.

Then do the small trial of D4 for that model. Send the canary files back with the results (part F).

### E4. What stops a run, and what to do

- **A call another provider served**, a call the router served after a fallback, or a response that names no provider: that case is an error row (`provider_mismatch`), never a scored one.
- **A guard model call that failed on the route or the service** (HTTP 402 (no credit), 404, 408, 409 or 5xx, a timeout, a connection error, a router error inside an HTTP 200 answer, or an HTTP 200 answer that is not JSON, such as a gateway's error page) makes its case an error row of kind `guard_route_failure`, for every model: it is excluded from the scores, listed under the report's errors row, and retried by the retry pass and by the next run of the same command. A model that answers badly (no JSON, the wrong fields, a refusal) is the guard's own behaviour and stays a scored fallback (owner decision 2026-10-01). A guard call's 429 is no such row: a per-minute 429 (and OpenRouter's short in-flight 402) is retried, and a used-up quota stops the run (`quota_exhausted`, below).
- **The run stops** (`STOP <config>: ...`, then the kit's `FAIL ... the run stopped`) **at once on a used-up quota or credit of any provider** (`quota_exhausted`, D5; an HTTP 402 that is not OpenRouter's short in-flight budget: no credit, or the key's limit is used up). On an OpenRouter route it also stops after three provider mismatches in a row, and after three HTTP 404, 502 or 503 in a row (the pinned provider is not serving, and with fallbacks off nothing else may serve). The kit then stops the whole campaign: no next config and no retry pass, which would only hit the same limit or route.
- **What to do:** fund the key or restore the quota (E2; a daily cap resets the next day), or wait until the provider serves again (its uptime is on the model's OpenRouter page), then run **the same command again**: finished configs are skipped and the error rows are retried. Another model's run may come in between, unless it uses the limit that stopped this one (D5). The `STOP` line also mentions `--retry-errors`: that is for a runner started by hand (the owner's runbook). With the kit, rerun the same `make bench-run` command unchanged; it already retries error rows, and it has no such option. **Never switch to another provider inside a `RUN`**: Qwen has no other provider, and Llama's others serve `fp8` or an unknown precision without structured outputs. A different provider is a new `RUN` and the owner's decision.
- **What is recorded:** the pin is in each config's settings (a resume under another pin is refused); every guarded row records which provider served its reply and its guard calls; the containers' log lines (D6) record the provider of triage, the summarizer and `C0`'s replies; the report's "Run setup" section sums the served providers.

---

## F. Send the results back

When a model's run is complete:

```bash
make bench-package RUN=2026-10-02-gpt4omini-live
```

This writes `bench-results-2026-10-02-gpt4omini-live.zip` in the repository folder: the whole run folder including `raw/` and `kit-log.jsonl`, and never a key (it refuses to write the zip if a key appears in a file). Send the zip to the owner by the route the owner names. Copy it to Windows if you need to attach it: `cp bench-results-*.zip /mnt/c/Users/<your Windows name>/Desktop/`.

**During the campaign, send zips only.** Committing changes your checkout's commit: the next `make bench-run` then refuses the containers until `make bench-setup` rebuilds them, and a committed `RUN` can no longer be resumed (the commit is part of each config's fingerprint). If the owner wants a branch, commit after the last model, and only if you have write access to the repository (`git push` needs it; without it, send the zips). `raw/` stays out of git on purpose (it holds attack emails), so it is only in the zip:

```bash
git switch -c bench/2026-10-02-gpt4omini-live
git add evaluation/results/mailguard_bench/2026-10-02-gpt4omini-live
git commit -m "bench: results of 2026-10-02-gpt4omini-live"
git push -u origin bench/2026-10-02-gpt4omini-live
```

Include in your message:

- which model each run used, and its `RUN` name;
- the embedding you chose (model and endpoint);
- the reader model you chose, and when;
- the two canary files, `evaluation/results/mailguard_bench/canary/<profile>.json` (git ignores them; they hold no key);
- any `STOP`, `FAIL` or `WARN` line the kit or the doctor printed;
- the usage totals for each run from the OpenAI and OpenRouter dashboards, and from your embedding's and your reader's providers (numbers only, never a key);
- what the report's `Errors (excluded)` and `AI-step fallbacks` rows say for each config. Check them before you send: errors left after the retry pass, or many fallbacks, usually mean a quota or a route problem, so say so.

The zip already holds each run's `kit-log.jsonl`, the containers' log lines (`raw/services.<config>.log`) and the report, whose "Run setup" section totals the providers that served the calls.

---

### F1. After the last model

When every model's run is finished and sent: delete `.env.stack` (it holds your keys: `rm .env.stack`). **Before any `make up`**, put `.env` back to the normal stack: comment out the `EMBEDDING__` lines (the offline mock embedder again), set `LLM__TIMEOUT_S` back to `15.0` (or delete the line) and `RETRIEVAL__CATEGORY_FILTER_ENABLED` back to `true` (or delete the line). Compose forwards all three to the normal stack, so otherwise it keeps calling your paid embedding endpoint, with the benchmark's 60 s LLM timeout and no category filter (runbook section 9.9, step 8, which the kit's last line also names).

## G. When something goes wrong

| What you see | What it means, and what to do |
|---|---|
| Doctor: `line endings: N tracked text files have CRLF` | The clone was made by Git for Windows, or under `/mnt/c`. Clone again inside Ubuntu, in `~/work` (part B). |
| `permission denied while trying to connect to the Docker daemon socket` | You are not in the `docker` group yet: `sudo usermod -aG docker $USER`, close the Ubuntu window and open a new one. |
| `Cannot connect to the Docker daemon` | Docker is not running. `sudo systemctl start docker`. If it says there is no systemd, part A2. |
| `port is already allocated` or doctor `ports: in use by another program` | Another program holds a port the stack needs (Postgres 5433, RabbitMQ 5672 and 15672, MinIO 9010 and 9011, Prometheus 9090, Grafana 3002, the API 8000, the review UI 3001, the guard-worker 8014). Find it with `ss -ltnp \| grep :<port>` inside Ubuntu, or `netstat -ano \| findstr :<port>` in PowerShell, and stop it. |
| Builds or containers are killed, `docker memory` fails | WSL has too little memory: part A3, then `wsl --shutdown`. |
| `the embedding check refused this run` | `make bench-run`'s one embedding call (part C) failed: the message names why (another width than 1536, `dimensions` rejected, the key, the model or URL, a used-up quota, or a per-minute rate limit twice: the kit waits out the first one). Fix it in `.env` (part C) or restore the quota, then the same command again. Nothing else ran. |
| `429` from the embedding endpoint (Gemini says `RESOURCE_EXHAUSTED`) | A per-minute limit: the services retry it. A used-up quota or a daily limit stops the run (`STOP ... quota_exhausted`, E4). Gemini applies limits per project, not per key (AI Studio shows them). Raise or restore the limit, then the same command again. |
| `429` from OpenAI, `STOP <config>: quota_exhausted: ...` | The account's quota, prepaid balance, spend limit or daily request cap is used up (OpenAI's error codes: `insufficient_quota`, `credit_balance_exhausted`, the spend and usage limits, or a per-day limit); the run stopped by itself. The end of the `STOP` line says which call hit it: `Embedding request failed` is the embedding's account, `LLM request failed` or `guard LLM stage` the model's. Restore that one (the next day for a daily cap), then the same command again (D5). A per-minute rate limit is retried and does not stop the run. |
| Rows with error kind `guard_route_failure` | A guard model call failed on the route or the service (E4). They are retried by the retry pass and by the same command again; many of them mean the provider was down. |
| Many rows with `retrieval_degraded` true | The embedding call ran out of its 3000 ms budget or failed; the row's error names why (a used-up embedding quota stops the run). Check the embedding endpoint, then rerun. |
| OpenRouter `401` | The key in `BENCH_OPENROUTER_API_KEY` is wrong or deleted. Create a new one (E1). |
| OpenRouter `402`, `STOP <config>: no_credit` | No credit, or the key's limit is used up. Add credit or raise the key's limit (E1, E2), then the same command again. |
| OpenRouter `404`, `502` or `503`, `STOP <config>: 3 consecutive no_provider errors` | The pinned provider cannot serve: your privacy settings exclude it (404, E1), or it is down. Check E1, wait, then the same command again. Never switch providers inside a `RUN` (E4). |
| `provider_mismatch`, `STOP <config>: 3 consecutive provider_mismatch errors` | A call was served by another provider, after a fallback, or with no provider named. Run the canary (E3) and send its file to the owner; do not go on. |
| The `STOP` line says to rerun `with --retry-errors` | That is for a runner started by hand. With the kit, rerun the same `make bench-run` command unchanged (E4). |
| `error` lines keep coming (their kind `CaseTimeoutError`, `rate_limited` or `retrieval_degraded`, or 429 or `RESOURCE_EXHAUSTED` in the row's error) and no `STOP` appears | Probably a limit the kit did not recognise as used up: it treats a 429 that names no documented quota code and no per-day limit as per-minute, and retries it until the case times out. Press Ctrl+C, check your providers' usage pages, wait for the limit, then run the same command again. |
| Canary: `FAIL strict_json` while `provider_match` is `ok` | The pinned provider does not keep to the JSON schema. Do not start that model's run: send `evaluation/results/mailguard_bench/canary/<profile>.json` to the owner and wait (E3). |
| `MEANING OK ... N errors` with N above 0 | The reader failed some reads. Fix the cause (the reader's key or model name), then run the same `make bench-report` command again: it reads them again (D7). |
| `STOP meaning: quota_exhausted: ...` or `STOP meaning: no_credit` | The reader's quota or credit is used up. Restore it, then the same `make bench-report` command again, unchanged (D7). The line also mentions `--retry-errors`: that is for the step run by hand; the kit already passes it. |
| `L1 classifier` FAIL in `bench-setup` or the doctor, and the owner's file has not arrived | The run cannot start without it (D1); ask the owner before Friday. |
| The laptop slept or the lid closed | The run stopped. Part A6, then run the same command again. If Docker looks stuck, `wsl --shutdown` in PowerShell, open Ubuntu, `docker ps`, and run the command again. |
| A container cannot reach Ollama (`connection refused`; appendix L only) | Nothing listens on the docker0 address. Route 1 of L4: `systemctl show ollama -p Environment`, and Docker must have started before Ollama. Route 2: the `socat` forwarder is not running (it stops when Ubuntu restarts), start it again. A timeout means a firewall drops traffic from Docker's network to port 11434. |
| The runner says `<model> is not loaded on the Ollama at ...` | Ollama has not loaded that model (it unloaded it after the keep-alive, or you started another one). Run `ollama run <model> "Reply with OK"` (L5), then the same `make bench-run` command again. |
| Doctor: `L1 classifier: ... is missing` or `has sha256 ...` | The classifier file is not in `evaluation/mailguard_bench/pinned/`, or it is another file. It is not in git, so `git checkout` cannot bring it back: ask the owner for `l1_injection_clf_v1.joblib` and place it as in D1. |
| Doctor: `ollama: ... it does not answer` for the docker0 address (appendix L only) | The containers cannot reach your Ollama yet: do route 1 or route 2 of L4. |
| `the shell sets ...` or `.env` problems named by setting | Part C: unset the exported variable, or fix the `.env` line. The messages name the setting, never its value. |
| The runner refuses to start and names a missing or extra drafting consumer | A process is on when it should be off (`ai-worker` versus the guard-worker). Do not start things by hand; run the same `make bench-run` command again. |
| `make: command not found`, `uv: command not found` | Part A5, then open a new window. |

If you are stuck, send the owner the output of `make bench-doctor` and the last lines of `kit-log.jsonl`. Both are safe to share; `.env` is not.

---

## L. Appendix: local models with Ollama (not used for the 2026-10-02 run)

**The 2026-10-02 run does not use this part** (ADR-0014: all three models are cloud models, part E). It is kept for a local run the owner may ask for later; its profiles are `qwen2.5-7b` and `llama-3.1-8b-local`, and their results are not comparable with the OpenRouter runs.

Both run on Ollama inside Ubuntu, using your RTX 4050. The time estimate of the owner's 7-case smoke run on the owner's desktop (an RTX 3060 with 12 GB): about 10 s per case and config for Qwen2.5-7B and 9 s for Llama-3.1-8B at concurrency 1, roughly **12 to 15 hours for each local model** on that desktop (this is an estimate, not a measurement of a full run), and longer here. Give a local model `CONCURRENCY=1` (one GPU answers one request at a time); the doctor then adds the Ollama and GPU checks (`--model-profile qwen2.5-7b`). **The laptop's GPU has 6 GB.** A 7B or 8B model in 4 bit is about 4.7 to 5 GB before its context, so part of the model will run on the CPU. That is expected and is recorded, but it makes the numbers and the speed different from the owner's 12 GB desktop: say so when you send results, and expect these two runs to be slow.

### L1. The GPU driver (Windows, not Ubuntu)

Install the current NVIDIA driver on **Windows** (from nvidia.com or GeForce Experience). NVIDIA's WSL guide says it is the only driver you need and that you must not install a Linux display driver inside WSL: Windows hands the GPU to Ubuntu. Check in Ubuntu:

```bash
nvidia-smi
```

It must print your RTX 4050. (If the shell cannot find it, it lives at `/usr/lib/wsl/lib/nvidia-smi`.)

### L2. Install Ollama

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

### L3. Keep-alive and context (runbook section 9.8, step 2)

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

### L4. Make Ollama reachable from the containers (runbook section 9.9, step 2)

Ollama listens on `127.0.0.1` only. Inside a container `localhost` is the container itself, so the containers reach the laptop's Ollama through the name `host.docker.internal`, which Docker Engine resolves to the **docker0 address** (the Docker bridge, normally `172.17.0.1`). Nothing listens there yet, and **the bridge address only exists while Docker runs**, so start Docker first. There are two ways to fix it. Use **one** of them: route 1 if you have `sudo` and want it permanent, route 2 if you prefer to leave Ollama alone. The doctor's `ollama` check tells you whether the containers can now reach it.

**Route 1: the systemd override (runbook 9.9 step 2; needs `sudo`).** Ollama listens on the bridge address, and only there:

```bash
BRIDGE_IP=$(ip -4 -o addr show docker0 | awk '{print $4}' | cut -d/ -f1)   # 172.17.0.1 normally
sudo mkdir -p /etc/systemd/system/ollama.service.d
printf '[Unit]\nAfter=docker.service\nWants=docker.service\n\n[Service]\nEnvironment="OLLAMA_HOST=%s:11434"\n' "$BRIDGE_IP" \
  | sudo tee /etc/systemd/system/ollama.service.d/bridge.conf
sudo systemctl daemon-reload && sudo systemctl restart ollama
systemctl show ollama -p Environment      # OLLAMA_HOST=<bridge ip>:11434, beside the two lines of L3
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

### L5. Pull the models, and load the one you are about to run

```bash
ollama pull qwen2.5:7b-instruct
ollama pull llama3.1:8b
```

**Before each local-model run, load the model**, in the shell where the `ollama` command reaches the server (route 1: with `OLLAMA_HOST` exported as in L4):

```bash
ollama run qwen2.5:7b-instruct "Reply with OK"
ollama ps
```

The live runner **refuses a model that Ollama has not loaded** (`<model> is not loaded on the Ollama at ...`): it reads the model's real context length from the loaded model and records it with the run. The keep-alive of L3 keeps the model loaded for 30 minutes after the last call; if you stop for longer, or start a different model, load it again before the run or the resume. The doctor's `ollama model loaded` line warns when it is not.

`ollama ps` shows, in the `PROCESSOR` column, where the loaded model sits: `100% GPU`, `100% CPU`, or a split such as `48%/52% CPU/GPU`. On 6 GB expect a split. **Write the split down with the run**: a CPU share means the run's latencies are not comparable with the desktop's. Run Llama **after** the Qwen run is finished (one 6 GB GPU cannot hold both): stop Qwen, then load Llama:

```bash
ollama stop qwen2.5:7b-instruct
ollama run llama3.1:8b "Reply with OK"
ollama ps
```

### L6. Record the server state with each local run

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
make bench-run MODEL=qwen2.5-7b RUN=<date>-qwen25-local-live CONCURRENCY=1
```

and, when it is completely done, the same for Llama (load it first):

```bash
ollama run llama3.1:8b "Reply with OK"
make bench-run MODEL=llama-3.1-8b-local RUN=<date>-llama31-local-live CONCURRENCY=1
```

Do a small trial first for each local model too (D4), after loading it: `make bench-run MODEL=qwen2.5-7b RUN=trial-qwen CONFIGS=C0,C0T,C7 LIMIT=5 CONCURRENCY=1`. A local model answers slowly (about 10 s per case on the owner's desktop), so the trial is a few minutes, not seconds.

---

## Sources

External facts in this guide come from these pages, read on 2026-09-30, and for the cloud route on 2026-10-01:

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
- Gemini rate limits (per project; tiers; read your own in AI Studio): https://ai.google.dev/gemini-api/docs/rate-limits ; the free tier's 1,000 requests a day for `gemini-embedding-001` is the last published number (web.archive.org snapshot of 2025-10-02 of the same page)
- `gemini-embedding-001` (128 to 3072 dimensions, 1536 recommended): https://ai.google.dev/gemini-api/docs/models/gemini-embedding-001
- OpenAI, read 2026-10-01: error codes (the 429s of a used-up balance, spend or usage limit, `insufficient_quota`; "Retrying billing, spend, or quota errors won't restore API access"): https://developers.openai.com/api/docs/guides/error-codes ; rate limits (RPM, RPD, TPM, TPD): https://developers.openai.com/api/docs/guides/rate-limits ; `gpt-4o-mini`: https://developers.openai.com/api/docs/models/gpt-4o-mini ; deprecations (none for `gpt-4o-mini`): https://developers.openai.com/api/docs/deprecations
- OpenRouter, read 2026-10-01: provider routing (`order`, `allow_fallbacks`, `require_parameters`, `quantizations`): https://openrouter.ai/docs/guides/routing/provider-selection ; router metadata (`X-OpenRouter-Metadata`): https://openrouter.ai/docs/guides/features/router-metadata ; errors and 402 `limit_source`: https://openrouter.ai/docs/api_reference/errors-and-debugging and https://openrouter.ai/docs/api_reference/limits ; structured outputs: https://openrouter.ai/docs/guides/features/structured-outputs ; FAQ (fees, credit delay): https://openrouter.ai/docs/faq
- OpenRouter's public endpoint listings, read 2026-10-01: https://openrouter.ai/api/v1/models/qwen/qwen-2.5-7b-instruct/endpoints (Phala only, precision `unknown`, $0.10 / $0.20) and https://openrouter.ai/api/v1/models/meta-llama/llama-3.1-8b-instruct/endpoints (CoreWeave `bf16`, $0.22 / $0.22, the only one listing structured outputs)
- Pausing Windows updates: https://support.microsoft.com/en-us/windows/deployment/updates-lifecycle/pause-updates-in-windows

The run times in "What you need" and appendix L are the owner's own, from a 7-case smoke run on 2026-09-30/10-01 on the owner's desktop (RTX 3060, 12 GB); they are not from a page, and the two OpenRouter models were not measured. The decisions cited are in `docs/adr/0012-post-review-v2-done-fixes-and-main.md` (15: the classifier is not redistributed; 16: Docker Engine inside WSL2 is the primary route). `socat`'s option syntax (`TCP-LISTEN`, `bind=`, `reuseaddr`, `fork`): its manual page, `man socat`, after `sudo apt install socat`.

The procedure itself is the owner's runbook, `docs/demo-runbook.md` section 9.9 (9.10 for the OpenRouter route, 9.8 for the local models); this guide is the same steps, run by the kit. The decision for the cloud route is `docs/adr/0014-full-cloud-route-for-the-v2-benchmark.md`.
