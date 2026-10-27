import assert from "node:assert/strict";
import test from "node:test";

import {
  canRequestEmbeddedFault,
  canRequestNextEmbeddedFault,
  getCurrentSessionCheckpointRun,
  getLatestConfirmedCheckpointRun,
  getLatestElevatedRiskEvent,
  getEmbeddedRiskIndex,
  getEmbeddedRiskRows,
} from "./embeddedRisk.ts";
import {
  getEmbeddedMlTaskView,
} from "./embeddedMl.ts";
import { formatAppInteger, formatBytes } from "./formatters.ts";

test("byte formatter uses readable binary units without redundant precision", () => {
  assert.equal(formatBytes(612), "612 B");
  assert.equal(formatBytes(12042240), "11.5 MiB");
  assert.equal(formatBytes(1024), "1 KiB");
  assert.equal(formatBytes(1024 ** 3), "1 GiB");
  assert.equal(formatBytes(null), "Unavailable");
});

test("application-state integers are grouped but remain exact dimensionless values", () => {
  assert.equal(formatAppInteger(34366666368), "34,366,666,368");
  assert.equal(formatAppInteger(0), "0");
  assert.equal(formatAppInteger(null), "Unavailable");
});

const assessment = (task_id, risk_category, risk_index) => ({
  task_id,
  risk_category,
  risk_index,
});

test("risk rows retain both FreeRTOS task identities and backend assessments", () => {
  const sensor = assessment("sensor-sampler", "LOW", 0);
  const control = assessment("control-loop", "ELEVATED", 100);

  assert.deepEqual(
    getEmbeddedRiskRows([sensor, control]),
    [
      { task_id: "sensor-sampler", assessment: sensor },
      { task_id: "control-loop", assessment: control },
    ],
  );
  assert.equal(getEmbeddedRiskIndex(sensor), 0);
  assert.equal(getEmbeddedRiskIndex(control), 100);
});

test("missing and unknown assessments never become a zero-risk score", () => {
  const unknown = assessment("sensor-sampler", "UNKNOWN", null);
  const [sensor, control] = getEmbeddedRiskRows([unknown]);

  assert.equal(getEmbeddedRiskIndex(sensor.assessment), null);
  assert.equal(control.assessment, null);
  assert.equal(getEmbeddedRiskIndex(control.assessment), null);
});

const mlObservationAt = "2026-10-27T08:30:59.777389+00:00";
const mlTask = {
  task_id: "sensor-sampler",
  bridge_session_id: "bridge-a",
  received_at_utc: mlObservationAt,
};
const freshMlRisk = {
  ...assessment("sensor-sampler", "LOW", 0),
  telemetry_freshness: "fresh",
};
const mlSnapshot = {
  mode: "shadow",
  shadow_only: true,
  checkpoint_commands_enabled: false,
  model: {
    available: true,
    model_version: "embedded-iforest-v1-example",
    reason: "model loaded",
  },
  latest_by_task: [{
    task_id: "sensor-sampler",
    bridge_session_id: "bridge-a",
    occurred_at_utc: mlObservationAt,
    model_version: "embedded-iforest-v1-example",
    checkpoint_command_issued: false,
    ml_assessment: {
      status: "available",
      task_id: "sensor-sampler",
      observed_at_utc: mlObservationAt,
      telemetry_fresh: true,
      model_version: "embedded-iforest-v1-example",
      prediction: "anomaly",
      raw_anomaly_score: 0.14106427021170787,
    },
  }],
};

test("embedded ML view displays only the real prediction matching the latest task observation", () => {
  const view = getEmbeddedMlTaskView(mlSnapshot, mlTask, freshMlRisk, true);

  assert.equal(view.modelAvailable, true);
  assert.equal(view.modelVersion, "embedded-iforest-v1-example");
  assert.equal(view.assessment?.prediction, "anomaly");
  assert.equal(view.assessment?.raw_anomaly_score, 0.14106427021170787);
});

test("embedded ML view accepts score signs matching Isolation Forest labels and rejects mismatches", () => {
  const withResult = (prediction, raw_anomaly_score) => ({
    ...mlSnapshot,
    latest_by_task: [{
      ...mlSnapshot.latest_by_task[0],
      ml_assessment: {
        ...mlSnapshot.latest_by_task[0].ml_assessment,
        prediction,
        raw_anomaly_score,
      },
    }],
  });

  assert.equal(
    getEmbeddedMlTaskView(withResult("anomaly", 0.1), mlTask, freshMlRisk, true).assessment?.prediction,
    "anomaly",
  );
  assert.equal(
    getEmbeddedMlTaskView(withResult("inlier", -0.1), mlTask, freshMlRisk, true).assessment?.prediction,
    "inlier",
  );
  assert.equal(
    getEmbeddedMlTaskView(withResult("inlier", 0), mlTask, freshMlRisk, true).assessment?.prediction,
    "inlier",
  );
  assert.equal(
    getEmbeddedMlTaskView(withResult("anomaly", -0.1), mlTask, freshMlRisk, true).assessment,
    null,
  );
  assert.equal(
    getEmbeddedMlTaskView(withResult("inlier", 0.1), mlTask, freshMlRisk, true).assessment,
    null,
  );
});

test("embedded ML view withholds inference for stale, mismatched, or disconnected telemetry", () => {
  const staleRisk = { ...freshMlRisk, telemetry_freshness: "stale" };
  assert.equal(
    getEmbeddedMlTaskView(mlSnapshot, mlTask, staleRisk, true).assessment,
    null,
  );
  assert.equal(
    getEmbeddedMlTaskView(
      mlSnapshot,
      { ...mlTask, received_at_utc: "2026-10-27T08:31:00.000Z" },
      freshMlRisk,
      true,
    ).assessment,
    null,
  );
  assert.equal(
    getEmbeddedMlTaskView(mlSnapshot, mlTask, freshMlRisk, false).assessment,
    null,
  );
});

