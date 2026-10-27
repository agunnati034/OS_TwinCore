import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Activity,
  AlertTriangle,
  Clock3,
  Cpu,
  Database,
  Gauge,
  HardDrive,
  LayoutDashboard,
  LoaderCircle,
  Network,
  RefreshCw,
  Shield,
  ShieldAlert,
  ShieldCheck,
  Signal,
  Zap,
} from "lucide-react";
import {
  EMBEDDED_TASK_IDS,
  canRequestNextEmbeddedFault,
  getCurrentSessionCheckpointRun,
  getLatestConfirmedCheckpointRun,
  getLatestElevatedRiskEvent,
  getEmbeddedRiskIndex,
  getEmbeddedRiskRows,
  type EmbeddedRiskAssessment,
} from "./embeddedRisk";
import {
  getEmbeddedMlTaskView,
  type EmbeddedMlSnapshot,
  type EmbeddedMlTaskView,
} from "./embeddedMl";
import { formatAppInteger, formatBytes } from "./formatters";

const API_ROOT = (import.meta.env.VITE_API_URL || "http://127.0.0.1:8765/api").replace(/\/$/, "");

type JsonRecord = Record<string, unknown>;
type Telemetry = {
  type: string;
  task_id: string;
  pid: number;
  captured_at_utc: string;
  cpu_utilization_percent_one_core: number | null;
  cpu_time_total_seconds: number | null;
  memory_rss_bytes: number | null;
  process_status: string | null;
};
type Risk = {
  worker_id: string;
  pid: number;
  risk_score: number | null;
  risk_category: string;
  explanation: string;
};
type Worker = {
  worker_id: string;
  telemetry: Telemetry | null;
  digital_twin: JsonRecord | null;
  risk: Risk | null;
};
type EmbeddedTaskTelemetry = {
  schema_version: number;
  target: string;
  task_id: string;
  task_state: string;
  tick_count: number;
  execution_count: number;
  stack_high_water_mark_words: number;
  bridge_session_id: string;
  run_id?: string;
  received_at_utc: string;
};
type EmbeddedWorkflowEvent = {
  event: string;
  occurred_at_utc: string;
  run_id: string;
  checkpoint_id: string;
  execution_count?: number;
  counter?: number;
  accumulator?: number;
  integrity_hash?: number;
  checkpoint_size_bytes?: number;
  risk_category?: string;
  risk_index?: number | null;
  stack_reserve_words?: number;
  threshold_words?: number;
  reason?: string;
};
type EmbeddedCheckpointStatus = {
  task_id: string;
  event: string;
  checkpoint_id?: string;
  occurred_at_utc: string;
  counter?: number;
  accumulator?: number;
  checkpoint_size_bytes?: number;
  error?: string;
};
type EmbeddedCheckpointStorage = {
  task_id: "sensor-sampler";
  checkpoint_id: string | null;
  run_id: string | null;
  bridge_session_id: string;
  created_at_utc: string;
  size_bytes: number | null;
  storage_used_bytes: number | null;
  storage_budget_bytes: number | null;
  storage_kind: "volatile_firmware_ram";
  storage_note: string;
  application_state: {
    counter: number;
    accumulator: number;
    integrity_hash: number;
  };
};
type EmbeddedRiskEvent = {
  task_id: string;
  previous_category?: string;
  risk_category: string;
  occurred_at_utc: string;
  bridge_session_id: string;
  execution_count: number;
  run_id?: string;
  stack_reserve_words?: number;
  threshold_words?: number;
  risk_index: number | null;
  reason: string;
};
type EmbeddedFaultInjection = {
  request_id: string;
  run_id: string;
  checkpoint_id: string;
  task_id: "sensor-sampler";
  bridge_session_id: string;
  status: string;
  requested_at_utc: string;
  updated_at_utc: string;
  explanation: string;
  error?: string;
  elevated_assessment?: EmbeddedRiskAssessment;
  checkpoint_saved_state?: { counter: number; accumulator: number; integrity_hash: number; checkpoint_size_bytes?: number };
  restored_state?: { counter: number; accumulator: number; integrity_hash: number };
  restored_progress?: { counter: number; accumulator: number; integrity_hash: number };
  recovery_completed_at_utc?: string;
  events: EmbeddedWorkflowEvent[];
};
type TwinSnapshot = { twins: JsonRecord[] };
type Checkpoint = {
  checkpoint_id: string;
  worker_id: string;
  worker_pid: number;
  created_at_utc: string;
  size_bytes: number;
  application_state: { counter: number; accumulator: number };
};
type BackendEvent = {
  type: string;
  event?: string;
  task_id?: string;
  worker_id?: string;
  captured_at_utc?: string;
  occurred_at_utc?: string;
  risk_category?: string;
  risk_score?: number;
  memory_rss_bytes?: number | null;
  cpu_utilization_percent_one_core?: number | null;
  [key: string]: unknown;
};
type DashboardData = {
  status: JsonRecord;
  workers: Worker[];
  telemetry: { latest: Telemetry[]; history: Telemetry[] };
  twins: TwinSnapshot;
  risks: { assessments: Risk[] };
  checkpoints: { checkpoints: Checkpoint[]; storage: { storage_usage_bytes: number; storage_budget_bytes: number } };
  events: { events: BackendEvent[] };
  recovery: { recovery: JsonRecord | null };
  embedded_tasks?: { latest: EmbeddedTaskTelemetry[]; history: EmbeddedTaskTelemetry[] };
  embedded_checkpoint_status?: EmbeddedCheckpointStatus[];
  embedded_checkpoint_storage?: EmbeddedCheckpointStorage | null;
  embedded_risk_assessments?: EmbeddedRiskAssessment[];
  embedded_risk_events?: EmbeddedRiskEvent[];
  embedded_ml?: EmbeddedMlSnapshot;
  embedded_fault_injection?: EmbeddedFaultInjection | null;
  embedded_fault_runs?: EmbeddedFaultInjection[];
};
type ApiError = { message: string };

async function getJson<T>(url: string): Promise<T> {
  const response = await fetch(url, { headers: { Accept: "application/json" } });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(typeof payload.error === "string" ? payload.error : `API returned ${response.status}`);
  }
  return payload as T;
}

