export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  taskId?: string | null;
  interim?: boolean;
  streaming?: boolean;
  streamKey?: string;
  live?: boolean;      // arrived over the WebSocket in this session (not loaded from history)
  createdAt: string;
}

export interface CrisisResource {
  name: string;
  contact: string;
}

export interface EmotionReading {
  label: string;
  valence: number;              // -1 unpleasant .. +1 pleasant
  arousal: number;              // 0 calm .. 1 activated
  confidence: number;
  intensity: number;
  cues: string[];
  sources?: Record<string, unknown>;
  crisis?: { level: string; resources?: CrisisResource[] } | null;
  needs_support?: boolean;
  description?: string;
  text?: string;
  snippet?: string;
  at?: string;
  ts?: string | null;
  id?: number;
}

export interface EmotionTrend {
  samples: number;
  average_valence?: number;
  direction?: string;
  dominant?: string;
  mood?: string;
  labels?: Record<string, number>;
}

export interface EmotionDay {
  day: string;
  samples: number;
  valence: number;
  arousal: number;
  mood: string;
  dominant: string;
}

export interface EmotionStatus {
  enabled: boolean;
  prosody: boolean;
  region: string;
  current: EmotionReading;
  trend: EmotionTrend;
  expressive_voice?: boolean;
  adapt_voice?: boolean;
  model?: string;
}

export interface PlanStep {
  index: number;
  description: string;
  status: "pending" | "running" | "done" | "failed" | "skipped";
  note?: string;
}

export interface Task {
  id: string;
  conversation_id: string | null;
  request: string;
  status: "PENDING" | "PLANNING" | "WAITING_FOR_CONFIRMATION" | "EXECUTING" | "COMPLETED" | "FAILED" | "CANCELLED";
  plan: PlanStep[];
  current_step: number;
  tools_used: string[];
  result: string | null;
  error: string | null;
  created_at: string;
  updated_at: string;
  duration_ms: number | null;
}

export interface Confirmation {
  id: string;
  task_id: string;
  tool: string;
  risk: string;
  description: string;
  details: Record<string, unknown>;
  created_at: string;
  expires_at: string;
}

export interface ActivityEvent {
  type: string;
  ts: string;
  task_id?: string | null;
  message?: string;
  level?: string;
  tool?: string;
  success?: boolean;
  duration_ms?: number;
  risk?: string;
  args?: Record<string, unknown>;
  artifacts?: { image_b64?: string; path?: string };
  content?: string;
  interim?: boolean;
  conversation_id?: string;
  confirmation?: Confirmation;
  confirmation_id?: string;
  approved?: boolean;
  task?: Task;
  plan?: PlanStep[];
  title?: string;
  hits?: string[];
  // streaming
  delta?: string;
  seq?: number;
  step?: number;
  discard?: boolean;
  // emotion
  emotion?: EmotionReading;
  trend?: EmotionTrend;
  stage?: string;
  // wake word
  engine?: string;
  text?: string;
  phrase?: string;
}

export interface WakeStatus {
  enabled: boolean;
  running: boolean;
  engine: string;
  phrase: string;
  error: string;
  detections: number;
  last_text: string;
  last_detection?: number | null;
  devices?: { index: number; name: string; default: boolean }[];
}

export interface DatabaseInfo {
  backend: string;
  location: string;
  supabase: boolean;
  ok?: boolean;
  detail?: string;
  file_index?: string;
}

export interface SystemStatus {
  name: string;
  version: string;
  online: boolean;
  mode: string;
  llm: { provider: string; model: string; ok: boolean; detail: string; supports_tools: boolean; models: string[] };
  voice: { stt: string; tts: string; stt_ok: boolean; tts_ok: boolean; detail: string; wake?: WakeStatus };
  database?: DatabaseInfo;
  emotion?: EmotionStatus;
  internet: { connected: boolean };
  computer: { connected: boolean; index: Record<string, unknown>; browser: { ok: boolean; detail: string } };
  tools: number;
  plugins: { name: string; description: string; tools: string[] }[];
  active_tasks: number;
  pending_confirmations: number;
  setup_complete: boolean;
  config: Record<string, unknown>;
}

