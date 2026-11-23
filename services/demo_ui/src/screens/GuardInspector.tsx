// Guard Inspector and C0 ⇄ C3 comparison (task 9.12, R25.6, R25.8).
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";
import type { GuardReport, Json } from "@/api/client";
import { Badge, Card, CardTitle, Empty, KeyValue } from "@/components/ui";
import { useRunState, useRuns, type Run } from "@/lib/runs";
import { useConsole } from "@/state";
import { stateTone } from "./LivePipeline";

type Obj = Record<string, unknown>;
const asObj = (v: unknown): Obj | null => (v && typeof v === "object" && !Array.isArray(v) ? (v as Obj) : null);

const LAYERS: [string, string][] = [
  ["inbound_guard", "L1 Inbound Guard"],
  ["doc_scanner", "L2 Doc Scanner"],
  ["inbound_decision", "L3 Inbound Gate"],
  ["output_guard", "L4 Output Guard"],
];

function severityTone(sev: unknown) {
  const s = String(sev ?? "").toLowerCase();
  if (["critical", "high"].includes(s)) return "danger" as const;
  if (["medium", "low"].includes(s)) return "warning" as const;
  return "success" as const;
}

function actionTone(action?: string | null) {
  if (!action) return "neutral" as const;
  if (["block", "quarantine"].includes(action)) return "danger" as const;
  if (action === "human_approval") return "warning" as const;
  return "success" as const;
}

