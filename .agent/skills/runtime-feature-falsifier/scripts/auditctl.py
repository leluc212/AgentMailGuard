#!/usr/bin/env python3
"""Deterministic control plane for Runtime Feature Falsifier audits.

Subcommands:
  init             Create an audit workspace and empty plan.
  validate-plan    Validate audit-plan.json and minimum falsification coverage.
  attempt-start    Append a STARTED event *before* a runtime probe.
  attempt-finish   Append a terminal event after the probe.
  hypothesis-open  Open a falsification hypothesis with a bounded investigation budget.
  hypothesis-update Append evidence/status to a hypothesis.
  investigation-status Summarize persistent investigation state.
  summary          Summarize attempts and derived feature verdicts.
  report           Generate feature-matrix.md and audit-report.md.
  gate             Refuse finalization when the audit is incomplete or inconsistent.

All machine-facing output is JSON unless --format text is explicitly selected.
No external Python packages are required; the JSON Schema engine is vendored with the skill.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

# Keep runtime audits self-contained: the release vendors fastjsonschema (BSD)
# under the skill root so auditctl works without pip/venv setup. Bundled schemas
# target JSON Schema Draft 7, which fastjsonschema implements.
_VENDOR_DIR = Path(__file__).resolve().parent.parent / "vendor"
if str(_VENDOR_DIR) not in sys.path:
    sys.path.insert(0, str(_VENDOR_DIR))
try:
    import fastjsonschema
except ImportError as exc:  # pragma: no cover - release validator prevents this
    raise SystemExit(f"missing bundled JSON Schema engine: {exc}")

try:
    import fcntl  # Unix; used when available for append serialization.
except ImportError:  # pragma: no cover - Windows
    fcntl = None

try:
    import msvcrt  # Windows; used for cross-process file locking.
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None

RESULTS = {"SURVIVED", "FALSIFIED", "BLOCKED", "INCONCLUSIVE"}
RELATIONS = {"VALID", "INVALID", "BOUNDARY", "CONTRACT_UNKNOWN", "ENVIRONMENT"}
PATTERNS = {
    "TODO", "PLACEHOLDER", "FAKE_NOOP", "BTTLP", "HARDCODED",
    "HARDCODED_SUSPECTED", "MOCK_ONLY", "PARTIAL_IMPLEMENTATION",
    "REAL_BUT_BROKEN", "AUDIT_ENVIRONMENT_INTERFERENCE", "NONE_OBSERVED", "UNKNOWN_PATTERN",
}
CONFIDENCE = {"HIGH", "MEDIUM", "LOW"}
HYPOTHESIS_STATUSES = {"OPEN", "SUPPORTED", "REFUTED", "CONFIRMED", "BLOCKED", "BUDGET_EXHAUSTED"}
HYPOTHESIS_TERMINAL = {"REFUTED", "CONFIRMED", "BLOCKED", "BUDGET_EXHAUSTED"}
ESCALATION_STAGES = {str(i) for i in range(12)}
INTENTS = {
    "environment_start", "runtime_identity", "environment_collision", "baseline_valid", "valid_variation", "format_variation",
    "causal_sensitivity", "invalid_type_or_shape", "mime_extension_mismatch",
    "corrupt_input", "boundary_min", "boundary_max", "boundary_above_max",
    "state_transition", "round_trip", "persistence", "repeat_or_duplicate",
    "authorization", "dependency_authenticity", "workflow_completion",
    "hardcode_discrimination", "mock_discrimination", "bttlp_discrimination",
}
ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
SCHEMA_DIR = Path(__file__).resolve().parent.parent / "assets"
_CURRENT_AUDIT_DIR: Path | None = None


def load_schema(name: str) -> dict[str, Any]:
    return json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))


_SCHEMA_VALIDATOR_CACHE: dict[str, Any] = {}


def validate_schema(instance: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Validate using the vendored standards-compliant JSON Schema Draft 7 engine.

    Semantic Runtime Feature Falsifier invariants are layered on top of schema
    validation; structural JSON Schema semantics are delegated to fastjsonschema
    rather than reimplemented here. Validators are compiled once per process so
    gate/report operations do not repeatedly compile the same event schema.
    """
    key = json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    try:
        validator = _SCHEMA_VALIDATOR_CACHE.get(key)
        if validator is None:
            validator = fastjsonschema.compile(schema)
            _SCHEMA_VALIDATOR_CACHE[key] = validator
        validator(instance)
        return []
    except fastjsonschema.JsonSchemaException as exc:
        # Preserve the controller's historical list[str] error contract while
        # surfacing the validator's precise data path and rule message.
        detail = getattr(exc, "message", str(exc))
        name = getattr(exc, "name", None)
        if name and name != "data":
            detail = f"{name}: {detail}"
        return [f"{path}: {detail}"]

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def jdump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def emit(obj: Any, fmt: str = "json") -> None:
    if isinstance(obj, dict) and _CURRENT_AUDIT_DIR is not None:
        obj = dict(obj)
        obj["chain_heads"] = chain_heads(audit_paths(_CURRENT_AUDIT_DIR))
    if fmt == "json":
        print(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        if isinstance(obj, dict) and "ok" in obj:
            print("PASS" if obj["ok"] else "FAIL")
            for msg in obj.get("errors", []):
                print(f"- {msg}")
            for msg in obj.get("warnings", []):
                print(f"warning: {msg}")
            if obj.get("chain_heads"):
                print("chain_heads: " + json.dumps(obj["chain_heads"], sort_keys=True))
        else:
            print(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True))


def _jsonl_lock_path(path: Path) -> Path:
    return path.with_name("." + path.name + ".lock")


