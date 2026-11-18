// Review & Dispatch, Timeline with replay, Knowledge upload (task 9.13, R23.4, R23.5, R23.7).
// The Jinja2 review UI (127.0.0.1:3001) keeps running beside these screens (owner decision Q5).
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";
import type { Json, Timeline as TimelineData } from "@/api/client";
import { Badge, Button, Card, CardTitle, Empty, KeyValue } from "@/components/ui";
import { useRuns } from "@/lib/runs";
import { useConsole } from "@/state";
import { stateTone } from "./LivePipeline";

const s = (v: unknown) => (v === null || v === undefined ? "—" : String(v));
const pick = (o: Json | null, ...keys: string[]): unknown => {
  for (const k of keys) {
    const parts = k.split(".");
    let cur: unknown = o;
    for (const p of parts) cur = cur && typeof cur === "object" ? (cur as Json)[p] : undefined;
    if (cur !== undefined && cur !== null) return cur;
  }
  return undefined;
};

export function Review() {
  const { t } = useTranslation();
  const { api } = useConsole();
  const [items, setItems] = useState<Json[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<Json | null>(null);
  const [body, setBody] = useState("");
  const [message, setMessage] = useState<string | null>(null);

  const load = useCallback(() => {
    api.drafts("draft").then((r) => setItems(r.items)).catch((e) => setMessage(String(e)));
  }, [api]);
  useEffect(load, [load]);
  useEffect(() => {
    if (!selected) return;
    api.draft(selected).then((d) => {
      setDetail(d);
      setBody(s(pick(d, "draft.body", "body")));
    });
  }, [api, selected]);

  const act = async (fn: () => Promise<Json>) => {
    setMessage(null);
    try {
      const out = await fn();
      const status = pick(out, "draft_status", "status");
      const job = pick(out, "job_state");
      setMessage(`✓ draft: ${s(status)}${job ? ` · job: ${s(job)}` : ""}`);
      load();
    } catch (e) {
      setMessage(String(e));
    }
  };

  return (
    <div className="grid grid-cols-[minmax(240px,360px)_minmax(0,1fr)] gap-4">
      <Card>
        <CardTitle extra={<Button size="sm" variant="ghost" onClick={load}>{t("common.refresh")}</Button>}>{t("review.queue")}</CardTitle>
        {items.length === 0 && <Empty>{t("review.empty")}</Empty>}
        <ul className="flex max-h-[70vh] flex-col gap-1 overflow-auto pr-1">
          {items.map((d) => {
            const id = s(pick(d, "id", "draft_id"));
            return (
              <li key={id}>
                <button className={`w-full rounded-md border px-3 py-2 text-left text-sm hover:bg-muted ${selected === id ? "border-primary" : ""}`} onClick={() => setSelected(id)}>
                  <div className="truncate font-medium">{s(pick(d, "subject", "original_subject"))}</div>
                  <div className="text-xs text-muted-foreground">{s(pick(d, "category"))} · {s(pick(d, "status"))}</div>
                </button>
              </li>
            );
          })}
        </ul>
      </Card>
      <Card>
        {!detail ? (
          <Empty>{t("review.queue")}</Empty>
        ) : (
          <div className="flex flex-col gap-3">
            <KeyValue
              rows={[
                ["from", s(pick(detail, "original.sender.email", "original_sender", "sender_email"))],
                ["subject", s(pick(detail, "original.subject", "subject"))],
                ["category", s(pick(detail, "category"))],
                ["dispatch_mode", s(pick(detail, "dispatch_mode"))],
                ["escalation", s(pick(detail, "draft.escalation_reason", "escalation_reason"))],
              ]}
            />
            <pre className="max-h-48 overflow-auto whitespace-pre-wrap rounded-md bg-muted p-3 text-xs">{s(pick(detail, "original.body_text", "original_body"))}</pre>
            <textarea aria-label="draft body" className="min-h-40 rounded-md border bg-background p-2 text-sm" value={body} onChange={(e) => setBody(e.target.value)} />
            <p className="text-xs text-muted-foreground">{t("review.demoNote")}</p>
            <div className="flex gap-2">
              <Button variant="outline" onClick={() => void act(() => api.editDraft(selected!, body))}>{t("review.save")}</Button>
              <Button onClick={() => void act(() => api.approve(selected!))}>{t("review.approve")}</Button>
              <Button variant="danger" onClick={() => void act(() => api.reject(selected!))}>{t("review.reject")}</Button>
            </div>
            {message && <p className="break-all text-sm text-muted-foreground">{message}</p>}
          </div>
        )}
      </Card>
    </div>
  );
}

export function Timeline() {
  const { t } = useTranslation();
  const { api, tenant } = useConsole();
  const runs = useRuns(tenant).filter((r) => r.jobId);
  const [params, setParams] = useSearchParams();
  const jobId = params.get("job") ?? runs[0]?.jobId;
  const [data, setData] = useState<TimelineData | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const load = useCallback(() => {
    if (jobId) api.timeline(jobId).then(setData).catch((e) => setMessage(String(e)));
  }, [api, jobId]);
  useEffect(load, [load]);

  const replay = async () => {
    if (!jobId) return;
    try {
      setMessage(JSON.stringify(await api.replay(jobId)));
      setTimeout(load, 1000);
    } catch (e) {
      setMessage(String(e));
    }
  };

  return (
    <Card>
      <CardTitle
        extra={
          <div className="flex items-center gap-2">
            <select className="min-h-9 rounded-md border bg-background px-2 text-sm" value={jobId ?? ""} onChange={(e) => setParams({ job: e.target.value })}>
              {runs.map((r) => (
                <option key={r.jobId} value={r.jobId}>{r.label}</option>
              ))}
            </select>
            <Button size="sm" variant="ghost" onClick={load}>{t("common.refresh")}</Button>
            <Button size="sm" disabled={data?.current_state !== "DEAD_LETTER"} onClick={() => void replay()}>{t("timeline.replay")}</Button>
          </div>
        }
      >
        {t("timeline.events")} {data && <Badge tone={stateTone(data.current_state)}>{data.current_state}</Badge>}
      </CardTitle>
      {message && <pre className="mb-2 overflow-auto text-xs text-muted-foreground">{message}</pre>}
      {!data ? (
        <Empty>{t("pipeline.noRuns")}</Empty>
      ) : (
        <ol className="flex flex-col gap-2">
          {data.events.map((e, i) => (
            <li key={i} className="rounded-md border p-2 text-sm">
              <div className="flex items-center gap-2">
                <span className="font-mono text-xs text-muted-foreground">{new Date(e.created_at).toLocaleTimeString()}</span>
                <Badge tone={stateTone(e.state_to)}>{e.state_from ?? "∅"} → {e.state_to}</Badge>
                {e.event_type !== "state_transition" && <Badge>{e.event_type}</Badge>}
              </div>
              {Object.keys(e.payload ?? {}).length > 0 && (
                <pre className="mt-1 max-h-40 overflow-auto text-xs text-muted-foreground">{JSON.stringify(e.payload, null, 2)}</pre>
              )}
            </li>
          ))}
        </ol>
      )}
    </Card>
  );
}

export function Knowledge() {
  const { t } = useTranslation();
  const { api } = useConsole();
  const [docs, setDocs] = useState<Json[]>([]);
  const [form, setForm] = useState({ title: "", category: "support", content: "" });
  const [message, setMessage] = useState<string | null>(null);
  const load = useCallback(() => {
    api.documents().then((r) => setDocs(r.items ?? [])).catch((e) => setMessage(String(e)));
  }, [api]);
  useEffect(() => {
    load();
    const timer = setInterval(load, 3000); // ingestion status moves pending → active
    return () => clearInterval(timer);
  }, [load]);

  const upload = async () => {
    setMessage(null);
    try {
      await api.upload(new Blob([form.content], { type: "text/markdown" }), "document.md", form.title, form.category);
      setForm({ ...form, title: "", content: "" });
      load();
    } catch (e) {
      setMessage(String(e));
    }
  };
  const field = "min-h-9 rounded-md border bg-background px-2 text-sm";
  return (
    <div className="grid grid-cols-[400px_1fr] gap-4">
      <Card>
        <CardTitle>{t("knowledge.upload")}</CardTitle>
        <div className="flex flex-col gap-2">
          <input className={field} aria-label={t("knowledge.title")} placeholder={t("knowledge.title")} value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} />
          <input className={field} aria-label={t("knowledge.category")} placeholder={t("knowledge.category")} value={form.category} onChange={(e) => setForm({ ...form, category: e.target.value })} />
          <textarea className={`${field} min-h-40 py-2`} aria-label={t("knowledge.content")} placeholder={t("knowledge.content")} value={form.content} onChange={(e) => setForm({ ...form, content: e.target.value })} />
          <Button disabled={!form.title || !form.content} onClick={() => void upload()}>{t("knowledge.upload")}</Button>
          {message && <p className="text-sm text-danger">{message}</p>}
        </div>
      </Card>
      <Card>
        <CardTitle>{t("knowledge.documents")}</CardTitle>
        <table className="w-full text-sm">
          <tbody>
            {docs.map((d) => (
              <tr key={s(pick(d, "id"))} className="border-t">
                <td className="py-1">{s(pick(d, "title"))}</td>
                <td>{s(pick(d, "category"))}</td>
                <td><Badge tone={pick(d, "status") === "active" ? "success" : pick(d, "status") === "failed" ? "danger" : "info"}>{s(pick(d, "status"))}</Badge></td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
    </div>
  );
}
