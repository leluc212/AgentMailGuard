// Scenario Runner (task 9.11, R25.2, R25.7): cards, run one or all, expected vs actual, composer.
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";
import { ApiClient, type GuardMode, type Scenario } from "@/api/client";
import { Badge, Button, Card, CardTitle, Empty } from "@/components/ui";
import { STOP_POLLING, actualOf, compare } from "@/lib/pipeline";
import { addRuns, useRunState, useRuns, type Run } from "@/lib/runs";
import { TENANTS, useConsole } from "@/state";
import { stateTone } from "./LivePipeline";

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** Upload a scenario's knowledge documents and wait until each is active (S12, G03, G05). */
async function uploadKnowledge(api: ApiClient, scenario: Scenario): Promise<void> {
  for (const doc of scenario.knowledge) {
    const blob = new Blob([doc.content], { type: "text/markdown" });
    const res = (await api.upload(blob, `${scenario.id}.md`, doc.title, doc.category)) as {
      document?: { id?: string };
      id?: string;
    };
    const id = res.document?.id ?? res.id;
    for (let i = 0; id && i < 60; i++) {
      const view = (await api.document(id)) as { status?: string };
      if (view.status === "active") break;
      if (view.status === "failed") throw new Error(`knowledge ${doc.title}: failed`);
      await sleep(1000);
    }
  }
}