export interface ToolInfo {
  name: string;
  description: string;
  category: string;
  risk_level: string;
  requires_confirmation: boolean;
  enabled: boolean;
}

export interface MemoryItem {
  id: string;
  content: string;
  category: string;
  tags: string[];
  importance: number;
  created_at: string;
}

export interface OAuthState {
  provider: string;
  configured: boolean;
  connected: boolean;
  email: string;
  expires_at: number | null;
  redirect_uri: string;
  missing: string[];
}

export interface SetupStatus {
  setup_complete: boolean;
  env_path: string;
  ollama: { available: boolean; models: string[] };
  lmstudio: { available: boolean; models: string[] };
  current: Record<string, unknown>;
  permissions: { enabled: Record<string, boolean> };
}

export interface Conversation {
  id: string;
  title: string;
  created_at?: string;
  updated_at: string;
}

export interface Schedule {
  id: string;
  name: string;
  kind: "reminder" | "request" | string;
  payload: string;
  schedule_type: "once" | "interval" | "cron" | string;
  run_at?: string | null;
  interval_seconds?: number | null;
  cron?: string | null;
  enabled: boolean;
  last_run?: string | null;
  next_run?: string | null;
  created_at?: string;
}

export interface AuditEntry {
  id: number;
  ts: string | null;
  task_id: string | null;
  event: string;
  tool: string | null;
  risk: string | null;
  decision: string | null;
  details: Record<string, unknown> | null;
  success: boolean | null;
  duration_ms: number | null;
}

// ---------------------------------------------------------------- hardware monitor
export interface CpuMetrics {
  percent: number;
  per_core: number[];
  cores_logical: number;
  cores_physical: number;
  frequency: { current_mhz: number; max_mhz: number | null } | null;
  temperature_c: number | null;
  /** Why there is no temperature, when there is none. Empty when the reading worked. */
  temperature_detail: string;
}

export interface MemoryMetrics {
  used_mb: number;
  total_mb: number;
  percent: number;
  available_mb: number;
  swap_used_mb: number;
  swap_total_mb: number;
}

export interface GpuProcess {
  pid: number;
  name: string;
  /** Usually null on Windows: the WDDM driver does not report per-process VRAM. */
  memory_mb: number | null;
}

export interface GpuMetrics {
  present: boolean;
  detail?: string;
  name?: string;
  utilization?: number | null;
  memory_utilization?: number | null;
  memory_used_mb?: number | null;
  memory_total_mb?: number | null;
  memory_percent?: number | null;
  temperature_c?: number | null;
  power_w?: number | null;
  power_limit_w?: number | null;
  clock_mhz?: number | null;
  clock_max_mhz?: number | null;
  fan_percent?: number | null;
  processes?: GpuProcess[];
}

export interface DiskMetrics {
  mount: string;
  used_gb: number;
  total_gb: number;
  percent: number;
}

export interface ProcessRow {
  pid: number;
  name: string;
  memory_mb: number;
}

/** What Zeta's own models are doing with the machine. */
export interface ZetaFootprint {
  sharing: boolean;
  llm: { model: string; loaded?: boolean; total_mb?: number; vram_mb?: number; cpu_mb?: number; on_cpu?: boolean };
  voice: { provider?: string; ok?: boolean; parked?: boolean | null; device?: string; detail?: string };
  speech?: { model: string; device: string };
}

export interface SystemMetrics {
  at: number;
  uptime_s: number;
  cpu: CpuMetrics;
  memory: MemoryMetrics;
  gpu: GpuMetrics;
  disks: DiskMetrics[];
  disk: { read_mb_s: number; write_mb_s: number } | null;
  network: { down_mb_s: number; up_mb_s: number } | null;
  battery: { percent: number; plugged: boolean; minutes_left: number | null } | null;
  processes: ProcessRow[];
  zeta: ZetaFootprint;
}
