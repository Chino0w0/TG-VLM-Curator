import { useCallback, useEffect, useState } from "react";
import type { AdminApiClient } from "../api/client";
import type {
  ConfigEntity,
  DashboardSummary,
  ProcessingRange,
  WorkspaceKey,
} from "../api/types";
import { ResponsiveTable } from "../components/ResponsiveTable";

function formatBytes(bytes: number): string {
  if (bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const idx = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / 1024 ** idx).toFixed(idx === 0 ? 0 : 2)} ${units[idx]}`;
}

export interface DashboardWorkspaceProps {
  client: AdminApiClient;
  onNavigate: (workspace: WorkspaceKey) => void;
}

export function DashboardWorkspace({
  client,
  onNavigate,
}: DashboardWorkspaceProps) {
  const [summary, setSummary] = useState<DashboardSummary | null>(null);
  const [ranges, setRanges] = useState<ProcessingRange[]>([]);
  const [inferenceProfiles, setInferenceProfiles] = useState<ConfigEntity[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const loadData = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [sumData, rangeData, infData] = await Promise.all([
        client.getDashboardSummary(),
        client.listProcessingRanges(),
        client.listConfigEntities("inference-profiles"),
      ]);
      setSummary(sumData);
      setRanges(rangeData);
      setInferenceProfiles(infData);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load dashboard.");
    } finally {
      setLoading(false);
    }
  }, [client]);

  useEffect(() => {
    void loadData();
  }, [loadData]);

  const latestRanges = ranges.filter((r) => r.end_mode === "latest");

  return (
    <section aria-labelledby="dashboard-heading">
      <div className="workspace-header">
        <div>
          <h1 id="dashboard-heading">Dashboard</h1>
          <p className="workspace-subtitle">
            Source channel health, ProcessingRange progress, LATEST steady-state
            windows, queue backlogs, identities, external Inference Profiles,
            and Archive storage.
          </p>
        </div>
        <button type="button" className="btn" onClick={() => void loadData()}>
          Refresh Telemetry
        </button>
      </div>

      {error ? (
        <div role="alert" className="alert-banner alert-error">
          {error}
        </div>
      ) : null}

      {loading && !summary ? <p>Loading dashboard telemetry...</p> : null}

      {summary ? (
        <>
          <div className="card-grid">
            <div className="panel">
              <h2>Source Channels</h2>
              <div className="stat-value">
                {summary.source_channels.enabled} / {summary.source_channels.total}
              </div>
              <p className="stat-meta">
                Active Profiles: {summary.source_channels.active_profiles} |
                Destinations: {summary.destination_channels.enabled}/
                {summary.destination_channels.total}
              </p>
              <button
                type="button"
                className="btn"
                onClick={() => onNavigate("source-channels")}
              >
                Manage Sources
              </button>
            </div>

            <div className="panel">
              <h2>Processing Ranges</h2>
              <div className="stat-value">{summary.processing_ranges.active}</div>
              <p className="stat-meta">
                Paused: {summary.processing_ranges.paused} | Completed:{" "}
                {summary.processing_ranges.completed} | Failed:{" "}
                {summary.processing_ranges.failed}
              </p>
              <button
                type="button"
                className="btn"
                onClick={() => onNavigate("processing-ranges")}
              >
                Inspect Watermarks
              </button>
            </div>

            <div className="panel">
              <h2>Messages & Review</h2>
              <div className="stat-value">{summary.messages.total}</div>
              <p className="stat-meta">
                Processed: {summary.messages.processed} | Blocked (Negative
                Gate): {summary.messages.blocked_from_analysis} | Failed:{" "}
                {summary.messages.failed} | Unreviewed:{" "}
                {summary.messages.unreviewed}
              </p>
              <div className="btn-row" style={{ marginBlockStart: "0.5rem" }}>
                <button
                  type="button"
                  className="btn"
                  onClick={() => onNavigate("messages")}
                >
                  Browse Messages
                </button>
                <button
                  type="button"
                  className="btn"
                  onClick={() => onNavigate("review")}
                >
                  Open Review
                </button>
              </div>
            </div>

            <div className="panel">
              <h2>Telegram Identities</h2>
              <div className="stat-value">
                {summary.identities.healthy} / {summary.identities.total}
              </div>
              <p className="stat-meta">
                Degraded: {summary.identities.degraded} | Failed:{" "}
                {summary.identities.failed} | FloodWait Events:{" "}
                {summary.operations.flood_wait.events}
              </p>
              <button
                type="button"
                className="btn"
                onClick={() => onNavigate("telegram-identities")}
              >
                Manage Identities
              </button>
            </div>

            <div className="panel">
              <h2>External Inference Profiles</h2>
              <div className="stat-value">{inferenceProfiles.length}</div>
              <p className="stat-meta">
                Published:{" "}
                {
                  inferenceProfiles.filter((item) => item.published_version !== null)
                    .length
                }{" "}
                (external HTTP VLM/LLM endpoints; never bundled in Compose)
              </p>
              <button
                type="button"
                className="btn"
                onClick={() => onNavigate("analysis-config")}
              >
                Configure Analysis
              </button>
            </div>

            <div className="panel">
              <h2>Archive Storage</h2>
              <div className="stat-value">
                {formatBytes(summary.archive.total_archive_bytes)}
              </div>
              <p className="stat-meta">
                Ready Images: {summary.archive.ready_images} | Ready Videos:{" "}
                {summary.archive.ready_videos} | Cleaned Blobs:{" "}
                {summary.archive.deleted_assets}
              </p>
              <button
                type="button"
                className="btn"
                onClick={() => onNavigate("archive-management")}
              >
                Archive Cleanup
              </button>
            </div>
          </div>

          <div className="card-grid">
            <div className="panel">
              <h2>Queue Backlogs & Worker Readiness</h2>
              <p className="stat-meta">
                Worker Ready:{" "}
                <span
                  className={
                    summary.operations.worker.ready
                      ? "badge badge-ok"
                      : "badge badge-danger"
                  }
                >
                  {summary.operations.worker.ready ? "READY" : "DEGRADED"}
                </span>{" "}
                Scheduler Ready:{" "}
                <span
                  className={
                    summary.operations.scheduler.ready
                      ? "badge badge-ok"
                      : "badge badge-danger"
                  }
                >
                  {summary.operations.scheduler.ready ? "READY" : "DEGRADED"}
                </span>
              </p>
              <ul>
                <li>
                  Publication Pending:{" "}
                  <strong className="mono">
                    {summary.operations.backlog.publication_pending}
                  </strong>{" "}
                  (Partial:{" "}
                  <strong className="mono">
                    {summary.operations.backlog.publication_partial}
                  </strong>
                  , Retry Due:{" "}
                  <strong className="mono">
                    {summary.operations.backlog.publication_retry_due}
                  </strong>
                  )
                </li>
                <li>
                  Cleanup Pending:{" "}
                  <strong className="mono">
                    {summary.operations.backlog.cleanup_pending}
                  </strong>
                </li>
                <li>
                  Durable Wake-ups Due:{" "}
                  <strong className="mono">
                    {summary.operations.backlog.durable_wakeups_due}
                  </strong>
                </li>
              </ul>
              <button
                type="button"
                className="btn"
                onClick={() => onNavigate("operations")}
              >
                Operations & Failed Steps
              </button>
            </div>

            <div className="panel">
              <h2>LATEST Continuous Ranges & Steady-State Windows</h2>
              {latestRanges.length === 0 ? (
                <p className="stat-meta">
                  No continuous LATEST ranges configured yet.
                </p>
              ) : (
                <ResponsiveTable
                  caption="Continuous LATEST ranges and watermark lag"
                  headers={[
                    "Range ID",
                    "Status",
                    "Steady Threshold",
                    "Active High Watermark",
                    "Processing Watermark",
                  ]}
                >
                  {latestRanges.map((item) => (
                    <tr key={item.id}>
                      <th scope="row">{item.id.slice(0, 8)}</th>
                      <td data-label="Status">
                        <span className="badge">{item.status}</span>
                      </td>
                      <td data-label="Steady Threshold" className="mono">
                        {item.steady_after_seconds ?? 0}s quiet wait
                      </td>
                      <td data-label="Active High Watermark" className="mono">
                        {item.active_high_watermark_message_id ?? "Waiting steady"}
                      </td>
                      <td data-label="Processing Watermark" className="mono">
                        {item.processing_watermark_message_id ?? 0}
                      </td>
                    </tr>
                  ))}
                </ResponsiveTable>
              )}
            </div>
          </div>
        </>
      ) : null}
    </section>
  );
}