export function GuardReportView({ report }: { report: GuardReport }) {
  const { t } = useTranslation();
  const r = report.report;
  const docs = asObj(r.doc_scanner);
  const chunks = (Array.isArray(docs?.chunks) ? docs?.chunks : []) as Obj[];
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        {t("guard.decision")}:
        <Badge tone={actionTone(report.inbound_action)}>inbound {report.inbound_action ?? "—"}</Badge>
        <Badge tone={actionTone(report.final_action)}>final {report.final_action ?? "—"}</Badge>
        {r.rule != null && <Badge>rule {String(r.rule)}</Badge>}
        <Badge>{report.model_profile ?? "?"}</Badge>
        <Badge>judge calls {report.guard_llm_calls}</Badge>
        <Badge>guard {report.timings_ms.guard ?? "?"} ms</Badge>
      </div>

      <div>
        <h3 className="mb-2 text-sm font-semibold">{t("guard.layers")}</h3>
        <div className="grid grid-cols-2 gap-2">
          {LAYERS.map(([key, label]) => {
            const layer = asObj(r[key]);
            return (
              <div key={key} className="min-w-0 rounded-md border p-3">
                <div className="mb-1 flex items-center justify-between">
                  <span className="text-sm font-medium">{label}</span>
                  {layer ? (
                    <Badge tone={severityTone(layer.severity ?? layer.action)}>
                      {String(layer.severity ?? layer.action ?? "ok")}
                    </Badge>
                  ) : (
                    <Badge>off / not reached</Badge>
                  )}
                </div>
                {layer && (
                  <KeyValue
                    rows={Object.entries(layer)
                      .filter(([, v]) => v === null || typeof v !== "object")
                      .slice(0, 8)
                      .map(([k, v]) => [k, String(v)])}
                  />
                )}
              </div>
            );
          })}
        </div>
      </div>

      <div>
        <h3 className="mb-2 text-sm font-semibold">{t("guard.chunks")}</h3>
        {chunks.length === 0 ? (
          <Empty>—</Empty>
        ) : (
          <table className="w-full text-xs">
            <tbody>
              {chunks.map((c, i) => (
                <tr key={i} className="border-t">
                  <td className="px-2 py-1 font-mono">{String(c.chunk_id)}</td>
                  <td className="px-2 py-1">
                    <Badge tone={c.trust === "quarantined" ? "danger" : c.trust === "untrusted" ? "warning" : "success"}>
                      {String(c.trust)}
                    </Badge>
                  </td>
                  <td className="px-2 py-1 font-mono">{c.score != null ? Number(c.score).toFixed(2) : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <details>
        <summary className="cursor-pointer text-sm font-semibold">{t("guard.prompt")}</summary>
        <pre className="mt-2 max-h-80 overflow-auto rounded-md bg-muted p-3 text-xs">
          {JSON.stringify(r.isolation ?? null, null, 2)}
        </pre>
      </details>
      <details>
        <summary className="cursor-pointer text-sm font-semibold">report (JSON)</summary>
        <pre className="mt-2 max-h-96 overflow-auto rounded-md bg-muted p-3 text-xs">{JSON.stringify(r, null, 2)}</pre>
      </details>
    </div>
  );
}

function DraftColumn({ run }: { run: Run | undefined }) {
  const { t } = useTranslation();
  const { api } = useConsole();
  const { timeline, guard } = useRunState(api, run, 800);
  const [draft, setDraft] = useState<Json | null>(null);
  const draftId = timeline?.events.find((e) => e.state_to === "DRAFTED")?.payload?.draft_id as string | undefined;
  useEffect(() => {
    if (draftId) api.draft(draftId).then(setDraft).catch(() => setDraft(null));
  }, [api, draftId]);
  if (!run) return <Empty>—</Empty>;
  const d = asObj(draft?.draft) ?? draft;
  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center gap-2">
        <Badge tone="info">{run.guardMode}</Badge>
        {timeline && <Badge tone={stateTone(timeline.current_state)}>{timeline.current_state}</Badge>}
        {guard && <Badge tone={actionTone(guard.final_action)}>{guard.final_action}</Badge>}
      </div>
      <KeyValue
        rows={[
          ["action", String(d?.action ?? "—")],
          ["model", String(d?.model_name ?? "—")],
          ["escalation", String(d?.escalation_reason ?? "—")],
        ]}
      />
      <pre className="min-h-24 whitespace-pre-wrap rounded-md bg-muted p-3 text-sm">{String(d?.body ?? (timeline ? "" : t("common.loading")))}</pre>
      {guard && <GuardReportView report={guard} />}
      {run.guardMode === "C3" && timeline && !guard && ["DRAFTED", "COMPLETED"].includes(timeline.current_state) && (
        <Empty>{t("guard.noReport")}</Empty>
      )}
    </div>
  );
}

export function GuardInspector() {
  const { t } = useTranslation();
  const { api, tenant } = useConsole();
  const runs = useRuns(tenant);
  const [params, setParams] = useSearchParams();

  // Comparison pairs: the two runs one comparison scenario started (C0 then C3)
  const pairs = useMemo(() => {
    const out: { key: string; label: string; c0?: Run; c3?: Run }[] = [];
    for (const r of runs) {
      if (!r.scenarioId?.startsWith("G")) continue;
      const key = r.batch ?? `${r.scenarioId}-${r.startedAt.slice(0, 19)}`;
      let pair = out.find((p) => p.key === key);
      if (!pair) {
        const time = new Date(r.startedAt).toLocaleTimeString();
        out.push((pair = { key, label: `${r.label} · ${time}` }));
      }
      if (r.guardMode === "C0") pair.c0 = r;
      else pair.c3 = r;
    }
    return out;
  }, [runs]);
  const pairKey = params.get("pair") ?? pairs[0]?.key;
  // a stale ?pair= (another tenant, cleared runs) falls back to the newest comparison
  const pair = pairs.find((p) => p.key === pairKey) ?? pairs[0];

  const jobId = params.get("job");
  const [report, setReport] = useState<GuardReport | null | "none">(null);
  useEffect(() => {
    if (!jobId) return;
    api.guard(jobId).then(setReport).catch(() => setReport("none"));
  }, [api, jobId]);

  return (
    <div className="flex flex-col gap-4">
      {jobId && (
        <Card>
          <CardTitle>{jobId}</CardTitle>
          {report === null ? <Empty>{t("common.loading")}</Empty> : report === "none" ? <Empty>{t("guard.noReport")}</Empty> : <GuardReportView report={report} />}
        </Card>
      )}
      <Card>
        <CardTitle
          extra={
            <select
              className="min-h-9 rounded-md border bg-background px-2 text-sm"
              value={pair?.key ?? ""}
              onChange={(e) => setParams({ pair: e.target.value })}
            >
              {pairs.map((p) => (
                <option key={p.key} value={p.key}>
                  {p.label}
                </option>
              ))}
            </select>
          }
        >
          {t("guard.compare")}
        </CardTitle>
        {pairs.length === 0 ? (
          <Empty>{t("guard.pickJob")}</Empty>
        ) : (
          <div className="grid grid-cols-2 gap-4">
            <DraftColumn run={pair?.c0} />
            <DraftColumn run={pair?.c3} />
          </div>
        )}
      </Card>
    </div>
  );
}
