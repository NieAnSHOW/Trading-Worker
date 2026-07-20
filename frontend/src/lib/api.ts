import { authHeaders, withAuthTicket } from "@/lib/apiAuth";

const BASE = "";

export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export const AUTH_REQUIRED_MESSAGE =
  "Remote API access requires an API key. Add it in Settings, or run the backend on localhost for local-only use.";

export function isAuthRequiredError(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 401 || error.status === 403);
}

async function errorFromResponse(res: Response): Promise<ApiError> {
  let detail = `HTTP ${res.status}`;
  try {
    const body = await res.json();
    detail = body.detail || body.message || detail;
  } catch { /* ignore */ }
  if (res.status === 401 || res.status === 403) {
    detail = AUTH_REQUIRED_MESSAGE;
  }
  return new ApiError(detail, res.status);
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const { headers, ...rest } = options ?? {};
  const mergedHeaders: Record<string, string> = { "Content-Type": "application/json", ...authHeaders() };
  if (headers) {
    new Headers(headers).forEach((value, key) => {
      mergedHeaders[key] = value;
    });
  }
  const res = await fetch(`${BASE}${path}`, {
    ...rest,
    headers: mergedHeaders,
  });
  if (!res.ok) {
    throw await errorFromResponse(res);
  }
  const text = await res.text();
  if (!text) return {} as T;

  const contentType = res.headers.get("content-type") || "";
  if (!contentType.includes("application/json")) {
    const preview = text.slice(0, 80).replace(/\s+/g, " ");
    throw new ApiError(
      `Expected JSON from ${path}, got ${contentType || "unknown content type"}: ${preview}`,
      res.status,
    );
  }

  try {
    return JSON.parse(text) as T;
  } catch {
    throw new ApiError(`Invalid JSON response from ${path}`, res.status);
  }
}

const NEWS_TRACK_IDS = ["ai", "semi", "robot", "auto", "energy", "bio", "space", "security", "tech", "consumer", "macro", "science"] as const;
const NEWS_TRACK_STATES = ["fresh", "stale", "unavailable"] as const;
const NEWS_REFRESH_PHASES = ["idle", "fetching", "normalizing", "enriching", "committing", "succeeded", "failed", "cancelled"] as const;
const NEWS_UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const NEWS_SENSITIVE_PARAMETER_NAMES = new Set([
  "access_token", "api_key", "apikey", "authorization", "credential", "credentials", "key", "password", "secret", "sig", "signature", "token",
]);
const NEWS_SENSITIVE_URL_VALUE_PATTERN = /(?:\bbearer\s+\S+|\bsk-[a-z0-9_-]{4,}\b|\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\b|\b(?:gh[pousr]_[a-z0-9]{20,}|github_pat_[a-z0-9_]{20,})\b|\bAKIA[0-9A-Z]{16}\b|\bxox[baprs]-[a-z0-9-]{10,}\b|\b(?=[A-Za-z0-9/+=]{40}\b)(?=[A-Za-z0-9/+=]{0,39}[A-Z])(?=[A-Za-z0-9/+=]{0,39}[a-z])(?=[A-Za-z0-9/+=]{0,39}\d)[A-Za-z0-9/+=]{40}\b|\bsecret_[a-z0-9_-]{16,}\b|\b(?:secret|private)[-_ ]?(?:token|key|value)\b)/i;
const NEWS_PUBLIC_ERRORS: Record<string, string> = {
  ai_unavailable: "AI highlights are unavailable",
  cancelled: "news refresh was cancelled",
  no_track_updated: "no news tracks were updated",
  snapshot_corrupt: "news snapshot is corrupt",
  snapshot_unavailable: "news snapshot is unavailable",
  snapshot_write_failed: "news snapshot could not be saved",
  upstream_failed: "news refresh failed",
};

export type NewsTrackId = (typeof NEWS_TRACK_IDS)[number];
export type NewsTrackState = (typeof NEWS_TRACK_STATES)[number];
export type NewsRefreshPhase = (typeof NEWS_REFRESH_PHASES)[number];

export interface NewsPublicError {
  code: keyof typeof NEWS_PUBLIC_ERRORS;
  message: string;
}

export interface NewsSourceStats {
  endpoint_success_count: number;
  endpoint_failure_count: number;
  assignment_success_count: number;
  assignment_failure_count: number;
}

export interface NewsArticleSource {
  id: string;
  name: string;
  url: string;
}

export interface NewsArticle {
  id: string;
  track_id: NewsTrackId;
  title: string;
  title_zh: string | null;
  summary: string | null;
  source: NewsArticleSource;
  published_at: string | null;
  url: string;
}

export interface NewsTrackAi {
  available: boolean;
  generated_at: string | null;
  highlights: string[];
  error: NewsPublicError | null;
}

export interface NewsTrack {
  track_id: NewsTrackId;
  state: NewsTrackState;
  generated_at: string | null;
  stale: boolean;
  partial: boolean;
  items: NewsArticle[];
  ai: NewsTrackAi;
  source_stats: NewsSourceStats;
}

export interface NewsSnapshot {
  schema_version: 1;
  generated_at: string;
  upstream_commit: string;
  source_stats: NewsSourceStats;
  errors: NewsPublicError[];
  tracks: NewsTrack[];
}

export interface NewsRefreshStatus {
  state: NewsRefreshPhase;
  task_id: string | null;
  started_at: string | null;
  completed_at: string | null;
  processed_endpoints: number;
  successful_endpoints: number;
  failed_endpoints: number;
  processed_tracks: number;
  total_endpoints: number;
  total_tracks: number;
  error: NewsPublicError | null;
}

export interface NewsSnapshotResponse {
  available: boolean;
  stale: boolean;
  snapshot: NewsSnapshot | null;
  refresh: NewsRefreshStatus;
  error: NewsPublicError | null;
}

export interface NewsRefreshAccepted {
  task_id: string;
  reused: boolean;
  status: NewsRefreshStatus;
}

function invalidNewsResponse(): never {
  throw new ApiError("Invalid news API response", 200);
}

function newsRecord(value: unknown, keys: readonly string[]): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) invalidNewsResponse();
  const record = value as Record<string, unknown>;
  const actualKeys = Object.keys(record);
  if (actualKeys.length !== keys.length || actualKeys.some((key) => !keys.includes(key))) invalidNewsResponse();
  return record;
}