function formatTime(value: unknown): string {
  if (typeof value !== "string") return "Unavailable";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "Unavailable"
    : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function formatDateTime(value: unknown): string {
  if (typeof value !== "string") return "Unavailable";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Unavailable" : date.toLocaleString();
}

function describeEmbeddedEvent(event: EmbeddedWorkflowEvent): string {
  switch (event.event) {
    case "uart_command_dispatched":
      return "UART command sent to the active QEMU bridge";
    case "fault_injection_requested":
      return "Firmware received the bounded stack-pressure command";
    case "fault_injected":
      return "Firmware applied stack pressure to sensor-sampler";
    case "embedded_risk_updated":
      return `Backend risk ${event.risk_category ?? "updated"} · reserve ${event.stack_reserve_words ?? "unavailable"} / ${event.threshold_words ?? "unavailable"} words`;
    case "checkpoint_requested":
      return "Firmware received the policy-triggered checkpoint request";
    case "checkpoint_saved":
      return `Checkpoint ${event.checkpoint_id.slice(0, 8)} saved · ${formatBytes(event.checkpoint_size_bytes)} RAM · counter ${formatAppInteger(event.counter)} · app accumulator ${formatAppInteger(event.accumulator)}`;
    case "restore_started":
      return `Application-state restore started from checkpoint ${event.checkpoint_id.slice(0, 8)}`;
    case "task_restart_started":
      return "Replacement sensor-sampler task creation requested";
    case "task_restarted":
      return "Replacement sensor-sampler task created";
    case "restore_succeeded":
      return `Saved application state verified · counter ${event.counter ?? "unavailable"}`;
    case "task_progress":
      return `Restored task progressed · counter ${event.counter ?? "unavailable"}`;
    case "recovery_completed":
      return "Application-state recovery completed";
    default:
      return event.event.replaceAll("_", " ");
  }
}

function MetricValue({ value, suffix = "" }: { value: number | null | undefined; suffix?: string }) {
  return value === null || value === undefined || !Number.isFinite(value)
    ? <>Unavailable</>
    : <>{value.toFixed(1)}{suffix}</>;
}

function StatusDot({ status }: { status: string }) {
  const color = status === "running" ? "bg-emerald-400" : status === "failed" ? "bg-rose-400" : "bg-amber-400";
  return <span className={`inline-block h-2 w-2 rounded-full ${color} ${status === "running" ? "shadow-[0_0_12px_rgba(52,211,153,.7)]" : ""}`} />;
}

function RiskBadge({ category }: { category: string | undefined }) {
  const normalized = category || "Unavailable";
  const classes = normalized === "WARMING_UP"
    ? "border-slate-700 bg-slate-800/60 text-slate-400"
    : normalized === "LOW"
    ? "border-emerald-500/20 bg-emerald-500/10 text-emerald-300"
    : normalized === "MEDIUM" || normalized === "ELEVATED"
      ? "border-amber-500/20 bg-amber-500/10 text-amber-300"
      : normalized === "HIGH" || normalized === "CRITICAL"
        ? "border-rose-500/20 bg-rose-500/10 text-rose-300"
        : "border-slate-700 bg-slate-800/60 text-slate-400";
  return <span className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[10px] font-bold tracking-[.12em] ${classes}`}><span className="h-1.5 w-1.5 rounded-full bg-current" />{normalized}</span>;
}

function EmbeddedRiskCard({
  taskId,
  assessment,
  ml,
  checkpointStatus,
}: {
  taskId: (typeof EMBEDDED_TASK_IDS)[number];
  assessment: EmbeddedRiskAssessment | null;
  ml: EmbeddedMlTaskView;
  checkpointStatus?: EmbeddedCheckpointStatus;
}) {
  const category = assessment?.risk_category ?? "UNKNOWN";
  const riskIndex = getEmbeddedRiskIndex(assessment);
  const categoryClasses = category === "LOW"
    ? "border-emerald-500/20 bg-emerald-500/10 text-emerald-300"
    : category === "ELEVATED"
      ? "border-amber-500/20 bg-amber-500/10 text-amber-300"
      : category === "CRITICAL"
        ? "border-rose-500/20 bg-rose-500/10 text-rose-300"
        : "border-slate-600 bg-slate-800/80 text-slate-300";
  const meterColor = category === "LOW"
    ? "bg-emerald-400"
    : category === "ELEVATED"
      ? "bg-amber-300"
      : category === "CRITICAL"
        ? "bg-rose-400"
        : "bg-slate-500";
  const inputs = assessment?.observed_inputs;
  const configuration = assessment?.policy_configuration;
  const stackWords = inputs?.stack_high_water_mark_words;
  const threshold = configuration?.minimum_stack_high_water_words;
  const age = inputs?.telemetry_age_seconds;
  const freshness = assessment?.telemetry_freshness ?? "unknown";
  const state = freshness === "fresh"
    ? inputs?.task_state ?? "Unavailable"
    : inputs?.task_state
      ? `last reported ${inputs.task_state}`
      : "Unavailable";

  return (
    <article className="rounded-xl border border-slate-800/80 bg-slate-950/40 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="font-mono text-sm font-semibold text-slate-100">{taskId}</h3>
          <div className="mt-1 text-xs text-slate-500">Task state · <span className="text-slate-300">{state}</span></div>
        </div>
        <span className={`rounded-full border px-3 py-1 text-[11px] font-bold tracking-[.12em] ${categoryClasses}`}>{category}</span>
      </div>

      <div className="mt-4 flex items-end justify-between gap-3">
        <div>
          <div className="text-[10px] font-bold uppercase tracking-[.16em] text-slate-600">Policy index</div>
          <div className="mt-1 text-xl font-semibold text-slate-100">{riskIndex === null ? "Unavailable" : `${riskIndex} / 100`}</div>
        </div>
        <div className="text-right text-xs text-slate-500">
          <div>Stack reserve <span className="font-mono text-sm text-slate-200">{stackWords === undefined ? "Unavailable" : `${stackWords} words`}</span></div>
          <div className="mt-1">Threshold <span className="font-mono text-slate-300">{threshold === undefined ? "Unavailable" : `${threshold} words`}</span></div>
        </div>
      </div>
      <div
        className="mt-2 h-2 overflow-hidden rounded-full bg-slate-800"
        role={riskIndex === null ? "img" : "meter"}
        aria-label={riskIndex === null ? `${taskId} policy index unavailable` : `${taskId} policy index`}
        aria-valuemin={riskIndex === null ? undefined : 0}
        aria-valuemax={riskIndex === null ? undefined : 100}
        aria-valuenow={riskIndex ?? undefined}
      >
        {riskIndex !== null && <div className={`h-full rounded-full transition-all ${meterColor}`} style={{ width: `${riskIndex}%` }} />}
      </div>
      <div className="mt-2 flex items-center justify-between text-xs text-slate-500">
        <span>Telemetry age · {age === undefined ? "Unavailable" : `${age.toFixed(1)} s`}</span>
        <span className={freshness === "fresh" ? "text-emerald-300" : freshness === "stale" ? "text-amber-300" : "text-slate-400"}>{freshness}</span>
      </div>
      <div className="mt-3 rounded-lg border border-violet-500/15 bg-slate-900/50 px-3 py-2">
        <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
          <span className="font-medium text-slate-300">ML anomaly score</span>
          <span className="font-mono text-slate-200">
            {typeof ml.assessment?.raw_anomaly_score === "number"
              ? ml.assessment.raw_anomaly_score.toFixed(6)
              : "Unavailable"}
          </span>
        </div>
        <div className="mt-1 flex flex-wrap items-center justify-between gap-2 text-[11px]">
          <span className="text-slate-500">ML prediction</span>
          <span className={
            ml.assessment?.prediction === "anomaly"
              ? "font-semibold text-amber-300"
              : ml.assessment?.prediction === "inlier"
                ? "font-semibold text-emerald-300"
                : "text-slate-400"
          }>
            {ml.assessment?.prediction?.toUpperCase() ?? "UNAVAILABLE"}
          </span>
          <span className={ml.assessment ? "text-emerald-300" : "text-slate-500"}>
            {ml.assessment ? "Fresh" : "Stale / unavailable"}
          </span>
        </div>
        {!ml.assessment && (
          <p className="mt-1 text-[10px] text-slate-500">{ml.unavailableReason}</p>
        )}
      </div>
      <div className="mt-3 space-y-2">
        <p className="text-xs leading-relaxed text-slate-400">
          {assessment?.reason ?? "No backend assessment is available; task health is unknown."}
        </p>
        <p className="text-xs text-slate-500">
          Latest activity · {assessment?.checkpoint_supported
            ? checkpointStatus
              ? `${checkpointStatus.event.replaceAll("_", " ")}${checkpointStatus.checkpoint_id ? ` · ${checkpointStatus.checkpoint_id.slice(0, 8)}` : ""} · ${formatTime(checkpointStatus.occurred_at_utc)}`
              : "none recorded"
            : "control-loop has no checkpoint or restore implementation"}
        </p>
      </div>
    </article>
  );
}

function EmbeddedTaskProfiles({
  tasks,
  assessments,
  backendConnected,
  checkpointStatus,
  checkpointStorage,
}: {
  tasks: EmbeddedTaskTelemetry[];
  assessments: EmbeddedRiskAssessment[];
  backendConnected: boolean;
  checkpointStatus: EmbeddedCheckpointStatus[];
  checkpointStorage: EmbeddedCheckpointStorage | null;
}) {
  return (
    <div className="mt-4 grid gap-3 md:grid-cols-2">
      {EMBEDDED_TASK_IDS.map((taskId) => {
        const task = tasks.find((item) => item.task_id === taskId);
        const assessment = assessments.find((item) => item.task_id === taskId);
        const effectiveAssessment = backendConnected ? assessment : undefined;
        const activity = checkpointStatus.find((item) => item.task_id === taskId);
        const isFresh = effectiveAssessment?.telemetry_freshness === "fresh";
        const taskFreshness = effectiveAssessment?.telemetry_freshness ?? "unknown";
        const statusClass = !task || !isFresh
          ? "border-slate-600 bg-slate-800/80 text-slate-300"
          : task.task_state === "running"
            ? "border-emerald-500/20 bg-emerald-500/10 text-emerald-300"
            : "border-amber-500/20 bg-amber-500/10 text-amber-300";

        return (
          <article key={taskId} className="rounded-xl border border-slate-800 bg-slate-950/40 p-4">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <h3 className="font-mono text-base font-semibold text-slate-100">{taskId}</h3>
                <p className="mt-1 text-xs text-slate-500">
                  {backendConnected
                    ? `${formatTime(task?.received_at_utc)} · ${assessment?.telemetry_freshness ?? "unknown"} telemetry`
                    : `Backend disconnected · last observation ${formatTime(task?.received_at_utc)}`}
                </p>
              </div>
              <span className={`rounded-full border px-3 py-1 text-xs font-semibold ${statusClass}`}>
                {isFresh
                  ? task?.task_state ?? "UNKNOWN"
                  : `UNKNOWN · ${backendConnected ? taskFreshness : "disconnected"}`}
              </span>
            </div>
            <div className="mt-4 grid grid-cols-2 gap-x-4 gap-y-3 text-xs">
              <div>
                <div className="text-slate-500">{isFresh ? "Stack reserve / threshold" : "Last reserve / threshold"}</div>
                <div className="mt-1 font-mono text-sm text-slate-200">
                  {task ? `${task.stack_high_water_mark_words} / ${effectiveAssessment?.policy_configuration?.minimum_stack_high_water_words ?? "Unavailable"} words` : "Unavailable"}
                </div>
              </div>
              <div>
                <div className="text-slate-500">{isFresh ? "Execution / tick" : "Last recorded execution / tick"}</div>
                <div className="mt-1 font-mono text-sm text-slate-200">
                  {task ? `${formatAppInteger(task.execution_count)} / ${formatAppInteger(task.tick_count)}` : "Unavailable"}
                </div>
              </div>
              <div>
                <div className="text-slate-500">Policy category</div>
                <div className="mt-1"><RiskBadge category={effectiveAssessment?.risk_category} /></div>
              </div>
              <div>
                <div className="text-slate-500">Policy index</div>
                <div className="mt-1 font-mono text-sm text-slate-200">
                  {getEmbeddedRiskIndex(effectiveAssessment ?? null) === null ? "Unavailable" : `${getEmbeddedRiskIndex(effectiveAssessment ?? null)} / 100`}
                </div>
              </div>
            </div>
            <p className="mt-3 text-xs leading-relaxed text-slate-400">
              {backendConnected
                ? effectiveAssessment?.reason ?? "No backend assessment; task health is unknown."
                : "Backend disconnected; displayed telemetry is historical and current task health is unknown."}
            </p>
            <p className="mt-2 text-xs text-slate-500">
              {taskId === "sensor-sampler"
                ? checkpointStorage
                  ? `Latest QEMU RAM checkpoint ${checkpointStorage.checkpoint_id ?? "ID unavailable"} · ${formatTime(checkpointStorage.created_at_utc)}`
                  : "No confirmed application-state checkpoint in this QEMU session."
                : "Checkpoint and restore are unsupported for control-loop."}
              {activity && ` · latest activity: ${activity.event.replaceAll("_", " ")}`}
            </p>
          </article>
        );
      })}
    </div>
  );
}

function HealthBadge({ health }: { health: string | undefined }) {
  const failed = health === "FAILED";
  const healthy = health === "HEALTHY";
  const classes = healthy ? "text-emerald-300" : failed ? "text-rose-300" : "text-amber-300";
  const Icon = healthy ? ShieldCheck : failed ? ShieldAlert : AlertTriangle;
  return <span className={`inline-flex items-center gap-1.5 text-xs font-medium ${classes}`}><Icon size={14} />{health || "Unavailable"}</span>;
}

function MetricCard({ label, value, detail, icon: Icon, tone = "cyan" }: {
  label: string;
  value: React.ReactNode;
  detail: string;
  icon: typeof Activity;
  tone?: string;
}) {
  const toneClass = tone === "rose" ? "text-rose-300 bg-rose-400/10" : tone === "amber" ? "text-amber-300 bg-amber-400/10" : tone === "violet" ? "text-violet-300 bg-violet-400/10" : "text-cyan-300 bg-cyan-400/10";
  return (
    <article className="panel relative overflow-hidden p-5">
      <div className="flex items-start justify-between">
        <div className="text-[10px] font-bold uppercase tracking-[.18em] text-slate-500">{label}</div>
        <span className={`grid h-9 w-9 place-items-center rounded-xl ${toneClass}`}><Icon size={17} /></span>
      </div>
      <div className="mt-4 text-[26px] font-semibold tracking-tight text-slate-100">{value}</div>
      <div className="mt-1 text-xs text-slate-500">{detail}</div>
      <div className="pointer-events-none absolute -bottom-8 -right-6 h-20 w-20 rounded-full bg-white/[0.02] blur-xl" />
    </article>
  );
}

function SparkChart({ title, detail, series, unit, color }: {
  title: string;
  detail: string;
  series: { label: string; value: number | null }[];
  unit: string;
  color: string;
}) {
  const values = series.map((item) => item.value).filter((item): item is number => item !== null && Number.isFinite(item));
  const latest = values.length ? values[values.length - 1] : null;
  const path = useMemo(() => {
    if (series.length < 2 || values.length < 2) return "";
    const valid = series.map((point, index) => ({ ...point, index })).filter((point) => point.value !== null && Number.isFinite(point.value));
    if (valid.length < 2) return "";
    const min = Math.min(...valid.map((point) => point.value as number));
    const max = Math.max(...valid.map((point) => point.value as number));
    const span = Math.max(max - min, Math.abs(max) * 0.08, 1);
    return valid.map((point, index) => {
      const x = 8 + (point.index / (series.length - 1)) * 584;
      const y = 106 - (((point.value as number) - min) / span) * 82;
      return `${index === 0 ? "M" : "L"} ${x.toFixed(1)} ${y.toFixed(1)}`;
    }).join(" ");
  }, [series, values.length]);
  return (
    <section className="panel min-w-0 p-5">
      <div className="flex items-start justify-between gap-3">
        <div><h3 className="text-sm font-semibold text-slate-200">{title}</h3><p className="mt-1 text-xs text-slate-500">{detail}</p></div>
        <div className="text-right text-lg font-semibold text-slate-100">
          {latest === null
            ? "Unavailable"
            : `${unit === " words" || unit === " samples" ? Math.round(latest).toLocaleString() : latest.toFixed(1)}${unit}`}
          <div className="mt-1 text-[10px] font-medium uppercase tracking-wider text-slate-500">latest sample</div>
        </div>
      </div>
      <div className="mt-5 h-36">
        {path ? (
          <svg className="h-full w-full overflow-visible" viewBox="0 0 600 120" role="img" aria-label={`${title} chart from observed telemetry`}>
            <defs><linearGradient id={`fill-${color}`} x1="0" x2="0" y1="0" y2="1"><stop offset="0%" stopColor={color} stopOpacity=".20" /><stop offset="100%" stopColor={color} stopOpacity="0" /></linearGradient></defs>
            {[24, 65, 106].map((y) => <line key={y} x1="8" x2="592" y1={y} y2={y} stroke="#202938" strokeDasharray="3 6" />)}
            <path d={`${path} L 592 112 L 8 112 Z`} fill={`url(#fill-${color})`} />
            <path d={path} fill="none" stroke={color} strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        ) : (
          <div className="grid h-full place-items-center rounded-xl border border-dashed border-slate-800 px-4 text-center text-xs text-slate-500">At least two real telemetry samples are needed to draw this history.</div>
        )}
      </div>
      <div className="mt-2 flex justify-between text-[10px] text-slate-600"><span>{series[0]?.label || "—"}</span><span>{series.at(-1)?.label || "—"}</span></div>
    </section>
  );
}