def _safe_chain_head(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        with exclusive_lock(_jsonl_lock_path(path)):
            events = _read_events_unlocked(path)
            return str(events[-1].get("event_sha256", "")) if events else ""
    except Exception:
        return "UNREADABLE"


def chain_heads(paths: dict[str, Path]) -> dict[str, str]:
    return {
        "attempts": _safe_chain_head(paths["log"]),
        "hypotheses": _safe_chain_head(paths["hypotheses"]),
    }


def with_heads(payload: dict[str, Any], paths: dict[str, Path]) -> dict[str, Any]:
    out = dict(payload)
    out["chain_heads"] = chain_heads(paths)
    return out


@contextmanager
def exclusive_lock(lock_path: Path):
    """Cross-process lock for JSONL writers on POSIX and Windows."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        elif msvcrt is not None:  # pragma: no cover - Windows
            if os.path.getsize(lock_path) == 0:
                os.write(fd, b"0")
                os.fsync(fd)
            while True:
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.05)
        yield
    finally:
        if fcntl is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        elif msvcrt is not None:  # pragma: no cover - Windows
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        os.close(fd)


def audit_paths(audit_dir: Path) -> dict[str, Path]:
    return {
        "dir": audit_dir,
        "plan": audit_dir / "audit-plan.json",
        "log": audit_dir / "attempts.jsonl",
        "hypotheses": audit_dir / "hypothesis-ledger.jsonl",
        "evidence": audit_dir / "evidence",
        "matrix": audit_dir / "feature-matrix.md",
        "report": audit_dir / "audit-report.md",
        "baseline": audit_dir / "tracked-source-baseline.json",
        "inventory": audit_dir / "feature-inventory.json",
        "coverage": audit_dir / "system-coverage.md",
        "active": audit_dir / ".active.json",
        "complete": audit_dir / ".complete.json",
        "report_state": audit_dir / ".report-state.json",
        "log_lock": audit_dir / ".attempts.jsonl.lock",
        "hypothesis_lock": audit_dir / ".hypothesis-ledger.jsonl.lock",
    }


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"missing required file: {path}")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON in {path}: {exc}")


def _file_sha256_unlocked(path: Path) -> str | None:
    if not path.exists():
        return None
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def file_sha256(path: Path) -> str | None:
    """Hash a file; canonical JSONL reads share the writer lock.

    This prevents report/gate freshness checks from hashing a file while another
    process is midway through an append.
    """
    if path.suffix == ".jsonl":
        with exclusive_lock(_jsonl_lock_path(path)):
            return _file_sha256_unlocked(path)
    return _file_sha256_unlocked(path)


def report_state_snapshot(paths: dict[str, Path], plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "audit_id": plan.get("audit_id"),
        "plan_sha256": file_sha256(paths["plan"]),
        "attempts_sha256": file_sha256(paths["log"]),
        "hypotheses_sha256": file_sha256(paths["hypotheses"]),
        "inventory_sha256": file_sha256(paths["inventory"]) if plan.get("audit_mode") == "SYSTEM" else None,
    }


def find_feature_probe(plan: dict[str, Any], feature_id: str, probe_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    for feature in plan.get("features", []):
        if feature.get("feature_id") == feature_id:
            for probe in feature.get("probes", []):
                if probe.get("probe_id") == probe_id:
                    return feature, probe
            raise SystemExit(f"probe not found in feature {feature_id}: {probe_id}")
    raise SystemExit(f"feature not found in plan: {feature_id}")


def validate_plan_obj(plan: Any) -> dict[str, Any]:
    errors: list[str] = validate_schema(plan, load_schema("audit-plan.schema.json"))
    warnings: list[str] = []
    if not isinstance(plan, dict):
        return {"ok": False, "errors": ["audit plan must be a JSON object"], "warnings": []}
    if plan.get("schema_version") != "2.0":
        errors.append('schema_version must be "2.0"')
    audit_mode = plan.get("audit_mode", "FEATURE")
    if audit_mode not in {"FEATURE", "SYSTEM"}:
        errors.append("audit_mode must be FEATURE or SYSTEM")
    for key in ("audit_id", "audit_mode", "target", "features"):
        if key not in plan:
            errors.append(f"missing top-level field: {key}")
    if not isinstance(plan.get("features"), list) or not plan.get("features"):
        errors.append("features must be a non-empty array")
        return {"ok": not errors, "errors": errors, "warnings": warnings}

    feature_ids: set[str] = set()
    probe_ids: set[str] = set()
    for idx, feature in enumerate(plan["features"], 1):
        prefix = f"feature[{idx}]"
        fid = feature.get("feature_id")
        if not isinstance(fid, str) or not ID_RE.match(fid):
            errors.append(f"{prefix}.feature_id must be a stable lowercase-ish id")
            continue
        if fid in feature_ids:
            errors.append(f"duplicate feature_id: {fid}")
        feature_ids.add(fid)
        for key in ("claim", "claim_source", "entry_points", "expected_end_effects"):
            if not feature.get(key):
                errors.append(f"{fid}: missing/non-empty {key}")
        for flag in ("input_sensitive", "stateful", "dependency_sensitive"):
            if not isinstance(feature.get(flag), bool):
                errors.append(f"{fid}: {flag} must be boolean")
        probes = feature.get("probes")
        if not isinstance(probes, list) or not probes:
            errors.append(f"{fid}: probes must be a non-empty array")
            continue
        intents: list[str] = []
        for pidx, probe in enumerate(probes, 1):
            pfx = f"{fid}.probe[{pidx}]"
            pid = probe.get("probe_id")
            if not isinstance(pid, str) or not ID_RE.match(pid):
                errors.append(f"{pfx}.probe_id must be a stable id")
            elif pid in probe_ids:
                errors.append(f"duplicate probe_id across audit: {pid}")
            else:
                probe_ids.add(pid)
            intent = probe.get("probe_intent")
            intents.append(intent)
            if not isinstance(intent, str) or not intent:
                errors.append(f"{pfx}: missing probe_intent")
            elif intent not in INTENTS:
                warnings.append(f"{pfx}: custom probe_intent {intent!r}; ensure it is specific")
            relation = probe.get("contract_relation")
            if relation not in RELATIONS:
                errors.append(f"{pfx}: contract_relation must be one of {sorted(RELATIONS)}")
            if not probe.get("expected"):
                errors.append(f"{pfx}: expected is required")
            if "required" not in probe or not isinstance(probe.get("required"), bool):
                errors.append(f"{pfx}: required must be boolean")
            if not isinstance(probe.get("effect_checks", []), list):
                errors.append(f"{pfx}: effect_checks must be an array")

        # Coverage gates. They are conditional on feature shape rather than blindly universal.
        required_intents = [p.get("probe_intent") for p in probes if p.get("required", True)]
        if "baseline_valid" not in required_intents:
            errors.append(f"{fid}: required probes must include baseline_valid")
        if feature.get("input_sensitive"):
            if not any(i in required_intents for i in ("valid_variation", "format_variation")):
                errors.append(f"{fid}: input_sensitive feature needs valid_variation or format_variation")
            if "causal_sensitivity" not in required_intents:
                errors.append(f"{fid}: input_sensitive feature needs causal_sensitivity")
            if not any(i in required_intents for i in ("invalid_type_or_shape", "corrupt_input", "boundary_min", "boundary_max")):
                errors.append(f"{fid}: input_sensitive feature needs at least one negative/boundary probe")
        if feature.get("stateful"):
            if not any(i in required_intents for i in ("state_transition", "round_trip", "persistence", "workflow_completion")):
                errors.append(f"{fid}: stateful feature needs a state/end-effect probe")
        if feature.get("dependency_sensitive") and "dependency_authenticity" not in required_intents:
            errors.append(f"{fid}: dependency_sensitive feature needs dependency_authenticity")

    target = plan.get("target") if isinstance(plan.get("target"), dict) else {}
    if not str(target.get("startup_path", "")).strip():
        errors.append("target.startup_path is required; document the real supported startup path")
    if not str(target.get("runtime_identity_expectation", "")).strip():
        errors.append("target.runtime_identity_expectation is required; state what process/server/build must actually be running")

    required_preflight = {
        "environment_start": "establish startup health",
        "runtime_identity": "prove what runtime/process is actually serving the target",
        "environment_collision": "check audit/test/live shared-resource interference",
    }
    for preflight_intent, purpose in required_preflight.items():
        probes_for_intent = [
            p for feature in plan.get("features", []) for p in feature.get("probes", [])
            if isinstance(p, dict) and p.get("required", True) and p.get("probe_intent") == preflight_intent
        ]
        if not probes_for_intent:
            errors.append(f"audit requires one required {preflight_intent} probe to {purpose}")
            continue
        if not any(p.get("contract_relation") == "ENVIRONMENT" for p in probes_for_intent):
            errors.append(f"{preflight_intent} probe must use contract_relation=ENVIRONMENT")
        if preflight_intent in {"runtime_identity", "environment_collision"} and not any(p.get("effect_checks") for p in probes_for_intent):
            errors.append(f"{preflight_intent} probe requires at least one effect_check")

    return {"ok": not errors, "errors": errors, "warnings": warnings, "feature_count": len(feature_ids), "probe_count": len(probe_ids)}


def validate_inventory_obj(inventory: Any, plan: dict[str, Any] | None = None) -> dict[str, Any]:
    errors: list[str] = validate_schema(inventory, load_schema("feature-inventory.schema.json"))
    warnings: list[str] = []
    if not isinstance(inventory, dict):
        return {"ok": False, "errors": ["feature inventory must be a JSON object"], "warnings": []}
    if inventory.get("schema_version") != "1.0":
        errors.append('feature inventory schema_version must be "1.0"')
    if not inventory.get("audit_id"):
        errors.append("feature inventory missing audit_id")
    status = inventory.get("discovery_status")
    if status == "IN_PROGRESS":
        errors.append("feature inventory discovery is still IN_PROGRESS; finish discovery before validation")
    elif status not in {"COMPLETE", "PARTIAL_BLOCKED"}:
        errors.append("feature inventory discovery_status must be COMPLETE or PARTIAL_BLOCKED")
    blockers = inventory.get("discovery_blockers", [])
    if status == "PARTIAL_BLOCKED" and not blockers:
        errors.append("PARTIAL_BLOCKED discovery requires at least one discovery_blocker")
    if status == "COMPLETE" and blockers:
        errors.append("COMPLETE discovery cannot retain discovery_blockers; use PARTIAL_BLOCKED")
    sources = inventory.get("discovery_sources")
    if not isinstance(sources, list) or not sources:
        errors.append("feature inventory requires at least one discovery source")
        sources = []
    kinds = {str(x.get("kind", "")).strip() for x in sources if isinstance(x, dict)}
    if len(kinds) < 2:
        if status == "COMPLETE":
            errors.append("COMPLETE SYSTEM discovery requires at least two materially independent discovery-source kinds")
        else:
            warnings.append("SYSTEM discovery used fewer than two source kinds; limitation must remain explicit")
    workflow = inventory.get("workflow_coverage")
    if not isinstance(workflow, dict) or not isinstance(workflow.get("applicable"), bool):
        errors.append("workflow_coverage.applicable must be boolean")
        workflow = {"applicable": False}
    elif workflow.get("applicable") is False and not str(workflow.get("reason", "")).strip():
        errors.append("workflow_coverage requires a reason when applicable=false")
    features = inventory.get("features")
    if not isinstance(features, list) or not features:
        errors.append("feature inventory features must be a non-empty array")
        features = []
    seen: set[str] = set()
    in_scope: set[str] = set()
    workflow_ids: set[str] = set()
    excluded: set[str] = set()
    for idx, item in enumerate(features, 1):
        pfx = f"inventory.feature[{idx}]"
        if not isinstance(item, dict):
            errors.append(f"{pfx} must be an object")
            continue
        fid = item.get("feature_id")
        if not isinstance(fid, str) or not ID_RE.match(fid):
            errors.append(f"{pfx}.feature_id must be a stable lowercase-ish id")
            continue
        if fid in seen:
            errors.append(f"duplicate inventory feature_id: {fid}")
        seen.add(fid)
        if item.get("kind") not in {"CAPABILITY", "FUNCTION", "WORKFLOW", "SYSTEM"}:
            errors.append(f"{fid}: kind must be CAPABILITY, FUNCTION, WORKFLOW, or SYSTEM")
        if not item.get("claim"):
            errors.append(f"{fid}: inventory claim is required")
        if not isinstance(item.get("claim_sources"), list) or not item.get("claim_sources"):
            errors.append(f"{fid}: claim_sources must be a non-empty array")
        if not isinstance(item.get("surfaces"), list) or not item.get("surfaces"):
            errors.append(f"{fid}: surfaces must be a non-empty array")
        disposition = item.get("disposition")
        if disposition == "IN_SCOPE":
            in_scope.add(fid)
            if item.get("kind") == "WORKFLOW":
                workflow_ids.add(fid)
        elif disposition == "EXCLUDED":
            excluded.add(fid)
            basis = item.get("exclusion_basis")
            if basis not in {"USER_SCOPE", "NOT_RUNTIME_CAPABILITY", "DUPLICATE_ALIAS", "OUTSIDE_TARGET_SYSTEM"}:
                errors.append(f"{fid}: EXCLUDED item requires exclusion_basis USER_SCOPE, NOT_RUNTIME_CAPABILITY, DUPLICATE_ALIAS, or OUTSIDE_TARGET_SYSTEM")
            if not str(item.get("exclusion_reason", "")).strip():
                errors.append(f"{fid}: EXCLUDED inventory item requires exclusion_reason")
        else:
            errors.append(f"{fid}: disposition must be IN_SCOPE or EXCLUDED")
    if workflow.get("applicable") is True and not workflow_ids:
        errors.append("workflow_coverage.applicable=true requires at least one IN_SCOPE WORKFLOW inventory item")
    unmapped: list[str] = []
    extra_planned: list[str] = []
    if plan is not None:
        if inventory.get("audit_id") and plan.get("audit_id") and inventory.get("audit_id") != plan.get("audit_id"):
            errors.append("feature inventory audit_id does not match audit plan")
        plan_ids = {f.get("feature_id") for f in plan.get("features", []) if isinstance(f, dict) and f.get("feature_id")}
        unmapped = sorted(in_scope - plan_ids)
        extra_planned = sorted(plan_ids - in_scope)
        if unmapped:
            errors.append("SYSTEM inventory has IN_SCOPE features missing from audit plan: " + ", ".join(unmapped))
        if extra_planned:
            errors.append("SYSTEM audit plan contains features not marked IN_SCOPE in inventory: " + ", ".join(extra_planned))
    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "discovered_count": len(seen),
        "in_scope_count": len(in_scope),
        "excluded_count": len(excluded),
        "workflow_count": len(workflow_ids),
        "unmapped": unmapped,
        "extra_planned": extra_planned,
        "discovery_status": status,
        "discovery_blockers": blockers,
    }


def _read_events_unlocked(log_path: Path) -> list[dict[str, Any]]:
    if not log_path.exists():
        return []
    events: list[dict[str, Any]] = []
    with log_path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"invalid JSONL at {log_path}:{lineno}: {exc}")
            event["_line"] = lineno
            events.append(event)
    return events


def read_events(log_path: Path) -> list[dict[str, Any]]:
    """Read a canonical JSONL snapshot while holding the same lock as writers."""
    with exclusive_lock(_jsonl_lock_path(log_path)):
        return _read_events_unlocked(log_path)


def verify_hash_chain(events: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    previous = ""
    expected_seq = 1
    schema = None
    for event in events:
        line = event.get("_line", "?")
        if event.get("sequence") != expected_seq:
            errors.append(f"attempts.jsonl line {line}: sequence {event.get('sequence')} != expected {expected_seq}")
        if event.get("prev_event_sha256", "") != previous:
            errors.append(f"attempts.jsonl line {line}: prev_event_sha256 chain mismatch")
        stated = event.get("event_sha256")
        material = {k: v for k, v in event.items() if k not in {"event_sha256", "_line"}}
        actual = hashlib.sha256(jdump(material).encode("utf-8")).hexdigest()
        if stated != actual:
            errors.append(f"attempts.jsonl line {line}: event_sha256 mismatch")
        previous = stated or ""
        expected_seq += 1
    return errors


def _event_schema_for(log_path: Path) -> dict[str, Any] | None:
    if log_path.name == "attempts.jsonl":
        return load_schema("attempt-event.schema.json")
    if log_path.name == "hypothesis-ledger.jsonl":
        return load_schema("hypothesis-event.schema.json")
    return None


def append_events(log_path: Path, payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Atomically serialize one or more hash-chained events under a cross-platform lock."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = _jsonl_lock_path(log_path)
    completed: list[dict[str, Any]] = []
    with exclusive_lock(lock_path):
        existing = _read_events_unlocked(log_path)
        prev_hash = existing[-1].get("event_sha256", "") if existing else ""
        sequence = len(existing) + 1
        schema = _event_schema_for(log_path)
        for raw_payload in payloads:
            payload = dict(raw_payload)
            payload["sequence"] = sequence
            payload["prev_event_sha256"] = prev_hash
            material = dict(payload)
            payload["event_sha256"] = hashlib.sha256(jdump(material).encode("utf-8")).hexdigest()
            if schema is not None:
                schema_errors = validate_schema(payload, schema)
                if schema_errors:
                    raise SystemExit("event failed bundled schema validation: " + "; ".join(schema_errors))
            completed.append(payload)
            prev_hash = payload["event_sha256"]
            sequence += 1
        if completed:
            with log_path.open("a", encoding="utf-8", newline="") as fh:
                for payload in completed:
                    fh.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
    return completed


def append_event(log_path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    return append_events(log_path, [payload])[0]


def fold_attempts(events: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    attempts: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for event in events:
        aid = event.get("attempt_id")
        et = event.get("event_type")
        if not aid:
            errors.append(f"line {event.get('_line')}: missing attempt_id")
            continue
        if et == "STARTED":
            if aid in attempts:
                errors.append(f"duplicate STARTED event for {aid}")
            attempts[aid] = {"start": event, "finish": None}
        elif et == "FINISHED":
            if aid not in attempts:
                errors.append(f"FINISHED without STARTED for {aid}")
                attempts[aid] = {"start": None, "finish": event}
            elif attempts[aid]["finish"] is not None:
                errors.append(f"duplicate FINISHED event for {aid}")
            else:
                attempts[aid]["finish"] = event
        else:
            errors.append(f"line {event.get('_line')}: event_type must be STARTED or FINISHED")
    return attempts, errors


def fold_hypotheses(events: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    hypotheses: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for event in events:
        hid = event.get("hypothesis_id")
        et = event.get("event_type")
        if not hid:
            errors.append(f"hypothesis line {event.get('_line')}: missing hypothesis_id")
            continue
        if et == "HYPOTHESIS_OPENED":
            if hid in hypotheses:
                errors.append(f"duplicate HYPOTHESIS_OPENED for {hid}")
                continue
            hypotheses[hid] = {
                "hypothesis_id": hid,
                "feature_id": event.get("feature_id"),
                "statement": event.get("statement"),
                "status": "OPEN",
                "max_attempts": int(event.get("max_attempts", 6)),
                "trigger_attempt_id": event.get("trigger_attempt_id"),
                "next_probe": event.get("next_probe", ""),
                "evidence_for": list(event.get("evidence_for") or []),
                "evidence_against": list(event.get("evidence_against") or []),
                "updates": 0,
            }
        elif et == "HYPOTHESIS_UPDATED":
            if hid not in hypotheses:
                errors.append(f"HYPOTHESIS_UPDATED without open for {hid}")
                continue
            h = hypotheses[hid]
            status = event.get("status")
            if status not in HYPOTHESIS_STATUSES:
                errors.append(f"invalid hypothesis status for {hid}: {status}")
                continue
            h["status"] = status
            h["next_probe"] = event.get("next_probe", h.get("next_probe", ""))
            h["evidence_for"].extend(event.get("evidence_for") or [])
            h["evidence_against"].extend(event.get("evidence_against") or [])
            h["updates"] += 1
            h["last_changed_variable"] = event.get("changed_variable", "")
            h["notes"] = event.get("notes", "")
        else:
            errors.append(f"hypothesis line {event.get('_line')}: invalid event_type {et!r}")
    return hypotheses, errors


def hypothesis_attempt_counts(attempt_events: list[dict[str, Any]]) -> Counter:
    counts: Counter = Counter()
    for event in attempt_events:
        if event.get("event_type") == "STARTED" and event.get("hypothesis_id"):
            counts[event["hypothesis_id"]] += 1
    return counts


def investigation_summary(paths: dict[str, Path]) -> dict[str, Any]:
    events = read_events(paths["hypotheses"])
    errors = verify_hash_chain(events) if events else []
    hypotheses, fold_errors = fold_hypotheses(events)
    errors.extend(fold_errors)
    attempt_events = read_events(paths["log"])
    counts = hypothesis_attempt_counts(attempt_events)
    rows = []
    for hid, h in sorted(hypotheses.items()):
        row = dict(h)
        row["attempts_used"] = counts.get(hid, 0)
        row["attempts_remaining"] = max(0, h.get("max_attempts", 0) - counts.get(hid, 0))
        rows.append(row)
    active = [r for r in rows if r.get("status") not in HYPOTHESIS_TERMINAL]
    return {
        "ok": not errors,
        "errors": errors,
        "hypothesis_count": len(rows),
        "active_count": len(active),
        "active_hypotheses": [r["hypothesis_id"] for r in active],
        "hypotheses": rows,
    }


def apply_investigation_verdicts(summary: dict[str, Any], investigation: dict[str, Any]) -> dict[str, Any]:
    """Project terminal investigation uncertainty into feature verdicts.

    A confirmed hypothesis must already be backed by a FALSIFIED attempt (gate rule),
    so FALSIFIED remains dominant. BLOCKED/BUDGET_EXHAUSTED hypotheses prevent a
    surviving matrix from being reported as NOT_FALSIFIED.
    """
    by_feature: dict[str, set[str]] = defaultdict(set)
    for h in investigation.get("hypotheses", []):
        if h.get("feature_id") and h.get("status"):
            by_feature[h["feature_id"]].add(h["status"])
    for feature in summary.get("features", []):
        if feature.get("verdict") == "FALSIFIED":
            continue
        statuses = by_feature.get(feature.get("feature_id", ""), set())
        if "BUDGET_EXHAUSTED" in statuses:
            feature["verdict"] = "INCONCLUSIVE"
        elif "BLOCKED" in statuses:
            feature["verdict"] = "BLOCKED"
    return summary


def derive_summary(plan: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    attempts, fold_errors = fold_attempts(events)
    by_probe: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for pair in attempts.values():
        if pair.get("finish"):
            by_probe[pair["finish"].get("probe_id", "")].append(pair["finish"])
    features_out: list[dict[str, Any]] = []
    for feature in plan.get("features", []):
        terminals: list[dict[str, Any]] = []
        required_missing: list[str] = []
        required_blocked: list[str] = []
        required_inconclusive: list[str] = []
        for probe in feature.get("probes", []):
            pid = probe.get("probe_id", "")
            rows = by_probe.get(pid, [])
            terminals.extend(rows)
            if probe.get("required", True):
                if not rows:
                    required_missing.append(pid or "<missing>")
                elif not any(r.get("result") in {"SURVIVED", "FALSIFIED"} for r in rows):
                    if any(r.get("result") == "INCONCLUSIVE" for r in rows):
                        required_inconclusive.append(pid)
                    else:
                        required_blocked.append(pid)
        if any(r.get("result") == "FALSIFIED" for r in terminals):
            verdict = "FALSIFIED"
        elif required_missing:
            verdict = "INCOMPLETE"
        elif required_inconclusive:
            verdict = "INCONCLUSIVE"
        elif required_blocked:
            verdict = "BLOCKED"
        elif terminals:
            verdict = "NOT_FALSIFIED"
        else:
            verdict = "INCOMPLETE"
        patterns = sorted({r.get("failure_pattern") for r in terminals if r.get("failure_pattern") not in (None, "NONE_OBSERVED")})
        features_out.append({
            "feature_id": feature.get("feature_id"),
            "claim": feature.get("claim"),
            "verdict": verdict,
            "terminal_attempts": len(terminals),
            "required_missing": required_missing,
            "required_blocked": required_blocked,
            "required_inconclusive": required_inconclusive,
            "patterns": patterns,
            "results": dict(Counter(r.get("result") for r in terminals)),
        })
    return {
        "audit_id": plan.get("audit_id"),
        "event_count": len(events),
        "attempt_count": len(attempts),
        "open_attempts": sorted(aid for aid, pair in attempts.items() if pair.get("start") and not pair.get("finish")),
        "fold_errors": fold_errors,
        "features": features_out,
    }



def tracked_source_snapshot(root: Path) -> dict[str, Any]:
    """Hash Git-tracked files so an audit cannot quietly mutate source/tests.

    Returns an unavailable snapshot outside Git rather than failing the audit setup.
    Runtime-generated untracked files are intentionally excluded.
    """
    try:
        top = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        return {"available": False, "reason": "target is not a readable Git worktree"}
    git_root = Path(top)
    try:
        head = subprocess.run(
            ["git", "-C", str(git_root), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        raw = subprocess.run(
            ["git", "-C", str(git_root), "ls-files", "-z"],
            check=True, capture_output=True,
        ).stdout
    except subprocess.CalledProcessError as exc:
        return {"available": False, "reason": f"git snapshot failed: {exc}"}
    files: dict[str, str] = {}
    for item in raw.split(b"\0"):
        if not item:
            continue
        rel = item.decode("utf-8", errors="surrogateescape")
        path = git_root / rel
        if not path.is_file():
            files[rel] = "<MISSING>"
            continue
        files[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "available": True,
        "git_root": str(git_root),
        "head": head,
        "tracked_file_count": len(files),
        "files": files,
    }


def compare_tracked_source(baseline: dict[str, Any]) -> list[str]:
    if not baseline.get("available"):
        return []
    root = Path(baseline["git_root"])
    current = tracked_source_snapshot(root)
    if not current.get("available"):
        return ["could not re-read Git tracked-source snapshot during final gate"]
    errors: list[str] = []
    before = baseline.get("files", {})
    after = current.get("files", {})
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    if changed:
        preview = ", ".join(changed[:12]) + (" ..." if len(changed) > 12 else "")
        errors.append(
            f"tracked source/test files changed during audit ({len(changed)}): {preview}. "
            "Restore only with user approval; do not hide the mutation."
        )
    return errors

def cmd_init(args: argparse.Namespace) -> int:
    paths = audit_paths(args.audit_dir)
    paths["dir"].mkdir(parents=True, exist_ok=True)
    paths["evidence"].mkdir(parents=True, exist_ok=True)
    if paths["complete"].exists():
        emit({"ok": False, "errors": [
            f"audit workspace is sealed: {paths['complete']}. For a re-audit after remediation, use a fresh auditor instance and a new audit directory (for example .runtime-feature-audit-run2)."
        ]}, args.format)
        return 2
    if paths["plan"].exists() and not args.force:
        emit({"ok": False, "errors": [f"plan already exists: {paths['plan']} (use --force to replace)"]}, args.format)
        return 2
    audit_id = args.audit_id or f"rff-{uuid4().hex[:10]}"
    audit_mode = args.mode.upper()
    plan = {
        "schema_version": "2.0",
        "audit_id": audit_id,
        "audit_mode": audit_mode,
        "created_at_utc": utc_now(),
        "target": {
            "project": args.project_name,
            "environment": args.environment,
            "scope": args.scope,
            "startup_path": "",
            "runtime_identity_expectation": "",
            "collision_surfaces": [],
        },
        "features": [],
    }
    paths["plan"].write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    if audit_mode == "SYSTEM":
        inventory = {
            "schema_version": "1.0",
            "audit_id": audit_id,
            "discovery_status": "IN_PROGRESS",
            "discovery_sources": [],
            "discovery_blockers": [],
            "workflow_coverage": {"applicable": False, "reason": "Populate after system discovery."},
            "features": [],
        }
        paths["inventory"].write_text(json.dumps(inventory, indent=2) + "\n", encoding="utf-8")
    baseline_root = args.target_root.resolve()
    baseline = tracked_source_snapshot(baseline_root)
    paths["baseline"].write_text(json.dumps(baseline, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if paths["complete"].exists():
        paths["complete"].unlink()
    if paths["report_state"].exists():
        paths["report_state"].unlink()
    paths["active"].write_text(json.dumps({
        "audit_id": audit_id,
        "audit_mode": audit_mode,
        "started_at_utc": utc_now(),
        "target_root": str(baseline_root),
    }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    next_step = "Populate features/probes, then run validate-plan."
    if audit_mode == "SYSTEM":
        next_step = "Populate feature-inventory.json and audit-plan.json from whole-system discovery, then run validate-plan."
    emit(with_heads({
        "ok": True,
        "audit_mode": audit_mode,
        "audit_dir": str(paths["dir"]),
        "plan": str(paths["plan"]),
        "inventory": str(paths["inventory"]) if audit_mode == "SYSTEM" else None,
        "tracked_source_baseline": baseline.get("available", False),
        "tracked_source_root": str(baseline_root),
        "next": next_step,
    }, paths), args.format)
    return 0


def validate_system_files(paths: dict[str, Path], plan: dict[str, Any]) -> dict[str, Any]:
    if plan.get("audit_mode", "FEATURE") != "SYSTEM":
        return {"ok": True, "errors": [], "warnings": [], "applicable": False}
    if not paths["inventory"].exists():
        return {"ok": False, "errors": [f"SYSTEM audit requires feature inventory: {paths['inventory']}"], "warnings": [], "applicable": True}
    inventory = load_json(paths["inventory"])
    result = validate_inventory_obj(inventory, plan)
    result["applicable"] = True
    return result


def cmd_validate_plan(args: argparse.Namespace) -> int:
    paths = audit_paths(args.audit_dir)
    plan = load_json(paths["plan"])
    result = validate_plan_obj(plan)
    system_result = validate_system_files(paths, plan)
    result["errors"].extend(system_result.get("errors", []))
    result["warnings"].extend(system_result.get("warnings", []))
    result["ok"] = not result["errors"]
    if system_result.get("applicable"):
        result["system_inventory"] = {k: v for k, v in system_result.items() if k not in {"ok", "errors", "warnings", "applicable"}}
    emit(with_heads(result, paths), args.format)
    return 0 if result["ok"] else 2


PREFLIGHT_INTENTS = {"environment_start", "runtime_identity", "environment_collision"}


def preflight_finishes(events: list[dict[str, Any]], intent: str) -> list[dict[str, Any]]:
    attempts, errors = fold_attempts(events)
    if errors:
        return []
    return [
        pair.get("finish") or {}
        for pair in attempts.values()
        if (pair.get("start") or {}).get("probe_intent") == intent and pair.get("finish")
    ]


def startup_health_survived(events: list[dict[str, Any]]) -> bool:
    return any(x.get("result") == "SURVIVED" for x in preflight_finishes(events, "environment_start"))


def runtime_identity_completed(events: list[dict[str, Any]]) -> bool:
    return bool(preflight_finishes(events, "runtime_identity"))


def collision_check_survived(events: list[dict[str, Any]]) -> bool:
    return any(x.get("result") == "SURVIVED" for x in preflight_finishes(events, "environment_collision"))


def preflight_error_for(intent: str, events: list[dict[str, Any]]) -> str | None:
    if intent == "environment_start":
        return None
    if not startup_health_survived(events):
        return "startup health has not completed SURVIVED; finish environment_start first"
    if intent == "runtime_identity":
        return None
    if not runtime_identity_completed(events):
        return "runtime identity has not completed; finish runtime_identity before collision/feature probes"
    if intent == "environment_collision":
        return None
    if not collision_check_survived(events):
        return "audit environment collision check has not completed SURVIVED; isolate shared queues/DB schemas/buckets/ports/tenants before feature probes"
    return None


def cmd_attempt_start(args: argparse.Namespace) -> int:
    paths = audit_paths(args.audit_dir)
    plan = load_json(paths["plan"])
    valid = validate_plan_obj(plan)
    system_valid = validate_system_files(paths, plan)
    combined_errors = [*valid["errors"], *system_valid.get("errors", [])]
    if combined_errors:
        emit({"ok": False, "errors": ["audit plan/system inventory is invalid; run validate-plan first", *combined_errors]}, args.format)
        return 2
    feature, probe = find_feature_probe(plan, args.feature_id, args.probe_id)
    if (feature.get("dependency_sensitive") or probe.get("probe_intent") in PREFLIGHT_INTENTS) and not str(args.repro_command or "").strip():
        emit(with_heads({"ok": False, "errors": [
            f"{feature['feature_id']}/{probe['probe_id']} requires --repro-command because it is preflight/dependency-sensitive"
        ]}, paths), args.format)
        return 2
    existing_attempt_events = read_events(paths["log"])
    existing_attempts, attempt_errors = fold_attempts(existing_attempt_events)
    if attempt_errors:
        emit({"ok": False, "errors": attempt_errors}, args.format)
        return 2
    preflight_error = preflight_error_for(probe.get("probe_intent", ""), existing_attempt_events)
    if preflight_error:
        emit(with_heads({"ok": False, "errors": [preflight_error]}, paths), args.format)
        return 2
    prior_same_probe = [pair for pair in existing_attempts.values() if pair.get("start") and pair["start"].get("probe_id") == args.probe_id]
    if prior_same_probe and not (getattr(args, "changed_variable", None) or getattr(args, "retry_reason", None)):
        emit({"ok": False, "errors": [
            f"repeated probe {args.probe_id} requires --changed-variable or --retry-reason; identical retries are not persistent investigation"
        ]}, args.format)
        return 2
    if getattr(args, "hypothesis_id", None):
        hstate = investigation_summary(paths)
        if not hstate.get("ok"):
            emit({"ok": False, "errors": hstate.get("errors", [])}, args.format)
            return 2
        h = next((x for x in hstate.get("hypotheses", []) if x.get("hypothesis_id") == args.hypothesis_id), None)
        if not h:
            emit({"ok": False, "errors": [f"hypothesis not found: {getattr(args, "hypothesis_id", None)}"]}, args.format)
            return 2
        if h.get("status") in HYPOTHESIS_TERMINAL:
            emit({"ok": False, "errors": [f"cannot attach attempt to terminal hypothesis {getattr(args, "hypothesis_id", None)}: {h.get('status')}"]}, args.format)
            return 2
        if h.get("attempts_used", 0) >= h.get("max_attempts", 0):
            emit({"ok": False, "errors": [f"hypothesis budget exhausted for {getattr(args, "hypothesis_id", None)}; close it as BUDGET_EXHAUSTED or justify a new hypothesis"]}, args.format)
            return 2
        if h.get("feature_id") != args.feature_id:
            emit({"ok": False, "errors": [f"hypothesis {getattr(args, "hypothesis_id", None)} belongs to {h.get('feature_id')}, not {args.feature_id}"]}, args.format)
            return 2
    attempt_id = args.attempt_id or f"att-{uuid4().hex[:12]}"
    event = append_event(paths["log"], {
        "schema_version": "2.0",
        "event_type": "STARTED",
        "attempt_id": attempt_id,
        "timestamp_utc": utc_now(),
        "feature_id": feature["feature_id"],
        "feature_claim": feature["claim"],
        "probe_id": probe["probe_id"],
        "probe_intent": probe["probe_intent"],
        "contract_relation": probe["contract_relation"],
        "input_family": probe.get("input_family", ""),
        "input_case": probe.get("input_case", ""),
        "entry_point": probe.get("entry_point") or (feature.get("entry_points") or [""])[0],
        "expected": probe["expected"],
        "effect_checks": probe.get("effect_checks", []),
        "action": args.action or probe.get("action", ""),
        "repro_command": args.repro_command,
        "preconditions": args.precondition,
        "hypothesis_id": getattr(args, "hypothesis_id", None),
        "changed_variable": getattr(args, "changed_variable", None),
        "retry_reason": getattr(args, "retry_reason", None),
        "escalation_stage": getattr(args, "escalation_stage", None),
    })
    emit(with_heads({"ok": True, "attempt_id": attempt_id, "sequence": event["sequence"], "event_sha256": event["event_sha256"]}, paths), args.format)
    return 0


def cmd_attempt_finish(args: argparse.Namespace) -> int:
    paths = audit_paths(args.audit_dir)
    events = read_events(paths["log"])
    attempts, errors = fold_attempts(events)
    if errors:
        emit({"ok": False, "errors": errors}, args.format)
        return 2
    pair = attempts.get(args.attempt_id)
    if not pair or not pair.get("start"):
        emit({"ok": False, "errors": [f"attempt has no STARTED event: {args.attempt_id}"]}, args.format)
        return 2
    if pair.get("finish"):
        emit({"ok": False, "errors": [f"attempt already finished: {args.attempt_id}"]}, args.format)
        return 2
    start = pair["start"]
    event = append_event(paths["log"], {
        "schema_version": "2.0",
        "event_type": "FINISHED",
        "attempt_id": args.attempt_id,
        "timestamp_utc": utc_now(),
        "feature_id": start["feature_id"],
        "feature_claim": start["feature_claim"],
        "probe_id": start["probe_id"],
        "probe_intent": start["probe_intent"],
        "contract_relation": start["contract_relation"],
        "input_family": start.get("input_family", ""),
        "input_case": start.get("input_case", ""),
        "observed": args.observed,
        "side_effect_check": args.side_effect_check,
        "persistence_check": args.persistence_check,
        "http_status": args.http_status,
        "exit_code": args.exit_code,
        "evidence_refs": args.evidence,
        "result": args.result,
        "failure_pattern": args.failure_pattern,
        "confidence": args.confidence,
        "notes": args.notes,
        "hypothesis_id": start.get("hypothesis_id"),
        "changed_variable": start.get("changed_variable", ""),
        "retry_reason": start.get("retry_reason", ""),
        "escalation_stage": start.get("escalation_stage"),
    })
    emit(with_heads({"ok": True, "attempt_id": args.attempt_id, "result": args.result, "sequence": event["sequence"], "event_sha256": event["event_sha256"]}, paths), args.format)
    return 0


def _load_batch_input(value: str) -> dict[str, Any]:
    if value == "-":
        try:
            return json.load(sys.stdin)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"invalid JSON on stdin: {exc}")
    return load_json(Path(value))


def cmd_attempt_batch(args: argparse.Namespace) -> int:
    paths = audit_paths(args.audit_dir)
    doc = _load_batch_input(args.input)
    schema_errors = validate_schema(doc, load_schema("attempt-batch.schema.json"))
    if schema_errors:
        emit(with_heads({"ok": False, "errors": schema_errors}, paths), args.format)
        return 2
    plan = load_json(paths["plan"])
    valid = validate_plan_obj(plan)
    system_valid = validate_system_files(paths, plan)
    errors = [*valid["errors"], *system_valid.get("errors", [])]
    if errors:
        emit(with_heads({"ok": False, "errors": ["audit plan/system inventory is invalid", *errors]}, paths), args.format)
        return 2
    phase = doc["phase"]
    specs = doc["attempts"]
    for idx, spec in enumerate(specs, 1):
        if not isinstance(spec, dict):
            errors.append(f"attempts[{idx}] must be an object")
            continue
        required = ("feature_id", "probe_id") if phase == "START" else ("attempt_id", "observed", "result")
        for key in required:
            if key not in spec or spec.get(key) in (None, ""):
                errors.append(f"attempts[{idx}] missing required field {key}")
        if phase == "FINISH":
            if spec.get("result") not in RESULTS:
                errors.append(f"attempts[{idx}].result must be one of {sorted(RESULTS)}")
            if spec.get("failure_pattern", "NONE_OBSERVED") not in PATTERNS:
                errors.append(f"attempts[{idx}].failure_pattern must be one of {sorted(PATTERNS)}")
            if spec.get("confidence", "MEDIUM") not in CONFIDENCE:
                errors.append(f"attempts[{idx}].confidence must be one of {sorted(CONFIDENCE)}")
        if spec.get("escalation_stage") is not None and str(spec.get("escalation_stage")) not in ESCALATION_STAGES:
            errors.append(f"attempts[{idx}].escalation_stage must be 0-11")
    if errors:
        emit({"ok": False, "phase": phase, "errors": errors}, args.format)
        return 2
    current = read_events(paths["log"])
    attempts, fold_errors = fold_attempts(current)
    if fold_errors:
        emit(with_heads({"ok": False, "errors": fold_errors}, paths), args.format)
        return 2
    if phase == "START":
        # Respect preflight ordering even in batch mode. A batch may contain only probes
        # whose prerequisites were already completed before this batch; this prevents
        # pre-registering feature probes before identity/isolation evidence exists.
        for spec in specs:
            try:
                _, planned_probe = find_feature_probe(plan, spec["feature_id"], spec["probe_id"])
            except (KeyError, SystemExit):
                continue
            preflight_error = preflight_error_for(planned_probe.get("probe_intent", ""), current)
            if preflight_error:
                emit(with_heads({"ok": False, "phase": phase, "errors": [preflight_error]}, paths), args.format)
                return 2
    payloads: list[dict[str, Any]] = []
    outputs: list[dict[str, Any]] = []
    if phase == "START":
        pending_probe_ids: list[str] = []
        hstate = investigation_summary(paths)
        hmap = {x.get("hypothesis_id"): x for x in hstate.get("hypotheses", [])} if hstate.get("ok") else {}
        h_pending: Counter[str] = Counter()
        for spec in specs:
            feature, probe = find_feature_probe(plan, spec["feature_id"], spec["probe_id"])
            repro = spec.get("repro_command")
            if (feature.get("dependency_sensitive") or probe.get("probe_intent") in PREFLIGHT_INTENTS) and not str(repro or "").strip():
                errors.append(f"{feature['feature_id']}/{probe['probe_id']} requires repro_command")
            prior_same = [pair for pair in attempts.values() if pair.get("start") and pair["start"].get("probe_id") == spec["probe_id"]]
            prior_same += [x for x in pending_probe_ids if x == spec["probe_id"]]
            if prior_same and not (spec.get("changed_variable") or spec.get("retry_reason")):
                errors.append(f"repeated probe {spec['probe_id']} requires changed_variable or retry_reason")
            hid = spec.get("hypothesis_id")
            if hid:
                h = hmap.get(hid)
                if not h:
                    errors.append(f"hypothesis not found: {hid}")
                elif h.get("status") in HYPOTHESIS_TERMINAL:
                    errors.append(f"cannot attach attempt to terminal hypothesis {hid}: {h.get('status')}")
                elif h.get("feature_id") != feature["feature_id"]:
                    errors.append(f"hypothesis {hid} belongs to {h.get('feature_id')}, not {feature['feature_id']}")
                elif h.get("attempts_used", 0) + h_pending[hid] >= h.get("max_attempts", 0):
                    errors.append(f"hypothesis budget exhausted for {hid}")
                h_pending[hid] += 1
            aid = spec.get("attempt_id") or f"att-{uuid4().hex[:12]}"
            if aid in attempts or any(x.get("attempt_id") == aid for x in payloads):
                errors.append(f"duplicate attempt_id: {aid}")
            payloads.append({
                "schema_version": "2.0", "event_type": "STARTED", "attempt_id": aid,
                "timestamp_utc": utc_now(), "feature_id": feature["feature_id"], "feature_claim": feature["claim"],
                "probe_id": probe["probe_id"], "probe_intent": probe["probe_intent"], "contract_relation": probe["contract_relation"],
                "input_family": probe.get("input_family", ""), "input_case": probe.get("input_case", ""),
                "entry_point": probe.get("entry_point") or (feature.get("entry_points") or [""])[0], "expected": probe["expected"],
                "effect_checks": probe.get("effect_checks", []), "action": spec.get("action") or probe.get("action", ""),
                "repro_command": repro, "preconditions": spec.get("preconditions", []), "hypothesis_id": hid,
                "changed_variable": spec.get("changed_variable"), "retry_reason": spec.get("retry_reason"),
                "escalation_stage": str(spec.get("escalation_stage")) if spec.get("escalation_stage") is not None else None,
            })
            outputs.append({"attempt_id": aid, "feature_id": feature["feature_id"], "probe_id": probe["probe_id"]})
            pending_probe_ids.append(spec["probe_id"])
    else:
        seen_finish: set[str] = set()
        for spec in specs:
            aid = spec["attempt_id"]
            if aid in seen_finish:
                errors.append(f"duplicate FINISH in batch for {aid}")
                continue
            seen_finish.add(aid)
            pair = attempts.get(aid)
            if not pair or not pair.get("start"):
                errors.append(f"attempt has no STARTED event: {aid}")
                continue
            if pair.get("finish"):
                errors.append(f"attempt already finished: {aid}")
                continue
            start = pair["start"]
            payloads.append({
                "schema_version": "2.0", "event_type": "FINISHED", "attempt_id": aid, "timestamp_utc": utc_now(),
                "feature_id": start["feature_id"], "feature_claim": start["feature_claim"], "probe_id": start["probe_id"],
                "probe_intent": start["probe_intent"], "contract_relation": start["contract_relation"],
                "input_family": start.get("input_family", ""), "input_case": start.get("input_case", ""),
                "observed": spec["observed"], "side_effect_check": spec.get("side_effect_check"),
                "persistence_check": spec.get("persistence_check"), "http_status": spec.get("http_status"),
                "exit_code": spec.get("exit_code"), "evidence_refs": spec.get("evidence", []), "result": spec["result"],
                "failure_pattern": spec.get("failure_pattern", "NONE_OBSERVED"), "confidence": spec.get("confidence", "MEDIUM"),
                "notes": spec.get("notes", ""), "hypothesis_id": start.get("hypothesis_id"),
                "changed_variable": start.get("changed_variable", ""), "retry_reason": start.get("retry_reason", ""),
                "escalation_stage": start.get("escalation_stage"),
            })
            outputs.append({"attempt_id": aid, "result": spec["result"]})
    if errors:
        emit(with_heads({"ok": False, "phase": phase, "errors": errors}, paths), args.format)
        return 2
    events = append_events(paths["log"], payloads)
    for out, event in zip(outputs, events):
        out["sequence"] = event["sequence"]
        out["event_sha256"] = event["event_sha256"]
    emit(with_heads({"ok": True, "phase": phase, "count": len(events), "attempts": outputs}, paths), args.format)
    return 0


def cmd_hypothesis_open(args: argparse.Namespace) -> int:
    paths = audit_paths(args.audit_dir)
    plan = load_json(paths["plan"])
    if not any(f.get("feature_id") == args.feature_id for f in plan.get("features", [])):
        emit({"ok": False, "errors": [f"feature not found in plan: {args.feature_id}"]}, args.format)
        return 2
    hid = args.hypothesis_id or f"hyp-{uuid4().hex[:12]}"
    existing = read_events(paths["hypotheses"])
    ledger_errors = verify_hash_chain(existing) if existing else []
    hypotheses, fold_errors = fold_hypotheses(existing)
    ledger_errors.extend(fold_errors)
    if args.max_attempts < 1:
        ledger_errors.append("max_attempts must be >= 1")
    if args.trigger_attempt_id:
        attempt_events = read_events(paths["log"])
        attempts, attempt_errors = fold_attempts(attempt_events)
        ledger_errors.extend(attempt_errors)
        if args.trigger_attempt_id not in attempts:
            ledger_errors.append(f"trigger attempt not found: {args.trigger_attempt_id}")
    if ledger_errors:
        emit({"ok": False, "errors": ledger_errors}, args.format)
        return 2
    if hid in hypotheses:
        emit({"ok": False, "errors": [f"hypothesis already exists: {hid}"]}, args.format)
        return 2
    event = append_event(paths["hypotheses"], {
        "schema_version": "1.0",
        "event_type": "HYPOTHESIS_OPENED",
        "hypothesis_id": hid,
        "timestamp_utc": utc_now(),
        "feature_id": args.feature_id,
        "statement": args.statement,
        "trigger_attempt_id": args.trigger_attempt_id,
        "next_probe": args.next_probe,
        "max_attempts": args.max_attempts,
        "evidence_for": args.evidence_for,
        "evidence_against": args.evidence_against,
    })
    emit(with_heads({"ok": True, "hypothesis_id": hid, "status": "OPEN", "sequence": event["sequence"], "event_sha256": event["event_sha256"]}, paths), args.format)
    return 0


def cmd_hypothesis_update(args: argparse.Namespace) -> int:
    paths = audit_paths(args.audit_dir)
    existing = read_events(paths["hypotheses"])
    errors = verify_hash_chain(existing) if existing else []
    hypotheses, fold_errors = fold_hypotheses(existing)
    errors.extend(fold_errors)
    h = hypotheses.get(args.hypothesis_id)
    if not h:
        errors.append(f"hypothesis not found: {getattr(args, "hypothesis_id", None)}")
    elif h.get("status") in HYPOTHESIS_TERMINAL:
        errors.append(f"hypothesis already terminal: {getattr(args, "hypothesis_id", None)} ({h.get('status')})")
    if args.status in {"OPEN", "SUPPORTED"} and not (args.next_probe or h and h.get("next_probe")):
        errors.append("non-terminal hypothesis update requires next_probe")
    if args.status == "CONFIRMED" and not (args.evidence_for or h and h.get("evidence_for")):
        errors.append("CONFIRMED hypothesis requires evidence_for")
    if args.status == "REFUTED" and not (args.evidence_against or h and h.get("evidence_against")):
        errors.append("REFUTED hypothesis requires evidence_against")
    if args.status in {"BLOCKED", "BUDGET_EXHAUSTED"} and not str(args.notes).strip():
        errors.append(f"{args.status} hypothesis requires explanatory --notes")
    if errors:
        emit({"ok": False, "errors": errors}, args.format)
        return 2
    event = append_event(paths["hypotheses"], {
        "schema_version": "1.0",
        "event_type": "HYPOTHESIS_UPDATED",
        "hypothesis_id": getattr(args, "hypothesis_id", None),
        "timestamp_utc": utc_now(),
        "status": args.status,
        "evidence_for": args.evidence_for,
        "evidence_against": args.evidence_against,
        "next_probe": args.next_probe,
        "changed_variable": getattr(args, "changed_variable", None),
        "notes": args.notes,
    })
    emit(with_heads({"ok": True, "hypothesis_id": getattr(args, "hypothesis_id", None), "status": args.status, "sequence": event["sequence"], "event_sha256": event["event_sha256"]}, paths), args.format)
    return 0


def cmd_investigation_status(args: argparse.Namespace) -> int:
    paths = audit_paths(args.audit_dir)
    emit(with_heads(investigation_summary(paths), paths), args.format)
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    paths = audit_paths(args.audit_dir)
    plan = load_json(paths["plan"])
    events = read_events(paths["log"])
    investigation = investigation_summary(paths)
    summary = apply_investigation_verdicts(derive_summary(plan, events), investigation)
    summary["investigation"] = investigation
    emit(with_heads(summary, paths), args.format)
    return 0


REPORTING_MISUSE_PATTERNS = [
    (re.compile(r"\bverified reality\b", re.I), "'verified reality' is prohibited as a surviving-feature verdict"),
    (re.compile(r"\b(run\s*\d*|feature|system|capability|function)\s*\(?verified\)?\b", re.I), "'verified' must not replace NOT_FALSIFIED as an audit verdict"),
    (re.compile(r"\bverdict\s*[:=-]\s*(verified|proven|alive|fully working|confirmed working)\b", re.I), "non-RFF verdict synonym detected"),
    (re.compile(r"\b(all|every)\s+(features?|functions?|capabilities?)\b[^\n.]{0,80}\b(verified|proven|alive|fully working|confirmed working)\b", re.I), "whole-scope success language overstates finite falsification"),
]


def reporting_language_errors(text: str, label: str) -> list[str]:
    errors: list[str] = []
    for pattern, message in REPORTING_MISUSE_PATTERNS:
        for match in pattern.finditer(text):
            line_no = text.count("\n", 0, match.start()) + 1
            errors.append(f"{label}:{line_no}: {message}: {match.group(0)!r}")
    return errors


def cmd_terminology_check(args: argparse.Namespace) -> int:
    errors: list[str] = []
    checked: list[str] = []
    for raw in args.input:
        path = Path(raw)
        if not path.exists():
            errors.append(f"missing input file: {path}")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        errors.extend(reporting_language_errors(text, str(path)))
        checked.append(str(path))
    emit({"ok": not errors, "errors": errors, "checked": checked}, args.format)
    return 0 if not errors else 2


def cmd_report(args: argparse.Namespace) -> int:
    paths = audit_paths(args.audit_dir)
    plan = load_json(paths["plan"])
    events = read_events(paths["log"])
    investigation = investigation_summary(paths)
    summary = apply_investigation_verdicts(derive_summary(plan, events), investigation)
    attempts, _ = fold_attempts(events)
    terminal_by_feature: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for pair in attempts.values():
        if pair.get("finish"):
            terminal_by_feature[pair["finish"].get("feature_id", "")].append(pair["finish"])

    matrix_lines = [
        "# Runtime Feature Matrix", "",
        "| Feature | Attempts | Verdict | Patterns | Strongest counterexample |",
        "|---|---:|---|---|---|",
    ]
    for f in summary["features"]:
        rows = terminal_by_feature.get(f["feature_id"], [])
        falsified = next((r for r in rows if r.get("result") == "FALSIFIED"), None)
        strongest = ""
        if falsified:
            strongest = f"{falsified.get('probe_id')}: {falsified.get('observed','')}"
        matrix_lines.append(f"| {f['feature_id']} | {f['terminal_attempts']} | {f['verdict']} | {', '.join(f['patterns']) or 'NONE_OBSERVED'} | {strongest.replace('|','/')} |")
    paths["matrix"].write_text("\n".join(matrix_lines) + "\n", encoding="utf-8")

    counts = Counter(f["verdict"] for f in summary["features"])
    system_result = validate_system_files(paths, plan)
    system_lines: list[str] = []
    if plan.get("audit_mode", "FEATURE") == "SYSTEM" and paths["inventory"].exists():
        inv = load_json(paths["inventory"])
        inv_features = inv.get("features", []) if isinstance(inv, dict) else []
        in_scope = [x for x in inv_features if isinstance(x, dict) and x.get("disposition") == "IN_SCOPE"]
        excluded = [x for x in inv_features if isinstance(x, dict) and x.get("disposition") == "EXCLUDED"]
        plan_ids = {f.get("feature_id") for f in plan.get("features", []) if isinstance(f, dict)}
        unmapped = [x.get("feature_id") for x in in_scope if x.get("feature_id") not in plan_ids]
        system_lines = [
            "# System Audit Coverage", "",
            f"- Discovery status: **{inv.get('discovery_status','')}**",
            f"- Discovered capabilities/workflows: {len(inv_features)}",
            f"- In scope: {len(in_scope)}",
            f"- Explicitly excluded: {len(excluded)}",
            f"- Planned: {len(plan.get('features', []))}",
            f"- Unmapped in-scope items: {len(unmapped)}",
            f"- Cross-feature workflow coverage applicable: {bool(inv.get('workflow_coverage',{}).get('applicable'))}",
            "",
        ]
        blockers = inv.get("discovery_blockers") or []
        if blockers:
            system_lines += ["## Discovery blockers", ""] + [f"- {b}" for b in blockers] + [""]
        if excluded:
            system_lines += ["## Explicit exclusions", ""] + [f"- `{x.get('feature_id')}` [{x.get('exclusion_basis','')}] — {x.get('exclusion_reason','')}" for x in excluded] + [""]
        if unmapped:
            system_lines += ["## Unmapped in-scope features", ""] + [f"- `{x}`" for x in unmapped] + [""]
        paths["coverage"].write_text("\n".join(system_lines), encoding="utf-8")
    report = [
        "# Runtime Feature Falsification Report", "",
        "## Scope", "",
        f"- Audit ID: `{plan.get('audit_id','')}`",
        f"- Project: {plan.get('target',{}).get('project','')}",
        f"- Environment: {plan.get('target',{}).get('environment','')}",
        f"- Scope: {plan.get('target',{}).get('scope','')}",
        f"- Audit mode: {plan.get('audit_mode','FEATURE')}", "",
        "## Executive result", "",
        f"- Features audited/planned: {len(summary['features'])}",
        f"- FALSIFIED: {counts.get('FALSIFIED',0)}",
        f"- NOT_FALSIFIED: {counts.get('NOT_FALSIFIED',0)}",
        f"- BLOCKED: {counts.get('BLOCKED',0)}",
        f"- INCONCLUSIVE: {counts.get('INCONCLUSIVE',0)}",
        f"- INCOMPLETE: {counts.get('INCOMPLETE',0)}", "",
        "> `NOT_FALSIFIED` means no counterexample was found in the completed declared matrix. It must not be rewritten as a verdict such as \"verified\", \"proven\", \"alive\", or \"fully working\"; it is not proof of correctness, completeness, or production readiness.", "",
        "## Runtime preflight", "",
        f"- Declared startup path: {plan.get('target',{}).get('startup_path','')}",
        f"- Expected runtime identity: {plan.get('target',{}).get('runtime_identity_expectation','')}",
        f"- Collision surfaces considered: {', '.join(plan.get('target',{}).get('collision_surfaces',[]) or []) or 'documented in environment_collision probe'}", "",
    ]
    if system_lines:
        report += ["## System discovery/coverage", ""] + system_lines[2:] + [""]
    report += [
        "## Feature results", "",
    ]
    for f in summary["features"]:
        report += [f"### {f['feature_id']}", "", f"- Claim: {f['claim']}", f"- Verdict: **{f['verdict']}**", f"- Terminal attempts: {f['terminal_attempts']}", f"- Patterns: {', '.join(f['patterns']) or 'NONE_OBSERVED'}"]
        for r in terminal_by_feature.get(f["feature_id"], []):
            if r.get("result") == "FALSIFIED":
                report += [f"- Counterexample `{r.get('probe_id')}`: {r.get('observed','')}", f"  - Pattern: {r.get('failure_pattern')}", f"  - Evidence: {', '.join(r.get('evidence_refs') or []) or '(no file evidence recorded)'}"]
        if f["required_missing"]:
            report += [f"- Missing required probes: {', '.join(f['required_missing'])}"]
        report.append("")
    inv = investigation
    report += ["## Persistent investigation", "", f"- Hypotheses opened: {inv.get('hypothesis_count',0)}", f"- Active hypotheses: {inv.get('active_count',0)}"]
    for h in inv.get("hypotheses", []):
        report += [f"- `{h.get('hypothesis_id')}` [{h.get('status')}] {h.get('statement')} — attempts {h.get('attempts_used')}/{h.get('max_attempts')}"]
    report += [""]
    report += ["## Attempt-log integrity", "", f"- Canonical event log: `attempts.jsonl`", f"- Events: {summary['event_count']}", f"- Attempts: {summary['attempt_count']}", f"- Open attempts: {', '.join(summary['open_attempts']) or 'none'}", ""]
    paths["report"].write_text("\n".join(report), encoding="utf-8")
    report_state = report_state_snapshot(paths, plan)
    report_state["generated_at_utc"] = utc_now()
    paths["report_state"].write_text(json.dumps(report_state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    output = {"ok": True, "feature_matrix": str(paths["matrix"]), "audit_report": str(paths["report"]), "report_state": str(paths["report_state"])}
    if paths["coverage"].exists():
        output["system_coverage"] = str(paths["coverage"])
    emit(with_heads(output, paths), args.format)
    return 0


def cmd_gate(args: argparse.Namespace) -> int:
    paths = audit_paths(args.audit_dir)
    errors: list[str] = []
    warnings: list[str] = []
    if not paths["plan"].exists():
        errors.append(f"missing audit plan: {paths['plan']}")
        emit({"ok": False, "errors": errors, "warnings": warnings}, args.format)
        return 2
    plan = load_json(paths["plan"])
    plan_result = validate_plan_obj(plan)
    errors.extend(plan_result["errors"])
    warnings.extend(plan_result["warnings"])
    system_result = validate_system_files(paths, plan)
    errors.extend(system_result.get("errors", []))
    warnings.extend(system_result.get("warnings", []))
    if paths["baseline"].exists():
        baseline = load_json(paths["baseline"])
        if baseline.get("available"):
            errors.extend(compare_tracked_source(baseline))
        else:
            warnings.append("tracked-source mutation gate unavailable: " + baseline.get("reason", "unknown reason"))
    else:
        warnings.append("tracked-source baseline missing; source-mutation gate unavailable")
    events = read_events(paths["log"])
    if not events:
        errors.append("attempts.jsonl is missing or empty")
    errors.extend(verify_hash_chain(events))
    attempt_schema = load_schema("attempt-event.schema.json")
    for event in events:
        errors.extend(validate_schema({k: v for k, v in event.items() if k != "_line"}, attempt_schema, f"attempts.jsonl:{event.get('_line','?')}"))
    attempts, fold_errors = fold_attempts(events)
    errors.extend(fold_errors)

    hypothesis_events = read_events(paths["hypotheses"])
    hypotheses: dict[str, dict[str, Any]] = {}
    if hypothesis_events:
        errors.extend(verify_hash_chain(hypothesis_events))
        hypothesis_schema = load_schema("hypothesis-event.schema.json")
        for event in hypothesis_events:
            errors.extend(validate_schema({k: v for k, v in event.items() if k != "_line"}, hypothesis_schema, f"hypothesis-ledger.jsonl:{event.get('_line','?')}"))
        hypotheses, hypothesis_errors = fold_hypotheses(hypothesis_events)
        errors.extend(hypothesis_errors)
        h_attempt_counts = hypothesis_attempt_counts(events)
        # Build terminal-attempt lookup once for evidence coupling.
        linked_finishes: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for pair in attempts.values():
            if pair.get("finish") and pair["finish"].get("hypothesis_id"):
                linked_finishes[pair["finish"]["hypothesis_id"]].append(pair["finish"])
        for hid, h in hypotheses.items():
            status = h.get("status")
            if status not in HYPOTHESIS_TERMINAL:
                errors.append(f"unresolved hypothesis at final gate: {hid} [{status}] — {h.get('statement')}")
            if status == "CONFIRMED":
                trigger = h.get("trigger_attempt_id")
                trigger_falsified = bool(trigger and attempts.get(trigger, {}).get("finish", {}).get("result") == "FALSIFIED")
                linked_falsified = any(x.get("result") == "FALSIFIED" for x in linked_finishes.get(hid, []))
                if not (trigger_falsified or linked_falsified):
                    errors.append(f"CONFIRMED hypothesis lacks a linked/trigger FALSIFIED runtime attempt: {hid}")
                if not h.get("evidence_for"):
                    errors.append(f"CONFIRMED hypothesis lacks evidence_for: {hid}")
            if status == "REFUTED" and not h.get("evidence_against"):
                errors.append(f"REFUTED hypothesis lacks evidence_against: {hid}")
            if status == "BUDGET_EXHAUSTED" and h_attempt_counts.get(hid, 0) < h.get("max_attempts", 0):
                errors.append(f"hypothesis marked BUDGET_EXHAUSTED before consuming declared budget: {hid}")

    # Runtime-entry evidence gates. These cannot cryptographically prove a public surface,
    # but they force reproducible startup, runtime-identity, and environment-isolation evidence.
    feature_by_id = {f.get("feature_id"): f for f in plan.get("features", []) if isinstance(f, dict)}
    finishes_by_intent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for aid, pair in attempts.items():
        start = pair.get("start") or {}
        finish = pair.get("finish") or {}
        feature = feature_by_id.get(start.get("feature_id"), {})
        intent = start.get("probe_intent")
        if intent in PREFLIGHT_INTENTS and finish:
            finishes_by_intent[intent].append(finish)
        if (feature.get("dependency_sensitive") or intent in PREFLIGHT_INTENTS) and not str(start.get("repro_command") or "").strip():
            errors.append(f"preflight/dependency-sensitive attempt lacks repro_command: {aid}")
        if intent in {"runtime_identity", "environment_collision"} and finish:
            if not (finish.get("evidence_refs") or finish.get("side_effect_check") or finish.get("persistence_check")):
                errors.append(f"{intent} attempt lacks corroborating evidence/effect observation: {aid}")

    startup_finishes = finishes_by_intent["environment_start"]
    identity_finishes = finishes_by_intent["runtime_identity"]
    collision_finishes = finishes_by_intent["environment_collision"]
    if not startup_finishes:
        errors.append("required environment_start probe never completed")
    elif not any(x.get("result") == "SURVIVED" for x in startup_finishes):
        non_startup_started = [pair for pair in attempts.values() if (pair.get("start") or {}).get("probe_intent") != "environment_start"]
        if non_startup_started:
            errors.append("non-startup probes were executed without a SURVIVED startup-health probe")
    if not identity_finishes:
        errors.append("required runtime_identity probe never completed")
    elif not any(x.get("result") == "SURVIVED" for x in identity_finishes):
        warnings.append("runtime identity did not SURVIVE; downstream probes characterize the deployed runtime and must not be described as proof of the intended source implementation")
    if not collision_finishes:
        errors.append("required environment_collision probe never completed")
    elif not any(x.get("result") == "SURVIVED" for x in collision_finishes):
        errors.append("audit environment collision/isolation did not SURVIVE; feature results may be contaminated by shared mutable resources")

    for aid, pair in attempts.items():
        start = pair.get("start") or {}
        if start.get("probe_intent") not in PREFLIGHT_INTENTS:
            if not startup_health_survived(events):
                errors.append(f"feature probe executed without healthy startup: {aid}")
            if not runtime_identity_completed(events):
                errors.append(f"feature probe executed before runtime identity was observed: {aid}")
            if not collision_check_survived(events):
                errors.append(f"feature probe executed before environment collision check SURVIVED: {aid}")

    # Ensure every STARTED attempt terminates. This catches crashes/timeouts that were never closed out.
    for aid, pair in attempts.items():
        if pair.get("start") and not pair.get("finish"):
            errors.append(f"open attempt without FINISHED event: {aid} ({pair['start'].get('probe_id')})")

    by_probe: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for pair in attempts.values():
        if pair.get("finish"):
            by_probe[pair["finish"].get("probe_id", "")].append(pair["finish"])
    for feature in plan.get("features", []):
        for probe in feature.get("probes", []):
            if probe.get("required", True) and not by_probe.get(probe.get("probe_id", "")):
                errors.append(f"required probe never completed: {feature.get('feature_id')}/{probe.get('probe_id')}")

    # Evidence quality gates: fake-success claims require downstream observation, not status text alone.
    for pair in attempts.values():
        finish = pair.get("finish")
        start = pair.get("start") or {}
        if not finish:
            continue
        if finish.get("result") == "FALSIFIED":
            if not (finish.get("observed") and (finish.get("evidence_refs") or finish.get("side_effect_check") or finish.get("persistence_check"))):
                errors.append(f"falsified attempt lacks corroborating effect/evidence: {finish.get('attempt_id')}")
            if finish.get("failure_pattern") == "NONE_OBSERVED":
                errors.append(f"falsified attempt has NONE_OBSERVED pattern: {finish.get('attempt_id')}")
        if finish.get("result") == "SURVIVED" and start.get("effect_checks"):
            if not (finish.get("side_effect_check") or finish.get("persistence_check") or finish.get("evidence_refs")):
                errors.append(f"survived attempt with required effect_checks lacks downstream evidence: {finish.get('attempt_id')}")
        for ref in finish.get("evidence_refs") or []:
            ref_path = Path(ref)
            candidate = ref_path if ref_path.is_absolute() else paths["dir"] / ref_path
            if not candidate.exists():
                errors.append(f"evidence reference does not exist for {finish.get('attempt_id')}: {ref}")

    if args.require_report:
        if not paths["matrix"].exists():
            errors.append("feature-matrix.md missing; run auditctl report")
        if not paths["report"].exists():
            errors.append("audit-report.md missing; run auditctl report")
        if plan.get("audit_mode", "FEATURE") == "SYSTEM" and not paths["coverage"].exists():
            errors.append("system-coverage.md missing for SYSTEM audit; run auditctl report")
        if not paths["report_state"].exists():
            errors.append("report-state marker missing; run auditctl report after the final attempt/hypothesis update")
        else:
            recorded_state = load_json(paths["report_state"])
            current_state = report_state_snapshot(paths, plan)
            stale_fields = [k for k, v in current_state.items() if recorded_state.get(k) != v]
            if stale_fields:
                errors.append("generated report is stale relative to canonical audit state (changed: " + ", ".join(stale_fields) + "); rerun auditctl report")
        for report_path in (paths["report"], paths["matrix"], paths["coverage"]):
            if report_path.exists():
                errors.extend(reporting_language_errors(report_path.read_text(encoding="utf-8", errors="replace"), str(report_path)))

    investigation = investigation_summary(paths)
    summary = apply_investigation_verdicts(derive_summary(plan, events), investigation)
    if any(f["verdict"] == "INCOMPLETE" for f in summary["features"]):
        errors.append("one or more features remain INCOMPLETE")

    ok = not errors
    if ok and args.require_report:
        paths["complete"].write_text(json.dumps({
            "audit_id": plan.get("audit_id"),
            "completed_at_utc": utc_now(),
            "audit_mode": plan.get("audit_mode", "FEATURE"),
            "sealed": True,
            "next_run_policy": "After remediation or a re-audit, use a fresh auditor instance and a new audit workspace; do not append to this sealed run.",
        }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        if paths["active"].exists():
            paths["active"].unlink()
    result = {
        "ok": ok,
        "errors": errors,
        "warnings": warnings,
        "audit_id": plan.get("audit_id"),
        "audit_mode": plan.get("audit_mode", "FEATURE"),
        "system_inventory": {k: v for k, v in system_result.items() if k not in {"ok", "errors", "warnings", "applicable"}} if system_result.get("applicable") else None,
        "event_count": len(events),
        "attempt_count": len(attempts),
        "feature_verdicts": {f["feature_id"]: f["verdict"] for f in summary["features"]},
        "investigation": investigation,
        "next_run_policy": "If remediation follows this sealed audit, spawn a fresh auditor and initialize a new audit workspace for the next run.",
    }
    emit(result, args.format)
    return 0 if result["ok"] else 2


def add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--audit-dir", type=Path, default=Path(".runtime-feature-audit"), help="Audit workspace (default: .runtime-feature-audit)")
    p.add_argument("--format", choices=("json", "text"), default="json", help="Output format (default: json)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Runtime Feature Falsifier deterministic audit controller")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="Create audit workspace and empty audit-plan.json")
    add_common(p)
    p.add_argument("--audit-id")
    p.add_argument("--project-name", default="current-project")
    p.add_argument("--environment", default="local")
    p.add_argument("--scope", default="user-requested feature scope")
    p.add_argument("--mode", choices=("feature", "system"), default="feature", help="Audit one/some features or the whole discovered system")
    p.add_argument("--target-root", type=Path, default=Path.cwd(), help="Target project root used for tracked-source integrity baseline")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("validate-plan", help="Validate plan structure and falsification coverage")
    add_common(p)
    p.set_defaults(func=cmd_validate_plan)

    p = sub.add_parser("attempt-start", help="Append STARTED event before executing one probe")
    add_common(p)
    p.add_argument("--feature-id", required=True)
    p.add_argument("--probe-id", required=True)
    p.add_argument("--attempt-id")
    p.add_argument("--action")
    p.add_argument("--repro-command")
    p.add_argument("--precondition", action="append", default=[])
    p.add_argument("--hypothesis-id", help="Attach this attempt to an open persistent-investigation hypothesis")
    p.add_argument("--changed-variable", help="Information-bearing variable changed from a prior attempt")
    p.add_argument("--retry-reason", help="Why an otherwise identical retry is informative (for example nondeterminism reproduction)")
    p.add_argument("--escalation-stage", choices=sorted(ESCALATION_STAGES), help="Investigation escalation stage 0-11")
    p.set_defaults(func=cmd_attempt_start)

    p = sub.add_parser("attempt-finish", help="Append FINISHED event after executing one probe")
    add_common(p)
    p.add_argument("--attempt-id", required=True)
    p.add_argument("--observed", required=True)
    p.add_argument("--side-effect-check")
    p.add_argument("--persistence-check")
    p.add_argument("--http-status", type=int)
    p.add_argument("--exit-code", type=int)
    p.add_argument("--evidence", action="append", default=[])
    p.add_argument("--result", required=True, choices=sorted(RESULTS))
    p.add_argument("--failure-pattern", default="NONE_OBSERVED", choices=sorted(PATTERNS))
    p.add_argument("--confidence", default="MEDIUM", choices=sorted(CONFIDENCE))
    p.add_argument("--notes", default="")
    p.set_defaults(func=cmd_attempt_finish)

    p = sub.add_parser("attempt-batch", help="Atomically ingest many STARTED or FINISHED attempt events from one JSON document")
    add_common(p)
    p.add_argument("--input", required=True, help="JSON file path or '-' for stdin; see assets/attempt-batch.schema.json")
    p.set_defaults(func=cmd_attempt_batch)

    p = sub.add_parser("hypothesis-open", help="Open a bounded falsification hypothesis")
    add_common(p)
    p.add_argument("--feature-id", required=True)
    p.add_argument("--hypothesis-id")
    p.add_argument("--statement", required=True)
    p.add_argument("--trigger-attempt-id")
    p.add_argument("--next-probe", required=True)
    p.add_argument("--max-attempts", type=int, default=6)
    p.add_argument("--evidence-for", action="append", default=[])
    p.add_argument("--evidence-against", action="append", default=[])
    p.set_defaults(func=cmd_hypothesis_open)

    p = sub.add_parser("hypothesis-update", help="Append evidence/status to an existing hypothesis")
    add_common(p)
    p.add_argument("--hypothesis-id", required=True)
    p.add_argument("--status", required=True, choices=sorted(HYPOTHESIS_STATUSES))
    p.add_argument("--evidence-for", action="append", default=[])
    p.add_argument("--evidence-against", action="append", default=[])
    p.add_argument("--next-probe", default="")
    p.add_argument("--changed-variable", default="")
    p.add_argument("--notes", default="")
    p.set_defaults(func=cmd_hypothesis_update)

    p = sub.add_parser("investigation-status", help="Summarize open/terminal persistent-investigation hypotheses")
    add_common(p)
    p.set_defaults(func=cmd_investigation_status)

    p = sub.add_parser("terminology-check", help="Check a retrospective/summary for forbidden verdict inflation such as calling NOT_FALSIFIED verified or proven")
    p.add_argument("--input", action="append", required=True, help="Markdown/text file to scan; repeat for multiple files")
    p.add_argument("--format", choices=("json", "text"), default="json", help="Output format")
    p.set_defaults(func=cmd_terminology_check)

    p = sub.add_parser("summary", help="Summarize attempts and derived feature verdicts")
    add_common(p)
    p.set_defaults(func=cmd_summary)

    p = sub.add_parser("report", help="Generate feature-matrix.md and audit-report.md")
    add_common(p)
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("gate", help="Validate completeness/evidence; nonzero exit blocks finalization")
    add_common(p)
    p.add_argument("--require-report", action="store_true", help="Also require generated matrix/report")
    p.set_defaults(func=cmd_gate)
    return parser


def main() -> int:
    global _CURRENT_AUDIT_DIR
    args = build_parser().parse_args()
    _CURRENT_AUDIT_DIR = getattr(args, "audit_dir", None)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
