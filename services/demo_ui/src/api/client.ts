// Typed client of the /v1 API (task 9.9). The types mirror the API's response models; run
// `npm run gen:api` against a running API to regenerate src/api/schema.d.ts from its OpenAPI
// document and compare. Every call sends X-Organization-ID (R23.6); nothing else is sent.

export type ModelProfile = "fake" | "live";
export type GuardMode = "C0" | "C3";

export interface RunOptions {
  model_profile: ModelProfile;
  guard_mode: GuardMode;
}

export interface Capabilities {
  live_model_available: boolean;
  live_model: string | null;
  guard_available: boolean;
  l1_classifier_loaded: boolean;
  demo_mailbox_id: string | null;
}

export interface Localized {
  vi: string;
  en: string;
}

export interface ScenarioEmail {
  sender_email: string;
  sender_name?: string | null;
  subject: string;
  body: string;
}

export interface Scenario {
  id: string;
  group: "pipeline" | "guard";
  kind: "email" | "comparison" | "flow";
  flow: string | null;
  title: Localized;
  description: Localized;
  run_options: { model_profile: ModelProfile; guard_mode: GuardMode | null };
  email: ScenarioEmail | null;
  thread: ScenarioEmail[];
  knowledge: { title: string; category: string; content: string }[];
  burst: number | null;
  query: string | null;
  source: { case_id: string; origin: string; license: string } | null;
  requires_live: boolean;
  expected: Record<string, unknown>;
}

export interface DemoAccepted {
  demo_request_id: string;
  messages: number;
  run_options: RunOptions;
}

export interface DemoRequestStatus {
  demo_request_id: string;
  status: "pending" | "ingested";
  job_id: string | null;
  message_id: string | null;
  job_state: string | null;
  run_options: RunOptions | null;
}

export interface JobEvent {
  id: number | null;
  event_type: string;
  state_from: string | null;
  state_to: string;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface Timeline {
  job_id: string;
  current_state: string;
  total_events: number;
  events: JobEvent[];
}

export interface JobDetail {
  id: string;
  message_id: string | null;
  state: string;
  attempt: number;
  last_error: string | null;
  run_options: RunOptions | null;
  created_at: string;
  updated_at: string;
}

export interface GuardReport {
  job_id: string;
  guard_mode: string;
  model_profile: string | null;
  inbound_action: string | null;
  final_action: string | null;
  guard_llm_calls: number;
  timings_ms: Record<string, number>;
  report: Record<string, unknown>;
}

export interface MetricsPanel {
  scope: "organization" | "stack";
  values: Record<string, number | null>;
}

export interface MetricsSummary {
  window: string;
  funnel: MetricsPanel;
  received: MetricsPanel;
  latency_ms: MetricsPanel;
  queue_depth: MetricsPanel;
  cost_usd: MetricsPanel;
}

export type Json = Record<string, unknown>;

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
  ) {
    super(message);
  }
}

export class ApiClient {
  constructor(
    private organizationId: string,
    private fetcher: typeof fetch = (...args) => fetch(...args),
  ) {}

  private async call<T>(path: string, init: RequestInit = {}): Promise<T> {
    const headers = new Headers(init.headers);
    headers.set("X-Organization-ID", this.organizationId);
    if (init.body && !(init.body instanceof FormData)) headers.set("Content-Type", "application/json");
    const response = await this.fetcher(`/v1${path}`, { ...init, headers });
    if (!response.ok) {
      let code = `HTTP_${response.status}`;
      let message = response.statusText;
      try {
        const body = (await response.json()) as { code?: string; error?: string };
        code = body.code ?? code;
        message = body.error ?? message;
      } catch {
        /* not JSON */
      }
      throw new ApiError(response.status, code, message);
    }
    return (await response.json()) as T;
  }

  capabilities = () => this.call<Capabilities>("/demo/capabilities");
  scenarios = () => this.call<Scenario[]>("/demo/scenarios");
  runScenario = (id: string, model_profile?: ModelProfile) =>
    this.call<{ scenario_id: string; requests: DemoAccepted[] }>(`/demo/scenarios/${id}/run`, {
      method: "POST",
      body: JSON.stringify(model_profile ? { model_profile } : {}),
    });
  sendEmail = (email: ScenarioEmail & { thread?: ScenarioEmail[]; run_options: RunOptions }) =>
    this.call<DemoAccepted>("/demo/messages", { method: "POST", body: JSON.stringify(email) });
  request = (id: string) => this.call<DemoRequestStatus>(`/demo/requests/${id}`);
  job = (id: string) => this.call<JobDetail>(`/jobs/${id}`);
  timeline = (id: string) => this.call<Timeline>(`/jobs/${id}/timeline`);
  guard = (id: string) => this.call<GuardReport>(`/jobs/${id}/guard`);
  replay = (id: string) =>
    this.call<Json>(`/jobs/${id}/replay`, {
      method: "POST",
      body: JSON.stringify({ reset_attempts: true, reason: "demo console replay" }),
    });
  message = (id: string) => this.call<Json>(`/messages/${id}`);
  drafts = (status?: string) =>
    this.call<{ items: Json[]; next_cursor: string | null }>(
      `/drafts?limit=50${status ? `&status=${status}` : ""}`,
    );
  draft = (id: string) => this.call<Json>(`/drafts/${id}`);
  editDraft = (id: string, body: string) =>
    this.call<Json>(`/drafts/${id}`, { method: "PATCH", body: JSON.stringify({ body }) });
  approve = (id: string) =>
    this.call<Json>(`/drafts/${id}/approve`, {
      method: "POST",
      body: JSON.stringify({ reviewer: "demo-console" }),
    });
  reject = (id: string) =>
    this.call<Json>(`/drafts/${id}/reject`, {
      method: "POST",
      body: JSON.stringify({ reviewer: "demo-console" }),
    });
  documents = () => this.call<{ items: Json[] }>("/knowledge/documents?limit=50");
  document = (id: string) => this.call<Json>(`/knowledge/documents/${id}`);
  upload = (file: Blob, filename: string, title: string, category: string) => {
    const form = new FormData();
    form.append("file", file, filename);
    form.append("title", title);
    form.append("category", category);
    return this.call<Json>("/knowledge/documents", { method: "POST", body: form });
  };
  searchDebug = (query: string) =>
    this.call<Json>("/search/debug", { method: "POST", body: JSON.stringify({ query }) });
  metrics = (window = "15m") => this.call<MetricsSummary>(`/metrics/summary?window=${window}`);
}
