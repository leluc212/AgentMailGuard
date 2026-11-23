import { describe, expect, it } from "vitest";
import type { JobEvent, Timeline } from "@/api/client";
import { actualOf, compare, deriveSteps } from "./pipeline";

const ev = (state_to: string, payload: Record<string, unknown> = {}, event_type = "state_transition"): JobEvent => ({
  id: null,
  event_type,
  state_from: null,
  state_to,
  payload,
  created_at: "2026-11-16T00:00:00Z",
});

const tl = (current: string, events: JobEvent[]): Timeline => ({
  job_id: "j",
  current_state: current,
  total_events: events.length,
  events,
});

describe("deriveSteps", () => {
  it("marks the AI steps skipped on an early exit", () => {
    const steps = deriveSteps(
      tl("COMPLETED", [ev("RECEIVED"), ev("NORMALIZED"), ev("CLASSIFIED"), ev("COMPLETED")]),
      false,
    );
    const status = Object.fromEntries(steps.map((s) => [s.id, s.status]));
    expect(status.triage).toBe("done");
    expect(status.context).toBe("skipped");
    expect(status.generate).toBe("skipped");
  });

  it("shows the next step active while the job moves", () => {
    const steps = deriveSteps(tl("QUEUED", [ev("RECEIVED"), ev("NORMALIZED"), ev("CLASSIFIED"), ev("QUEUED")]), false);
    expect(steps.find((s) => s.id === "context")?.status).toBe("active");
  });

  it("puts the context_built event on the context step", () => {
    const steps = deriveSteps(tl("CONTEXT_READY", [ev("CONTEXT_READY", { retrieved: [] }, "context_built")]), false);
    expect(steps.find((s) => s.id === "context")?.events).toHaveLength(1);
  });
});

describe("compare", () => {
  it("passes matching keys and leaves unknown ones open", () => {
    const actual = actualOf(
      tl("COMPLETED", [ev("CLASSIFIED", { category: "automated_notification", reply_required: false }), ev("COMPLETED")]),
    );
    const rows = compare({ final_state: "COMPLETED", reply_required: false, model_calls: 0, category: "billing" }, actual);
    expect(rows.map((r) => r.ok)).toEqual([true, true, true, false]);
  });

  it("does not count a guard-escalated draft as a model call", () => {
    const actual = actualOf(tl("DRAFTED", [ev("GENERATING"), ev("DRAFTED", { model: "agentmailguard" })]));
    expect(actual.model_calls).toBe(0);
  });
});
