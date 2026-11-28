// Scenario Runner helpers (task 9.19): category order, text highlighting, the guard's reasons.
import type { Scenario, ScenarioCategory } from "@/api/client";

/** Display order of the categories; the card grid is grouped by them. */
export const CATEGORIES: ScenarioCategory[] = [
  "early_exit",
  "drafting",
  "operations",
  "guard_email",
  "guard_knowledge",
  "guard_safety",
];

export function byCategory(scenarios: Scenario[]): [ScenarioCategory, Scenario[]][] {
  return CATEGORIES.map((c) => [c, scenarios.filter((s) => s.category === c)] as [ScenarioCategory, Scenario[]]).filter(
    ([, list]) => list.length > 0,
  );
}

/** expected: authored in the catalog; detected: reported by the guard on the run. */
export type MarkKind = "expected" | "detected" | "both";
export interface Segment {
  text: string;
  mark: MarkKind | null;
}

/** Split ``text`` into plain and marked segments; every occurrence of every needle is marked.
 * Overlapping marks merge; a character marked by both sources is ``both``. */
export function highlightSegments(text: string, expected: string[], detected: string[] = []): Segment[] {
  const flags = new Uint8Array(text.length); // bit 1 expected, bit 2 detected
  const paint = (needles: string[], bit: number) => {
    for (const needle of needles) {
      if (!needle) continue;
      for (let i = text.indexOf(needle); i !== -1; i = text.indexOf(needle, i + 1)) {
        for (let j = i; j < i + needle.length; j++) flags[j] |= bit;
      }
    }
  };
  paint(expected, 1);
  paint(detected, 2);
  const kindOf = (f: number): MarkKind | null => (f === 3 ? "both" : f === 2 ? "detected" : f === 1 ? "expected" : null);
  const out: Segment[] = [];
  for (let i = 0; i < text.length; ) {
    let j = i;
    while (j < text.length && flags[j] === flags[i]) j++;
    out.push({ text: text.slice(i, j), mark: kindOf(flags[i]) });
    i = j;
  }
  return out;
}

type Obj = Record<string, unknown>;
const asObj = (v: unknown): Obj | null => (v && typeof v === "object" && !Array.isArray(v) ? (v as Obj) : null);
const asList = (v: unknown): Obj[] => (Array.isArray(v) ? (v.map(asObj).filter(Boolean) as Obj[]) : []);
const str = (v: unknown): string | null => (typeof v === "string" && v ? v : null);

export interface GuardReason {
  layer: string;
  severity: string | null;
  /** rule id, technique or detector: what decided */
  label: string;
  excerpt: string | null;
  rationale: string | null;
}

const LAYER_LABEL: Record<string, string> = {
  inbound_guard: "L1",
  doc_scanner: "L2",
  inbound_decision: "L3",
  output_guard: "L4",
  decision: "L3/L4",
};

/** The guard report's evidence in pipeline order: each layer's findings and the text L1 stripped,
 * then the gate decisions' reasons. Read from the report as stored (``GET /v1/jobs/{id}/guard``). */
export function guardReasons(report: Obj | null | undefined): GuardReason[] {
  if (!report) return [];
  const out: GuardReason[] = [];
  for (const key of ["inbound_guard", "doc_scanner", "output_guard"]) {
    const layer = asObj(report[key]);
    if (!layer) continue;
    for (const f of asList(layer.findings)) {
      out.push({
        layer: LAYER_LABEL[key],
        severity: str(f.severity),
        label: str(f.rule_id) ?? str(f.technique) ?? str(f.detector) ?? str(f.threat_type) ?? "finding",
        excerpt: str(f.excerpt),
        rationale: str(f.rationale),
      });
    }
    for (const s of asList(layer.stripped_segments)) {
      out.push({ layer: LAYER_LABEL[key], severity: null, label: "stripped", excerpt: str(s.text), rationale: str(s.reason) });
    }
    const error = str(layer.error);
    if (error) out.push({ layer: LAYER_LABEL[key], severity: null, label: "error", excerpt: null, rationale: error });
  }
  const seen = new Set<string>();
  for (const key of ["inbound_decision", "decision"]) {
    const d = asObj(report[key]);
    if (!d) continue;
    const action = str(d.action);
    const rule = str(d.matched_rule_id);
    const reasons = (Array.isArray(d.reasons) ? d.reasons : []).filter((r): r is string => typeof r === "string");
    const sig = `${action}|${rule}|${reasons.join(";")}`;
    if (seen.has(sig)) continue; // the final decision is often the inbound one
    seen.add(sig);
    out.push({
      layer: LAYER_LABEL[key],
      severity: action,
      label: rule ?? "decision",
      excerpt: null,
      rationale: reasons.join("; ") || null,
    });
  }
  return out;
}

/** The texts the guard quoted (finding excerpts, stripped segments): marked as ``detected``. */
export function detectedExcerpts(reasons: GuardReason[]): string[] {
  return [...new Set(reasons.map((r) => r.excerpt).filter((e): e is string => !!e && e.length >= 4))];
}