function newsString(value: unknown, minLength: number, maxLength: number): string {
  if (typeof value !== "string") invalidNewsResponse();
  const length = Array.from(value).length;
  if (length < minLength || length > maxLength) invalidNewsResponse();
  return value;
}

function newsBoolean(value: unknown): boolean {
  if (typeof value !== "boolean") invalidNewsResponse();
  return value;
}

function newsInteger(value: unknown, maximum?: number): number {
  if (!Number.isSafeInteger(value) || (value as number) < 0 || (maximum !== undefined && (value as number) > maximum)) invalidNewsResponse();
  return value as number;
}

function newsArray(value: unknown, minLength: number, maxLength: number): unknown[] {
  if (!Array.isArray(value) || value.length < minLength || value.length > maxLength) invalidNewsResponse();
  return value;
}

function newsUtcDate(value: unknown, nullable = false): string | null {
  if (nullable && value === null) return null;
  const date = newsString(value, 1, 64);
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(?:Z|[+]00:00)$/.exec(date);
  if (!match) invalidNewsResponse();
  if (Number(match[1]) < 1) invalidNewsResponse();
  const parsed = new Date(date);
  if (
    Number.isNaN(parsed.getTime()) ||
    parsed.getUTCFullYear() !== Number(match[1]) ||
    parsed.getUTCMonth() + 1 !== Number(match[2]) ||
    parsed.getUTCDate() !== Number(match[3]) ||
    parsed.getUTCHours() !== Number(match[4]) ||
    parsed.getUTCMinutes() !== Number(match[5]) ||
    parsed.getUTCSeconds() !== Number(match[6])
  ) invalidNewsResponse();
  return date;
}

function newsUrlComponent(value: string): string {
  return value.replace(/\+/g, " ").replace(/(?:%[0-9a-f]{2})+/gi, (encoded) => {
    const bytes = Uint8Array.from(encoded.match(/%[0-9a-f]{2}/gi)!.map((part) => Number.parseInt(part.slice(1), 16)));
    return new TextDecoder().decode(bytes);
  });
}

function newsSensitiveParameter(name: string): boolean {
  const normalized = name.toLowerCase().replace(/[^a-z0-9]/g, "_").replace(/^_+|_+$/g, "");
  return NEWS_SENSITIVE_PARAMETER_NAMES.has(normalized) || normalized.endsWith("_signature") ||
    normalized.split("_").some((part) => ["authorization", "credential", "key", "password", "secret", "token"].includes(part));
}

function newsPublicUrlComponent(value: string): void {
  for (const segment of newsUrlComponent(value).split(/[&;]/)) {
    const separator = segment.indexOf("=");
    const name = separator === -1 ? segment : segment.slice(0, separator);
    const parameterValue = separator === -1 ? "" : segment.slice(separator + 1);
    if ((name && newsSensitiveParameter(name)) || (separator !== -1 && NEWS_SENSITIVE_URL_VALUE_PATTERN.test(parameterValue))) {
      invalidNewsResponse();
    }
  }
}

function newsUrlWithPermissivePort(url: string, rawAuthority: string): URL {
  try {
    return new URL(url);
  } catch {
    let hostname: string;
    if (rawAuthority.startsWith("[")) {
      const closeBracket = rawAuthority.indexOf("]");
      if (closeBracket === -1 || closeBracket === rawAuthority.length - 1) invalidNewsResponse();
      hostname = rawAuthority.slice(0, closeBracket + 1);
    } else {
      const portStart = rawAuthority.indexOf(":");
      if (portStart === -1) invalidNewsResponse();
      hostname = rawAuthority.slice(0, portStart);
    }
    if (!hostname) invalidNewsResponse();
    const authorityStart = url.indexOf("//") + 2;
    try {
      return new URL(`${url.slice(0, authorityStart)}${hostname}${url.slice(authorityStart + rawAuthority.length)}`);
    } catch {
      invalidNewsResponse();
    }
  }
}