function ResultRow({ run, scenario }: { run: Run; scenario?: Scenario }) {
  const { t } = useTranslation();
  const { api } = useConsole();
  const { timeline, guard } = useRunState(api, run, 800);
  const [isolation, setIsolation] = useState<boolean | null>(null);
  const expected = (() => {
    const e = scenario?.expected ?? {};
    const byMode = e[run.guardMode] as Record<string, unknown> | undefined;
    return scenario?.kind === "comparison" ? byMode ?? {} : e;
  })();
  const rows = timeline ? compare(expected, actualOf(timeline, guard)) : [];

  // S09: another tenant must not see this job (R23.6)
  useEffect(() => {
    if (scenario?.flow !== "cross_tenant" || !run.jobId || isolation !== null) return;
    const other = TENANTS.find((x) => x.id !== run.tenant)!.id;
    new ApiClient(other)
      .job(run.jobId)
      .then(() => setIsolation(false))
      .catch((e: { status?: number }) => setIsolation(e.status === 404));
  }, [scenario?.flow, run.jobId, run.tenant, isolation]);

  // A flow scenario (S11) waits at DRAFTED for the presenter to approve it in Review & Dispatch.
  const awaitingReview =
    scenario?.kind === "flow" && timeline?.current_state === "DRAFTED" && expected.final_state !== "DRAFTED";
  // Judge only a job that has stopped moving: S10 passes through RETRY_PENDING on its way to DEAD_LETTER.
  const stopped = !!timeline && STOP_POLLING.has(timeline.current_state);
  const verdict = rows.length === 0 || awaitingReview || !stopped ? null : rows.every((r) => r.ok !== false);
  return (
    <div className="rounded-md border p-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <Link className="font-medium underline" to={`/?run=${run.requestId}`}>
          {run.label}
        </Link>
        <Badge>{run.guardMode}</Badge>
        {timeline && <Badge tone={stateTone(timeline.current_state)}>{timeline.current_state}</Badge>}
        {verdict === null ? (
          // nothing to check (no expected keys) once the job has stopped: show only its state
          rows.length === 0 && stopped ? null : (
            <Badge tone="info">{awaitingReview ? t("top.awaitingReview") : t("scenarios.pending")}</Badge>
          )
        ) : (
          <Badge tone={verdict ? "success" : "danger"}>{t(verdict ? "scenarios.pass" : "scenarios.fail")}</Badge>
        )}
        {isolation !== null && (
          <Badge tone={isolation ? "success" : "danger"}>isolation {isolation ? "404 ✓" : "LEAK"}</Badge>
        )}
        {run.guardMode === "C3" && run.jobId && (
          <Link className="text-xs underline" to={`/guard?job=${run.jobId}`}>
            {t("nav.guard")}
          </Link>
        )}
        {timeline?.current_state === "DEAD_LETTER" && run.jobId && (
          <Link className="text-xs underline" to={`/timeline?job=${run.jobId}`}>
            {t("timeline.replay")}
          </Link>
        )}
      </div>
      {rows.length > 0 && (
        <table className="mt-2 text-xs">
          <thead>
            <tr className="text-muted-foreground">
              <th className="pr-4 text-left font-medium" />
              <th className="pr-4 text-left font-medium">{t("scenarios.expected")}</th>
              <th className="text-left font-medium">{t("scenarios.actual")}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.key}>
                <td className="pr-4 font-mono">{r.key}</td>
                <td className="pr-4 font-mono">{String(r.expected)}</td>
                <td className={`font-mono ${r.ok === false ? "text-danger" : r.ok ? "text-success" : ""}`}>
                  {String(r.actual ?? "…")}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function SearchDebugResult({ query }: { query: string }) {
  const { api } = useConsole();
  const [result, setResult] = useState<unknown>(null);
  useEffect(() => {
    api.searchDebug(query).then(setResult).catch((e) => setResult({ error: String(e) }));
  }, [api, query]);
  return (
    <pre className="max-h-96 overflow-auto rounded-md bg-muted p-3 text-xs">
      {result ? JSON.stringify(result, null, 2) : "…"}
    </pre>
  );
}

function Composer() {
  const { t } = useTranslation();
  const { api, tenant, profile, liveModel } = useConsole();
  const [form, setForm] = useState({ sender_email: "you@example.com", subject: "", body: "" });
  const [mode, setMode] = useState<GuardMode>("C0");
  const [error, setError] = useState<string | null>(null);
  const send = async () => {
    setError(null);
    try {
      const accepted = await api.sendEmail({
        ...form,
        run_options: {
          model_profile: profile,
          guard_mode: mode,
          live_model: profile === "live" ? liveModel : null,
        },
      });
      addRuns(tenant, [accepted], form.subject || "(free email)");
    } catch (e) {
      setError(String(e));
    }
  };
  const field = "min-h-9 w-full rounded-md border bg-background px-2 text-sm";
  return (
    <Card>
      <CardTitle>{t("scenarios.composer")}</CardTitle>
      <div className="grid gap-2">
        <input className={field} aria-label={t("scenarios.from")} value={form.sender_email} onChange={(e) => setForm({ ...form, sender_email: e.target.value })} />
        <input className={field} aria-label={t("scenarios.subject")} placeholder={t("scenarios.subject")} value={form.subject} onChange={(e) => setForm({ ...form, subject: e.target.value })} />
        <textarea className={`${field} min-h-28 py-2`} aria-label={t("scenarios.body")} placeholder={t("scenarios.body")} value={form.body} onChange={(e) => setForm({ ...form, body: e.target.value })} />
        <div className="flex items-center gap-2">
          {t("scenarios.guardMode")}
          {(["C0", "C3"] as const).map((m) => (
            <Button key={m} size="sm" variant={mode === m ? "default" : "outline"} onClick={() => setMode(m)}>
              {m}
            </Button>
          ))}
          <Button className="ml-auto" disabled={!form.subject || !form.body} onClick={() => void send()}>
            {t("scenarios.send")}
          </Button>
        </div>
        {error && <p className="text-sm text-danger">{error}</p>}
      </div>
    </Card>
  );
}

export function ScenarioRunner() {
  const { t, i18n } = useTranslation();
  const { api, tenant, profile, liveModel, capabilities } = useConsole();
  const runs = useRuns(tenant);
  const [scenarios, setScenarios] = useState<Scenario[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [debugQuery, setDebugQuery] = useState<string | null>(null);
  const lang = i18n.language === "vi" ? "vi" : "en";

  useEffect(() => {
    api.scenarios().then(setScenarios).catch((e) => setError(String(e)));
  }, [api]);

  const run = async (s: Scenario) => {
    setError(null);
    setBusy(s.id);
    try {
      if (s.flow === "search_debug" && s.query) {
        setDebugQuery(s.query);
        return;
      }
      if (s.knowledge.length) await uploadKnowledge(api, s);
      const live = s.requires_live || profile === "live";
      const res = await api.runScenario(s.id, s.requires_live ? undefined : profile, live ? liveModel : null);
      addRuns(tenant, res.requests, `${s.id} · ${s.title[lang]}${live && liveModel ? ` · ${liveModel}` : ""}`, s.id);
    } catch (e) {
      setError(`${s.id}: ${String(e)}`);
    } finally {
      setBusy(null);
    }
  };

  const runAll = async () => {
    for (const s of scenarios) {
      if (s.requires_live && !capabilities?.live_model_available) continue;
      if (s.flow === "burst") continue; // a burst skews every other scenario's latency
      await run(s);
    }
  };

  return (
    <div className="grid grid-cols-[1fr_420px] gap-4">
      <div className="flex flex-col gap-4">
        <div className="flex items-center gap-2">
          <Button onClick={() => void runAll()} disabled={busy !== null || scenarios.length === 0}>
            {t("scenarios.runAll")}
          </Button>
          {error && <span className="text-sm text-danger">{error}</span>}
        </div>
        {(["pipeline", "guard"] as const).map((group) => (
          <div key={group} className="grid grid-cols-2 gap-3 xl:grid-cols-3">
            {scenarios
              .filter((s) => s.group === group)
              .map((s) => {
                const blocked = s.requires_live && !capabilities?.live_model_available;
                return (
                  <Card key={s.id} className="flex flex-col gap-2">
                    <div className="flex items-center gap-2">
                      <Badge tone="info">{s.id}</Badge>
                      <span className="font-medium">{s.title[lang]}</span>
                    </div>
                    <p className="flex-1 text-sm text-muted-foreground">{s.description[lang]}</p>
                    <div className="flex flex-wrap items-center gap-2">
                      {s.kind === "comparison" && <Badge>C0 ⇄ C3</Badge>}
                      {s.kind === "flow" && <Badge>{t("scenarios.flow")}</Badge>}
                      {s.requires_live && <Badge tone="warning">{t("scenarios.liveOnly")}</Badge>}
                      {s.source && <Badge title={s.source.case_id}>{s.source.origin} · {s.source.license}</Badge>}
                      <Button className="ml-auto" size="sm" disabled={busy !== null || blocked} onClick={() => void run(s)}>
                        {busy === s.id ? "…" : t("scenarios.run")}
                      </Button>
                    </div>
                  </Card>
                );
              })}
          </div>
        ))}
      </div>
      <div className="flex flex-col gap-4">
        <Composer />
        {debugQuery && (
          <Card>
            <CardTitle>S14 · /v1/search/debug</CardTitle>
            <SearchDebugResult query={debugQuery} />
          </Card>
        )}
        <Card>
          <CardTitle>{t("pipeline.runs")}</CardTitle>
          {runs.length === 0 && <Empty>{t("pipeline.noRuns")}</Empty>}
          <div className="flex max-h-[60vh] flex-col gap-2 overflow-auto">
            {runs.slice(0, 30).map((r) => (
              <ResultRow key={r.requestId} run={r} scenario={scenarios.find((s) => s.id === r.scenarioId)} />
            ))}
          </div>
        </Card>
      </div>
    </div>
  );
}
