# Google Antigravity CLI (`agy`) host guidance

> **Normative authority:** `SKILL.md` is canonical for verdict semantics, retry rules, hard invariants, and completion requirements. This reference only elaborates procedures/examples and must not override `SKILL.md`.

Use this reference when the active host is **Google Antigravity CLI**, launched with `agy`.

## Native CLI discovery

Workspace skill:

```text
<workspace-root>/.agents/skills/runtime-feature-falsifier/SKILL.md
```

Workspace custom auditor:

```text
<workspace-root>/.agents/agents/runtime-feature-auditor/agent.md
```

Inside the TUI:

```text
/skills
```

should expose installed workspace skills, and:

```text
/agents
```

should expose `runtime-feature-auditor` among available custom agents.

The CLI's **global** skill root is different from Antigravity 2.0's global customization root:

```text
~/.gemini/antigravity-cli/skills/
```

RFF installs workspace scope by default, so this global-path difference does not create duplicate project copies.

## SYSTEM audit planning

Use Antigravity CLI plan mode before executing an exhaustive SYSTEM audit. The purpose is read-only discovery and a decision-complete audit plan, not runtime falsification itself.

After planning, switch back to an execution-capable mode before starting `environment_start` and subsequent runtime probes.

The CLI exposes execution modes and the `/agents`, `/skills`, `/hooks`, `/permissions`, and related TUI panels. Use `/hooks` to inspect strict RFF hooks if you explicitly installed them.

## Asynchronous subagents

Antigravity CLI can run background subagents. Parallel **execution** is allowed, but canonical RFF logging remains serialized through `auditctl.py`.

For parallel probes:

1. pre-register probes using `attempt-batch` START,
2. delegate runtime work,
3. collect concrete outputs/evidence,
4. batch FINISH through `auditctl.py`,
5. never let subagents hand-edit `attempts.jsonl` or `hypothesis-ledger.jsonl`.

The dedicated auditor intentionally limits direct mutation tools, but Antigravity subagents inherit granted capabilities and shell execution can still mutate the workspace. The tracked-source baseline/final gate remain required.

## Known hook boundary

Hooks are defense-in-depth, not a complete sandbox. Treat the audit controller's source baseline, append-only chains, evidence, and final gate as authoritative. Do not infer that a hook alone proves a probe used the intended runtime surface.

## Post-install smoke check

After installation/update:

1. launch `agy` in the project root,
2. open `/skills` and confirm `runtime-feature-falsifier`,
3. open `/agents` and confirm `runtime-feature-auditor`,
4. if strict hooks were installed, open `/hooks` and confirm the namespaced RFF hooks,
5. begin a fresh conversation after upgrading if discovery appears stale.