function newsHttpUrl(value: unknown): string {
  const url = newsString(value, 1, 2048);
  try {
    const rawAuthority = /^https?:\/\/([^/?#]+)/i.exec(url)?.[1];
    if (!rawAuthority || rawAuthority.includes("@")) invalidNewsResponse();
    const parsed = newsUrlWithPermissivePort(url, rawAuthority);
    if ((parsed.protocol !== "http:" && parsed.protocol !== "https:") || !parsed.hostname || parsed.username || parsed.password) {
      invalidNewsResponse();
    }
    newsPublicUrlComponent(parsed.search.slice(1));
    newsPublicUrlComponent(parsed.hash.slice(1));
  } catch {
    invalidNewsResponse();
  }
  return url;
}

function parseNewsPublicError(value: unknown, nullable = false): NewsPublicError | null {
  if (nullable && value === null) return null;
  const record = newsRecord(value, ["code", "message"]);
  const code = newsString(record.code, 1, 64);
  if (!/^[a-z][a-z0-9_]{0,63}$/.test(code) || NEWS_PUBLIC_ERRORS[code] !== record.message) invalidNewsResponse();
  return { code: code as NewsPublicError["code"], message: newsString(record.message, 1, 200) };
}

function parseNewsSourceStats(value: unknown): NewsSourceStats {
  const record = newsRecord(value, ["endpoint_success_count", "endpoint_failure_count", "assignment_success_count", "assignment_failure_count"]);
  const endpoint_success_count = newsInteger(record.endpoint_success_count, 106);
  const endpoint_failure_count = newsInteger(record.endpoint_failure_count, 106);
  const assignment_success_count = newsInteger(record.assignment_success_count, 108);
  const assignment_failure_count = newsInteger(record.assignment_failure_count, 108);
  if (endpoint_success_count + endpoint_failure_count > 106 || assignment_success_count + assignment_failure_count > 108) invalidNewsResponse();
  return { endpoint_success_count, endpoint_failure_count, assignment_success_count, assignment_failure_count };
}

function parseNewsArticle(value: unknown, trackId: NewsTrackId): NewsArticle {
  const record = newsRecord(value, ["id", "track_id", "title", "title_zh", "summary", "source", "published_at", "url"]);
  if (record.track_id !== trackId) invalidNewsResponse();
  const source = newsRecord(record.source, ["id", "name", "url"]);
  const title_zh = record.title_zh === null ? null : newsString(record.title_zh, 1, 300);
  const summary = record.summary === null ? null : newsString(record.summary, 0, 1000);
  return {
    id: newsString(record.id, 1, 128),
    track_id: trackId,
    title: newsString(record.title, 1, 300),
    title_zh,
    summary,
    source: { id: newsString(source.id, 1, 128), name: newsString(source.name, 1, 200), url: newsHttpUrl(source.url) },
    published_at: newsUtcDate(record.published_at, true),
    url: newsHttpUrl(record.url),
  };
}

function parseNewsTrackAi(value: unknown): NewsTrackAi {
  const record = newsRecord(value, ["available", "generated_at", "highlights", "error"]);
  const available = newsBoolean(record.available);
  const generated_at = newsUtcDate(record.generated_at, true);
  const highlights = newsArray(record.highlights, 0, 5).map((highlight) => newsString(highlight, 1, 300));
  const error = parseNewsPublicError(record.error, true);
  if ((available && (generated_at === null || highlights.length < 3 || error !== null)) || (!available && (generated_at !== null || highlights.length > 0))) {
    invalidNewsResponse();
  }
  return { available, generated_at, highlights, error };
}

function parseNewsTrack(value: unknown, expectedTrackId: NewsTrackId): NewsTrack {
  const record = newsRecord(value, ["track_id", "state", "generated_at", "stale", "partial", "items", "ai", "source_stats"]);
  if (record.track_id !== expectedTrackId || !NEWS_TRACK_STATES.includes(record.state as NewsTrackState)) invalidNewsResponse();
  const state = record.state as NewsTrackState;
  const generated_at = newsUtcDate(record.generated_at, true);
  const stale = newsBoolean(record.stale);
  const partial = newsBoolean(record.partial);
  const source_stats = parseNewsSourceStats(record.source_stats);
  const items = newsArray(record.items, 0, 100).map((item) => parseNewsArticle(item, expectedTrackId));
  const ai = parseNewsTrackAi(record.ai);
  const hasSourceFailure = source_stats.endpoint_failure_count > 0 || source_stats.assignment_failure_count > 0;
  if (
    (state === "fresh" && (generated_at === null || stale || partial !== hasSourceFailure)) ||
    (state === "stale" && (generated_at === null || !stale || partial)) ||
    (state === "unavailable" && (generated_at !== null || stale || partial || items.length > 0 || ai.available))
  ) invalidNewsResponse();
  return { track_id: expectedTrackId, state, generated_at, stale, partial, items, ai, source_stats };
}

function parseNewsRefreshStatus(value: unknown): NewsRefreshStatus {
  const record = newsRecord(value, ["state", "task_id", "started_at", "completed_at", "processed_endpoints", "successful_endpoints", "failed_endpoints", "processed_tracks", "total_endpoints", "total_tracks", "error"]);
  if (!NEWS_REFRESH_PHASES.includes(record.state as NewsRefreshPhase)) invalidNewsResponse();
  const task_id = record.task_id === null ? null : newsString(record.task_id, 36, 36);
  if (task_id !== null && !NEWS_UUID_PATTERN.test(task_id)) invalidNewsResponse();
  return {
    state: record.state as NewsRefreshPhase,
    task_id,
    started_at: newsUtcDate(record.started_at, true),
    completed_at: newsUtcDate(record.completed_at, true),
    processed_endpoints: newsInteger(record.processed_endpoints),
    successful_endpoints: newsInteger(record.successful_endpoints),
    failed_endpoints: newsInteger(record.failed_endpoints),
    processed_tracks: newsInteger(record.processed_tracks),
    total_endpoints: newsInteger(record.total_endpoints),
    total_tracks: newsInteger(record.total_tracks),
    error: parseNewsPublicError(record.error, true),
  };
}

function parseNewsSnapshot(value: unknown): NewsSnapshot {
  const record = newsRecord(value, ["schema_version", "generated_at", "upstream_commit", "source_stats", "errors", "tracks"]);
  if (record.schema_version !== 1) invalidNewsResponse();
  const tracks = newsArray(record.tracks, NEWS_TRACK_IDS.length, NEWS_TRACK_IDS.length)
    .map((track, index) => parseNewsTrack(track, NEWS_TRACK_IDS[index]));
  return {
    schema_version: 1,
    generated_at: newsUtcDate(record.generated_at) as string,
    upstream_commit: newsString(record.upstream_commit, 1, 128),
    source_stats: parseNewsSourceStats(record.source_stats),
    errors: newsArray(record.errors, 0, 200).map((error) => parseNewsPublicError(error) as NewsPublicError),
    tracks,
  };
}

export function parseNewsSnapshotResponse(value: unknown): NewsSnapshotResponse {
  const record = newsRecord(value, ["available", "stale", "snapshot", "refresh", "error"]);
  const available = newsBoolean(record.available);
  const stale = newsBoolean(record.stale);
  const snapshot = record.snapshot === null ? null : parseNewsSnapshot(record.snapshot);
  if (available !== (snapshot !== null) || (!available && stale)) invalidNewsResponse();
  return { available, stale, snapshot, refresh: parseNewsRefreshStatus(record.refresh), error: parseNewsPublicError(record.error, true) };
}

function parseNewsRefreshAccepted(value: unknown): NewsRefreshAccepted {
  const record = newsRecord(value, ["task_id", "reused", "status"]);
  const task_id = newsString(record.task_id, 36, 36);
  if (!NEWS_UUID_PATTERN.test(task_id)) invalidNewsResponse();
  return { task_id, reused: newsBoolean(record.reused), status: parseNewsRefreshStatus(record.status) };
}

export interface UploadResult {
  status: string;
  file_path: string;
  filename: string;
}

async function uploadFile(file: File): Promise<UploadResult> {
  const form = new FormData();
  form.append("file", file);
  const res = await fetch(`${BASE}/upload`, { method: "POST", headers: authHeaders(), body: form });
  if (!res.ok) {
    throw await errorFromResponse(res);
  }
  return res.json();
}

function appendQueryParam(url: string, key: string, value: string): string {
  const sep = url.includes("?") ? "&" : "?";
  return `${url}${sep}${encodeURIComponent(key)}=${encodeURIComponent(value)}`;
}

export const api = {
  uploadFile,
  listRuns: (limit?: number) => request<RunListItem[]>(`/runs${limit ? `?limit=${encodeURIComponent(String(limit))}` : ""}`),
  getRun: (id: string, params: RunDetailParams = {}) => {
    const q = new URLSearchParams();
    if (params.chart_payload) q.set("chart_payload", params.chart_payload);
    if (params.chart_symbol) q.set("chart_symbol", params.chart_symbol);
    const qs = q.toString();
    return request<RunData>(`/runs/${id}${qs ? `?${qs}` : ""}`);
  },
  getRunCode: (id: string) => request<Record<string, string>>(`/runs/${id}/code`),
  getRunPine: (id: string) => request<PineScriptResult>(`/runs/${id}/pine`),
  listSessions: () => request<SessionItem[]>("/sessions"),
  createSession: (title?: string) => request<SessionItem>("/sessions", { method: "POST", body: JSON.stringify({ title: title || "" }) }),
  deleteSession: (sid: string) => request<{ status: string }>(`/sessions/${sid}`, { method: "DELETE" }),
  renameSession: (sid: string, title: string) => request<{ status: string }>(`/sessions/${sid}`, { method: "PATCH", body: JSON.stringify({ title }) }),
  sendMessage: (sid: string, content: string) => request<{ message_id: string; attempt_id: string }>(`/sessions/${sid}/messages`, { method: "POST", body: JSON.stringify({ content }) }),
  cancelSession: (sid: string) => request<{ status: string }>(`/sessions/${sid}/cancel`, { method: "POST" }),
  getSessionMessages: (sid: string) => request<MessageItem[]>(`/sessions/${sid}/messages`),
  createGoal: (sid: string, body: CreateGoalRequest) =>
    request<GoalSnapshot>(`/sessions/${sid}/goal`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  getGoal: (sid: string) => request<GoalSnapshot>(`/sessions/${sid}/goal`),
  updateGoal: (sid: string, body: UpdateGoalRequest) =>
    request<UpdateGoalResponse>(`/sessions/${sid}/goal`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  addGoalEvidence: (sid: string, body: AddGoalEvidenceRequest) =>
    request<AddGoalEvidenceResponse>(`/sessions/${sid}/goal/evidence`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateGoalStatus: (sid: string, body: UpdateGoalStatusRequest) =>
    request<UpdateGoalStatusResponse>(`/sessions/${sid}/goal/status`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  sseUrl: (sid: string, options?: { replay?: "active" }) => {
    let url = `${BASE}/sessions/${sid}/events`;
    if (options?.replay) url = appendQueryParam(url, "replay", options.replay);
    return url;
  },

  // Swarm API
  listSwarmPresets: () => request<SwarmPreset[]>("/swarm/presets"),
  createSwarmRun: (preset_name: string, user_vars: Record<string, string>) =>
    request<{ id: string; status: string }>("/swarm/runs", {
      method: "POST",
      body: JSON.stringify({ preset_name, user_vars }),
    }),
  listSwarmRuns: () => request<SwarmRunSummary[]>("/swarm/runs"),
  getSwarmRun: (id: string) => request<Record<string, unknown>>(`/swarm/runs/${id}`),
  swarmSseUrl: (id: string) => withAuthTicket(`${BASE}/swarm/runs/${id}/events`),
  cancelSwarmRun: (id: string) =>
    request<{ status: string }>(`/swarm/runs/${id}/cancel`, { method: "POST" }),
  retrySwarmRun: (id: string) =>
    request<{ id: string; status: string; preset_name: string }>(`/swarm/runs/${id}/retry`, { method: "POST" }),
  getLLMSettings: () => request<LLMSettings>("/settings/llm"),
  updateLLMSettings: (settings: UpdateLLMSettingsRequest) =>
    request<LLMSettings>("/settings/llm", {
      method: "PUT",
      body: JSON.stringify(settings),
    }),
  getVipModels: (body: VIPModelListRequest = {}) =>
    request<VIPModelListResponse>("/settings/llm/vip-models", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  getDataSourceSettings: () => request<DataSourceSettings>("/settings/data-sources"),
  updateDataSourceSettings: (settings: UpdateDataSourceSettingsRequest) =>
    request<DataSourceSettings>("/settings/data-sources", {
      method: "PUT",
      body: JSON.stringify(settings),
    }),
  getChannelStatus: () => request<ChannelRuntimeStatus>("/channels/status"),
  startChannels: () => request<ChannelRuntimeActionResponse>("/channels/start", { method: "POST" }),
  stopChannels: () => request<ChannelRuntimeActionResponse>("/channels/stop", { method: "POST" }),
  runChannelPairingCommand: (body: ChannelPairingCommandRequest) =>
    request<ChannelPairingCommandResponse>("/channels/pairing/command", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // WeChat QR login (channel-management-ui 6.3)
  startWeixinLogin: () =>
    request<{ login_id: string; qr_image: string }>("/channels/weixin/login/start", { method: "POST" }),
  weixinLoginStatus: (loginId: string) =>
    request<{ status: string }>(`/channels/weixin/login/status?login_id=${encodeURIComponent(loginId)}`),

  // Alpha Zoo API
  listAlphas: (params: AlphaListParams = {}) => {
    const q = new URLSearchParams();
    if (params.zoo) q.set("zoo", params.zoo);
    if (params.theme) q.set("theme", params.theme);
    if (params.universe) q.set("universe", params.universe);
    if (params.limit !== undefined) q.set("limit", String(params.limit));
    const qs = q.toString();
    return request<AlphaListResponse>(`/alpha/list${qs ? `?${qs}` : ""}`);
  },
  getAlpha: (alphaId: string) =>
    request<AlphaDetailResponse>(`/alpha/${encodeURIComponent(alphaId)}`),
  createAlphaBench: (body: AlphaBenchRequest) =>
    request<{ status: string; job_id: string }>("/alpha/bench", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  alphaBenchStreamUrl: (jobId: string) =>
    withAuthTicket(`${BASE}/alpha/bench/${encodeURIComponent(jobId)}/stream`),
  createAlphaCompare: (body: AlphaCompareRequest) =>
    request<{ status: string; job_id: string }>("/alpha/compare", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  alphaCompareStreamUrl: (jobId: string) =>
    withAuthTicket(`${BASE}/alpha/compare/${encodeURIComponent(jobId)}/stream`),

  // Connector runtime channel — privileged surface actions (NOT agent tools).
  // commit is the ONLY action that writes a mandate; halt trips the kill switch.
  commitMandate: (body: CommitMandateRequest) =>
    request<CommitMandateResponse>("/mandate/commit", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  haltLive: (session_id?: string, broker?: string, reason?: string) =>
    request<HaltLiveResponse>("/live/halt", {
      method: "POST",
      body: JSON.stringify({ session_id, broker, reason }),
    }),
  // Read the persistent runtime status across all authorized brokers (SPEC §7.5).
  // Polled by the RunnerStatus panel; a plain authenticated GET, never a chat message.
  getLiveStatus: (signal?: AbortSignal) => request<LiveStatus>("/live/status", { signal }),
  authorizeLive: (broker: string) =>
    request<LiveAuthorizeResponse>("/live/authorize", {
      method: "POST",
      body: JSON.stringify({ broker }),
    }),
  // Start/stop the persistent runner (SPEC §7.5). Privileged surface actions, not agent tools.
  startLiveRunner: (broker: string) =>
    request<LiveRunnerResponse>("/live/runner/start", {
      method: "POST",
      body: JSON.stringify({ broker }),
    }),
  stopLiveRunner: (broker: string) =>
    request<LiveRunnerResponse>("/live/runner/stop", {
      method: "POST",
      body: JSON.stringify({ broker }),
    }),

  // Optional deps — on-demand broker SDK install (desktop runtime).
  listOptionalDeps: () =>
    request<OptionalDepsListResponse>("/optional-deps/list"),
  installOptionalDep: (pkg: string) =>
    request<{ job_id: string; status: string }>(
      "/optional-deps/install",
      { method: "POST", body: JSON.stringify({ package: pkg }) },
    ),
  uninstallOptionalDep: (pkg: string) =>
    request<{ status: string }>(
      "/optional-deps/uninstall",
      { method: "POST", body: JSON.stringify({ package: pkg }) },
    ),
  optionalDepStatusUrl: (jobId: string) =>
    withAuthTicket(`${BASE}/optional-deps/status/${encodeURIComponent(jobId)}`),
  getOptionalDepsMirror: () =>
    request<MirrorInfo>("/optional-deps/mirror"),
  updateOptionalDepsMirror: (body: UpdateMirrorRequest) =>
    request<MirrorInfo>("/optional-deps/mirror", {
      method: "PUT",
      body: JSON.stringify(body),
    }),

  // Dashboard AI market summary
  createMarketSummary: (body: MarketSnapshotRequest) =>
    request<MarketSummaryResponse>("/dashboard/market-summary", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  getDashboardBoardHeat: (kind: "concept" | "industry") =>
    request<DashboardBoardHeatResponse>(
      `/dashboard/board-heat?kind=${encodeURIComponent(kind)}`,
    ),
  getDashboardDailyBars: (symbol: string) =>
    request<DashboardDailyBarsResponse>(
      `/dashboard/daily-bars?symbol=${encodeURIComponent(symbol)}`,
    ),
  getNewsSnapshot: async (signal?: AbortSignal) =>
    parseNewsSnapshotResponse(await request<unknown>("/news-api/snapshot", { signal })),
  startNewsRefresh: async (signal?: AbortSignal) =>
    parseNewsRefreshAccepted(await request<unknown>("/news-api/refresh", { method: "POST", signal })),
  getNewsRefreshStatus: async (signal?: AbortSignal) =>
    parseNewsRefreshStatus(await request<unknown>("/news-api/refresh/status", { signal })),
};

// --- Swarm types ---

export interface SwarmPreset {
  name: string;
  title: string;
  description: string;
  agent_count: number;
  variables: { name: string; description: string; required: boolean }[];
}

export interface SwarmRunSummary {
  id: string;
  preset_name: string;
  status: string;
  created_at: string;
  task_count: number;
  completed_count: number;
}

export interface LLMProviderOption {
  name: string;
  label: string;
  api_key_env?: string | null;
  base_url_env: string;
  default_model: string;
  default_base_url: string;
  api_key_required: boolean;
  auth_type?: string;
  login_command?: string | null;
}

export interface LLMSettings {
  provider: string;
  model_name: string;
  base_url: string;
  api_key_env?: string | null;
  api_key_configured: boolean;
  api_key_hint?: string | null;
  api_key_required: boolean;
  temperature: number;
  timeout_seconds: number;
  max_retries: number;
  reasoning_effort: string;
  sse_timeout_seconds: number;
  env_path: string;
  providers: LLMProviderOption[];
  desktop_login_provisioned?: boolean;
}

export interface UpdateLLMSettingsRequest {
  provider: string;
  model_name: string;
  base_url: string;
  api_key?: string;
  clear_api_key?: boolean;
  temperature: number;
  timeout_seconds: number;
  max_retries: number;
  reasoning_effort?: string;
}

export interface VIPModelListResponse {
  models: string[];
}

export interface VIPModelListRequest {
  api_key?: string;
  base_url?: string;
}

export interface DataSourceSettings {
  tushare_token_configured: boolean;
  tushare_token_hint?: string | null;
  baostock_supported: boolean;
  baostock_installed: boolean;
  baostock_message: string;
  env_path: string;
}

export interface UpdateDataSourceSettingsRequest {
  tushare_token?: string;
  clear_tushare_token?: boolean;
}

export interface ChannelAdapterStatus {
  name: string;
  display_name: string;
  configured: boolean;
  enabled: boolean;
  available: boolean;
  loaded: boolean;
  running: boolean;
  health?: string;
  error?: string;
  install_hint?: string;
}

export interface ChannelRuntimeStatus {
  running: boolean;
  inbound_queue: number;
  outbound_queue: number;
  session_count: number;
  channels: Record<string, ChannelAdapterStatus>;
}

export interface ChannelRuntimeActionResponse extends ChannelRuntimeStatus {
  status: string;
}

export interface ChannelPairingCommandRequest {
  channel: string;
  command: string;
}

export interface ChannelPairingCommandResponse {
  channel: string;
  reply: string;
}

// --- Types matching backend API contracts ---

export interface RunListItem {
  run_id: string;
  status: string;
  created_at: string;
  prompt?: string;
  total_return?: number;
  sharpe?: number;
  codes?: string[];
  start_date?: string;
  end_date?: string;
}

export interface RunDetailParams {
  chart_payload?: "summary";
  chart_symbol?: string;
}

export interface PriceBar {
  time: string;
  timestamp?: string;
  code?: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface TradeMarker {
  time: string;
  timestamp?: string;
  code?: string;
  side: "BUY" | "SELL";
  price: number;
  qty?: number;
  reason?: string;
  text?: string;
}

export interface EquityPoint {
  time: string;
  equity: string | number;
  drawdown: string | number;
}

export interface ValidationData {
  monte_carlo?: {
    actual_sharpe: number;
    actual_max_dd: number;
    p_value_sharpe: number;
    p_value_max_dd: number;
    simulated_sharpe_mean: number;
    simulated_sharpe_std: number;
    simulated_sharpe_p5: number;
    simulated_sharpe_p95: number;
    n_simulations: number;
    n_trades: number;
    error?: string;
  };
  bootstrap?: {
    observed_sharpe: number;
    ci_lower: number;
    ci_upper: number;
    median_sharpe: number;
    prob_positive: number;
    confidence: number;
    n_bootstrap: number;
    error?: string;
  };
  walk_forward?: {
    n_windows: number;
    windows: Array<{
      window: number;
      start: string;
      end: string;
      return: number;
      sharpe: number;
      max_dd: number;
      trades: number;
      win_rate: number;
    }>;
    profitable_windows: number;
    consistency_rate: number;
    return_mean: number;
    return_std: number;
    sharpe_mean: number;
    sharpe_std: number;
    error?: string;
  };
}

export interface RunData {
  status: string;
  run_id: string;
  prompt?: string;
  elapsed_seconds?: number;
  run_directory?: string;
  run_stage?: string;
  run_context?: Record<string, unknown>;

  metrics?: BacktestMetrics;
  artifacts?: ArtifactInfo[];
  run_card?: RunCard;
  validation?: ValidationData;

  chart_symbols?: string[];
  price_series?: Record<string, PriceBar[]>;
  indicator_series?: Record<string, Record<string, IndicatorPoint[]>>;
  trade_markers?: TradeMarker[];
  equity_curve?: EquityPoint[];
  trade_log?: Array<Record<string, string>>;
  run_logs?: Array<{ source?: string; line_number?: number; message?: string }>;
}

export interface RunCard {
  schema_version?: string;
  generated_at?: string;
  run_dir?: string;
  backtest?: Record<string, unknown>;
  reproducibility?: Record<string, unknown>;
  data_sources?: string[];
  metrics?: Record<string, unknown>;
  validation?: unknown;
  warnings?: string[];
  artifacts?: RunCardArtifact[];
  [key: string]: unknown;
}

export interface RunCardArtifact {
  path: string;
  size_bytes: number;
  sha256: string;
}

export interface BacktestMetrics {
  final_value: number;
  total_return: number;
  annual_return: number;
  max_drawdown: number;
  sharpe: number;
  win_rate: number;
  trade_count: number;
  [key: string]: number;
}


export interface IndicatorPoint {
  time: string;
  value: number;
}

export interface ArtifactInfo {
  name: string;
  path: string;
  type: string;
  size: number;
  exists: boolean;
}

export interface PineScriptResult {
  exists: boolean;
  content: string | null;
}

export interface SessionItem {
  session_id: string;
  title?: string;
  status?: string;
  created_at?: string;
  updated_at?: string;
  last_attempt_id?: string;
}

// --- Goal types ---

export type GoalStatus =
  | "active"
  | "paused"
  | "waiting_user"
  | "needs_refresh"
  | "insufficient_evidence"
  | "compliance_blocked"
  | "blocked"
  | "budget_limited"
  | "usage_limited"
  | "complete"
  | "cancelled"
  | "superseded";

export type GoalRiskTier =
  | "research_general"
  | "market_specific_short_term"
  | "personalized_advice_or_position_sizing";

export interface GoalRecord {
  goal_id: string;
  session_id: string;
  status: GoalStatus;
  objective: string;
  ui_summary: string;
  source: string;
  protocol: string;
  risk_tier: GoalRiskTier;
  token_budget?: number | null;
  tokens_used: number;
  turn_budget?: number | null;
  turns_used: number;
  time_budget_seconds?: number | null;
  time_used_seconds: number;
  budget_wrapup_sent: boolean;
  created_at: string;
  updated_at: string;
  completed_at?: string | null;
  recap?: string | null;
}

export interface GoalClaim {
  claim_id: string;
  goal_id: string;
  session_id: string;
  claim_type: string;
  text: string;
  status: string;
  created_at: string;
  updated_at: string;
}

export interface GoalCriterion {
  criterion_id: string;
  goal_id: string;
  session_id: string;
  text: string;
  required: boolean;
  status: string;
  freshness_requirement?: string | null;
  protocol_step?: string | null;
  created_at: string;
  updated_at: string;
}

export interface GoalEvidence {
  evidence_id: string;
  goal_id: string;
  session_id: string;
  text: string;
  criterion_id?: string | null;
  claim_id?: string | null;
  evidence_type: string;
  tool_call_id?: string | null;
  run_id?: string | null;
  source_provider?: string | null;
  source_type?: string | null;
  source_uri?: string | null;
  symbol_universe: string[];
  benchmark: string[];
  timeframe?: string | null;
  method?: string | null;
  assumptions: Record<string, unknown>;
  artifact_path?: string | null;
  artifact_hash?: string | null;
  retrieved_at: string;
  data_as_of?: string | null;
  freshness_status: string;
  verification_status: string;
  confidence?: string | null;
  caveat?: string | null;
  contradicts_claim_ids: string[];
  created_at: string;
}

export interface GoalSnapshot {
  goal: GoalRecord;
  claims: GoalClaim[];
  criteria: GoalCriterion[];
  evidence: GoalEvidence[];
  evidence_count: number;
}

export interface CreateGoalRequest {
  objective: string;
  criteria?: string[];
  ui_summary?: string;
  protocol?: string;
  risk_tier?: GoalRiskTier;
  token_budget?: number;
  turn_budget?: number;
  time_budget_seconds?: number;
}

export interface AddGoalEvidenceRequest {
  goal_id: string;
  expected_goal_id: string;
  text: string;
  criterion_id?: string | null;
  claim_id?: string | null;
  evidence_type?: string;
  tool_call_id?: string | null;
  run_id?: string | null;
  source_provider?: string | null;
  source_type?: string | null;
  source_uri?: string | null;
  symbol_universe?: string[];
  benchmark?: string[];
  timeframe?: string | null;
  method?: string | null;
  assumptions?: Record<string, unknown>;
  artifact_path?: string | null;
  artifact_hash?: string | null;
  data_as_of?: string | null;
  confidence?: string | null;
  caveat?: string | null;
  contradicts_claim_ids?: string[];
}

export interface UpdateGoalRequest {
  goal_id: string;
  expected_goal_id: string;
  objective?: string;
  ui_summary?: string;
}

export interface UpdateGoalResponse {
  goal: GoalRecord;
  snapshot: GoalSnapshot;
}

export interface AddGoalEvidenceResponse {
  evidence: GoalEvidence;
  snapshot: GoalSnapshot;
}

export interface GoalAuditRowRequest {
  criterion_id: string;
  result: string;
  evidence_ids?: string[];
  notes?: string;
}

export interface UpdateGoalStatusRequest {
  goal_id: string;
  expected_goal_id: string;
  status: GoalStatus;
  audit?: GoalAuditRowRequest[];
  recap?: string | null;
}

export interface UpdateGoalStatusResponse {
  goal: GoalRecord;
  snapshot: GoalSnapshot;
}

// --- Alpha Zoo types ---

export interface AlphaListParams {
  zoo?: string;
  theme?: string;
  universe?: string;
  limit?: number;
}

export interface AlphaSummary {
  id: string;
  zoo: string;
  theme: string[];
  universe: string[];
  nickname?: string;
  decay_horizon?: number | null;
  min_warmup_bars?: number | null;
  requires_sector?: boolean;
}

export interface AlphaListResponse {
  status: string;
  alphas: AlphaSummary[];
  total: number;
  returned: number;
  truncated: boolean;
}

export interface AlphaDetail {
  id: string;
  zoo: string;
  module_path?: string;
  meta: Record<string, unknown>;
}

export interface AlphaDetailResponse {
  status: string;
  alpha: AlphaDetail;
  source_code: string;
}

export interface AlphaBenchRequest {
  zoo: string;
  universe: string;
  period: string;
  top?: number;
}

export interface AlphaBenchTopRow {
  id: string;
  ic_mean: number;
  ir: number;
  theme: string[];
  formula_latex: string;
  category: "alive" | "reversed" | "dead";
}

export interface AlphaBenchResult {
  alive: number;
  reversed: number;
  dead: number;
  skipped?: number;
  top5_by_ir: AlphaBenchTopRow[];
  dead_examples: AlphaBenchTopRow[];
  by_theme: Record<string, { alive: number; reversed: number; dead: number }>;
}

export interface AlphaCompareRequest {
  alpha_ids: string[];
  universe: string;
  period: string;
  /** One of: ir | ic_mean | ic_positive_ratio | ic_count (default ir). */
  sort?: string;
}

export interface AlphaCompareRow {
  rank: number;
  id: string;
  zoo: string;
  ic_mean: number;
  ic_std: number;
  ir: number;
  ic_positive_ratio: number;
  ic_count: number;
  /** `delta_<sort>_vs_best` — gap to the top-ranked alpha on the active metric. */
  [deltaKey: string]: number | string;
}

export interface AlphaCompareSkip {
  id: string;
  reason: string;
}

export interface AlphaCompareResult {
  universe: string;
  period: string;
  sort: string;
  n_compared: number;
  n_skipped: number;
  winner: string;
  ranking: AlphaCompareRow[];
  skipped: AlphaCompareSkip[];
}

// --- Connector runtime channel types ---

/** One mandate profile inside a `mandate.proposal` event (SPEC Consent §1). */
export interface MandateProfile {
  ordinal: number;
  label: string;
  /** Concrete ticker list, or a structural universe descriptor (e.g. "tech_sector"). */
  universe: string[] | string;
  max_order_usd: number;
  daily_trade_cap: number;
  /** "none" for cash-only, otherwise a leverage descriptor/multiple. */
  leverage: string | number;
  instruments: string[];
  notes?: string;
}

/** Account block of a `mandate.proposal` event. */
export interface MandateProposalAccount {
  broker: string;
  type: string;
  funded_by: string;
}

/** Payload of the `mandate.proposal` SSE event (SPEC Consent §1). */
export interface MandateProposal {
  type?: string;
  proposal_id: string;
  session_id?: string;
  intent_normalized?: string;
  account?: MandateProposalAccount;
  ceilings_ref?: string;
  profiles: MandateProfile[];
  funding_note?: string;
  halt_note?: string;
  /** Present only when this proposal was triggered by a mandate breach (SPEC Consent §3). */
  reauth_for?: { breach_id?: string } | null;
}

/** Payload of the `mandate.committed` SSE event (SPEC Consent §1 COMMIT). */
export interface MandateCommitted {
  proposal_id?: string;
  mandate_id?: string;
  consent_record_id?: string;
  selected_ordinal?: number;
  broker?: string;
  /** Resolved limits, surfaced for the compact active-mandate badge. */
  max_order_usd?: number;
  daily_trade_cap?: number;
  expires_at?: string;
}

/** Payload of the `live.halted` SSE event (SPEC Consent §4). */
export interface LiveHalted {
  broker?: string | null;
  tripped_at?: string;
  by?: string;
  reason?: string;
}

/** Payload of the `live.action` SSE event (SPEC Consent §5 audit notify). */
export interface LiveAction {
  audit_id?: string;
  ts?: string;
  kind: string;
  intent_normalized?: string;
  outcome?: string;
  broker?: string;
  remote_tool?: string;
  error?: string | null;
}

export interface CommitMandateRequest {
  broker: string;
  proposal_id: string;
  selected_ordinal: number;
  /** Present only on the adjust path (SPEC Consent §3); null otherwise. */
  adjustments?: Record<string, unknown> | null;
  /** Explicit affirmative consent; the surface sets it on the user's click. */
  consent_ack: boolean;
  session_id?: string;
  account_ref?: string;
  lifetime_days?: number;
}

export interface CommitMandateResponse {
  mandate_id: string;
  consent_record_id: string;
  selected_ordinal?: number;
  broker?: string;
  max_order_usd?: number;
  daily_trade_cap?: number;
  expires_at?: string;
}

export interface HaltLiveResponse {
  halted: boolean;
  broker?: string | null;
  reason: string;
  sentinel: string;
}

export interface LiveAuthorizeRequest {
  broker: string;
}

export interface LiveAuthorizeResponse {
  broker: string;
  connector_profile: string;
  oauth_token_present: boolean;
  instruction: string;
  note?: string;
}

/** Mandate limits surfaced inside a `GET /live/status` broker entry (SPEC §7.5). */
export interface LiveMandateLimits {
  max_order_notional_usd?: number;
  max_total_exposure_usd?: number;
  max_leverage?: number;
  max_trades_per_day?: number;
  allowed_instruments?: string[];
  account_funding_usd?: number;
  [key: string]: unknown;
}

/** Active mandate block of a `GET /live/status` broker entry. */
export interface LiveMandateStatus {
  broker?: string;
  mandate_id?: string;
  account_ref?: string;
  created_at?: string;
  limits?: LiveMandateLimits;
  /** ISO timestamp the mandate auto-expires (SPEC §7.5 #7 proactive expiry). */
  expires_at?: string;
  expires_in_seconds?: number | null;
  expired?: boolean;
}

/** Runner liveness block of a `GET /live/status` broker entry (SPEC §7.5 #3). */
export interface LiveRunnerLiveness {
  broker?: string;
  alive: boolean;
  /** Unix epoch seconds of the last heartbeat tick; null if the runner never started. */
  last_tick?: number | string | null;
  last_tick_age_seconds?: number | null;
}

export interface LiveBrokerAuthStatus {
  broker: string;
  oauth_token_present: boolean;
  is_live_broker: boolean;
}

/** One broker entry in the `GET /live/status` response. */
export interface LiveBrokerStatus {
  auth: LiveBrokerAuthStatus;
  mandate?: LiveMandateStatus | null;
  runner: LiveRunnerLiveness;
  halted: boolean;
}

/** Response of `GET /live/status` (SPEC §7.5 runner status panel + C2). */
export interface LiveStatus {
  brokers: LiveBrokerStatus[];
  global_halted: boolean;
}

/** Response of `POST /live/runner/start|stop`. */
export interface LiveRunnerResponse {
  broker: string;
  started?: boolean;
  already_running?: boolean;
  stopped?: boolean;
  was_running?: boolean;
}

export interface OptionalDepBroker {
  id: string;
  label: string;
  package: string;
  description: string;
  platforms: string[];
  recommended_mirror: string;
  installed: boolean;
  installed_version: string;
}

export interface OptionalDepsListResponse {
  brokers: OptionalDepBroker[];
}

export interface MirrorInfo {
  name: string;
  custom_index_url: string;
  available: Record<string, string>;
}

export interface UpdateMirrorRequest {
  name: string;
  custom_index_url?: string;
}

export interface MessageItem {
  message_id: string;
  session_id: string;
  role: string;
  content: string;
  created_at: string;
  linked_attempt_id?: string;
  metadata?: Record<string, unknown>;
}

// ─── Watchlist ────────────────────────────────────────────────────────────────

export interface WatchlistStock {
  code: string;
  name: string;
  market: string;
  added_at: string;
}

export interface WatchlistStocksResponse {
  stocks: WatchlistStock[];
}

export interface AddStockResult {
  added: boolean;
  exists: boolean;
}

export interface DeleteStockResult {
  deleted: boolean;
}

export interface QuoteData {
  code: string;
  name?: string | null;
  price?: number | null;
  change_pct?: number | null;
  change_amt?: number | null;
  high?: number | null;
  low?: number | null;
  volume?: number | null;
  stale?: boolean;
  error?: string;
}

export type QuotesResponse = Record<string, QuoteData>;

export async function fetchWatchlistStocks(): Promise<WatchlistStocksResponse> {
  return request<WatchlistStocksResponse>("/watchlist/stocks");
}

export async function addWatchlistStock(code: string, market = "a_stock"): Promise<AddStockResult> {
  return request<AddStockResult>("/watchlist/stocks", {
    method: "POST",
    body: JSON.stringify({ code, market }),
  });
}

export async function deleteWatchlistStock(code: string, market = "a_stock"): Promise<DeleteStockResult> {
  return request<DeleteStockResult>(`/watchlist/stocks/${encodeURIComponent(code)}?market=${encodeURIComponent(market)}`, {
    method: "DELETE",
  });
}

export async function fetchWatchlistQuotes(codes: string[], market = "a_stock"): Promise<QuotesResponse> {
  const codesParam = codes.map(encodeURIComponent).join(",");
  return request<QuotesResponse>(`/watchlist/quotes?codes=${codesParam}&market=${encodeURIComponent(market)}`);
}

// ─── Dashboard AI market summary ──────────────────────────────────────────

export interface MarketSummaryResponse {
  available: boolean;
  summary: {
    headline: string;
    drivers: string[];
    risks: string[];
    focus: string[];
  } | null;
  market_as_of: string | null;
  generated_at: string | null;
  cached: boolean;
  stale: boolean;
  error_code?: string;
}

export interface MarketSnapshotRequest {
  market: "a_share";
  market_as_of: string;
  force_refresh?: boolean;
  indices: Array<{ code: string; name: string; price: number; change_pct: number }>;
  sectors: Array<{ name: string; change_pct: number }>;
  watchlist: Array<{ code: string; name: string; change_pct: number | null }>;
}

export interface DashboardBoardHeatItem {
  code: string;
  name: string;
  change_pct: number | null;
  rise_count: number | null;
  fall_count: number | null;
  leading_stock: string | null;
  leading_stock_change_pct: number | null;
}

export interface DashboardBoardHeatResponse {
  data: DashboardBoardHeatItem[];
  as_of: string;
  source: string;
  stale: boolean;
}

export interface DashboardDailyBarsResponse {
  data: PriceBar[];
  as_of: string;
  source: string;
  stale: boolean;
}
