// The console's runs (demo requests it started), kept in localStorage per tenant, and polling.
import { useEffect, useState, useSyncExternalStore } from "react";
import type { ApiClient, DemoAccepted, GuardReport, Timeline } from "@/api/client";
import { STOP_POLLING } from "./pipeline";

export interface Run {
  requestId: string;
  tenant: string;
  label: string;
  scenarioId?: string;
  /** The first request id of the scenario run that started this one (pairs C0 with C3). */
  batch?: string;
  guardMode: "C0" | "C3";
  profile: "fake" | "live";
  liveModel?: string;
  startedAt: string;
  jobId?: string;
  messageId?: string;
}

const KEY = "runs";
const listeners = new Set<() => void>();
let cache: Run[] = JSON.parse(localStorage.getItem(KEY) ?? "[]") as Run[];

function save(runs: Run[]): void {
  cache = runs.slice(0, 200);
  localStorage.setItem(KEY, JSON.stringify(cache));
  listeners.forEach((l) => l());
}

export function addRuns(tenant: string, accepted: DemoAccepted[], label: string, scenarioId?: string): Run[] {
  const startedAt = new Date().toISOString();
  const batch = accepted[0]?.demo_request_id; // one scenario run: its C0 and C3 share it
  const runs = accepted.map<Run>((a) => ({
    requestId: a.demo_request_id,
    tenant,
    label,
    scenarioId,
    batch,
    guardMode: a.run_options.guard_mode,
    profile: a.run_options.model_profile,
    liveModel: a.run_options.live_model ?? undefined,
    startedAt,
  }));
  save([...runs, ...cache]);
  return runs;
}

export function updateRun(requestId: string, patch: Partial<Run>): void {
  save(cache.map((r) => (r.requestId === requestId ? { ...r, ...patch } : r)));
}

export function useRuns(tenant: string): Run[] {
  const all = useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    () => cache,
  );
  return all.filter((r) => r.tenant === tenant);
}

export interface RunState {
  timeline: Timeline | null;
  guard: GuardReport | null;
  error: string | null;
}

/** Poll a run: its request until the job exists, then the timeline about every 500 ms until it
 * stops moving (ADR-0018 decision 8), then the guard report once for a C3 run. */
export function useRunState(api: ApiClient, run: Run | undefined, intervalMs = 500): RunState {
  const [state, setState] = useState<RunState>({ timeline: null, guard: null, error: null });

  useEffect(() => {
    if (!run) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    let jobId = run.jobId;
    setState({ timeline: null, guard: null, error: null });

    const tick = async () => {
      try {
        if (!jobId) {
          const req = await api.request(run.requestId);
          if (req.job_id) {
            jobId = req.job_id;
            updateRun(run.requestId, { jobId, messageId: req.message_id ?? undefined });
          }
        }
        if (jobId) {
          const timeline = await api.timeline(jobId);
          if (stopped) return;
          setState((s) => ({ ...s, timeline, error: null }));
          if (STOP_POLLING.has(timeline.current_state)) {
            if (run.guardMode === "C3") {
              const guard = await api.guard(jobId).catch(() => null);
              if (!stopped) setState((s) => ({ ...s, guard }));
            }
            return;
          }
        }
      } catch (err) {
        if (!stopped) setState((s) => ({ ...s, error: String(err) }));
      }
      if (!stopped) timer = setTimeout(tick, intervalMs);
    };
    void tick();
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
    // Keyed by the request id: updateRun (job id found) must not restart the polling.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api, run?.requestId, intervalMs]);

  return state;
}
