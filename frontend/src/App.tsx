import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Activity,
  AlertTriangle,
  Check,
  ChevronRight,
  CircleHelp,
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
  X,
  Zap,
} from "lucide-react";

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
  risk_score: number;
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
  received_at_utc: string;
};
type EmbeddedRiskAssessment = {
  task_id: string;
  risk_level: "unknown" | "normal" | "elevated";
  recommendation: "none" | "checkpoint_recommended";
  matched_rules: string[];
  observed_inputs: {
    stack_high_water_mark_words?: number;
    telemetry_age_seconds?: number;
  };
  reason: string;
  policy_id: string;
};
type EmbeddedCheckpointStatus = {
  task_id: string;
  event: string;
  counter?: number;
  integrity_hash?: number;
  occurred_at_utc: string;
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
  embedded_risk_assessments?: EmbeddedRiskAssessment[];
  embedded_checkpoint_status?: EmbeddedCheckpointStatus[];
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

function formatBytes(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "Unavailable";
  if (value < 1024) return `${value} B`;
  const units = ["KiB", "MiB", "GiB", "TiB"];
  let scaled = value / 1024;
  let unit = units[0];
  for (let index = 0; scaled >= 1024 && index < units.length - 1; index += 1) {
    scaled /= 1024;
    unit = units[index + 1];
  }
  return `${scaled.toFixed(1)} ${unit}`;
}

function formatTime(value: unknown): string {
  if (typeof value !== "string") return "Unavailable";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "Unavailable"
    : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
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
  const classes = normalized === "LOW"
    ? "border-emerald-500/20 bg-emerald-500/10 text-emerald-300"
    : normalized === "MEDIUM"
      ? "border-amber-500/20 bg-amber-500/10 text-amber-300"
      : normalized === "HIGH"
        ? "border-rose-500/20 bg-rose-500/10 text-rose-300"
        : "border-slate-700 bg-slate-800/60 text-slate-400";
  return <span className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[10px] font-bold tracking-[.12em] ${classes}`}><span className="h-1.5 w-1.5 rounded-full bg-current" />{normalized}</span>;
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
          {latest === null ? "Unavailable" : `${latest.toFixed(1)}${unit}`}
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
          <div className="grid h-full place-items-center rounded-xl border border-dashed border-slate-800 text-xs text-slate-500">Collecting real observations…</div>
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
  const [faultPending, setFaultPending] = useState(false);
  const [showConfirm, setShowConfirm] = useState(false);
  const [faultOutcome, setFaultOutcome] = useState<{ ok: boolean; text: string } | null>(null);
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
  const eventList = [...(data?.events.events || [])].reverse();
  const storage = data?.checkpoints.storage;
  const usagePercent = storage && storage.storage_budget_bytes > 0
    ? Math.min(100, (storage.storage_usage_bytes / storage.storage_budget_bytes) * 100)
    : null;
  const connected = data?.status.status === "running" && !error;

  async function injectFault() {
    if (!selectedWorker || faultPending) return;
    setFaultPending(true);
    setShowConfirm(false);
    setFaultOutcome(null);
    try {
      const response = await fetch(`${API_ROOT}/workers/${encodeURIComponent(selectedWorker)}/fault`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ confirm: true }),
      });
      const payload = await response.json();
      if (!response.ok || payload.success !== true || payload.recovery?.success !== true) {
        const stages = Array.isArray(payload.events)
          ? payload.events.map((event: { event?: string }) => event.event).filter(Boolean).join(" → ")
          : "";
        const reason = payload.error || payload.recovery?.error || "Backend did not verify recovery";
        throw new Error(stages ? `${reason} · ${stages}` : reason);
      }
      const recovery = payload.recovery;
      const stages = Array.isArray(recovery.events)
        ? recovery.events.map((event: { event?: string }) => event.event).filter(Boolean).join(" → ")
        : "Verified recovery sequence unavailable";
      setFaultOutcome({
        ok: true,
        text: `${recovery.worker_id} restored in PID ${recovery.replacement_pid}; counter ${recovery.restored_state.counter} → ${recovery.continued_state.counter}. Stages: ${stages}.`,
      });
      await refresh();
    } catch (caught) {
      setFaultOutcome({ ok: false, text: caught instanceof Error ? caught.message : "Recovery request failed" });
      await refresh();
    } finally {
      setFaultPending(false);
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
            <a href="#workers" className="nav-item"><Cpu size={16} />Workers <span className="ml-auto rounded-md bg-slate-800 px-2 py-0.5 text-[10px] text-slate-400">{String(data?.status.active_worker_count ?? "Unavailable")}</span></a>
            <a href="#embedded-tasks" className="nav-item"><Network size={16} />Embedded RTOS</a>
            <a href="#twins" className="nav-item"><Network size={16} />Digital Twins</a>
            <a href="#risk" className="nav-item"><Shield size={16} />Risk analysis</a>
            <a href="#checkpoints" className="nav-item"><Database size={16} />Checkpoints</a>
            <a href="#events" className="nav-item"><Clock3 size={16} />Event stream</a>
          </nav>
          <div className="mt-auto rounded-xl border border-slate-800 bg-slate-900/50 p-4">
            <div className="flex items-center gap-2 text-xs font-semibold text-slate-300"><CircleHelp size={14} className="text-cyan-300" />Host prototype</div>
            <p className="mt-2 text-[11px] leading-relaxed text-slate-500">Live OS process telemetry with application-level checkpoint recovery.</p>
          </div>
          <div className="mt-4 px-2 text-[10px] text-slate-600">TwinCoreOS <span className="float-right">MILESTONE 06</span></div>
        </aside>

        <main id="overview" className="min-w-0 flex-1">
          <header className="sticky top-0 z-20 flex h-[72px] items-center justify-between border-b border-slate-800/80 bg-[#080b12]/90 px-5 backdrop-blur-xl md:px-8">
            <div className="flex items-center gap-3">
              <div className="grid h-9 w-9 place-items-center rounded-lg border border-cyan-400/20 bg-cyan-400/10 text-cyan-300 lg:hidden"><Activity size={18} /></div>
              <div><div className="text-sm font-semibold text-slate-100">System overview</div><div className="mt-1 hidden text-[11px] text-slate-500 sm:block">Live worker telemetry · host-process prototype</div></div>
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
                <div><div className="flex items-center gap-2 text-[10px] font-bold uppercase tracking-[.2em] text-cyan-300"><span className="h-1.5 w-1.5 rounded-full bg-cyan-300" />Operations / live overview</div><h1 className="mt-2 text-2xl font-semibold tracking-tight text-white md:text-[28px]">Runtime observability</h1><p className="mt-1 text-xs text-slate-500">Real measurements from supervisor-owned processes · refreshes every second</p></div>
                <div className="flex items-center gap-2 text-[11px] text-slate-500"><span className="h-1.5 w-1.5 rounded-full bg-emerald-400" />Last update {lastUpdated?.toLocaleTimeString() || "—"}</div>
              </div>

              <section className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-5" aria-label="System metrics">
                <MetricCard label="Active workers" value={String(data.status.active_worker_count ?? "Unavailable")} detail={`of ${data.status.configured_worker_count ?? "Unavailable"} configured`} icon={Cpu} />
                <MetricCard label="Avg. CPU / core" value={<MetricValue value={cpuAverage} suffix="%" />} detail={`Interval utilization · ${cpus.length} available`} icon={Gauge} tone="violet" />
                <MetricCard label="Resident memory" value={rssLabel} detail={`OS RSS · ${rssValues.length}/${liveWorkers.length} available`} icon={HardDrive} tone="amber" />
                <MetricCard label="High-risk workers" value={String(highRisk)} detail="Baseline heuristic · not probability" icon={ShieldAlert} tone="rose" />
                <MetricCard label="Checkpoint storage" value={formatBytes(storage?.storage_usage_bytes)} detail={`of ${formatBytes(storage?.storage_budget_bytes)} budget`} icon={Database} />
              </section>

              <section className="grid grid-cols-1 gap-4 xl:grid-cols-2" aria-label="Telemetry history">
                <SparkChart title="CPU utilization" detail={`${selectedWorker || "Selected worker"} · interval % · one logical core`} series={seriesFor("cpu_utilization_percent_one_core")} unit="%" color="#36c9e8" />
                <SparkChart title="Resident memory" detail={`${selectedWorker || "Selected worker"} · OS-reported RSS · bytes`} series={seriesFor("memory_rss_bytes")} unit="" color="#9a84ff" />
              </section>

              <section id="workers" className="panel overflow-hidden">
                <div className="flex flex-col gap-3 border-b border-slate-800/80 px-5 py-4 sm:flex-row sm:items-center sm:justify-between">
                  <div><h2 className="text-sm font-semibold text-slate-100">Worker processes</h2><p className="mt-1 text-[11px] text-slate-500">OS measurements and Digital Twin health</p></div>
                  <div className="flex items-center gap-2 text-[10px] text-slate-500"><Signal size={13} className="text-emerald-300" />{liveWorkers.length} reporting</div>
                </div>
                <div className="overflow-x-auto">
                  <table className="w-full min-w-[820px] text-left">
                    <thead><tr className="border-b border-slate-800/70 text-[9px] font-bold uppercase tracking-[.16em] text-slate-600"><th className="px-5 py-3">Worker / PID</th><th className="px-4 py-3">CPU · one core</th><th className="px-4 py-3">RSS memory</th><th className="px-4 py-3">Twin health</th><th className="px-4 py-3">Risk</th><th className="px-5 py-3">Observed</th></tr></thead>
                    <tbody>
                      {data.workers.length === 0 && <tr><td colSpan={6} className="px-5 py-10 text-center text-xs text-slate-500">{data.status.status === "running" ? "Waiting for the first telemetry sample…" : "No worker observations available."}</td></tr>}
                      {data.workers.map((worker) => {
                        const telemetry = worker.telemetry;
                        const health = String(worker.digital_twin?.health_state || "Unavailable");
                        return <tr key={worker.worker_id} className="border-b border-slate-800/50 transition hover:bg-slate-800/20">
                          <td className="px-5 py-3.5"><div className="flex items-center gap-2.5"><StatusDot status={health === "FAILED" ? "failed" : telemetry ? "running" : "starting"} /><div><div className="text-xs font-semibold text-slate-200">{worker.worker_id}</div><div className="mt-1 font-mono text-[10px] text-slate-500">{telemetry?.pid ? `PID ${telemetry.pid}` : "PID unavailable"}</div></div></div></td>
                          <td className="px-4 py-3.5 text-xs text-slate-300"><MetricValue value={telemetry?.cpu_utilization_percent_one_core} suffix="%" /></td>
                          <td className="px-4 py-3.5 text-xs text-slate-300">{formatBytes(telemetry?.memory_rss_bytes)}</td>
                          <td className="px-4 py-3.5"><HealthBadge health={health} /></td>
                          <td className="px-4 py-3.5"><RiskBadge category={worker.risk?.risk_category} /><div className="mt-1 text-[10px] text-slate-500">{worker.risk ? `${(worker.risk.risk_score * 100).toFixed(0)} / 100 heuristic` : "Unavailable"}</div></td>
                          <td className="px-5 py-3.5 text-[10px] text-slate-500">{formatTime(telemetry?.captured_at_utc)}</td>
                        </tr>;
                      })}
                    </tbody>
                  </table>
                </div>
                <div className="flex items-center gap-2 px-5 py-3 text-[10px] text-slate-600"><CircleHelp size={12} />RSS is resident set size. CPU is interval utilization normalized to one logical core.</div>
              </section>

              <section id="embedded-tasks" className="panel overflow-hidden">
                <div className="flex flex-col gap-2 border-b border-slate-800/80 px-5 py-4 sm:flex-row sm:items-center sm:justify-between">
                  <div><h2 className="text-sm font-semibold text-slate-100">FreeRTOS tasks · QEMU</h2><p className="mt-1 text-[11px] text-slate-500">Separate embedded task identity · no host PID, CPU, or RSS attribution</p></div>
                  <span className="rounded-full border border-cyan-500/20 bg-cyan-500/[0.06] px-2.5 py-1 text-[10px] font-medium text-cyan-300">{data.embedded_tasks?.latest.length ?? 0} task identities received</span>
                </div>
                {(data.embedded_tasks?.latest.length ?? 0) === 0 ? (
                  <div className="px-5 py-9 text-center text-xs text-slate-500">No embedded task telemetry received. Start the QEMU bridge to publish live UART records.</div>
                ) : <>
                  <div className="overflow-x-auto">
                    <table className="w-full min-w-[760px] text-left">
                      <thead><tr className="border-b border-slate-800/70 text-[9px] font-bold uppercase tracking-[.16em] text-slate-600"><th className="px-5 py-3">RTOS task ID · not PID</th><th className="px-4 py-3">State</th><th className="px-4 py-3">Execution count</th><th className="px-4 py-3">FreeRTOS tick</th><th className="px-4 py-3">Stack high-water</th><th className="px-5 py-3">Received</th></tr></thead>
                      <tbody>{data.embedded_tasks?.latest.map((task) => <tr key={`${task.target}-${task.task_id}`} className="border-b border-slate-800/50">
                        <td className="px-5 py-3.5"><div className="text-xs font-semibold text-slate-200">{task.task_id}</div><div className="mt-1 font-mono text-[9px] text-slate-600">{task.target} · schema v{task.schema_version}</div></td>
                        <td className="px-4 py-3.5"><span className="rounded-full border border-emerald-500/20 bg-emerald-500/10 px-2 py-1 text-[10px] font-medium text-emerald-300">{task.task_state}</span></td>
                        <td className="px-4 py-3.5 font-mono text-xs text-slate-200">{task.execution_count}</td>
                        <td className="px-4 py-3.5 font-mono text-xs text-slate-300">{task.tick_count}</td>
                        <td className="px-4 py-3.5 text-xs text-slate-300">{task.stack_high_water_mark_words} words</td>
                        <td className="px-5 py-3.5 text-[10px] text-slate-500">{formatTime(task.received_at_utc)}</td>
                      </tr>)}</tbody>
                    </table>
                  </div>
                  <div className="border-t border-slate-800/70 px-5 py-3">
                    <div className="mb-2 text-[9px] font-bold uppercase tracking-[.16em] text-slate-600">Recent UART samples</div>
                    <div className="flex flex-wrap gap-2">{(data.embedded_tasks?.history || []).slice(-8).reverse().map((task, index) => <span key={`${task.bridge_session_id}-${task.task_id}-${task.execution_count}-${index}`} className="rounded-md border border-slate-800 bg-slate-950/60 px-2 py-1 font-mono text-[9px] text-slate-400">{task.task_id} · #{task.execution_count} · tick {task.tick_count}</span>)}</div>
                  </div>
                </>}
                <div className="border-t border-slate-800/70 px-5 py-4">
                  <div className="mb-3">
                    <h3 className="text-xs font-semibold text-slate-200">Embedded operational risk · heuristic</h3>
                    <p className="mt-1 text-[10px] text-slate-500">Uses fresh embedded stack reserve only; eligible sensor-sampler recommendations are forwarded to firmware over QEMU UART. Heuristic, not a calibrated failure probability.</p>
                  </div>
                  <div className="grid gap-2 md:grid-cols-2">
                    {(data.embedded_risk_assessments || []).map((assessment) => {
                      const checkpointStatus = (data.embedded_checkpoint_status || []).find((status) => status.task_id === assessment.task_id);
                      return <div key={assessment.task_id} className="rounded-lg border border-slate-800/80 bg-slate-950/40 p-3">
                      <div className="flex items-center justify-between gap-2">
                        <span className="font-mono text-[11px] font-medium text-slate-200">{assessment.task_id}</span>
                        <span className={`rounded-full border px-2 py-0.5 text-[9px] font-semibold uppercase ${assessment.risk_level === "elevated" ? "border-amber-500/20 bg-amber-500/10 text-amber-300" : assessment.risk_level === "normal" ? "border-emerald-500/20 bg-emerald-500/10 text-emerald-300" : "border-slate-700 bg-slate-800/70 text-slate-400"}`}>{assessment.risk_level}</span>
                      </div>
                      <div className="mt-2 text-[10px] text-slate-400">Stack reserve {assessment.observed_inputs.stack_high_water_mark_words === undefined ? "Unavailable" : `${assessment.observed_inputs.stack_high_water_mark_words} words`} · telemetry age {assessment.observed_inputs.telemetry_age_seconds === undefined ? "Unavailable" : `${assessment.observed_inputs.telemetry_age_seconds.toFixed(1)} s`}</div>
                      <div className="mt-1 text-[10px] text-slate-500">Rule: {assessment.matched_rules.length > 0 ? assessment.matched_rules.join(", ") : "none matched"} · {assessment.reason}</div>
                      <div className="mt-2 text-[10px] font-medium text-cyan-300">Policy recommendation: {assessment.recommendation === "checkpoint_recommended" ? "Checkpoint recommended" : "None"}</div>
                      <div className="mt-1 text-[10px] text-slate-500">Latest checkpoint activity: {checkpointStatus ? `${checkpointStatus.event.replaceAll("_", " ")}${checkpointStatus.counter === undefined ? "" : ` · counter ${checkpointStatus.counter}`}` : "No checkpoint request or result"}</div>
                    </div>;
                    })}
                    {(data.embedded_risk_assessments || []).length === 0 && <div className="text-[10px] text-slate-500">Embedded risk assessment unavailable.</div>}
                  </div>
                </div>
              </section>

              <div className="grid grid-cols-1 gap-4 xl:grid-cols-[1.1fr_.9fr]">
                <section id="twins" className="panel p-5">
                  <div className="flex items-start justify-between"><div><h2 className="text-sm font-semibold text-slate-100">Digital Twin profiles</h2><p className="mt-1 text-[11px] text-slate-500">Latest observed state per process identity</p></div><Network size={17} className="text-cyan-300" /></div>
                  <div className="mt-4 space-y-2">
                    {(data.twins.twins || []).length === 0 && <div className="rounded-lg border border-dashed border-slate-800 p-5 text-center text-xs text-slate-500">Digital Twin observations are not available yet.</div>}
                    {(data.twins.twins || []).map((twin) => <div key={String(twin.worker_id)} className="rounded-xl border border-slate-800/80 bg-slate-950/40 p-3.5">
                      <div className="flex items-center justify-between"><div className="text-xs font-semibold text-slate-200">{String(twin.worker_id)}</div><HealthBadge health={String(twin.health_state || "Unavailable")} /></div>
                      <div className="mt-3 grid grid-cols-2 gap-3 text-[10px]">
                        <div><div className="text-slate-600">PID</div><div className="mt-1 font-mono text-slate-300">{typeof twin.pid === "number" ? twin.pid : "Unavailable"}</div></div>
                        <div><div className="text-slate-600">Freshness</div><div className="mt-1 text-slate-300">{twin.stale === true ? "Stale" : twin.stale === false ? "Current" : "Unavailable"}</div></div>
                        <div><div className="text-slate-600">CPU interval</div><div className="mt-1 text-slate-300"><MetricValue value={typeof twin.cpu_utilization_percent_one_core === "number" ? twin.cpu_utilization_percent_one_core : null} suffix="%" /></div></div>
                        <div><div className="text-slate-600">RSS</div><div className="mt-1 text-slate-300">{formatBytes(typeof twin.memory_rss_bytes === "number" ? twin.memory_rss_bytes : null)}</div></div>
                      </div>
                    </div>)}
                  </div>
                </section>

                <section id="risk" className="panel p-5">
                  <div className="flex items-start justify-between"><div><h2 className="text-sm font-semibold text-slate-100">Risk assessment</h2><p className="mt-1 text-[11px] text-slate-500">Transparent baseline heuristic · not a probability</p></div><Shield size={17} className="text-violet-300" /></div>
                  <div className="mt-5 space-y-4">
                    {risks.length === 0 && <div className="rounded-lg border border-dashed border-slate-800 p-5 text-center text-xs text-slate-500">Risk assessments are unavailable until observations arrive.</div>}
                    {risks.map((risk) => <div key={risk.worker_id}>
                      <div className="mb-2 flex items-center justify-between gap-3"><div className="min-w-0"><div className="truncate text-xs font-medium text-slate-300">{risk.worker_id}</div><div className="mt-1 truncate text-[10px] text-slate-600">{risk.explanation || "No explanation available"}</div></div><div className="shrink-0 text-right"><div className="text-xs font-semibold text-slate-200">{Number.isFinite(risk.risk_score) ? `${(risk.risk_score * 100).toFixed(0)}%` : "Unavailable"}</div><div className="mt-1"><RiskBadge category={risk.risk_category} /></div></div></div>
                      <div className="h-1.5 overflow-hidden rounded-full bg-slate-800"><div className={`h-full rounded-full transition-all ${risk.risk_category === "HIGH" ? "bg-rose-400" : risk.risk_category === "MEDIUM" ? "bg-amber-300" : "bg-emerald-400"}`} style={{ width: `${Math.max(0, Math.min(100, risk.risk_score * 100))}%` }} /></div>
                    </div>)}
                  </div>
                  <div className="mt-5 flex flex-wrap gap-4 border-t border-slate-800/80 pt-4 text-[10px] text-slate-500">{["LOW", "MEDIUM", "HIGH"].map((category) => <span key={category} className="flex items-center gap-1.5"><span className={`h-1.5 w-1.5 rounded-full ${category === "LOW" ? "bg-emerald-400" : category === "MEDIUM" ? "bg-amber-300" : "bg-rose-400"}`} />{category}</span>)}</div>
                </section>
              </div>

              <section id="checkpoints" className="panel overflow-hidden">
                <div className="flex flex-col gap-3 border-b border-slate-800/80 px-5 py-4 sm:flex-row sm:items-center sm:justify-between">
                  <div><h2 className="text-sm font-semibold text-slate-100">Checkpoint storage</h2><p className="mt-1 text-[11px] text-slate-500">Validated application-state snapshots · strict JSON</p></div>
                  <div className="text-xs text-slate-400">{formatBytes(storage?.storage_usage_bytes)} <span className="text-slate-600">/ {formatBytes(storage?.storage_budget_bytes)}</span></div>
                </div>
                <div className="mx-5 mt-4 h-1.5 overflow-hidden rounded-full bg-slate-800"><div className="h-full rounded-full bg-cyan-400 transition-all" style={{ width: `${usagePercent ?? 0}%` }} /></div>
                <div className="overflow-x-auto">
                  <table className="w-full min-w-[600px] text-left">
                    <thead><tr className="text-[9px] font-bold uppercase tracking-[.16em] text-slate-600"><th className="px-5 py-3">Checkpoint</th><th className="px-4 py-3">Worker / PID</th><th className="px-4 py-3">Supported state</th><th className="px-4 py-3">Size</th><th className="px-5 py-3">Created</th></tr></thead>
                    <tbody>
                      {(data.checkpoints.checkpoints || []).length === 0 && <tr><td colSpan={5} className="px-5 py-8 text-center text-xs text-slate-500">No checkpoints have been created yet.</td></tr>}
                      {(data.checkpoints.checkpoints || []).map((checkpoint) => <tr key={checkpoint.checkpoint_id} className="border-t border-slate-800/50 text-xs">
                        <td className="px-5 py-3 font-mono text-[10px] text-cyan-300">{checkpoint.checkpoint_id.slice(0, 12)}…</td><td className="px-4 py-3 text-slate-300">{checkpoint.worker_id}<div className="mt-1 font-mono text-[10px] text-slate-600">PID {checkpoint.worker_pid}</div></td><td className="px-4 py-3 text-slate-400">counter {checkpoint.application_state.counter} · accumulator {checkpoint.application_state.accumulator}</td><td className="px-4 py-3 text-slate-300">{formatBytes(checkpoint.size_bytes)}</td><td className="px-5 py-3 text-slate-500">{formatTime(checkpoint.created_at_utc)}</td>
                      </tr>)}
                    </tbody>
                  </table>
                </div>
              </section>

              <div className="grid grid-cols-1 gap-4 xl:grid-cols-[.85fr_1.15fr]">
                <section className="panel p-5">
                  <div className="flex items-start justify-between"><div><h2 className="text-sm font-semibold text-slate-100">Controlled recovery</h2><p className="mt-1 text-[11px] text-slate-500">Targets only supervisor-owned worker IDs</p></div><Zap size={17} className="text-amber-300" /></div>
                  <label htmlFor="worker-select" className="mt-5 block text-[10px] font-bold uppercase tracking-[.14em] text-slate-500">Select active worker</label>
                  <select id="worker-select" value={selectedWorker} onChange={(event) => { setSelectedWorker(event.target.value); setFaultOutcome(null); }} disabled={!liveWorkers.length || faultPending} className="mt-2 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2.5 text-xs text-slate-200 outline-none focus:border-cyan-400/60 disabled:opacity-50">
                    {liveWorkers.length === 0 && <option value="">No active workers available</option>}
                    {liveWorkers.map((worker) => <option key={worker.worker_id} value={worker.worker_id}>{worker.worker_id} · PID {worker.telemetry?.pid ?? "Unavailable"}</option>)}
                  </select>
                  <button onClick={() => setShowConfirm(true)} disabled={!selectedWorker || faultPending || !connected} className="mt-3 flex w-full items-center justify-center gap-2 rounded-lg bg-rose-500/90 px-4 py-2.5 text-xs font-semibold text-white transition hover:bg-rose-400 disabled:cursor-not-allowed disabled:bg-slate-800 disabled:text-slate-500">
                    {faultPending ? <LoaderCircle size={15} className="animate-spin" /> : <Zap size={15} />}Inject fault & recover
                  </button>
                  <p className="mt-3 text-[10px] leading-relaxed text-slate-600">The backend will checkpoint supported application state, terminate this worker, then verify restore and continuation in a fresh process.</p>
                  {faultOutcome && <div className={`mt-4 flex gap-2 rounded-lg border p-3 text-xs ${faultOutcome.ok ? "border-emerald-500/20 bg-emerald-500/[0.06] text-emerald-200" : "border-rose-500/20 bg-rose-500/[0.06] text-rose-200"}`} role="status">{faultOutcome.ok ? <Check size={15} className="shrink-0" /> : <X size={15} className="shrink-0" />}{faultOutcome.text}</div>}
                  {data.recovery.recovery && <div className="mt-3 text-[10px] text-slate-500">Latest backend recovery: <span className={data.recovery.recovery.status === "completed" ? "text-emerald-300" : "text-rose-300"}>{String(data.recovery.recovery.status)}</span>{typeof data.recovery.recovery.error === "string" ? ` · ${data.recovery.recovery.error}` : ""}</div>}
                </section>

                <section id="events" className="panel overflow-hidden">
                  <div className="flex items-center justify-between border-b border-slate-800/80 px-5 py-4"><div><h2 className="text-sm font-semibold text-slate-100">Live event log</h2><p className="mt-1 text-[11px] text-slate-500">Actual backend telemetry, risk, checkpoint and recovery events</p></div><Activity size={16} className="text-cyan-300" /></div>
                  <div className="max-h-[330px] divide-y divide-slate-800/50 overflow-y-auto">
                    {eventList.length === 0 && <div className="p-8 text-center text-xs text-slate-500">No backend events received yet.</div>}
                    {eventList.slice(0, 30).map((event, index) => {
                      const kind = typeof event.event === "string" ? event.event.replaceAll("_", " ") : event.type.replaceAll("_", " ");
                      const identity = event.worker_id || event.task_id || "system";
                      const time = event.occurred_at_utc || event.captured_at_utc || event.received_at_utc || event.assessed_at_utc || event.created_at_utc;
                      const isFailure = kind.includes("failed") || kind.includes("error");
                      return <div key={`${String(time)}-${index}`} className="flex gap-3 px-5 py-3">
                        <div className={`mt-1 grid h-6 w-6 shrink-0 place-items-center rounded-lg ${isFailure ? "bg-rose-400/10 text-rose-300" : event.type === "recovery_event" ? "bg-amber-400/10 text-amber-300" : event.type === "checkpoint_event" || event.type === "embedded_task_event" ? "bg-violet-400/10 text-violet-300" : "bg-cyan-400/10 text-cyan-300"}`}><Activity size={12} /></div>
                        <div className="min-w-0 flex-1"><div className="flex flex-wrap items-center gap-x-2 gap-y-1"><span className="text-[11px] font-medium capitalize text-slate-300">{kind}</span><span className="rounded bg-slate-800/80 px-1.5 py-0.5 font-mono text-[9px] text-slate-500">{String(identity)}</span></div><div className="mt-1 truncate text-[10px] text-slate-600">{event.type === "telemetry" ? `CPU ${event.cpu_utilization_percent_one_core == null ? "Unavailable" : `${Number(event.cpu_utilization_percent_one_core).toFixed(1)}%`} · RSS ${formatBytes(typeof event.memory_rss_bytes === "number" ? event.memory_rss_bytes : null)}` : event.type === "embedded_task_telemetry" ? `Execution ${event.execution_count} · ${event.task_state} · tick ${event.tick_count} · stack ${event.stack_high_water_mark_words} words` : event.type === "embedded_task_event" ? `RTOS application-state event${typeof event.counter === "number" ? ` · counter ${event.counter}` : ""}${typeof event.integrity_hash === "number" ? ` · integrity ${event.integrity_hash}` : ""}` : event.type === "risk_assessment" ? `${event.risk_category || "Unavailable"} · ${typeof event.risk_score === "number" ? `${(event.risk_score * 100).toFixed(0)} / 100` : "score unavailable"}` : typeof event.reason === "string" ? event.reason : typeof event.error === "string" ? event.error : ""}</div></div>
                        <time className="shrink-0 pt-0.5 text-[9px] text-slate-600">{formatTime(time)}</time>
                      </div>;
                    })}
                  </div>
                  <div className="flex items-center justify-between border-t border-slate-800/80 px-5 py-3 text-[10px] text-slate-600"><span>{eventList.length} retained events · capped by backend</span><span className="flex items-center gap-1.5"><span className="h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-400" />Polling live</span></div>
                </section>
              </div>
              <footer className="flex flex-col gap-2 border-t border-slate-800/70 py-4 text-[10px] text-slate-600 sm:flex-row sm:items-center sm:justify-between"><span>Host process monitoring · application-level state recovery only</span><span>Baseline heuristic risk · not calibrated probability <ChevronRight size={12} className="inline" /></span></footer>
            </>}
          </div>
        </main>
      </div>

      {showConfirm && <div className="fixed inset-0 z-50 grid place-items-center bg-black/70 p-4 backdrop-blur-sm" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) setShowConfirm(false); }}>
        <div role="dialog" aria-modal="true" aria-labelledby="fault-title" className="panel w-full max-w-md p-6 shadow-2xl shadow-black/50">
          <div className="flex items-start gap-3"><div className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-rose-400/10 text-rose-300"><AlertTriangle size={19} /></div><div><h2 id="fault-title" className="text-base font-semibold text-white">Inject controlled fault?</h2><p className="mt-2 text-sm leading-relaxed text-slate-400">TwinCoreOS will terminate <span className="font-semibold text-slate-200">{selectedWorker}</span>, restore its latest supported application checkpoint into a fresh worker, and verify that state continues.</p></div></div>
          <div className="mt-5 flex justify-end gap-2"><button onClick={() => setShowConfirm(false)} className="rounded-lg border border-slate-700 px-4 py-2 text-xs font-medium text-slate-300 hover:bg-slate-800">Cancel</button><button onClick={() => void injectFault()} className="rounded-lg bg-rose-500 px-4 py-2 text-xs font-semibold text-white hover:bg-rose-400">Confirm fault injection</button></div>
        </div>
      </div>}
    </div>
  );
}

export default App;
