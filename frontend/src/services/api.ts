import type { ActivityEvent, AuditEntry, Confirmation, Conversation, DatabaseInfo, EmotionDay, EmotionReading, EmotionStatus, EmotionTrend, MemoryItem, OAuthState, Schedule, SetupStatus, SystemMetrics, SystemStatus, Task, ToolInfo, WakeStatus } from "../types";

const TOKEN_KEY = "zeta_api_token";

export function getToken(): string {
  try {
    return localStorage.getItem(TOKEN_KEY) || "";
  } catch {
    return "";
  }
}

export function setToken(t: string) {
  try {
    if (t) localStorage.setItem(TOKEN_KEY, t);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* ignore */
  }
}

async function req<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = { ...(init.headers as Record<string, string>) };
  if (!(init.body instanceof FormData)) headers["Content-Type"] = "application/json";
  const token = getToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const r = await fetch(path, { ...init, headers });
  if (!r.ok) {
    let detail = r.statusText;
    try {
      const j = await r.json();
      detail = j.detail || JSON.stringify(j);
    } catch {
      /* ignore */
    }
    if (r.status === 401) detail = "Unauthorized: set the API token in Settings (Advanced).";
    throw new Error(detail);
  }
  const ct = r.headers.get("content-type") || "";
  return (ct.includes("application/json") ? r.json() : r.blob()) as Promise<T>;
}

export const api = {
  status: () => req<SystemStatus>("/api/system/status"),
  databaseInfo: () => req<DatabaseInfo>("/api/system/database"),
  chat: (message: string, conversation_id?: string | null) =>
    req<{ task_id: string; conversation_id: string; status: string }>("/api/chat", {
      method: "POST",
      body: JSON.stringify({ message, conversation_id: conversation_id || null }),
    }),
  messages: (conversationId: string) =>
    req<{ id: string; role: string; content: string; task_id: string | null; created_at: string }[]>(`/api/conversations/${conversationId}/messages`),
  conversations: () => req<Conversation[]>("/api/conversations"),
  deleteConversation: (id: string) => req(`/api/conversations/${id}`, { method: "DELETE" }),
  tasks: (active = false) => req<Task[]>(`/api/tasks?active=${active}`),
  taskHistory: (limit = 50) => req<Task[]>(`/api/tasks?history=true&limit=${limit}`),
  cancelTask: (id: string) => req(`/api/tasks/${id}/cancel`, { method: "POST" }),
  cancelAll: () => req("/api/tasks/cancel_all", { method: "POST" }),
  confirmations: () => req<Confirmation[]>("/api/confirmations"),
  confirm: (confirmation_id: string, approved: boolean, remember = false) =>
    req("/api/confirm", { method: "POST", body: JSON.stringify({ confirmation_id, approved, remember }) }),
  activity: () => req<ActivityEvent[]>("/api/activity"),
  audit: (limit = 100, event = "") => req<AuditEntry[]>(`/api/audit?limit=${limit}${event ? `&event=${encodeURIComponent(event)}` : ""}`),
  tools: () => req<ToolInfo[]>("/api/tools"),
  permissions: () => req<{ permissions: Record<string, Record<string, string>>; enabled: Record<string, boolean> }>("/api/permissions"),
  updatePermissions: (body: { permissions?: Record<string, Record<string, string>>; enabled?: Record<string, boolean> }) =>
    req("/api/permissions", { method: "PUT", body: JSON.stringify(body) }),
  memory: () => req<MemoryItem[]>("/api/memory"),
  addMemory: (content: string, category = "fact") => req<MemoryItem>("/api/memory", { method: "POST", body: JSON.stringify({ content, category }) }),
  deleteMemory: (id: string) => req(`/api/memory/${id}`, { method: "DELETE" }),
  schedules: () => req<Schedule[]>("/api/schedules"),
  createSchedule: (body: Partial<Schedule>) => req<Schedule>("/api/schedules", { method: "POST", body: JSON.stringify(body) }),
  deleteSchedule: (id: string) => req(`/api/schedules/${id}`, { method: "DELETE" }),
  enableSchedule: (id: string, enabled: boolean) => req(`/api/schedules/${id}/enable?enabled=${enabled}`, { method: "POST" }),
  setupStatus: () => req<SetupStatus>("/api/setup/status"),
  oauthStatus: () =>
    req<{ gmail: OAuthState; outlook: OAuthState; email_provider: string; email_auth: string }>("/api/email/oauth/status"),
  oauthStart: (provider: "gmail" | "outlook") =>
    req<{ url: string; state: string }>(`/api/email/oauth/start?provider=${provider}&open_browser=false`, { method: "POST" }),
  oauthDisconnect: (provider: "gmail" | "outlook") => req(`/api/email/oauth/disconnect?provider=${provider}`, { method: "POST" }),
  setup: (body: Record<string, unknown>) => req("/api/setup", { method: "POST", body: JSON.stringify(body) }),
  transcribe: async (blob: Blob) => {
    const fd = new FormData();
    fd.append("file", blob, "audio.webm");
    return req<{ text: string; emotion?: EmotionReading | null }>("/api/voice/transcribe", { method: "POST", body: fd });
  },
  speak: async (text: string): Promise<Blob> => req<Blob>("/api/voice/speak", { method: "POST", body: JSON.stringify({ text }) }),
  emotion: () => req<EmotionStatus>("/api/emotion"),
  emotionHistory: (limit = 50, days = 14) => req<EmotionReading[]>(`/api/emotion/history?limit=${limit}&days=${days}`),
  emotionDaily: (days = 14) => req<{ days: EmotionDay[]; trend: EmotionTrend }>(`/api/emotion/daily?days=${days}`),
  analyzeEmotion: (text: string) => req<EmotionReading>("/api/emotion/analyze", { method: "POST", body: JSON.stringify({ text }) }),
  voices: () => req<{ provider: string; voices: { id: string; name: string }[] }>("/api/voice/voices"),
  wakeStatus: () => req<WakeStatus>("/api/voice/wake"),
  wakePause: (seconds = 20) => req(`/api/voice/wake/pause?seconds=${seconds}`, { method: "POST" }),
  wakeTest: () => req("/api/voice/wake/test", { method: "POST" }),
  metrics: () => req<SystemMetrics>("/api/system/metrics"),
};

export function wsUrl(): string {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const token = getToken();
  return `${proto}://${location.host}/ws${token ? `?token=${encodeURIComponent(token)}` : ""}`;
}
