// Observability (task 9.14, R25.9) and Benchmark Explorer (R25.10, read-only).
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Bar, BarChart, CartesianGrid, Cell, Legend, Pie, PieChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { MetricsPanel, MetricsSummary } from "@/api/client";
import { Badge, Button, Card, CardTitle, Empty } from "@/components/ui";
import { useConsole } from "@/state";

const COLORS = ["hsl(var(--primary))", "hsl(var(--success))", "hsl(var(--warning))", "hsl(var(--danger))", "hsl(var(--muted-foreground))"];
const series = (p: MetricsPanel) => Object.entries(p.values).map(([name, value]) => ({ name, value: value ?? 0 }));

function Scope({ panel }: { panel: MetricsPanel }) {
  const { t } = useTranslation();
  return <Badge tone={panel.scope === "organization" ? "info" : "neutral"}>{t(panel.scope === "organization" ? "obs.scopeOrg" : "obs.scopeStack")}</Badge>;
}

export function Observability() {
  const { t } = useTranslation();
  const { api } = useConsole();
  const [window, setWindow] = useState("15m");
  const [data, setData] = useState<MetricsSummary | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    const load = () =>
      api
        .metrics(window)
        .then((d) => alive && (setData(d), setError(null)))
        .catch(() => alive && setError(t("obs.unavailable")));
    load();
    const timer = setInterval(load, 5000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [api, window, t]);

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center gap-2 text-sm">
        {t("obs.window")}
        {["5m", "15m", "1h", "6h", "1d"].map((w) => (
          <Button key={w} size="sm" variant={w === window ? "default" : "outline"} onClick={() => setWindow(w)}>{w}</Button>
        ))}
        {error && <span className="text-danger">{error}</span>}
      </div>
      {!data ? (
        <Empty>{t("common.loading")}</Empty>
      ) : (
        <div className="grid grid-cols-2 gap-4">
          <Card>
            <CardTitle extra={<Scope panel={data.funnel} />}>{t("obs.funnel")}</CardTitle>
            <div className="h-64">
              <ResponsiveContainer>
                <PieChart>
                  <Pie data={series(data.funnel)} dataKey="value" nameKey="name" outerRadius={90} label>
                    {series(data.funnel).map((_, i) => <Cell key={i} fill={COLORS[i % COLORS.length]} />)}
                  </Pie>
                  <Tooltip />
                  <Legend />
                </PieChart>
              </ResponsiveContainer>
            </div>
            <p className="text-sm text-muted-foreground">received: {data.received.values.total ?? 0}</p>
          </Card>
          <Card>
            <CardTitle extra={<Scope panel={data.latency_ms} />}>{t("obs.latency")}</CardTitle>
            <div className="h-64">
              <ResponsiveContainer>
                <BarChart data={series(data.latency_ms)}>
                  <CartesianGrid strokeDasharray="3 3" />
                  <XAxis dataKey="name" />
                  <YAxis />
                  <Tooltip />
                  <Bar dataKey="value" fill={COLORS[0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          </Card>
          <Card>
            <CardTitle extra={<Scope panel={data.queue_depth} />}>{t("obs.queues")}</CardTitle>
            <div className="h-64">
              <ResponsiveContainer>
                <BarChart data={series(data.queue_depth)} layout="vertical" margin={{ left: 120 }}>
                  <XAxis type="number" allowDecimals={false} />
                  <YAxis type="category" dataKey="name" width={160} tick={{ fontSize: 11 }} />
                  <Tooltip />
                  <Bar dataKey="value" fill={COLORS[2]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          </Card>
          <Card>
            <CardTitle extra={<Scope panel={data.cost_usd} />}>{t("obs.cost")}</CardTitle>
            <div className="h-64">
              <ResponsiveContainer>
                <BarChart data={series(data.cost_usd)}>
                  <CartesianGrid strokeDasharray="3 3" />
                  <XAxis dataKey="name" />
                  <YAxis />
                  <Tooltip formatter={(v: number) => `$${v.toFixed(4)}`} />
                  <Bar dataKey="value" fill={COLORS[1]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          </Card>
        </div>
      )}
    </div>
  );
}

interface BenchIndex {
  runs: { id: string; model: string; summary: string; metrics: string }[];
}

export function parseCsv(text: string): Record<string, string>[] {
  const [head, ...lines] = text.trim().split(/\r?\n/);
  const cols = head.split(",");
  return lines.map((line) => Object.fromEntries(line.split(",").map((v, i) => [cols[i], v])));
}

export function BenchmarkExplorer() {
  const { t } = useTranslation();
  const [index, setIndex] = useState<BenchIndex | null | "missing">(null);
  const [runId, setRunId] = useState<string | null>(null);
  const [rows, setRows] = useState<Record<string, string>[]>([]);
  const [summary, setSummary] = useState<unknown>(null);

  useEffect(() => {
    fetch("/bench/index.json")
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then((i: BenchIndex) => {
        setIndex(i);
        setRunId(i.runs[0]?.id ?? null);
      })
      .catch(() => setIndex("missing"));
  }, []);

  useEffect(() => {
    if (!index || index === "missing" || !runId) return;
    const run = index.runs.find((r) => r.id === runId);
    if (!run) return;
    fetch(run.metrics).then((r) => r.text()).then((x) => setRows(parseCsv(x)));
    fetch(run.summary).then((r) => r.json()).then(setSummary);
  }, [index, runId]);

  if (index === null) return <Empty>{t("common.loading")}</Empty>;
  if (index === "missing") return <Card><Empty>{t("bench.missing")}</Empty></Card>;
  const cols = rows[0] ? Object.keys(rows[0]) : [];
  const numeric = cols.filter((c) => rows.every((r) => r[c] !== "" && !Number.isNaN(Number(r[c]))));
  const label = cols.find((c) => !numeric.includes(c)) ?? cols[0];
  const metric = numeric[0];

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardTitle
          extra={
            <select className="min-h-9 rounded-md border bg-background px-2 text-sm" value={runId ?? ""} onChange={(e) => setRunId(e.target.value)}>
              {index.runs.map((r) => <option key={r.id} value={r.id}>{r.model}</option>)}
            </select>
          }
        >
          {t("bench.title")}
        </CardTitle>
        <p className="mb-3 text-sm text-muted-foreground">{t("bench.note")}</p>
        {metric && (
          <div className="h-64">
            <ResponsiveContainer>
              <BarChart data={rows.map((r) => ({ name: r[label], value: Number(r[metric]) }))}>
                <CartesianGrid strokeDasharray="3 3" />
                <XAxis dataKey="name" tick={{ fontSize: 11 }} />
                <YAxis />
                <Tooltip />
                <Bar dataKey="value" name={metric} fill={COLORS[0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        )}
        <div className="mt-4 overflow-auto">
          <table className="w-full text-xs">
            <thead>
              <tr>{cols.map((c) => <th key={c} className="px-2 py-1 text-left font-medium text-muted-foreground">{c}</th>)}</tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={i} className="border-t">{cols.map((c) => <td key={c} className="px-2 py-1 font-mono">{r[c]}</td>)}</tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
      <Card>
        <CardTitle>summary.json</CardTitle>
        <pre className="max-h-96 overflow-auto text-xs">{JSON.stringify(summary, null, 2)}</pre>
      </Card>
    </div>
  );
}
