// Scenario Runner (task 9.11, R25.2, R25.7): cards, run one or all, expected vs actual, composer.
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";
import { ApiClient, type GuardMode, type Scenario } from "@/api/client";
import { Badge, Button, Card, CardTitle, Empty } from "@/components/ui";
import { STOP_POLLING, actualOf, compare } from "@/lib/pipeline";
import { addRuns, useRunState, useRuns, type Run } from "@/lib/runs";
import { byCategory, detectedExcerpts, guardReasons, highlightSegments, type MarkKind } from "@/lib/scenarios";
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

const MARK_CLASS: Record<MarkKind, string> = {
  expected: "rounded-sm bg-warning/25 underline decoration-warning decoration-2 underline-offset-2",
  detected: "rounded-sm bg-danger/25 underline decoration-danger decoration-2 underline-offset-2",
  both: "rounded-sm bg-danger/30 underline decoration-warning decoration-double decoration-2 underline-offset-2",
};

/** ``text`` with the catalog's attack text and the guard's quotes marked (task 9.19). */
function Marked({ text, expected, detected }: { text: string; expected: string[]; detected: string[] }) {
  return (
    <>
      {highlightSegments(text, expected, detected).map((s, i) =>
        s.mark ? (
          <mark key={i} data-mark={s.mark} className={`text-inherit ${MARK_CLASS[s.mark]}`}>
            {s.text}
          </mark>
        ) : (
          <span key={i}>{s.text}</span>
        ),
      )}
    </>
  );
}

function MailView({
  mail,
  expected,
  detected,
}: {
  mail: { sender_email: string; sender_name?: string | null; subject: string; body: string };
  expected: string[];
  detected: string[];
}) {
  const { t } = useTranslation();
  return (
    <div className="rounded-md border text-sm">
      <div className="grid grid-cols-[max-content_minmax(0,1fr)] gap-x-3 gap-y-1 border-b bg-muted/50 p-3">
        <span className="text-muted-foreground">{t("scenarios.from")}</span>
        <span className="break-all font-mono text-xs leading-5">
          {mail.sender_name ? `${mail.sender_name} <` : ""}
          <Marked text={mail.sender_email} expected={expected} detected={detected} />
          {mail.sender_name ? ">" : ""}
        </span>
        <span className="text-muted-foreground">{t("scenarios.subject")}</span>
        <span className="font-medium">
          <Marked text={mail.subject} expected={expected} detected={detected} />
        </span>
      </div>
      <pre className="whitespace-pre-wrap break-words p-3 font-mono text-xs leading-5">
        <Marked text={mail.body} expected={expected} detected={detected} />
      </pre>
    </div>
  );
}

