# Google Antigravity 2.0 host guidance

> **Normative authority:** `SKILL.md` is canonical for verdict semantics, retry rules, hard invariants, and completion requirements. This reference only elaborates procedures/examples and must not override `SKILL.md`.

Use this reference when the active host is **Google Antigravity 2.0** (desktop/standalone app or Antigravity IDE). For the terminal product (`agy`), also read [ANTIGRAVITY-CLI.md](ANTIGRAVITY-CLI.md).

## Workspace installation

Antigravity 2.0 discovers workspace skills from:

```text
<workspace-root>/.agents/skills/runtime-feature-falsifier/SKILL.md
```

and custom agents from:

```text
<workspace-root>/.agents/agents/runtime-feature-auditor/agent.md
```

The RFF installer deliberately uses these workspace paths because Antigravity 2.0 and Antigravity CLI share them. Selecting both Google integrations must not create duplicate same-name skill copies.

Global Antigravity 2.0 skills, when installed manually, belong under `~/.gemini/config/skills/`; RFF's project installer does not modify global configuration by default.

## Invocation and planning

For a bounded `FEATURE` audit, explicitly invoke/use `runtime-feature-falsifier` or select the `runtime-feature-auditor` custom agent.

For a `SYSTEM` audit, use Antigravity Planning Mode / an Implementation Plan artifact for read-only discovery and audit design before runtime probes. Establish the system boundary, feature inventory, runtime entry points, dependencies, and execution batches before leaving planning mode.

## Dedicated auditor

The distribution installs:

```text
.agents/agents/runtime-feature-auditor/agent.md
```

It is selectable as a primary custom agent and can also be invoked as a subagent. The definition preloads `skills/runtime-feature-falsifier`, excludes direct file-edit tools, and retains `run_command` for starting the target system and exercising real runtime surfaces.

The auditor is intentionally not a full sandbox. Shell commands can mutate files; therefore the tracked-source baseline and final deterministic gate remain authoritative.

## Browser-heavy features

Antigravity 2.0 has a specialized Browser capability. If the restricted auditor cannot access it, use the normal Antigravity agent/browser capability while preserving the same `.runtime-feature-audit/` workspace:

1. controlling auditor records `STARTED`,
2. browser interaction exercises the real UI,
3. evidence is captured,
4. controlling auditor immediately records `FINISHED`.

Do not replace browser runtime evidence with source inspection merely because the restricted auditor lacks Browser access.

## Optional strict workspace hooks

Antigravity JSON hooks can be configured at `.agents/hooks.json`. RFF keeps strict hooks **opt-in** because workspace hooks affect ordinary Antigravity work outside this audit.

When enabled they are dormant unless `.runtime-feature-audit/.active.json` exists and provide:

- `PreToolUse`: block obvious target-source/test mutation attempts;
- `PreInvocation`: re-inject the active-audit invariant after long trajectories/context compaction;
- `Stop`: continue execution until `auditctl gate --require-report` passes.

Do not manually edit/delete canonical audit lifecycle/log files to escape the gate.

## Shared 2.0 / CLI behavior

Antigravity 2.0 and Antigravity CLI share the same agent harness and workspace customization layout, so RFF installs the skill and auditor once even when both integrations are selected. Product-specific verification and TUI commands are documented in [ANTIGRAVITY-CLI.md](ANTIGRAVITY-CLI.md).

## Field-tested multi-run rule

A completed/idle Antigravity subagent should be treated as finished for that sealed audit run. For a remediation + re-audit cycle, terminate/leave the old auditor intact and invoke a **fresh `runtime-feature-auditor`** with a new audit workspace. Do not rely on messaging an idle subagent to restart itself, and do not append post-fix evidence to the pre-fix hash chain.

The dedicated auditor manifest intentionally uses only currently documented Antigravity tools. Permission approval is handled by Antigravity's permission system/UI; `list_permissions` and `ask_permission` are not custom-agent tools and must not appear in the manifest.
