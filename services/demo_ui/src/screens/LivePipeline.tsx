// Live Pipeline (task 9.10, R25.7): inbox of runs, animated diagram, per-step details.
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";
import type { JobEvent } from "@/api/client";
import { PipelineDiagram } from "@/components/PipelineDiagram";
import { Badge, Card, CardTitle, Empty, KeyValue } from "@/components/ui";
import { deriveSteps, type StepId, type StepView } from "@/lib/pipeline";
import { useRunState, useRuns, type Run } from "@/lib/runs";
import { useConsole } from "@/state";

export function stateTone(state?: string | null) {
  if (!state) return "neutral" as const;
  if (["DEAD_LETTER", "FAILED"].includes(state)) return "danger" as const;
  if (["DRAFTED", "COMPLETED", "DISPATCHED"].includes(state)) return "success" as const;
  if (state === "RETRY_PENDING") return "warning" as const;
  return "info" as const;
}

export function RunInbox({ runs, selected, onSelect }: { runs: Run[]; selected?: string; onSelect: (id: string) => void }) {
  const { t } = useTranslation();
  if (runs.length === 0) return <Empty>{t("pipeline.noRuns")}</Empty>;
  return (
    <ul className="flex max-h-[70vh] flex-col gap-1 overflow-auto">
      {runs.map((r) => (
        <li key={r.requestId}>
          <button
            className={`w-full rounded-md border px-3 py-2 text-left text-sm hover:bg-muted ${
              selected === r.requestId ? "border-primary bg-primary/10" : ""
            }`}
            onClick={() => onSelect(r.requestId)}
          >
            <div className="flex items-center justify-between gap-2">
              <span className="truncate font-medium">{r.label}</span>
              <Badge tone={r.guardMode === "C3" ? "info" : "neutral"}>{r.guardMode}</Badge>
            </div>
            <div className="text-xs text-muted-foreground">
              {r.profile} · {new Date(r.startedAt).toLocaleTimeString()}
            </div>
          </button>
        </li>
      ))}
    </ul>
  );
}

function ms(from?: string, to?: string): string | null {
  if (!from || !to) return null;
  return `${Math.max(0, new Date(to).getTime() - new Date(from).getTime())} ms`;
}

function Retrieved({ rows }: { rows: Record<string, unknown>[] }) {
  if (rows.length === 0) return <Empty>—</Empty>;
  const cols = ["rank", "chunk_id", "lexical_score", "vector_score", "fused_score", "rerank_score"];
  return (
    <div className="overflow-auto">
      <table className="w-full text-xs">
        <thead>
          <tr className="text-left text-muted-foreground">
            {cols.map((c) => (
              <th key={c} className="px-2 py-1 font-medium">
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} className="border-t">
              {cols.map((c) => (
                <td key={c} className="px-2 py-1 font-mono">
                  {typeof r[c] === "number" ? Number(r[c]).toFixed(4).replace(/\.?0+$/, "") : String(r[c] ?? "—")}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function StepDetail({ step, all }: { step: StepView | undefined; all: JobEvent[] }) {
  const { t } = useTranslation();
  if (!step) return <Empty>{t("pipeline.pickStep")}</Empty>;
  if (step.events.length === 0) return <Empty>{step.status}</Empty>;
  const first = all[0]?.created_at;
  return (
    <div className="flex flex-col gap-4">
      {step.events.map((e, i) => {
        const p = e.payload ?? {};
        const retrieved = Array.isArray(p.retrieved) ? (p.retrieved as Record<string, unknown>[]) : null;
        const rows = Object.entries(p).filter(([k, v]) => k !== "retrieved" && typeof v !== "object");
        return (
          <div key={i} className="flex flex-col gap-2">
            <div className="flex items-center gap-2">
              <Badge tone={stateTone(e.state_to)}>{e.event_type === "context_built" ? "context_built" : e.state_to}</Badge>
              <span className="text-xs text-muted-foreground">+{ms(first, e.created_at)}</span>
            </div>
            <KeyValue rows={rows.map(([k, v]) => [k, String(v)])} />
            {retrieved && <Retrieved rows={retrieved} />}
          </div>
        );
      })}
    </div>
  );
}

export function LivePipeline() {
  const { t } = useTranslation();
  const { api, tenant } = useConsole();
  const runs = useRuns(tenant);
  const [params, setParams] = useSearchParams();
  const selectedId = params.get("run") ?? runs[0]?.requestId;
  const run = runs.find((r) => r.requestId === selectedId);
  const { timeline, error } = useRunState(api, run);
  const steps = useMemo(() => deriveSteps(timeline, run?.guardMode === "C3"), [timeline, run?.guardMode]);
  const [stepId, setStepId] = useState<StepId | null>(null);

  return (
    <div className="grid grid-cols-[280px_1fr] gap-4">
      <Card>
        <CardTitle>{t("pipeline.runs")}</CardTitle>
        <RunInbox runs={runs} selected={selectedId} onSelect={(id) => setParams({ run: id })} />
      </Card>
      <div className="flex min-w-0 flex-col gap-4">
        <Card>
          <CardTitle
            extra={
              timeline && (
                <Badge tone={stateTone(timeline.current_state)}>{timeline.current_state}</Badge>
              )
            }
          >
            {run?.label ?? "—"}
          </CardTitle>
          {run && !timeline && <p className="text-sm text-muted-foreground">{t("pipeline.waiting")}</p>}
          {error && <p className="text-sm text-danger">{error}</p>}
          <PipelineDiagram steps={steps} selected={stepId} onSelect={setStepId} />
        </Card>
        <Card>
          <CardTitle>{t("pipeline.step")}</CardTitle>
          <StepDetail step={steps.find((s) => s.id === stepId)} all={timeline?.events ?? []} />
        </Card>
      </div>
    </div>
  );
}