/** One scenario's input and its latest result, side by side with the guard's evidence. */
function ScenarioDetail({
  scenario,
  runs,
  busy,
  blocked,
  onRun,
  onClose,
}: {
  scenario: Scenario;
  runs: Run[];
  busy: boolean;
  blocked: boolean;
  onRun: () => void;
  onClose: () => void;
}) {
  const { t, i18n } = useTranslation();
  const { api } = useConsole();
  const lang = i18n.language === "vi" ? "vi" : "en";
  // the newest batch of this scenario (a comparison's C0 and C3 share it)
  const mine = runs.filter((r) => r.scenarioId === scenario.id);
  const batch = mine[0]?.batch;
  const latest = (batch ? mine.filter((r) => r.batch === batch) : mine.slice(0, 1)).sort((a, b) =>
    a.guardMode.localeCompare(b.guardMode),
  );
  const c3 = latest.find((r) => r.guardMode === "C3");
  const { guard } = useRunState(api, c3, 1000);
  const reasons = guardReasons(guard?.report);
  const detected = detectedExcerpts(reasons);
  const expected = scenario.highlights;

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const section = "mb-2 text-sm font-semibold";
  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-black/40" onClick={onClose}>
      <aside
        role="dialog"
        aria-label={`${scenario.id} ${scenario.title[lang]}`}
        className="flex h-full w-[min(760px,100vw)] flex-col gap-4 overflow-auto border-l bg-card p-5 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start gap-2">
          <Badge tone="info">{scenario.id}</Badge>
          <div className="flex-1">
            <h2 className="font-semibold">{scenario.title[lang]}</h2>
            <p className="text-sm text-muted-foreground">{scenario.description[lang]}</p>
          </div>
          <Button size="sm" disabled={busy || blocked} onClick={onRun}>
            {busy ? "…" : t("scenarios.run")}
          </Button>
          <Button size="sm" variant="ghost" onClick={onClose} aria-label={t("common.close")}>
            ✕
          </Button>
        </div>

        {scenario.rationale && (
          <div className="rounded-md border-l-4 border-warning bg-warning/10 p-3 text-sm">
            <div className="mb-1 font-semibold">{t("scenarios.why")}</div>
            {scenario.rationale[lang]}
          </div>
        )}

        {(expected.length > 0 || detected.length > 0) && (
          <div className="flex flex-wrap gap-3 text-xs text-muted-foreground">
            <span>
              <mark className={`px-1 text-inherit ${MARK_CLASS.expected}`}>abc</mark> {t("scenarios.markExpected")}
            </span>
            <span>
              <mark className={`px-1 text-inherit ${MARK_CLASS.detected}`}>abc</mark> {t("scenarios.markDetected")}
            </span>
          </div>
        )}

        {scenario.thread.length > 0 && (
          <details>
            <summary className={`cursor-pointer ${section}`}>
              {t("scenarios.thread")} ({scenario.thread.length})
            </summary>
            <div className="flex flex-col gap-2">
              {scenario.thread.map((m, i) => (
                <MailView key={i} mail={m} expected={[]} detected={[]} />
              ))}
            </div>
          </details>
        )}

        {scenario.email && (
          <div>
            <h3 className={section}>{t("scenarios.email")}</h3>
            <MailView mail={scenario.email} expected={expected} detected={detected} />
          </div>
        )}

        {scenario.query && (
          <div>
            <h3 className={section}>{t("scenarios.query")}</h3>
            <pre className="rounded-md border p-3 font-mono text-xs">{scenario.query}</pre>
          </div>
        )}

        {scenario.knowledge.length > 0 && (
          <div>
            <h3 className={section}>{t("scenarios.knowledge")}</h3>
            <div className="flex flex-col gap-2">
              {scenario.knowledge.map((d) => (
                <div key={d.title} className={`rounded-md border p-3 text-sm ${d.poisoned ? "border-danger/60" : ""}`}>
                  <div className="mb-1 flex items-center gap-2">
                    <span className="font-medium">{d.title}</span>
                    <Badge>{d.category}</Badge>
                    <Badge tone={d.poisoned ? "danger" : "success"}>
                      {t(d.poisoned ? "scenarios.poisoned" : "scenarios.trusted")}
                    </Badge>
                  </div>
                  <p className="whitespace-pre-wrap break-words font-mono text-xs leading-5">
                    <Marked text={d.content} expected={expected} detected={detected} />
                  </p>
                </div>
              ))}
            </div>
          </div>
        )}

        {scenario.source && (
          <p className="text-xs text-muted-foreground">
            {t("scenarios.source")}: <span className="font-mono">{scenario.source.case_id}</span> · {scenario.source.origin} ·{" "}
            {scenario.source.license}
            {scenario.source.modified ? ` · ${scenario.source.modified}` : ""}
          </p>
        )}

        <div>
          <h3 className={section}>{t("scenarios.latest")}</h3>
          {latest.length === 0 ? (
            <Empty>{t("scenarios.notRun")}</Empty>
          ) : (
            <div className={`grid gap-2 ${latest.length > 1 ? "grid-cols-2" : ""}`}>
              {latest.map((r) => (
                <ResultRow key={r.requestId} run={r} scenario={scenario} />
              ))}
            </div>
          )}
        </div>

        {c3 && guard && (
          <div>
            <h3 className={section}>{t("scenarios.whyActual")}</h3>
            {reasons.length === 0 ? (
              <Empty>{t("scenarios.noReasons")}</Empty>
            ) : (
              <table className="w-full text-xs">
                <tbody>
                  {reasons.map((r, i) => (
                    <tr key={i} className="border-t align-top">
                      <td className="py-1 pr-2">
                        <Badge tone="info">{r.layer}</Badge>
                      </td>
                      <td className="py-1 pr-2">{r.severity && <Badge>{r.severity}</Badge>}</td>
                      <td className="break-all py-1 pr-2 font-mono">{r.label}</td>
                      <td className="py-1">
                        {r.rationale}
                        {r.excerpt && <div className="mt-1 break-words font-mono text-muted-foreground">“{r.excerpt}”</div>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        )}
      </aside>
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
  const [selectedId, setSelectedId] = useState<string | null>(null);
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

  const runMany = async (list: Scenario[]) => {
    for (const s of list) {
      if (s.requires_live && !capabilities?.live_model_available) continue;
      if (s.flow === "burst") continue; // a burst skews every other scenario's latency
      await run(s);
    }
  };
  const runAll = () => runMany(scenarios);
  const selected = scenarios.find((s) => s.id === selectedId);
  const isBlocked = (s: Scenario) => s.requires_live && !capabilities?.live_model_available;

  return (
    <div className="grid grid-cols-[1fr_420px] gap-4">
      <div className="flex flex-col gap-4">
        <div className="flex items-center gap-2">
          <Button onClick={() => void runAll()} disabled={busy !== null || scenarios.length === 0}>
            {t("scenarios.runAll")}
          </Button>
          {error && <span className="text-sm text-danger">{error}</span>}
        </div>
        {byCategory(scenarios).map(([category, list]) => (
          <section key={category} aria-label={t(`scenarios.categories.${category}`)} className="flex flex-col gap-2">
            <div className="flex items-end gap-3 border-b pb-1">
              <div className="flex-1">
                <h2 className="font-semibold">{t(`scenarios.categories.${category}`)}</h2>
                <p className="text-xs text-muted-foreground">{t(`scenarios.categoryHints.${category}`)}</p>
              </div>
              <Button size="sm" variant="outline" disabled={busy !== null} onClick={() => void runMany(list)}>
                {t("scenarios.runGroup")}
              </Button>
            </div>
            <div className="grid grid-cols-2 gap-3 xl:grid-cols-3">
              {list.map((s) => (
                <Card key={s.id} className={`flex flex-col gap-2 ${selectedId === s.id ? "ring-2 ring-primary" : ""}`}>
                  <div className="flex items-center gap-2">
                    <Badge tone="info">{s.id}</Badge>
                    <span className="font-medium">{s.title[lang]}</span>
                  </div>
                  <p className="flex-1 text-sm text-muted-foreground">{s.description[lang]}</p>
                  {s.email && (
                    <p className="truncate font-mono text-xs text-muted-foreground" title={s.email.subject}>
                      ✉ {s.email.subject}
                    </p>
                  )}
                  <div className="flex flex-wrap items-center gap-2">
                    {s.kind === "comparison" && <Badge>C0 ⇄ C3</Badge>}
                    {s.kind === "flow" && <Badge>{t("scenarios.flow")}</Badge>}
                    {s.requires_live && <Badge tone="warning">{t("scenarios.liveOnly")}</Badge>}
                    {s.source && <Badge title={s.source.case_id}>{s.source.origin} · {s.source.license}</Badge>}
                    <div className="ml-auto flex gap-2">
                      <Button size="sm" variant="outline" onClick={() => setSelectedId(s.id)}>
                        {t("scenarios.details")}
                      </Button>
                      <Button size="sm" disabled={busy !== null || isBlocked(s)} onClick={() => void run(s)}>
                        {busy === s.id ? "…" : t("scenarios.run")}
                      </Button>
                    </div>
                  </div>
                </Card>
              ))}
            </div>
          </section>
        ))}
      </div>
      {selected && (
        <ScenarioDetail
          scenario={selected}
          runs={runs}
          busy={busy !== null}
          blocked={isBlocked(selected)}
          onRun={() => void run(selected)}
          onClose={() => setSelectedId(null)}
        />
      )}
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