test("missing embedded model is unavailable rather than a fabricated normal prediction", () => {
  const snapshot = {
    ...mlSnapshot,
    model: { available: false, model_version: null, reason: "model artifact not found" },
    latest_by_task: [],
  };
  const view = getEmbeddedMlTaskView(snapshot, mlTask, freshMlRisk, true);

  assert.equal(view.modelAvailable, false);
  assert.equal(view.assessment, null);
  assert.equal(view.unavailableReason, "model artifact not found");
});

test("host worker risk records cannot populate embedded task identities", () => {
  const rows = getEmbeddedRiskRows([
    { worker_id: "worker-1", risk_category: "HIGH", risk_score: 1 },
  ]);

  assert.deepEqual(
    rows.map((row) => row.assessment),
    [null, null],
  );
});

test("recent elevated transitions stay visible after live risk returns to LOW", () => {
  const transitions = [
    { task_id: "sensor-sampler", previous_category: "LOW", risk_category: "ELEVATED", risk_index: 100 },
    { task_id: "sensor-sampler", previous_category: "ELEVATED", risk_category: "LOW", risk_index: 0 },
  ];

  assert.equal(getLatestElevatedRiskEvent(transitions), transitions[0]);
});

test("historical checkpoint selection requires firmware save confirmation", () => {
  const runs = [
    {
      run_id: "request-only",
      events: [{ event: "checkpoint_requested" }],
    },
    {
      run_id: "saved",
      checkpoint_saved_state: { counter: 4 },
      events: [{ event: "checkpoint_saved" }],
    },
    {
      run_id: "state-without-event",
      checkpoint_saved_state: { counter: 5 },
      events: [{ event: "checkpoint_requested" }],
    },
  ];

  assert.equal(getLatestConfirmedCheckpointRun(runs), runs[1]);
  assert.equal(
    getLatestConfirmedCheckpointRun([{ events: [{ event: "checkpoint_requested" }] }]),
    null,
  );
});

test("checkpoint save evidence is live only for its matching QEMU session", () => {
  const run = {
    run_id: "saved",
    bridge_session_id: "qemu-session-a",
    checkpoint_saved_state: { counter: 4 },
    events: [{ event: "checkpoint_saved" }],
  };

  assert.equal(getCurrentSessionCheckpointRun([run], "qemu-session-a"), run);
  assert.equal(getCurrentSessionCheckpointRun([run], "qemu-session-b"), null);
  assert.equal(getCurrentSessionCheckpointRun([run], null), null);
});

test("embedded fault controls require both tasks to be fresh, running, and LOW", () => {
  const freshLow = (task_id) => ({
    ...assessment(task_id, "LOW", 0),
    telemetry_freshness: "fresh",
    observed_inputs: { task_state: "running" },
  });

  test("a new embedded run waits for fresh telemetry beyond verified restore progress", () => {
    const runId = "b5534e8f-bc8f-4b44-9bec-c69bbd9ee7ee";
    const assessments = ["sensor-sampler", "control-loop"].map((task_id) => ({
      ...assessment(task_id, "LOW", 0),
      telemetry_freshness: "fresh",
      observed_inputs: {
        task_state: "running",
        ...(task_id === "sensor-sampler" ? { run_id: runId } : {}),
      },
    }));
    const run = {
      run_id: runId,
      status: "application_state_recovery_verified",
      restored_progress: { counter: 12 },
      recovery_completed_at_utc: "2026-01-01T00:00:02.000Z",
    };

    assert.equal(
      canRequestNextEmbeddedFault(assessments, [], run),
      false,
    );
    assert.equal(
      canRequestNextEmbeddedFault(
        assessments,
        [{ task_id: "sensor-sampler", run_id: "old-run", execution_count: 14 }],
        run,
      ),
      false,
    );
    assert.equal(
      canRequestNextEmbeddedFault(
        assessments,
        [{ task_id: "sensor-sampler", run_id: runId, execution_count: 12 }],
        run,
      ),
      false,
    );
    assert.equal(
      canRequestNextEmbeddedFault(
        assessments,
        [{
          task_id: "sensor-sampler",
          run_id: runId,
          execution_count: 13,
          received_at_utc: "2026-01-01T00:00:01.900Z",
        }],
        run,
      ),
      false,
    );
    assert.equal(
      canRequestNextEmbeddedFault(
        assessments,
        [{
          task_id: "sensor-sampler",
          run_id: runId,
          execution_count: 13,
          received_at_utc: "2026-01-01T00:00:02.100Z",
        }],
        run,
      ),
      true,
    );
    assert.equal(
      canRequestNextEmbeddedFault(
        assessments,
        [{
          task_id: "sensor-sampler",
          run_id: runId,
          execution_count: 13,
          received_at_utc: "2026-01-01T00:00:03.000Z",
        }],
        { ...run, status: "failed" },
      ),
      false,
    );
  });
  const bothLow = [freshLow("sensor-sampler"), freshLow("control-loop")];

  assert.equal(canRequestEmbeddedFault(bothLow), true);
  assert.equal(
    canRequestEmbeddedFault([
      { ...bothLow[0], risk_category: "ELEVATED", risk_index: 100 },
      bothLow[1],
    ]),
    false,
  );
  assert.equal(
    canRequestEmbeddedFault([
      { ...bothLow[0], telemetry_freshness: "stale" },
      bothLow[1],
    ]),
    false,
  );
  assert.equal(
    canRequestEmbeddedFault([
      bothLow[0],
      { ...bothLow[1], observed_inputs: { task_state: "blocked" } },
    ]),
    false,
  );
});