function App() {
  const [data, setData] = useState<DashboardData | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(true);
  const [selectedWorker, setSelectedWorker] = useState("");
  const [embeddedFaultPending, setEmbeddedFaultPending] = useState(false);
  const [embeddedFaultError, setEmbeddedFaultError] = useState<string | null>(null);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const refreshing = useRef(false);

  const refresh = useCallback(async () => {
    if (refreshing.current) return;
    refreshing.current = true;
    try {
      const dashboard = await getJson<DashboardData>(`${API_ROOT}/dashboard`);
      setData(dashboard);
      setError(null);
      setLastUpdated(new Date());
      setSelectedWorker((current) => {
        if (dashboard.workers.some((worker) => worker.worker_id === current && worker.digital_twin?.health_state !== "FAILED")) return current;
        return dashboard.workers.find((worker) => worker.digital_twin?.health_state !== "FAILED")?.worker_id || "";
      });
    } catch (caught) {
      setError({ message: caught instanceof Error ? caught.message : "Could not reach the TwinCoreOS API" });
    } finally {
      setLoading(false);
      refreshing.current = false;
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 1000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const connected = data?.status.status === "running" && !error;
  const liveWorkers = data?.workers.filter((worker) => worker.telemetry && worker.digital_twin?.health_state !== "FAILED") || [];
  const cpus = liveWorkers.map((worker) => worker.telemetry?.cpu_utilization_percent_one_core).filter((value): value is number => value !== null && value !== undefined);
  const cpuAverage = cpus.length ? cpus.reduce((sum, value) => sum + value, 0) / cpus.length : null;
  const rssValues = liveWorkers
    .map((worker) => worker.telemetry?.memory_rss_bytes)
    .filter((value): value is number => value !== null && value !== undefined);
  const rssTotal = rssValues.length ? rssValues.reduce((sum, value) => sum + value, 0) : null;
  const rssLabel = rssTotal === null
    ? "Unavailable"
    : `${rssValues.length < liveWorkers.length ? "Partial · " : ""}${formatBytes(rssTotal)}`;
  const risks = data?.risks.assessments || [];
  const highRisk = risks.filter((risk) => risk.risk_category === "HIGH").length;
  const history = data?.telemetry.history || [];
  const seriesFor = (metric: "cpu_utilization_percent_one_core" | "memory_rss_bytes") => history.filter((item) => item.task_id === selectedWorker).slice(-48).map((item) => ({
    label: formatTime(item.captured_at_utc),
    value: item[metric],
  }));
  const eventList = [...(data?.events.events || [])]
    .filter((event) => event.type.startsWith("embedded_"))
    .reverse();
  const storage = data?.checkpoints.storage;
  const usagePercent = storage && storage.storage_budget_bytes > 0
    ? Math.min(100, (storage.storage_usage_bytes / storage.storage_budget_bytes) * 100)
    : null;
  const embeddedTasks = data?.embedded_tasks?.latest ?? [];
  const embeddedTaskHistory = data?.embedded_tasks?.history ?? [];
  const embeddedAssessments = data?.embedded_risk_assessments ?? [];
  const embeddedFreshCount = connected ? embeddedAssessments.filter(
    (assessment) => assessment.telemetry_freshness === "fresh",
  ).length : null;
  const embeddedElevatedCount = connected ? embeddedAssessments.filter(
    (assessment) => assessment.risk_category === "ELEVATED" || assessment.risk_category === "CRITICAL",
  ).length : null;
  const embeddedCheckpoint = data?.embedded_checkpoint_storage ?? null;
  const workflowRuns = data?.embedded_fault_runs ?? [];
  const currentBridgeSessionId = embeddedTasks.find(
    (task) => task.task_id === "sensor-sampler",
  )?.bridge_session_id;
  const currentSessionCheckpointRun = getCurrentSessionCheckpointRun(
    workflowRuns,
    currentBridgeSessionId,
  );
  const confirmedSaveEvent = currentSessionCheckpointRun?.events.find(
    (event) => event.event === "checkpoint_saved",
  );
  const savedApplicationState = currentSessionCheckpointRun?.checkpoint_saved_state;
  const eventReportedSize = confirmedSaveEvent?.checkpoint_size_bytes;
  const savedRecordSize = typeof savedApplicationState?.checkpoint_size_bytes === "number"
    ? savedApplicationState.checkpoint_size_bytes
    : typeof eventReportedSize === "number" ? eventReportedSize : null;
  const checkpointFromCurrentSessionEvent = (
    currentSessionCheckpointRun
    && confirmedSaveEvent
    && savedApplicationState
  ) ? {
    task_id: "sensor-sampler" as const,
    checkpoint_id: currentSessionCheckpointRun.checkpoint_id || currentSessionCheckpointRun.run_id,
    run_id: currentSessionCheckpointRun.run_id,
    bridge_session_id: currentSessionCheckpointRun.bridge_session_id,
    created_at_utc: confirmedSaveEvent.occurred_at_utc,
    size_bytes: savedRecordSize,
    storage_used_bytes: savedRecordSize,
    storage_budget_bytes: null,
    storage_kind: "volatile_firmware_ram" as const,
    storage_note: savedRecordSize === null
      ? "Current-session checkpoint save is confirmed by the firmware event; this event did not report record size. One volatile RAM slot; no byte budget is configured."
      : "Current-session checkpoint save is confirmed by the firmware event. One volatile RAM slot; no byte budget is configured.",
    application_state: {
      counter: savedApplicationState.counter,
      accumulator: savedApplicationState.accumulator,
      integrity_hash: savedApplicationState.integrity_hash,
    },
  } : null;
  const directCheckpointBelongsToCurrentSession = embeddedCheckpoint !== null
    && embeddedCheckpoint.bridge_session_id === currentBridgeSessionId;
  const currentSessionCheckpoint = directCheckpointBelongsToCurrentSession
    ? embeddedCheckpoint
    : checkpointFromCurrentSessionEvent;
  const checkpointBelongsToCurrentSession = currentSessionCheckpoint !== null;
  const embeddedSeriesFor = (
    taskId: (typeof EMBEDDED_TASK_IDS)[number],
    metric: "stack_high_water_mark_words" | "execution_count",
  ) => embeddedTaskHistory
    .filter((item) => item.task_id === taskId)
    .slice(-48)
    .map((item) => ({
      label: formatTime(item.received_at_utc),
      value: item[metric],
    }));
  const embeddedFaultRequest = data?.embedded_fault_injection ?? null;
  const embeddedRequestActive = embeddedFaultRequest !== null
    && !["application_state_recovery_verified", "failed"].includes(embeddedFaultRequest.status);
  const latestCompletedWorkflowRun = [...workflowRuns].reverse().find(
    (run) => ["application_state_recovery_verified", "failed"].includes(run.status),
  ) ?? null;
  const latestHistoricalCheckpointRun = getLatestConfirmedCheckpointRun(workflowRuns);
  const runRiskEvents: EmbeddedRiskEvent[] = workflowRuns.flatMap((run) =>
    run.events
      .filter((event) => event.event === "embedded_risk_updated")
      .map((event) => ({
        task_id: run.task_id,
        risk_category: event.risk_category === "ELEVATED" ? "ELEVATED" : "UNKNOWN",
        occurred_at_utc: event.occurred_at_utc,
        bridge_session_id: run.bridge_session_id,
        execution_count: event.execution_count ?? 0,
        run_id: run.run_id,
        stack_reserve_words: event.stack_reserve_words,
        threshold_words: event.threshold_words,
        risk_index: event.risk_index ?? null,
        reason: event.reason ?? "Risk transition retained in recovery event history",
      })),
  );
  const retainedRiskEvents = data?.embedded_risk_events?.length
    ? data.embedded_risk_events
    : runRiskEvents;
  const recentElevatedRiskEvent = getLatestElevatedRiskEvent(
    [...retainedRiskEvents].sort(
      (left, right) => Date.parse(left.occurred_at_utc) - Date.parse(right.occurred_at_utc),
    ),
  );
  const embeddedFaultReady = canRequestNextEmbeddedFault(
    data?.embedded_risk_assessments,
    data?.embedded_tasks?.latest,
    workflowRuns.at(-1) ?? null,
  );

  async function injectEmbeddedFault() {
    if (!embeddedFaultReady || embeddedRequestActive || embeddedFaultPending) return;
    setEmbeddedFaultPending(true);
    setEmbeddedFaultError(null);
    try {
      const response = await fetch(`${API_ROOT}/embedded/fault-injection`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ task_id: "sensor-sampler", confirm: true }),
      });
      const payload = await response.json();
      if (!response.ok || payload.success !== true || !payload.request) {
        throw new Error(typeof payload.error === "string" ? payload.error : `Embedded fault request failed (${response.status})`);
      }
      await refresh();
    } catch (caught) {
      setEmbeddedFaultError(caught instanceof Error ? caught.message : "Embedded UART fault request failed");
      await refresh();
    } finally {
      setEmbeddedFaultPending(false);
    }
  }

  return (
    <div className="min-h-screen bg-[#080b12] text-slate-200">
      <div className="flex min-h-screen">
        <aside className="hidden w-[248px] shrink-0 border-r border-slate-800/80 bg-[#0a0e16] px-5 py-6 lg:flex lg:flex-col">
          <div className="flex items-center gap-3 px-2">
            <div className="grid h-10 w-10 place-items-center rounded-xl border border-cyan-400/20 bg-cyan-400/10 text-cyan-300"><Activity size={20} /></div>
            <div><div className="text-[15px] font-bold tracking-tight text-white">TwinCore<span className="text-cyan-300">OS</span></div><div className="mt-0.5 text-[9px] font-semibold uppercase tracking-[.2em] text-slate-500">Operations console</div></div>
          </div>
          <div className="mt-10 px-3 text-[9px] font-bold uppercase tracking-[.2em] text-slate-600">Workspace</div>
          <nav className="mt-3 space-y-1" aria-label="Main navigation">
            <a href="#overview" className="nav-item nav-active"><LayoutDashboard size={16} />Overview</a>
            <a href="#twins" className="nav-item"><Network size={16} />Task profiles</a>
            <a href="#risk" className="nav-item"><Shield size={16} />Risk analysis</a>
            <a href="#checkpoints" className="nav-item"><Database size={16} />Checkpoints</a>
            <a href="#host-monitoring" className="nav-item"><Cpu size={16} />Host diagnostics</a>
            <a href="#events" className="nav-item"><Clock3 size={16} />Event stream</a>
          </nav>
          <div className="mt-auto rounded-xl border border-slate-800 bg-slate-900/50 p-4">
            <div className="flex items-center gap-2 text-xs font-semibold text-slate-300"><ShieldCheck size={14} className="text-cyan-300" />Embedded resilience</div>
            <p className="mt-2 text-[11px] leading-relaxed text-slate-500">Live FreeRTOS task telemetry, risk-triggered checkpointing, and verified application-state recovery.</p>
          </div>
          <div className="mt-4 px-2 text-[10px] text-slate-600">TwinCoreOS <span className="float-right">MILESTONE 06</span></div>
        </aside>

        <main id="overview" className="min-w-0 flex-1">
          <header className="sticky top-0 z-20 flex h-[72px] items-center justify-between border-b border-slate-800/80 bg-[#080b12]/90 px-5 backdrop-blur-xl md:px-8">
            <div className="flex items-center gap-3">
              <div className="grid h-9 w-9 place-items-center rounded-lg border border-cyan-400/20 bg-cyan-400/10 text-cyan-300 lg:hidden"><Activity size={18} /></div>
              <div><div className="text-sm font-semibold text-slate-100">Embedded resilience</div><div className="mt-1 hidden text-[11px] text-slate-500 sm:block">Live FreeRTOS telemetry · host diagnostics secondary</div></div>
            </div>
            <div className="flex items-center gap-3">
              <div className={`flex items-center gap-2 rounded-full border px-3 py-1.5 text-[11px] font-medium ${connected ? "border-emerald-500/20 bg-emerald-500/[0.06] text-emerald-300" : "border-rose-500/20 bg-rose-500/[0.06] text-rose-300"}`}>
                <StatusDot status={connected ? "running" : "failed"} />{connected ? "Backend connected" : "Disconnected"}
              </div>
              <button onClick={() => void refresh()} aria-label="Refresh live data" className="grid h-9 w-9 place-items-center rounded-lg border border-slate-800 text-slate-400 transition hover:border-slate-700 hover:text-white"><RefreshCw size={15} /></button>
            </div>
          </header>

          <div className="mx-auto max-w-[1600px] space-y-6 px-5 py-7 md:px-8">
            {error && <div className="flex items-center gap-3 rounded-xl border border-rose-500/20 bg-rose-500/[0.07] px-4 py-3 text-sm text-rose-200" role="alert"><AlertTriangle size={17} /><span>{error.message}. Live data will retry automatically.</span></div>}
            {loading && !data && <div className="panel flex min-h-52 items-center justify-center gap-3 text-sm text-slate-400"><LoaderCircle size={18} className="animate-spin text-cyan-300" />Connecting to live backend…</div>}

            {data && <>
              {data.status.status !== "running" && <div className="flex items-center gap-3 rounded-xl border border-amber-500/20 bg-amber-500/[0.07] px-4 py-3 text-sm text-amber-200"><AlertTriangle size={17} />Monitor status: {String(data.status.status)}{typeof data.status.error === "string" ? ` · ${data.status.error}` : ""}</div>}
              <div className="flex flex-col justify-between gap-4 sm:flex-row sm:items-end">
                <div><div className="flex items-center gap-2 text-[10px] font-bold uppercase tracking-[.2em] text-cyan-300"><span className="h-1.5 w-1.5 rounded-full bg-cyan-300" />FreeRTOS / live overview</div><h1 className="mt-2 text-2xl font-semibold tracking-tight text-white md:text-[28px]">Embedded task resilience</h1><p className="mt-1 text-xs text-slate-500">Firmware-reported task state, stack reserve, policy risk, and application-state recovery</p></div>
                <div className="flex items-center gap-2 text-[11px] text-slate-500"><span className="h-1.5 w-1.5 rounded-full bg-emerald-400" />Last update {lastUpdated?.toLocaleTimeString() || "—"}</div>
              </div>

              <section className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4" aria-label="Embedded task metrics">
                <MetricCard label="Task identities" value={`${embeddedTasks.length} / 2`} detail="sensor-sampler · control-loop" icon={Network} />
                <MetricCard label="Fresh telemetry" value={embeddedFreshCount === null ? "Unavailable" : `${embeddedFreshCount} / 2`} detail={connected ? "Backend freshness assessment" : "Backend disconnected"} icon={Signal} tone="violet" />
                <MetricCard label="Elevated task risk" value={embeddedElevatedCount === null ? "Unavailable" : String(embeddedElevatedCount)} detail={connected ? "Current embedded policy results" : "Backend disconnected"} icon={AlertTriangle} tone="amber" />
                <MetricCard label="Current-session checkpoint" value={!connected ? "Unavailable" : checkpointBelongsToCurrentSession ? "Saved" : embeddedTasks.length ? "Empty" : "Unknown"} detail={connected ? "One volatile QEMU RAM slot · no byte budget" : "Backend disconnected"} icon={Database} />
              </section>

              <section className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4" aria-label="Embedded task telemetry history">
                <SparkChart title="sensor-sampler · stack reserve" detail={connected ? "Measured FreeRTOS high-water reserve" : "Historical telemetry · backend disconnected"} series={embeddedSeriesFor("sensor-sampler", "stack_high_water_mark_words")} unit=" words" color="#36c9e8" />
                <SparkChart title="sensor-sampler · progress" detail={connected ? "Firmware execution counter" : "Historical telemetry · backend disconnected"} series={embeddedSeriesFor("sensor-sampler", "execution_count")} unit=" samples" color="#9a84ff" />
                <SparkChart title="control-loop · stack reserve" detail={connected ? "Measured FreeRTOS high-water reserve" : "Historical telemetry · backend disconnected"} series={embeddedSeriesFor("control-loop", "stack_high_water_mark_words")} unit=" words" color="#36c9e8" />
                <SparkChart title="control-loop · progress" detail={connected ? "Firmware execution counter" : "Historical telemetry · backend disconnected"} series={embeddedSeriesFor("control-loop", "execution_count")} unit=" samples" color="#9a84ff" />
              </section>

              <section id="twins" className="panel p-5">
                <div className="flex items-start justify-between gap-3">
                  <div><h2 className="text-base font-semibold text-slate-100">Embedded task profiles</h2><p className="mt-1 text-xs text-slate-500">Live UART telemetry and backend assessment for the two FreeRTOS task identities</p></div>
                  <Network size={17} className="shrink-0 text-cyan-300" />
                </div>
                <EmbeddedTaskProfiles
                  tasks={embeddedTasks}
                  assessments={embeddedAssessments}
                  backendConnected={connected}
                  checkpointStatus={data.embedded_checkpoint_status ?? []}
                  checkpointStorage={currentSessionCheckpoint}
                />
              </section>

              <section id="risk" className="panel p-5">
                  <div className="flex items-start justify-between gap-3">
                    <div><h2 className="text-sm font-semibold text-slate-100">Risk assessment · FreeRTOS tasks</h2><p className="mt-1 text-[11px] text-slate-500">Backend embedded policy · host worker CPU, RSS, PIDs, and Isolation Forest are excluded</p></div>
                    <Shield size={17} className="shrink-0 text-violet-300" />
                  </div>
                  <p className="mt-2 text-xs text-slate-500">Index is a discrete policy state, not a failure probability; missing or stale telemetry is UNKNOWN.</p>
                  <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-slate-500">
                    <span className="rounded-full border border-violet-400/20 bg-violet-400/[.06] px-2 py-1 text-violet-200">
                      {data.embedded_ml?.shadow_only && !data.embedded_ml.checkpoint_commands_enabled
                        ? "ML shadow mode · informational only"
                        : "ML shadow/actuation status unavailable"}
                    </span>
                    <span>
                      {data.embedded_ml?.model.available
                        ? `Model loaded · ${data.embedded_ml.model.model_version ?? "version unavailable"}`
                        : `Model unavailable · ${data.embedded_ml?.model.reason ?? "No model status from backend"}`}
                    </span>
                    <span>{data.embedded_ml?.shadow_only && !data.embedded_ml.checkpoint_commands_enabled
                      ? "Threshold policy remains authoritative; ML does not issue checkpoint commands."
                      : "Checkpoint behavior follows the backend threshold policy."}</span>
                  </div>
                  <div className="mt-4 grid gap-3 md:grid-cols-2">
                    {getEmbeddedRiskRows(data.embedded_risk_assessments).map(({ task_id, assessment }) => {
                      const task = embeddedTasks.find((item) => item.task_id === task_id);
                      const effectiveAssessment = connected ? assessment : null;
                      return (
                        <EmbeddedRiskCard
                          key={task_id}
                          taskId={task_id}
                          assessment={effectiveAssessment}
                          ml={getEmbeddedMlTaskView(
                            data.embedded_ml,
                            task,
                            effectiveAssessment,
                            connected,
                          )}
                          checkpointStatus={data.embedded_checkpoint_status?.find((status) => status.task_id === task_id)}
                        />
                      );
                    })}
                  </div>
                  <div className="mt-4 flex flex-wrap gap-x-4 gap-y-2 border-t border-slate-800/80 pt-3 text-[9px] text-slate-500">
                    {[
                      ["LOW", "bg-emerald-400"],
                      ["ELEVATED", "bg-amber-300"],
                      ["CRITICAL", "bg-rose-400"],
                      ["UNKNOWN", "bg-slate-400"],
                    ].map(([category, color]) => <span key={category} className="flex items-center gap-1.5"><span className={`h-1.5 w-1.5 rounded-full ${color}`} />{category}</span>)}
                  </div>
                  {recentElevatedRiskEvent && (
                    <div className="mt-3 rounded-lg border border-amber-400/20 bg-amber-400/[.05] px-3 py-2 text-xs" aria-live="polite">
                      <div className="font-semibold text-amber-200">Recent risk event · {recentElevatedRiskEvent.task_id}</div>
                      <div className="mt-1 text-slate-300">
                        {recentElevatedRiskEvent.previous_category ? `${recentElevatedRiskEvent.previous_category} → ` : ""}ELEVATED · reserve {recentElevatedRiskEvent.stack_reserve_words ?? "Unavailable"} / {recentElevatedRiskEvent.threshold_words ?? "Unavailable"} words · index {recentElevatedRiskEvent.risk_index ?? "Unavailable"}
                      </div>
                      <div className="mt-1 text-[10px] text-slate-500">{formatDateTime(recentElevatedRiskEvent.occurred_at_utc)} · live status remains based on latest telemetry</div>
                    </div>
                  )}
                  <p className="mt-2 text-[10px] text-slate-500">CRITICAL has no implemented rule. Host-process risk is separate below.</p>
              </section>

              <section id="embedded-recovery" className="panel p-5">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <h2 className="text-base font-semibold text-slate-100">Embedded recovery demonstration</h2>
                    <p className="mt-1 text-xs text-slate-500">Policy-triggered sensor-sampler checkpoint and application-state recovery over QEMU UART</p>
                  </div>
                  <Network size={17} className="text-violet-300" />
                </div>
                <p className="mt-3 max-w-4xl text-xs leading-relaxed text-slate-400">
                  The bounded stack-pressure demo lowers measured reserve; the backend policy then requests checkpointing and replacement before task termination. Recovery validates application state only—not task stacks, registers, or kernel state.
                </p>
                <button
                  onClick={() => void injectEmbeddedFault()}
                  disabled={!connected || !embeddedFaultReady || embeddedRequestActive || embeddedFaultPending}
                  className="mt-4 flex min-h-11 w-full items-center justify-center gap-2 rounded-lg border border-violet-400/30 bg-violet-500/15 px-4 py-2.5 text-sm font-semibold text-violet-100 transition hover:bg-violet-500/25 disabled:cursor-not-allowed disabled:border-slate-800 disabled:bg-slate-900 disabled:text-slate-500 sm:w-auto"
                >
                  {embeddedFaultPending ? <LoaderCircle size={15} className="animate-spin" /> : <Zap size={15} />}
                  {embeddedRequestActive ? "Embedded recovery in progress" : "Inject fault & verify application-state recovery"}
                </button>
                {!embeddedFaultReady && (
                  <p className="mt-2 text-xs text-amber-300">
                    {embeddedFaultRequest?.status === "failed"
                      ? "The last run failed; inspect its evidence and restart the bridge before retrying."
                      : embeddedFaultRequest?.status === "application_state_recovery_verified"
                        ? "Waiting for fresh sensor-sampler telemetry to advance beyond restored progress."
                        : "Waiting for fresh, running LOW-risk telemetry from both embedded tasks."}
                  </p>
                )}
                {embeddedFaultError && <p className="mt-2 text-[10px] text-rose-300" role="alert">{embeddedFaultError}</p>}
                {workflowRuns.length === 0
                  ? <p className="mt-4 text-xs text-slate-500">No embedded recovery runs recorded.</p>
                  : <div className="mt-5 space-y-2" aria-live="polite">
                    {[...workflowRuns].reverse().map((run, index) => {
                      const verified = run.status === "application_state_recovery_verified";
                      const failed = run.status === "failed";
                      const riskEvent = run.events.find((event) => event.event === "embedded_risk_updated");
                      return <details key={run.run_id} open={index === 0} className="group rounded-xl border border-slate-800 bg-slate-950/50">
                        <summary className="flex cursor-pointer list-none flex-wrap items-center justify-between gap-2 p-4 marker:hidden">
                          <div className="min-w-0 flex-1">
                            <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                              <span className="text-sm font-semibold text-slate-200">sensor-sampler · {formatDateTime(run.requested_at_utc)}</span>
                              {index === 0 && <span className="rounded bg-cyan-400/10 px-2 py-0.5 text-[10px] font-semibold text-cyan-200">Latest run</span>}
                              {run.run_id === latestCompletedWorkflowRun?.run_id && <span className="rounded bg-emerald-400/10 px-2 py-0.5 text-[10px] font-semibold text-emerald-200">Latest completed</span>}
                            </div>
                            <div className="mt-1 break-all font-mono text-[10px] text-slate-500">
                              {riskEvent ? `${riskEvent.risk_category ?? "ELEVATED"} risk · ${riskEvent.stack_reserve_words ?? "?"}/${riskEvent.threshold_words ?? "?"} words` : "Risk transition not confirmed"} · checkpoint {run.checkpoint_saved_state ? run.run_id : "not saved"}
                            </div>
                          </div>
                          <span className={`rounded-full border px-3 py-1 text-xs font-semibold ${failed ? "border-rose-500/30 bg-rose-500/10 text-rose-300" : verified ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-300" : "border-violet-500/30 bg-violet-500/10 text-violet-200"}`}>
                            {run.status.replaceAll("_", " ")}
                          </span>
                          <span className="sr-only">Expand or collapse recovery run details</span>
                        </summary>
                        <div className="border-t border-slate-800/80 p-4">
                          <p className="break-all font-mono text-[10px] text-slate-500">Run / checkpoint ID · {run.run_id}</p>
                          {riskEvent && <p className="mt-2 text-xs text-amber-200">{formatDateTime(riskEvent.occurred_at_utc)} · ELEVATED · measured reserve {riskEvent.stack_reserve_words ?? "Unavailable"} / {riskEvent.threshold_words ?? "Unavailable"} words · index {riskEvent.risk_index ?? "Unavailable"}</p>}
                          {run.error && <p className="mt-2 text-xs text-rose-300">{run.error}</p>}
                          <div className="mt-3 grid gap-2 text-xs sm:grid-cols-3">
                            <div className="rounded-lg bg-slate-900/70 p-2 text-slate-400">
                              <span className="block text-[10px] text-slate-500">Checkpoint saved</span>
                              <span className="mt-1 block break-words text-sm text-slate-200">{run.checkpoint_saved_state ? `counter ${formatAppInteger(run.checkpoint_saved_state.counter)} · accumulator ${formatAppInteger(run.checkpoint_saved_state.accumulator)}` : "Not confirmed"}</span>
                              {run.checkpoint_saved_state && <span className="mt-1 block text-[10px] text-slate-500">Firmware-confirmed record · {formatBytes(run.checkpoint_saved_state.checkpoint_size_bytes)}</span>}
                            </div>
                            <div className="rounded-lg bg-slate-900/70 p-2 text-slate-400">
                              <span className="block text-[10px] text-slate-500">Restore verified</span>
                              <span className="mt-1 block break-words text-sm text-slate-200">{run.restored_state ? `counter ${formatAppInteger(run.restored_state.counter)} · accumulator ${formatAppInteger(run.restored_state.accumulator)}` : "Not verified"}</span>
                              <span className="mt-1 block text-[10px] text-slate-500">Accumulator is an app-defined integer</span>
                            </div>
                            <div className="rounded-lg bg-slate-900/70 p-2 text-slate-400">
                              <span className="block text-[10px] text-slate-500">Post-restore progress</span>
                              <span className="mt-1 block break-words text-sm text-slate-200">{run.restored_progress ? `counter ${formatAppInteger(run.restored_progress.counter)} · accumulator ${formatAppInteger(run.restored_progress.accumulator)}` : "Not verified"}</span>
                              <span className="mt-1 block text-[10px] text-slate-500">Fresh progress after restored state</span>
                            </div>
                          </div>
                          <ol className="mt-3 space-y-2 border-l border-slate-700 pl-3">
                            {run.events.map((event, eventIndex) => (
                              <li key={`${run.run_id}-${event.event}-${eventIndex}`} className="relative flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1 text-xs">
                                <span className="min-w-0 flex-1 text-slate-300">{describeEmbeddedEvent(event)}</span>
                                <time className="shrink-0 text-[10px] text-slate-500">{formatDateTime(event.occurred_at_utc)}</time>
                              </li>
                            ))}
                          </ol>
                        </div>
                      </details>;
                    })}
                  </div>}
              </section>

              <section id="checkpoints" className="panel overflow-hidden">
                <div className="flex flex-wrap items-start justify-between gap-3 px-5 py-4">
                  <div>
                    <h2 className="text-base font-semibold text-slate-100">Embedded checkpoint storage</h2>
                    <p className="mt-1 text-xs text-slate-500">Latest firmware-confirmed sensor-sampler checkpoint · volatile QEMU RAM</p>
                  </div>
                  <div className="text-right text-xs text-slate-400">
                    <div>{currentSessionCheckpoint
                      ? currentSessionCheckpoint.storage_used_bytes === null
                        ? "Firmware save confirmed · size not reported"
                        : `Firmware save confirmed · ${formatBytes(currentSessionCheckpoint.storage_used_bytes)}`
                      : "No current-session save confirmed"}</div>
                    <div className="mt-1 text-slate-500">One volatile slot · byte budget not configured</div>
                  </div>
                </div>
                {!checkpointBelongsToCurrentSession || !currentSessionCheckpoint ? (
                  <div className="space-y-3 border-t border-slate-800/70 px-5 py-5">
                    <p className="text-sm text-slate-400">
                      {embeddedTasks.length
                        ? "No checkpoint is confirmed in the current QEMU session. Its RAM slot may be empty or have been lost when QEMU restarted."
                        : "No active QEMU task telemetry is available to establish current-session checkpoint state."}
                    </p>
                    {latestHistoricalCheckpointRun?.checkpoint_saved_state && (
                      <div className="rounded-lg border border-slate-800 bg-slate-950/60 p-3">
                        <div className="text-xs font-semibold text-slate-300">Latest historical firmware confirmation · not live RAM</div>
                        <div className="mt-1 break-all font-mono text-[10px] text-slate-500">sensor-sampler · checkpoint {latestHistoricalCheckpointRun.run_id}</div>
                        <div className="mt-1 text-xs text-slate-400">
                          Saved {formatDateTime(latestHistoricalCheckpointRun.events.find((event) => event.event === "checkpoint_saved")?.occurred_at_utc)} · record {typeof latestHistoricalCheckpointRun.checkpoint_saved_state.checkpoint_size_bytes === "number" ? formatBytes(latestHistoricalCheckpointRun.checkpoint_saved_state.checkpoint_size_bytes) : "size not reported"}
                        </div>
                        <div className="mt-1 break-words text-xs text-slate-400">
                          Counter {formatAppInteger(latestHistoricalCheckpointRun.checkpoint_saved_state.counter)} · app accumulator {formatAppInteger(latestHistoricalCheckpointRun.checkpoint_saved_state.accumulator)}
                        </div>
                        <div className="mt-1 text-[10px] text-slate-500">Backend recovery history only; a historical save does not imply the checkpoint remains in QEMU RAM.</div>
                      </div>
                    )}
                  </div>
                ) : (
                  <>
                    <div className="grid gap-3 border-t border-slate-800/70 p-5 sm:grid-cols-2 xl:grid-cols-4">
                      <div className="min-w-0 rounded-lg bg-slate-950/50 p-3">
                        <div className="text-[10px] uppercase tracking-wider text-slate-500">Checkpoint ID</div>
                        <div className="mt-1 break-all font-mono text-sm text-cyan-200">{currentSessionCheckpoint.checkpoint_id ?? "Unavailable"}</div>
                        <div className={`mt-2 text-xs ${checkpointBelongsToCurrentSession ? "text-emerald-300" : "text-amber-300"}`}>
                          {checkpointBelongsToCurrentSession ? "Current QEMU session" : "Not confirmed for current QEMU session"}
                        </div>
                      </div>
                      <div className="rounded-lg bg-slate-950/50 p-3">
                        <div className="text-[10px] uppercase tracking-wider text-slate-500">Task · created</div>
                        <div className="mt-1 text-sm font-semibold text-slate-200">{currentSessionCheckpoint.task_id}</div>
                        <div className="mt-1 text-xs text-slate-400">{formatDateTime(currentSessionCheckpoint.created_at_utc)}</div>
                      </div>
                      <div className="rounded-lg bg-slate-950/50 p-3">
                        <div className="text-[10px] uppercase tracking-wider text-slate-500">Record size · storage used</div>
                        <div className="mt-1 text-sm font-semibold text-slate-200">{currentSessionCheckpoint.size_bytes === null ? "Not reported" : formatBytes(currentSessionCheckpoint.size_bytes)}</div>
                        <div className="mt-1 text-xs text-slate-400">{currentSessionCheckpoint.storage_used_bytes === null ? "Size absent from confirmed event" : `${formatBytes(currentSessionCheckpoint.storage_used_bytes)} of one volatile RAM slot`}</div>
                      </div>
                      <div className="min-w-0 rounded-lg bg-slate-950/50 p-3">
                        <div className="text-[10px] uppercase tracking-wider text-slate-500">Saved application state</div>
                        <div className="mt-1 break-words text-sm text-slate-200">Counter {formatAppInteger(currentSessionCheckpoint.application_state.counter)}</div>
                        <div className="mt-1 break-words text-xs text-slate-400">App-defined accumulator {formatAppInteger(currentSessionCheckpoint.application_state.accumulator)}</div>
                      </div>
                    </div>
                    <p className="px-5 pb-4 text-xs text-slate-500">{currentSessionCheckpoint.storage_note}</p>
                  </>
                )}
              </section>

              <div className="grid grid-cols-1 gap-4">
                <section id="events" className="panel overflow-hidden">
                  <div className="flex items-center justify-between border-b border-slate-800/80 px-5 py-4"><div><h2 className="text-base font-semibold text-slate-100">Embedded event stream</h2><p className="mt-1 text-xs text-slate-500">UART task telemetry and firmware checkpoint/recovery events</p></div><Activity size={16} className="text-cyan-300" /></div>
                  <div className="max-h-[330px] divide-y divide-slate-800/50 overflow-y-auto">
                    {eventList.length === 0 && <div className="p-8 text-center text-xs text-slate-500">No backend events received yet.</div>}
                    {eventList.slice(0, 30).map((event, index) => {
                      const kind = typeof event.event === "string" ? event.event.replaceAll("_", " ") : event.type.replaceAll("_", " ");
                      const identity = event.worker_id || event.task_id || "system";
                      const time = event.occurred_at_utc || event.captured_at_utc || event.received_at_utc || event.assessed_at_utc || event.created_at_utc;
                      const isFailure = kind.includes("failed") || kind.includes("error");
                      return <div key={`${String(time)}-${index}`} className="flex gap-3 px-5 py-3">
                        <div className={`mt-1 grid h-6 w-6 shrink-0 place-items-center rounded-lg ${isFailure ? "bg-rose-400/10 text-rose-300" : event.type === "embedded_task_event" ? "bg-violet-400/10 text-violet-300" : "bg-cyan-400/10 text-cyan-300"}`}><Activity size={12} /></div>
                        <div className="min-w-0 flex-1"><div className="flex flex-wrap items-center gap-x-2 gap-y-1"><span className="text-xs font-medium capitalize text-slate-300">{kind}</span><span className="rounded bg-slate-800/80 px-1.5 py-0.5 font-mono text-[10px] text-slate-500">{String(identity)}</span></div><div className="mt-1 truncate text-[11px] text-slate-500">{event.type === "embedded_task_telemetry" ? `Execution ${event.execution_count} · ${event.task_state} · tick ${event.tick_count} · stack ${event.stack_high_water_mark_words} words` : event.type === "embedded_task_event" ? `Application-state event${typeof event.counter === "number" ? ` · counter ${formatAppInteger(event.counter)}` : ""}${typeof event.checkpoint_size_bytes === "number" ? ` · RAM checkpoint ${formatBytes(event.checkpoint_size_bytes)}` : ""}` : typeof event.reason === "string" ? event.reason : typeof event.error === "string" ? event.error : ""}</div></div>
                        <time className="shrink-0 pt-0.5 text-[9px] text-slate-600">{formatTime(time)}</time>
                      </div>;
                    })}
                  </div>
                  <div className="flex items-center justify-between border-t border-slate-800/80 px-5 py-3 text-[10px] text-slate-600"><span>{eventList.length} retained events · capped by backend</span><span className="flex items-center gap-1.5"><span className="h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-400" />Polling live</span></div>
                </section>
              </div>

              <details id="host-monitoring" className="panel overflow-hidden">
                <summary className="cursor-pointer list-none px-5 py-4">
                  <span className="text-base font-semibold text-slate-100">Host monitoring · secondary diagnostics</span>
                  <span className="mt-1 block text-xs text-slate-500">Python worker CPU, RSS, host-only risk, host Digital Twins, and host application-state checkpoints</span>
                </summary>
                <div className="space-y-5 border-t border-slate-800/70 p-5">
                  <section className="grid grid-cols-1 gap-3 sm:grid-cols-3" aria-label="Host process metrics">
                    <MetricCard label="Active host workers" value={String(data.status.active_worker_count ?? "Unavailable")} detail={`of ${data.status.configured_worker_count ?? "Unavailable"} configured`} icon={Cpu} />
                    <MetricCard label="Host CPU / core" value={<MetricValue value={cpuAverage} suffix="%" />} detail={`${cpus.length} process measurements available`} icon={Gauge} tone="violet" />
                    <MetricCard label="Host resident memory" value={rssLabel} detail={`OS RSS · ${rssValues.length}/${liveWorkers.length} available`} icon={HardDrive} tone="amber" />
                  </section>

                  <div className="flex flex-wrap items-center justify-between gap-3">
                    <div>
                      <h3 className="text-sm font-semibold text-slate-200">Host telemetry history</h3>
                      <p className="mt-1 text-xs text-slate-500">Host-only CPU and RSS; not inputs to embedded risk.</p>
                    </div>
                    <label className="flex items-center gap-2 text-xs text-slate-400">
                      Worker
                      <select value={selectedWorker} onChange={(event) => setSelectedWorker(event.target.value)} className="rounded-lg border border-slate-700 bg-slate-950 px-2 py-1.5 text-xs text-slate-200">
                        {data.workers.map((worker) => <option key={worker.worker_id} value={worker.worker_id}>{worker.worker_id}</option>)}
                      </select>
                    </label>
                  </div>
                  <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
                    <SparkChart title="Host CPU utilization" detail={`${selectedWorker || "Selected worker"} · one logical core`} series={seriesFor("cpu_utilization_percent_one_core")} unit="%" color="#36c9e8" />
                    <SparkChart title="Host resident memory" detail={`${selectedWorker || "Selected worker"} · RSS`} series={seriesFor("memory_rss_bytes").map((point) => ({ ...point, value: point.value === null ? null : point.value / (1024 ** 2) }))} unit=" MiB" color="#9a84ff" />
                  </div>

                  <section id="workers" className="overflow-hidden rounded-xl border border-slate-800">
                    <div className="flex flex-wrap items-center justify-between gap-2 px-4 py-3">
                      <div><h3 className="text-sm font-semibold text-slate-200">Host worker processes</h3><p className="mt-1 text-xs text-slate-500">Host OS identity and host-only risk</p></div>
                      <span className="text-xs text-slate-500">{liveWorkers.length} reporting · {highRisk} high-risk</span>
                    </div>
                    <div className="overflow-x-auto">
                      <table className="w-full min-w-[760px] text-left">
                        <thead><tr className="border-t border-slate-800 text-[10px] uppercase tracking-wider text-slate-500"><th className="px-4 py-3">Worker / PID</th><th className="px-3 py-3">CPU</th><th className="px-3 py-3">RSS</th><th className="px-3 py-3">Twin health</th><th className="px-3 py-3">Host risk</th><th className="px-4 py-3">Observed</th></tr></thead>
                        <tbody>
                          {data.workers.map((worker) => {
                            const telemetry = worker.telemetry;
                            const health = String(worker.digital_twin?.health_state || "Unavailable");
                            return <tr key={worker.worker_id} className="border-t border-slate-800/60 text-xs">
                              <td className="px-4 py-3 text-slate-200">{worker.worker_id}<div className="mt-1 font-mono text-[10px] text-slate-500">{telemetry?.pid ? `PID ${telemetry.pid}` : "PID unavailable"}</div></td>
                              <td className="px-3 py-3 text-slate-300"><MetricValue value={telemetry?.cpu_utilization_percent_one_core} suffix="%" /></td>
                              <td className="px-3 py-3 text-slate-300">{formatBytes(telemetry?.memory_rss_bytes)}</td>
                              <td className="px-3 py-3"><HealthBadge health={health} /></td>
                              <td className="px-3 py-3"><RiskBadge category={worker.risk?.risk_category} /></td>
                              <td className="px-4 py-3 text-slate-500">{formatTime(telemetry?.captured_at_utc)}</td>
                            </tr>;
                          })}
                        </tbody>
                      </table>
                    </div>
                  </section>

                  <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
                    <section className="rounded-xl border border-slate-800 p-4">
                      <h3 className="text-sm font-semibold text-slate-200">Host Digital Twin profiles</h3>
                      <div className="mt-3 space-y-2">
                        {(data.twins.twins || []).length === 0 && <p className="text-xs text-slate-500">No host process profiles received.</p>}
                        {(data.twins.twins || []).map((twin) => <div key={String(twin.worker_id)} className="rounded-lg bg-slate-950/50 p-3">
                          <div className="flex items-center justify-between gap-2"><span className="text-xs font-semibold text-slate-200">{String(twin.worker_id)}</span><HealthBadge health={String(twin.health_state || "Unavailable")} /></div>
                          <div className="mt-2 grid grid-cols-2 gap-2 text-xs text-slate-500">
                            <span>PID {typeof twin.pid === "number" ? twin.pid : "Unavailable"}</span>
                            <span>CPU <MetricValue value={typeof twin.cpu_utilization_percent_one_core === "number" ? twin.cpu_utilization_percent_one_core : null} suffix="%" /></span>
                            <span>RSS {formatBytes(typeof twin.memory_rss_bytes === "number" ? twin.memory_rss_bytes : null)}</span>
                            <span>{twin.stale === true ? "Stale" : twin.stale === false ? "Fresh" : "Unavailable"}</span>
                          </div>
                        </div>)}
                      </div>
                    </section>

                    <section className="overflow-hidden rounded-xl border border-slate-800">
                      <div className="flex items-center justify-between gap-3 p-4">
                        <div><h3 className="text-sm font-semibold text-slate-200">Host application checkpoints</h3><p className="mt-1 text-xs text-slate-500">Separate from the volatile embedded RAM checkpoint</p></div>
                        <div className="text-right text-xs text-slate-400">{formatBytes(storage?.storage_usage_bytes)} / {formatBytes(storage?.storage_budget_bytes)}</div>
                      </div>
                      <div className="mx-4 h-1.5 overflow-hidden rounded-full bg-slate-800"><div className="h-full bg-cyan-400" style={{ width: `${usagePercent ?? 0}%` }} /></div>
                      <div className="mt-3 max-h-72 overflow-auto">
                        {(data.checkpoints.checkpoints || []).length === 0
                          ? <p className="p-4 text-xs text-slate-500">No host checkpoints have been created.</p>
                          : data.checkpoints.checkpoints.map((checkpoint) => <div key={checkpoint.checkpoint_id} className="border-t border-slate-800/70 px-4 py-3 text-xs">
                            <div className="flex flex-wrap items-center justify-between gap-2"><span className="break-all font-mono text-cyan-200">{checkpoint.checkpoint_id}</span><span className="text-slate-400">{formatBytes(checkpoint.size_bytes)}</span></div>
                            <div className="mt-1 text-slate-500">{checkpoint.worker_id} · PID {checkpoint.worker_pid} · created {formatDateTime(checkpoint.created_at_utc)}</div>
                            <div className="mt-1 text-slate-400">Host application state · counter {formatAppInteger(checkpoint.application_state.counter)} · app accumulator {formatAppInteger(checkpoint.application_state.accumulator)}</div>
                          </div>)}
                      </div>
                    </section>
                  </div>
                </div>
              </details>
              <footer className="border-t border-slate-800/70 py-4 text-xs text-slate-500">
                Embedded policy index is rule-derived, not calibrated failure probability. Host process metrics remain isolated in the secondary diagnostics section.
              </footer>
            </>}
          </div>
        </main>
      </div>

    </div>
  );
}

export default App;
