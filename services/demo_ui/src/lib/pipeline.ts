// Pure helpers: pipeline steps from a job timeline, scenario checks (unit-tested, task 9.10/9.11).
import type { JobEvent, Timeline } from "@/api/client";

export const TERMINAL = new Set(["DRAFTED", "COMPLETED", "DEAD_LETTER", "FAILED", "DISPATCHED"]);
/** States after which the console stops polling: DISPATCHED still moves on to COMPLETED. */
export const STOP_POLLING = new Set(["DRAFTED", "COMPLETED", "DEAD_LETTER"]);

export type StepId =
  | "ingest"
  | "normalize"
  | "triage"
  | "gate"
  | "context"
  | "generate"
  | "guard"
  | "drafted"
  | "dispatch";

export const STEPS: StepId[] = [
  "ingest",
  "normalize",
  "triage",
  "gate",
  "context",
  "generate",
  "guard",
  "drafted",
  "dispatch",
];

export type StepStatus = "done" | "active" | "skipped" | "failed" | "idle";

export interface StepView {
  id: StepId;
  status: StepStatus;
  events: JobEvent[];
}

const STATE_STEP: Record<string, StepId> = {
  RECEIVED: "ingest",
  NORMALIZED: "normalize",
  CLASSIFIED: "triage",
  QUEUED: "gate",
  CONTEXT_READY: "context",
  GENERATING: "generate",
  DRAFTED: "drafted",
  DISPATCHED: "dispatch",
  COMPLETED: "dispatch",
};

export function stepOf(event: JobEvent): StepId | null {
  if (event.event_type === "context_built") return "context";
  return STATE_STEP[event.state_to] ?? null;
}

/** Each step's status and events. An early exit marks the AI steps skipped, not pending. */
export function deriveSteps(timeline: Timeline | null, guarded: boolean): StepView[] {
  const events = timeline?.events ?? [];
  const byStep = new Map<StepId, JobEvent[]>();
  for (const e of events) {
    const step = stepOf(e);
    if (step) byStep.set(step, [...(byStep.get(step) ?? []), e]);
  }
  const states = events.map((e) => e.state_to);
  const current = timeline?.current_state ?? "";
  const earlyExit = states.includes("CLASSIFIED") && !states.includes("QUEUED") && current === "COMPLETED";
  const template = !states.includes("QUEUED") && states.includes("DRAFTED");
  const failed = current === "DEAD_LETTER" || current === "FAILED";
  const reached = new Set(byStep.keys());
  if (guarded && (reached.has("drafted") || reached.has("generate"))) reached.add("guard");
  const lastIndex = Math.max(-1, ...STEPS.map((s, i) => (reached.has(s) ? i : -1)));

  return STEPS.map((id, i) => {
    let status: StepStatus = "idle";
    if (reached.has(id)) status = "done";
    if (id === "guard" && !guarded) status = "skipped";
    if ((earlyExit || template) && ["gate", "context", "generate", "guard"].includes(id)) {
      status = id === "gate" && template ? "done" : "skipped";
    }
    if (earlyExit && ["drafted", "dispatch"].includes(id)) status = "skipped";
    if (!TERMINAL.has(current) && i === lastIndex + 1 && status === "idle") status = "active";
    if (failed && i === lastIndex + 1) status = "failed";
    return { id, status, events: byStep.get(id) ?? [] };
  });
}

export interface Actual {
  final_state?: string;
  category?: string;
  reply_required?: boolean;
  workflow_hint?: string;
  retrieval_required?: boolean;
  model_calls?: number;
  inbound_action?: string | null;
  final_action?: string | null;
}

/** What a finished run looked like, in the catalog's `expected` keys. */
export function actualOf(timeline: Timeline | null, guard?: { inbound_action: string | null; final_action: string | null } | null): Actual {
  const events = timeline?.events ?? [];
  // CLASSIFIED carries reply_required, QUEUED the routing fields: merge both, later events win.
  const p = Object.assign(
    {},
    ...events.filter((e) => e.state_to === "CLASSIFIED" || e.state_to === "QUEUED").map((e) => e.payload ?? {}),
  ) as Record<string, unknown>;
  const drafted = events.find((e) => e.state_to === "DRAFTED");
  const model = drafted?.payload?.model as string | undefined;
  const generated = events.some((e) => e.state_to === "GENERATING") && model !== "agentmailguard";
  return {
    final_state: timeline?.current_state,
    category: p.category as string | undefined,
    reply_required: p.reply_required as boolean | undefined,
    workflow_hint: p.workflow_hint as string | undefined,
    retrieval_required: p.retrieval_required as boolean | undefined,
    model_calls: generated ? 1 : 0,
    inbound_action: guard?.inbound_action,
    final_action: guard?.final_action,
  };
}

/** Each expected key with its actual value and whether they match; undefined actual = unknown. */
export function compare(expected: Record<string, unknown>, actual: Actual): { key: string; expected: unknown; actual: unknown; ok: boolean | null }[] {
  return Object.entries(expected).map(([key, want]) => {
    const got = (actual as Record<string, unknown>)[key];
    return { key, expected: want, actual: got, ok: got === undefined ? null : got === want };
  });
}
