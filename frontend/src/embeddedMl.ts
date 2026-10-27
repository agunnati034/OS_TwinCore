import type { EmbeddedRiskAssessment } from "./embeddedRisk";

export type EmbeddedMlAssessment = {
  status: "available" | "unavailable";
  task_id: string;
  observed_at_utc: string;
  telemetry_fresh: boolean;
  model_version: string;
  prediction?: "anomaly" | "inlier";
  raw_anomaly_score?: number;
  reason?: string;
  limitations?: string;
};

export type EmbeddedMlDecision = {
  task_id: string;
  bridge_session_id: string;
  run_id?: string | null;
  occurred_at_utc: string;
  model_version: string;
  ml_assessment: EmbeddedMlAssessment;
  checkpoint_command_issued: boolean;
};

export type EmbeddedMlSnapshot = {
  mode: string;
  shadow_only: boolean;
  checkpoint_commands_enabled: boolean;
  model: {
    available: boolean;
    model_version: string | null;
    reason: string;
  };
  latest_by_task: EmbeddedMlDecision[];
};

export type EmbeddedMlTaskView = {
  modelAvailable: boolean;
  modelVersion: string | null;
  assessment: EmbeddedMlAssessment | null;
  unavailableReason: string;
};

export function getEmbeddedMlTaskView(
  snapshot: EmbeddedMlSnapshot | undefined,
  task: {
    task_id: string;
    bridge_session_id: string;
    run_id?: string;
    received_at_utc: string;
  } | undefined,
  risk: EmbeddedRiskAssessment | null | undefined,
  backendConnected: boolean,
): EmbeddedMlTaskView {
  const modelAvailable = snapshot?.model.available === true;
  const modelVersion = modelAvailable ? snapshot.model.model_version : null;
  const unavailable = (reason: string): EmbeddedMlTaskView => ({
    modelAvailable,
    modelVersion,
    assessment: null,
    unavailableReason: reason,
  });

  if (!modelAvailable || !snapshot) {
    return unavailable(snapshot?.model.reason ?? "No model status is available.");
  }
  if (!backendConnected) {
    return unavailable("Backend disconnected; live inference cannot be confirmed.");
  }
  if (!task || !risk || risk.telemetry_freshness !== "fresh") {
    return unavailable("Fresh valid telemetry is required for inference.");
  }

  const decision = snapshot.latest_by_task.find(
    (item) => item.task_id === task.task_id,
  );
  const result = decision?.ml_assessment;
  if (
    !decision
    || !result
    || decision.bridge_session_id !== task.bridge_session_id
    || decision.occurred_at_utc !== task.received_at_utc
    || decision.model_version !== modelVersion
    || result.model_version !== modelVersion
    || result.task_id !== task.task_id
    || result.observed_at_utc !== task.received_at_utc
    || (task.run_id !== undefined && decision.run_id !== task.run_id)
  ) {
    return unavailable("No inference matches the latest task observation.");
  }
  if (
    result.status !== "available"
    || result.telemetry_fresh !== true
    || (result.prediction !== "anomaly" && result.prediction !== "inlier")
    || typeof result.raw_anomaly_score !== "number"
    || !Number.isFinite(result.raw_anomaly_score)
    || (result.prediction === "anomaly") !== (result.raw_anomaly_score > 0)
  ) {
    return unavailable(result.reason ?? "Inference is unavailable for this observation.");
  }

  return {
    modelAvailable,
    modelVersion,
    assessment: result,
    unavailableReason: "",
  };
}
