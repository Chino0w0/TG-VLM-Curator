import { type FormEvent, useCallback, useEffect, useState } from "react";
import type { AdminApiClient } from "../api/client";
import type { FailedStepItem, OperationsStatusResponse } from "../api/types";
import { ResponsiveTable } from "../components/ResponsiveTable";
import { useMutationGuard } from "../hooks/useMutationGuard";
import { useRequestGeneration } from "../hooks/useRequestGeneration";

export interface OperationsWorkspaceProps {
  client: AdminApiClient;
}

export function OperationsWorkspace({ client }: OperationsWorkspaceProps) {
  const reads = useRequestGeneration();
  const { busy, runAction } = useMutationGuard();
  const [loading, setLoading] = useState(true);
  const [status, setStatus] = useState<OperationsStatusResponse | null>(null);
  const [stepKindFilter, setStepKindFilter] = useState("");
  const [failedItems, setFailedItems] = useState<FailedStepItem[]>([]);
  const [retryReason, setRetryReason] = useState(
    "Operator targeted step retry",
  );
  const [statusMsg, setStatusMsg] = useState<string | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const loadOperations = useCallback(
    async (stepKind: string) => {
      const request = reads.next();
      setLoading(true);
      setErrorMsg(null);
      try {
        const [opsStatus, items] = await Promise.all([
          client.getOperationsStatus(),
          client.listFailedItems(stepKind || undefined, 50),
        ]);
        if (!reads.isCurrent(request)) return;
        setStatus(opsStatus);
        setFailedItems(items);
      } catch (err) {
        if (!reads.isCurrent(request)) return;
        setErrorMsg(
          err instanceof Error
            ? err.message
            : "Failed to load operations telemetry.",
        );
      } finally {
        if (reads.isCurrent(request)) {
          setLoading(false);
        }
      }
    },
    [client, reads],
  );

  useEffect(() => {
    void loadOperations(stepKindFilter);
  }, [loadOperations, stepKindFilter]);

  function handleFilterSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void loadOperations(stepKindFilter);
  }

  async function handleRetryStep(item: FailedStepItem) {
    setErrorMsg(null);
    setStatusMsg(null);
    await runAction(async () => {
      try {
        await client.retryFailedStep({
          step_kind: item.step_kind,
          target_id: item.target_id,
          reason: retryReason.trim() || "Operator targeted step retry",
        });
        setStatusMsg(
          `Queued targeted step retry for ${item.step_kind} (${item.target_id.slice(0, 8)}).`,
        );
        await loadOperations(stepKindFilter);
      } catch (err) {
        setErrorMsg(
          err instanceof Error ? err.message : "Failed to retry failed step.",
        );
      }
    });
  }

  return (
    <section aria-labelledby="operations-heading">
      <div className="workspace-header">
        <div>
          <h1 id="operations-heading">Operations &amp; Step-Level Recovery</h1>
          <p className="workspace-subtitle">
            Inspect Worker and Scheduler readiness probes, queue backlogs,
            Telegram FloodWait counters, and retry individual failed steps
            without replaying healthy upstream stages.
          </p>
        </div>
        <button
          type="button"
          className="btn"
          onClick={() => void loadOperations(stepKindFilter)}
        >
          Refresh Status
        </button>
      </div>

      {errorMsg ? (
        <div role="alert" className="alert-banner alert-error">
          {errorMsg}
        </div>
      ) : null}
      {statusMsg ? (
        <div role="status" className="alert-banner alert-info">
          {statusMsg}
        </div>
      ) : null}

      {status ? (
        <div className="card-grid">
          <div className="panel">
            <h2>Worker Probe</h2>
            <p className="stat-meta">
              Ready:{" "}
              <span
                className={
                  status.operations.worker.ready
                    ? "badge badge-ok"
                    : "badge badge-danger"
                }
              >
                {status.operations.worker.ready ? "READY" : "NOT READY"}
              </span>{" "}
              | DB: {String(status.operations.worker.database_ready)} | Broker:{" "}
              {String(status.operations.worker.broker_ready)}
            </p>
            <p className="stat-meta">
              Blockers:{" "}
              <span className="mono">
                {status.operations.worker.blockers.join(", ") || "none"}
              </span>
            </p>
          </div>

          <div className="panel">
            <h2>Scheduler Probe</h2>
            <p className="stat-meta">
              Ready:{" "}
              <span
                className={
                  status.operations.scheduler.ready
                    ? "badge badge-ok"
                    : "badge badge-danger"
                }
              >
                {status.operations.scheduler.ready ? "READY" : "NOT READY"}
              </span>{" "}
              | DB: {String(status.operations.scheduler.database_ready)} |
              Broker: {String(status.operations.scheduler.broker_ready)}
            </p>
            <p className="stat-meta">
              Blockers:{" "}
              <span className="mono">
                {status.operations.scheduler.blockers.join(", ") || "none"}
              </span>
            </p>
          </div>

          <div className="panel">
            <h2>Publication &amp; FloodWait Telemetry</h2>
            <p className="stat-meta">
              Sent:{" "}
              <strong className="mono">
                {status.operations.publication.sent}
              </strong>{" "}
              | Retry Scheduled:{" "}
              <strong className="mono">
                {status.operations.publication.retry_scheduled}
              </strong>{" "}
              | Reconciled Parts:{" "}
              <strong className="mono">
                {status.operations.publication.reconciled_parts}
              </strong>
            </p>
            <p className="stat-meta">
              FloodWait Events:{" "}
              <strong className="mono">
                {status.operations.flood_wait.events}
              </strong>{" "}
              (Max: {status.operations.flood_wait.wait_seconds_max}s, Total:{" "}
              {status.operations.flood_wait.wait_seconds_total}s)
            </p>
          </div>

          <div className="panel">
            <h2>Queue Backlogs</h2>
            <pre className="code-block">
              {JSON.stringify(status.queues, null, 2)}
            </pre>
          </div>
        </div>
      ) : null}

      <div className="panel">
        <h2 id="failed-steps-heading">
          Failed Steps ({failedItems.length}) — Targeted Step Retry
        </h2>
        <div role="search" aria-labelledby="failed-steps-heading">
          <form onSubmit={handleFilterSubmit} className="form-grid">
            <div className="field">
              <label htmlFor="ops-step-kind">Filter by Step Kind</label>
              <select
                id="ops-step-kind"
                value={stepKindFilter}
                onChange={(e) => {
                  reads.invalidate();
                  setStepKindFilter(e.target.value);
                }}
              >
                <option value="">All step kinds</option>
                <option value="stage_run">stage_run</option>
                <option value="publication_intent">publication_intent</option>
                <option value="image_archive">image_archive</option>
                <option value="video_archive">video_archive</option>
                <option value="range_execution">range_execution</option>
                <option value="cleanup_job">cleanup_job</option>
              </select>
            </div>
            <div className="field">
              <label htmlFor="ops-retry-reason">Audit Reason for Retry</label>
              <input
                id="ops-retry-reason"
                type="text"
                value={retryReason}
                onChange={(e) => setRetryReason(e.target.value)}
              />
            </div>
          </form>
        </div>

        <ResponsiveTable
          caption="Failed pipeline steps eligible for targeted retry"
          headers={[
            "Step Kind",
            "Target ID",
            "Status",
            "Error Code",
            "Attempts",
            "Action",
          ]}
          loading={loading}
          emptyMessage="No failed pipeline steps match the selected filter."
        >
          {failedItems.map((item) => (
            <tr key={`${item.step_kind}-${item.target_id}`}>
              <th scope="row">{item.step_kind}</th>
              <td data-label="Target ID" className="mono">
                {item.target_id}
              </td>
              <td data-label="Status">
                <span className="badge badge-danger">{item.status}</span>
              </td>
              <td data-label="Error Code" className="mono">
                {item.error_code ?? "unknown"}
              </td>
              <td data-label="Attempts" className="mono">
                {item.attempt_count}
              </td>
              <td data-label="Action">
                <button
                  type="button"
                  className="btn btn-primary"
                  disabled={busy}
                  onClick={() => void handleRetryStep(item)}
                >
                  Retry Exact Step
                </button>
              </td>
            </tr>
          ))}
        </ResponsiveTable>
      </div>
    </section>
  );
}
