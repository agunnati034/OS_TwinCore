export const EMBEDDED_TASK_IDS = ["sensor-sampler", "control-loop"] as const;

export type EmbeddedTaskId = (typeof EMBEDDED_TASK_IDS)[number];
export type EmbeddedRiskCategory = "LOW" | "ELEVATED" | "CRITICAL" | "UNKNOWN";

export type EmbeddedRiskAssessment = {
  task_id: EmbeddedTaskId;
  risk_level: "unknown" | "normal" | "elevated";
  risk_category: EmbeddedRiskCategory;
  risk_index: number | null;
  telemetry_freshness: "unknown" | "fresh" | "stale";
  recommendation: string;
  checkpoint_supported: boolean;
  matched_rules: string[];
  observed_inputs: {
    task_state?: string;
    stack_high_water_mark_words?: number;
    telemetry_age_seconds?: number;
    run_id?: string;
  };
  policy_configuration?: {
    minimum_stack_high_water_words?: number;
    stale_after_seconds?: number;
  };
  reason: string;
  policy_id: string;
};

export type EmbeddedRiskRow = {
  task_id: EmbeddedTaskId;
  assessment: EmbeddedRiskAssessment | null;
};

export function getLatestElevatedRiskEvent<T extends { risk_category: string }>(
  events: T[] | undefined,
): T | null {
  return [...(events ?? [])].reverse().find(
    (event) => event.risk_category === "ELEVATED",
  ) ?? null;
}

export function getLatestConfirmedCheckpointRun<
  T extends {
    checkpoint_saved_state?: unknown;
    events: { event: string }[];
  },
>(runs: T[]): T | null {
  return [...runs].reverse().find(
    (run) => run.checkpoint_saved_state !== undefined
      && run.events.some((event) => event.event === "checkpoint_saved"),
  ) ?? null;
}

export function getCurrentSessionCheckpointRun<
  T extends {
    bridge_session_id: string;
    checkpoint_saved_state?: unknown;
    events: { event: string }[];
  },
>(runs: T[], bridgeSessionId: string | null | undefined): T | null {
  if (!bridgeSessionId) return null;
  return [...runs].reverse().find(
    (run) => run.bridge_session_id === bridgeSessionId
      && run.checkpoint_saved_state !== undefined
      && run.events.some((event) => event.event === "checkpoint_saved"),
  ) ?? null;
}

export function getEmbeddedRiskRows(
  assessments: EmbeddedRiskAssessment[] | undefined,
): EmbeddedRiskRow[] {
  return EMBEDDED_TASK_IDS.map((task_id) => ({
    task_id,
    assessment:
      assessments?.find((assessment) => assessment.task_id === task_id) ?? null,
  }));
}

export function getEmbeddedRiskIndex(
  assessment: EmbeddedRiskAssessment | null,
): number | null {
  if (
    assessment === null ||
    assessment.risk_category === "UNKNOWN" ||
    assessment.risk_index === null ||
    !Number.isFinite(assessment.risk_index) ||
    assessment.risk_index < 0 ||
    assessment.risk_index > 100
  ) {
    return null;
  }
  return assessment.risk_index;
}

export function canRequestEmbeddedFault(
  assessments: EmbeddedRiskAssessment[] | undefined,
): boolean {
  const rows = getEmbeddedRiskRows(assessments);
  return rows.every(({ assessment }) => (
    assessment?.risk_category === "LOW"
    && assessment.telemetry_freshness === "fresh"
    && assessment.observed_inputs.task_state === "running"
  ));
}

export function canRequestNextEmbeddedFault(
  assessments: EmbeddedRiskAssessment[] | undefined,
  latestTasks: {
    task_id: string;
    run_id?: string;
    execution_count: number;
    received_at_utc?: string;
  }[] | undefined,
  latestRun: {
    run_id: string;
    status: string;
    restored_progress?: { counter: number };
    recovery_completed_at_utc?: string;
  } | null,
): boolean {
  if (!canRequestEmbeddedFault(assessments)) return false;
  if (latestRun === null) return true;
  if (
    latestRun.status !== "application_state_recovery_verified"
    || latestRun.restored_progress === undefined
    || latestRun.recovery_completed_at_utc === undefined
  ) {
    return false;
  }
  const sensorAssessment = assessments?.find(
    (assessment) => assessment.task_id === "sensor-sampler",
  );
  const sensorTelemetry = latestTasks?.find(
    (task) => task.task_id === "sensor-sampler",
  );
  return (
    sensorAssessment?.observed_inputs.run_id === latestRun.run_id
    && sensorTelemetry?.run_id === latestRun.run_id
    && sensorTelemetry.execution_count > latestRun.restored_progress.counter
    && sensorTelemetry.received_at_utc !== undefined
    && Date.parse(sensorTelemetry.received_at_utc)
      > Date.parse(latestRun.recovery_completed_at_utc)
  );
}
