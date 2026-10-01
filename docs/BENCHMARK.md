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

- **An OpenAI API key** with credit on the account (for `gpt-4o-mini`). It goes in `.env` as `BENCH_OPENAI_API_KEY`. Check your usage tier (platform.openai.com, Settings, Organization, Limits): on Tier 1 (`$5` paid) `gpt-4o-mini` is capped at 10,000 requests a day, and one full `gpt-4o-mini` run makes about 8,000 to 11,000. Tier 2 (`$50` paid) has no daily cap listed; otherwise expect to finish the run the next day (a run resumes, D5).
- **An OpenRouter API key with credit**, for Qwen2.5-7B and Llama-3.1-8B. It goes in `.env` as `BENCH_OPENROUTER_API_KEY`. Part E sets it up (credit, a per-key limit, the privacy settings that must allow the two pinned providers).
- **An embedding endpoint of your choice** and its key, `EMBEDDING__API_KEY` (part C): for example your OpenAI key with `text-embedding-3-small`, or a Gemini key with `gemini-embedding-001`. It must accept the `dimensions` parameter (part C; OpenAI's `text-embedding-ada-002` does not). Use the same one for all three models. A free Gemini key is not enough (part C).
- **The Layer-1 classifier file, `l1_injection_clf_v1.joblib`, from the owner.** It is **not in git** (ADR-0012 decision 15: a dataset that went into its training declares no license, so the file is not redistributed, see `evaluation/mailguard_bench/pinned/NOTICE.md`). The owner sends it to you privately. It is 28 MB. Do not put it in a public link, a chat group or the repository. You place it in step D1.
- **Disk:** the doctor warns below 25 GB free: the Docker images and the run folders are the big parts.
- **A charger and a laptop that stays awake** for the whole run (part A, last step).
- **Time, an estimate:** a full model is 9 configs x 550 cases = 4,950 case runs. For `gpt-4o-mini`, about 3 s per case and config at concurrency 2 (the owner's 7-case smoke run), so roughly **4 to 6 hours**. The two OpenRouter models were **not measured**: plan 4 to 8 hours each. The meaning column (D7) reads up to about 3,600 drafts per model, about 1 to 3 hours each. That is about 15 to 25 hours for everything: **plan an overnight window**, not a working day. The kit writes `evaluation/results/mailguard_bench/<RUN>/kit-log.jsonl`, one JSON line per finished step with its times: after your first model, its lines are the real estimate for the next two; please send it back with the results (part F). A stopped run resumes where it stopped (D5), so the hours do not need to be in one sitting.
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

Two more things stop an overnight run:

- **Windows Update restarts.** Pause updates before the run: Start, Settings, Windows Update, "Pause updates" (up to 35 days). While updates are paused the device does not restart by itself to install them.
- **Closing the Ubuntu window** ends the commands running in it. Start each long run inside `tmux` (`sudo apt install -y tmux`, then `tmux`; detach with Ctrl+B then D, come back with `tmux attach`), so a closed window or a dropped terminal does not stop the run.

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
```

(`BENCH_OPENAI_API_KEY` and `BENCH_OPENROUTER_API_KEY` are new lines: add them. The others already exist in `.env.example`, some with other values.)

### The embedding: your choice, the same for all three models

The embedding model turns the case documents and the queries into vectors. **You choose it** (ADR-0014), with four rules:

- any OpenAI-compatible `/embeddings` endpoint, with its own key in `EMBEDDING__API_KEY` (never an LLM key);
- `EMBEDDING__DIMENSION=1536`, and the endpoint must **accept the `dimensions` parameter** and return 1536 numbers: the kit sends `dimensions: 1536` with every request, also to a model that is 1536 wide anyway. OpenAI documents the parameter for `text-embedding-3` and later models (`text-embedding-3-small` and `-large`); Google's endpoint took it for `gemini-embedding-001` (checked 2026-09-29). A model that rejects it, such as OpenAI's `text-embedding-ada-002` (1536 wide, but older than the parameter), fails every embedding call. Use one of the two examples below; any other server is at your own risk, because nothing embeds a document before the run's first RAG case (D4). 1536 is the width of the database column; another width needs a database migration, so the kit refuses it;
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

A run embeds about 6,000 to 10,000 times (the knowledge documents of the 100 RAG cases in every config, plus one query per drafted email that retrieves), in bursts of up to about 150 a minute at concurrency 2. A free Gemini key (1,000 requests a day when Google last published the number) runs out inside the first config: with Gemini, link billing to the project first and read its limits in AI Studio. With OpenAI the key's usage tier sets the limit.

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
grep -nE '^(BENCH_OPENAI_API_KEY|BENCH_OPENROUTER_API_KEY|EMBEDDING__API_KEY)=.' .env | cut -d= -f1
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

Run the models **in this order, and finish one completely (all its configs, the retry pass, the reports) before you start the next**:

1. `gpt-4o-mini`
2. `qwen2.5-7b-openrouter` (part E: credit check and canary first)
3. `llama-3.1-8b-openrouter` (part E: credit check and canary first)

A `RUN` is the name of the results folder. Use one name per model, with today's date, for example:

```bash
make bench-run MODEL=gpt-4o-mini RUN=2026-10-02-gpt4omini-live CONCURRENCY=2
make bench-run MODEL=qwen2.5-7b-openrouter RUN=2026-10-02-qwen25-openrouter-live CONCURRENCY=2
make bench-run MODEL=llama-3.1-8b-openrouter RUN=2026-10-02-llama31-openrouter-live CONCURRENCY=2
```

`CONCURRENCY=2` for all three; never more. The configs default to the v2 list `C0, C0T, C1` to `C7` (`C0` is rag-email alone; `C0T` the guard's reply template with no layer active; `C1` to `C5` one detection layer each (L1, L2, L3, L3b, L4) together with the policy layer L5; `C6` the policy layer alone; `C7` the full guard); `CONFIGS=C0,C7` runs only those, and `LIMIT=5` runs only the first five cases of each config.

**Do a small trial first**, under a throwaway name, and read what it says:

```bash
make bench-run MODEL=gpt-4o-mini RUN=trial-gpt CONFIGS=C0,C0T,C7 LIMIT=5 CONCURRENCY=1
```

It costs a few calls. Do the same for each OpenRouter model before its run, after its canary (`RUN=trial-qwen` and `RUN=trial-llama`, with its `MODEL=`), and run the report of one trial with your reader (`make bench-report RUN=trial-gpt READER=<your reader model>`, with the `LLM__` words of D7) so the reader's endpoint is tried once. If it ends without a `FAIL`, delete the trial folder (`rm -r evaluation/results/mailguard_bench/trial-gpt`): a trial folder is never a result. `docs/demo-runbook.md` section 9.9 step 5 explains what a good preflight looks like (for example that cases reach drafting and that retrieval did not fall back to lexical).

What the run does for you, in the order of the owner's runbook (section 9.9, steps 3 to 7): it writes the model's container settings and recreates the four model-calling containers with them (it prints the model, for an OpenRouter model the pinned provider, and the embedding); for each config it switches which process drafts (the `ai-worker` container for `C0`, the guard-worker, a process on your machine, for every other config), waits until it is ready, runs the cases, stops it, and saves the containers' log lines of that config; then it makes one retry pass over the configs that left errors, and builds the reports. If the route of an OpenRouter model stops serving, the whole run stops (part E4).

While a run is going:

- **Do not** run `make up`, `docker compose up` or `docker compose down`, edit `.env`, rebuild the images or commit to the repository. Every config records a fingerprint of these, and a resume under other settings stops.
- Keep the laptop plugged in and awake (part A6).
- Close other heavy programs.

### D5. Resume

If something stops (Ctrl+C, a power cut, sleep, a network failure, a `STOP` of the route), run **the same command again**. Each case is written as it finishes, a rerun skips the cases it has, and it retries cases recorded as errors. The kit log says which configs are finished, so they are not started again. Ctrl+C stops the guard-worker cleanly and prints the command that resumes. A resume refuses to mix settings: the same model, pin, embedding, guard commit and images, or a new `RUN`.

A quota that runs out (a daily request cap, a used-up key limit) does not come back by waiting a few seconds: the services retry for a few seconds only, and the kit's retry pass runs right after the first pass. Watch the counts the first config prints; if error rows climb, press Ctrl+C, fix the cause (credit, tier, quota), and run the same command again, the next day if a daily cap ran out.

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

The 2026-09-29 run of the owner stopped on HTTP 402 "Insufficient credits". Before each `make bench-run` of an OpenRouter model, open https://openrouter.ai/settings/credits and the key's page, and check that the balance and the key's remaining limit cover the run. (For OpenAI, check the balance and the tier the same way: an OpenAI account out of balance answers HTTP 429, not 402.)

### E3. The canary, once before each OpenRouter run

```bash
make bench-canary MODEL=qwen2.5-7b-openrouter
make mailguard-probe MODEL=qwen2.5-7b-openrouter
```

and the same two lines with `MODEL=llama-3.1-8b-openrouter` before the Llama run. Each makes **one** live call, under a cent:

- `bench-canary` sends one strict-JSON request through rag-email's own pinned client and prints three checks: `strict_json` (the answer is valid for the schema), `provider_match` (the pinned provider served it, on the first attempt) and `captured`. It saves the request and the response, without the key, to `evaluation/results/mailguard_bench/canary/<profile>.json`, a folder git ignores. It exits 1 and says why when a check fails: 401 is the key, 402 the credit or the key's limit, 404 the privacy settings of E1, 502 or 503 the provider being down.
- `mailguard-probe` makes one call of the guard's judge through the guard's own client, and checks the same pin.

Then do the small trial of D4 for that model. Send the canary files back with the results (part F).

### E4. What stops a run, and what to do

- **A call another provider served**, a call the router served after a fallback, or a response that names no provider: that case is an error row (`provider_mismatch`), never a scored one.
- **The run stops** (`STOP <config>: ...`, then the kit's `FAIL ... the model's route stopped serving`) after three such errors in a row, after three HTTP 404, 502 or 503 in a row (the pinned provider is not serving, and with fallbacks off nothing else may serve), and **at once on an HTTP 402** that is not OpenRouter's short in-flight budget (no credit, or the key's limit is used up). The kit then stops the whole campaign: no next config and no retry pass, which would only hit the same route.
- **What to do:** fund the key (E2), or wait until the provider serves again (its uptime is on the model's OpenRouter page), then run **the same command again**: finished configs are skipped and the error rows are retried. **Never switch to another provider inside a `RUN`**: Qwen has no other provider, and Llama's others serve `fp8` or an unknown precision without structured outputs. A different provider is a new `RUN` and the owner's decision.
- **What is recorded:** the pin is in each config's settings (a resume under another pin is refused); every guarded row records which provider served its reply and its guard calls; the containers' log lines (D6) record the provider of triage, the summarizer and `C0`'s replies; the report's "Run setup" section sums the served providers.

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

Include in your message:

- which model each run used, and its `RUN` name;
- the embedding you chose (model and endpoint) and your OpenAI usage tier;
- the reader model you chose, and when;
- the two canary files, `evaluation/results/mailguard_bench/canary/<profile>.json` (git ignores them; they hold no key);
- any `STOP`, `FAIL` or `WARN` line the kit or the doctor printed;
- the usage totals the OpenAI and OpenRouter dashboards show for each run (numbers only, never a key).

The zip already holds each run's `kit-log.jsonl`, the containers' log lines (`raw/services.<config>.log`) and the report, whose "Run setup" section totals the providers that served the calls.

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
| `429` from the embedding endpoint (Gemini says `RESOURCE_EXHAUSTED`) | Its rate or daily limit. Every case's knowledge documents and every query that needs retrieval call the embedding model in every run. The services retry for a few seconds only; a daily limit needs the next day. Gemini applies limits per project, not per key (AI Studio shows them); OpenAI per usage tier. Raise the limit, then run the same command again. |
| `429` from OpenAI | OpenAI shows your limits under Settings, Organization, Limits (Tier 1 caps `gpt-4o-mini` at 10,000 requests a day). An account with no balance or a hard spend limit also answers 429. Fix it, then run the same command again to retry error rows. |
| Many rows with `retrieval_degraded` true | The embedding call ran out of its 3000 ms budget or its quota. Check the embedding endpoint's limits, then rerun. |
| OpenRouter `401` | The key in `BENCH_OPENROUTER_API_KEY` is wrong or deleted. Create a new one (E1). |
| OpenRouter `402`, `STOP <config>: no_credit` | No credit, or the key's limit is used up. Add credit or raise the key's limit (E1, E2), then the same command again. |
| OpenRouter `404`, `502` or `503`, `STOP <config>: 3 consecutive no_provider errors` | The pinned provider cannot serve: your privacy settings exclude it (404, E1), or it is down. Check E1, wait, then the same command again. Never switch providers inside a `RUN` (E4). |
| `provider_mismatch`, `STOP <config>: 3 consecutive provider_mismatch errors` | A call was served by another provider, after a fallback, or with no provider named. Run the canary (E3) and send its file to the owner; do not go on. |
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
- OpenAI rate limits and tiers (Tier 1: $5 paid, 10,000 requests a day for `gpt-4o-mini`): https://developers.openai.com/api/docs/guides/rate-limits ; `gpt-4o-mini`: https://developers.openai.com/api/docs/models/gpt-4o-mini ; deprecations (none for `gpt-4o-mini`): https://developers.openai.com/api/docs/deprecations
- OpenRouter, read 2026-10-01: provider routing (`order`, `allow_fallbacks`, `require_parameters`, `quantizations`): https://openrouter.ai/docs/guides/routing/provider-selection ; router metadata (`X-OpenRouter-Metadata`): https://openrouter.ai/docs/guides/features/router-metadata ; errors and 402 `limit_source`: https://openrouter.ai/docs/api_reference/errors-and-debugging and https://openrouter.ai/docs/api_reference/limits ; structured outputs: https://openrouter.ai/docs/guides/features/structured-outputs ; FAQ (fees, credit delay): https://openrouter.ai/docs/faq
- OpenRouter's public endpoint listings, read 2026-10-01: https://openrouter.ai/api/v1/models/qwen/qwen-2.5-7b-instruct/endpoints (Phala only, precision `unknown`, $0.10 / $0.20) and https://openrouter.ai/api/v1/models/meta-llama/llama-3.1-8b-instruct/endpoints (CoreWeave `bf16`, $0.22 / $0.22, the only one listing structured outputs)
- Pausing Windows updates: https://support.microsoft.com/en-us/windows/deployment/updates-lifecycle/pause-updates-in-windows

The run times in "What you need" and appendix L are the owner's own, from a 7-case smoke run on 2026-09-30/10-01 on the owner's desktop (RTX 3060, 12 GB); they are not from a page, and the two OpenRouter models were not measured. The decisions cited are in `docs/adr/0012-post-review-v2-done-fixes-and-main.md` (15: the classifier is not redistributed; 16: Docker Engine inside WSL2 is the primary route). `socat`'s option syntax (`TCP-LISTEN`, `bind=`, `reuseaddr`, `fork`): its manual page, `man socat`, after `sudo apt install socat`.

The procedure itself is the owner's runbook, `docs/demo-runbook.md` section 9.9 (9.10 for the OpenRouter route, 9.8 for the local models); this guide is the same steps, run by the kit. The decision for the cloud route is `docs/adr/0014-full-cloud-route-for-the-v2-benchmark.md`.
