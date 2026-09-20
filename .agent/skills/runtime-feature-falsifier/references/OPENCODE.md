# OpenCode integration

**Normative authority:** `SKILL.md` is normative for audit semantics. This reference only explains OpenCode-specific installation and operation and must not override the skill.

This reference is client-specific guidance for running Runtime Feature Falsifier (RFF) under OpenCode v2. `SKILL.md` remains normative for audit semantics.

## Native installation layout

RFF should be installed project-locally as:

```text
.opencode/
├── skills/
│   └── runtime-feature-falsifier/
│       ├── SKILL.md
│       ├── scripts/
│       ├── references/
│       └── assets/
└── agents/
    └── runtime-feature-auditor.md
```

OpenCode can discover `.agents/skills`, but RFF intentionally installs the skill at native project path `.opencode/skills/runtime-feature-falsifier/` so the installation has OpenCode's documented project-local precedence.

## Loading the skill

The skill ID is the directory name:

```text
runtime-feature-falsifier
```

The dedicated `runtime-feature-auditor` agent should load that exact ID with OpenCode's `skill` tool before starting an audit. Supporting files are loaded on demand from the skill directory.

## FEATURE audit

Use the dedicated agent and ask for a bounded feature audit, for example:

```text
Use runtime-feature-falsifier to audit upload-image. Do not repair implementation or tests. Persist until the declared matrix is exhausted, a reproducible counterexample is confirmed, or a legitimate BLOCKED/INCONCLUSIVE stop condition is reached.
```

## SYSTEM audit

For a whole-project audit, start in OpenCode's plan-oriented workflow when available, discover the complete runtime capability universe, then execute through the dedicated auditor. Do not replace exhaustive SYSTEM inventory with representative sampling.

## Permissions and integrity

The installed OpenCode auditor denies the `edit` action, but runtime auditing requires shell execution. Shell commands therefore remain available. RFF's tracked-source baseline and final completion gate are the authoritative protection against source mutation performed indirectly through shell commands.

Do not weaken the audit because an OpenCode permission prompt is inconvenient. If access to a real dependency or runtime surface is unavailable, record `BLOCKED` instead of substituting a mock or editing the target.

## Refresh after installation

Restart OpenCode or start a fresh session after installing/updating RFF so skill and agent discovery refresh cleanly.
