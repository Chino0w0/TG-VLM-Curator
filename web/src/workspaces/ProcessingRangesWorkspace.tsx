import { type FormEvent, useCallback, useEffect, useState } from "react";
import type { AdminApiClient } from "../api/client";
import type {
  ProcessingRange,
  RangeExecution,
  SourceChannel,
} from "../api/types";
import { ResponsiveTable } from "../components/ResponsiveTable";
import { useMutationGuard } from "../hooks/useMutationGuard";
import { useRequestGeneration } from "../hooks/useRequestGeneration";

export interface ProcessingRangesWorkspaceProps {
  client: AdminApiClient;
}

export function ProcessingRangesWorkspace({
  client,
}: ProcessingRangesWorkspaceProps) {
  const reads = useRequestGeneration();
  const execReads = useRequestGeneration();
  const { busy, runAction } = useMutationGuard();
  const [loading, setLoading] = useState(true);
  const [execLoading, setExecLoading] = useState(false);
  const [ranges, setRanges] = useState<ProcessingRange[]>([]);
  const [sources, setSources] = useState<SourceChannel[]>([]);
  const [selectedRangeId, setSelectedRangeId] = useState<string | null>(null);
  const [executions, setExecutions] = useState<RangeExecution[]>([]);
  const [sourceChannelId, setSourceChannelId] = useState("");
  const [startAt, setStartAt] = useState("2026-11-01T00:00:00Z");
  const [endMode, setEndMode] = useState<"fixed" | "latest">("latest");
  const [endAt, setEndAt] = useState("2026-11-12T00:00:00Z");
  const [steadyAfterSeconds, setSteadyAfterSeconds] = useState("300");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [statusMsg, setStatusMsg] = useState<string | null>(null);

  const loadAll = useCallback(async () => {
    const request = reads.next();
    setLoading(true);
    setErrorMsg(null);
    try {
      const [rangeItems, sourceItems] = await Promise.all([
        client.listProcessingRanges(),
        client.listSourceChannels(),
      ]);
      if (!reads.isCurrent(request)) return;
      setRanges(rangeItems);
      setSources(sourceItems);
      if (sourceItems.length > 0) {
        setSourceChannelId((prev) => prev || sourceItems[0].id);
      }
    } catch (err) {
      if (!reads.isCurrent(request)) return;
      setErrorMsg(err instanceof Error ? err.message : "Failed to load processing ranges.");
    } finally {
      if (reads.isCurrent(request)) {
        setLoading(false);
      }
    }
  }, [client, reads]);

  useEffect(() => {
    void loadAll();
  }, [loadAll]);

  async function handleInspectExecutions(rangeId: string) {
    const request = execReads.next();
    setSelectedRangeId(rangeId);
    setExecLoading(true);
    setExecutions([]);
    setErrorMsg(null);
    try {
      const items = await client.listRangeExecutions(rangeId);
      if (!execReads.isCurrent(request)) return;
      setExecutions(items);
    } catch (err) {
      if (!execReads.isCurrent(request)) return;
      setErrorMsg(err instanceof Error ? err.message : "Failed to load executions.");
    } finally {
      if (execReads.isCurrent(request)) {
        setExecLoading(false);
      }
    }
  }

  async function handleCreateRange(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setErrorMsg(null);
    setStatusMsg(null);
    const trimmedStart = startAt.trim();
    const startMs = Date.parse(trimmedStart);
    if (Number.isNaN(startMs)) {
      setErrorMsg("Start Timestamp must be a valid ISO-8601 timestamp.");
      return;
    }
    const payload: Record<string, unknown> = {
      source_channel_id: sourceChannelId,
      start_at: trimmedStart,
      end_mode: endMode,
    };
    if (endMode === "fixed") {
      const trimmedEnd = endAt.trim();
      const endMs = Date.parse(trimmedEnd);
      if (Number.isNaN(endMs) || endMs <= startMs) {
        setErrorMsg(
          "End Timestamp must be a valid ISO-8601 timestamp after Start Timestamp.",
        );
        return;
      }
      payload.end_at = trimmedEnd;
    } else {
      const trimmedSteady = steadyAfterSeconds.trim();
      const parsedSteady = Number(trimmedSteady);
      if (
        !/^\d+$/.test(trimmedSteady) ||
        !Number.isSafeInteger(parsedSteady) ||
        parsedSteady < 1
      ) {
        setErrorMsg(
          "Steady Quiet Threshold must be a positive integer (>= 1 second).",
        );
        return;
      }
      payload.steady_after_seconds = parsedSteady;
    }

    await runAction(async () => {
      try {
        const created = await client.createProcessingRange(payload);
        setStatusMsg(`Created ${created.end_mode.toUpperCase()} processing range.`);
        await loadAll();
      } catch (err) {
        setErrorMsg(err instanceof Error ? err.message : "Failed to create processing range.");
      }
    });
  }

  async function handlePauseResume(range: ProcessingRange) {
    setErrorMsg(null);
    await runAction(async () => {
      try {
        if (range.status === "paused") {
          await client.resumeProcessingRange(range.id);
        } else if (range.status === "pending" || range.status === "active") {
          await client.pauseProcessingRange(range.id);
        }
        await loadAll();
      } catch (err) {
        setErrorMsg(err instanceof Error ? err.message : "Failed to update range state.");
      }
    });
  }

  return (
    <section aria-labelledby="ranges-heading">
      <div className="workspace-header">
        <div>
          <h1 id="ranges-heading">Processing Ranges</h1>
          <p className="workspace-subtitle">
            Create FIXED historical windows or continuous `start_at -&gt; latest`
            ranges with quiet-period stabilization, and monitor continuous
            gap-free `processing_watermark` vs `active_high_watermark`.
          </p>
        </div>
        <button type="button" className="btn" onClick={() => void loadAll()}>
          Refresh
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

      <div className="panel">
        <h2 id="create-range-title">Create ProcessingRange</h2>
        <form
          method="post"
          aria-labelledby="create-range-title"
          onSubmit={handleCreateRange}
        >
          <div className="form-grid">
            <div className="field">
              <label htmlFor="range-source">Source Channel *</label>
              <select
                id="range-source"
                name="source_channel_id"
                required
                value={sourceChannelId}
                onChange={(e) => setSourceChannelId(e.target.value)}
              >
                <option value="">-- Choose channel --</option>
                {sources.map((src) => (
                  <option key={src.id} value={src.id}>
                    {src.display_name} ({src.telegram_peer_id})
                  </option>
                ))}
              </select>
            </div>

            <div className="field">
              <label htmlFor="range-start-at">Start Timestamp (ISO-8601 UTC) *</label>
              <input
                id="range-start-at"
                name="start_at"
                type="text"
                required
                value={startAt}
                onChange={(e) => setStartAt(e.target.value)}
              />
            </div>

            <fieldset>
              <legend>Right Boundary Mode</legend>
              <div className="field-inline">
                <input
                  id="range-mode-latest"
                  name="end_mode"
                  type="radio"
                  value="latest"
                  checked={endMode === "latest"}
                  onChange={() => setEndMode("latest")}
                />
                <label htmlFor="range-mode-latest">
                  LATEST (wait steady quiet period)
                </label>
              </div>
              <div className="field-inline" style={{ marginBlockEnd: 0 }}>
                <input
                  id="range-mode-fixed"
                  name="end_mode"
                  type="radio"
                  value="fixed"
                  checked={endMode === "fixed"}
                  onChange={() => setEndMode("fixed")}
                />
                <label htmlFor="range-mode-fixed">
                  FIXED (immediate finite end_at)
                </label>
              </div>
            </fieldset>

            {endMode === "fixed" ? (
              <div className="field">
                <label htmlFor="range-end-at">End Timestamp (ISO-8601 UTC) *</label>
                <input
                  id="range-end-at"
                  name="end_at"
                  type="text"
                  required
                  value={endAt}
                  onChange={(e) => setEndAt(e.target.value)}
                />
              </div>
            ) : (
              <div className="field">
                <label htmlFor="range-steady-seconds">
                  Steady Quiet Threshold (seconds) *
                </label>
                <input
                  id="range-steady-seconds"
                  name="steady_after_seconds"
                  type="text"
                  inputMode="numeric"
                  required
                  value={steadyAfterSeconds}
                  onChange={(e) => setSteadyAfterSeconds(e.target.value)}
                />
              </div>
            )}
          </div>

          <button type="submit" className="btn btn-primary" disabled={busy}>
            Create ProcessingRange
          </button>
        </form>
      </div>

      <div className="panel">
        <h2>Configured ProcessingRanges ({ranges.length})</h2>
        <ResponsiveTable
          caption="Processing ranges, boundary modes, and monotonic watermarks"
          headers={[
            "Range ID",
            "Source",
            "Window Mode",
            "Status",
            "Active High Watermark",
            "Processing Watermark",
            "Actions",
          ]}
          loading={loading}
          emptyMessage="No processing ranges created yet."
        >
          {ranges.map((item) => {
            const source = sources.find((s) => s.id === item.source_channel_id);
            const canPause =
              item.status === "pending" || item.status === "active";
            const canResume = item.status === "paused";
            return (
              <tr key={item.id}>
                <th scope="row">{item.id.slice(0, 8)}</th>
                <td data-label="Source">
                  {source ? source.display_name : item.source_channel_id.slice(0, 8)}
                </td>
                <td data-label="Window Mode" className="mono">
                  {item.end_mode === "latest"
                    ? `${item.start_at} -> latest (${item.steady_after_seconds ?? 0}s)`
                    : `${item.start_at} -> ${item.end_at}`}
                </td>
                <td data-label="Status">
                  <span className="badge">{item.status}</span>
                  {item.last_error_code ? (
                    <div className="hint">{item.last_error_code}</div>
                  ) : null}
                </td>
                <td data-label="Active High Watermark" className="mono">
                  {item.active_high_watermark_message_id ?? "-"}
                </td>
                <td data-label="Processing Watermark" className="mono">
                  {item.processing_watermark_message_id ?? 0}
                </td>
                <td data-label="Actions">
                  <div className="btn-row">
                    {canPause || canResume ? (
                      <button
                        type="button"
                        className="btn"
                        disabled={busy}
                        onClick={() => void handlePauseResume(item)}
                      >
                        {canResume ? "Resume" : "Pause"}
                      </button>
                    ) : (
                      <span className="stat-meta">Terminal</span>
                    )}
                    <button
                      type="button"
                      className="btn"
                      onClick={() => void handleInspectExecutions(item.id)}
                    >
                      Executions
                    </button>
                  </div>
                </td>
              </tr>
            );
          })}
        </ResponsiveTable>
      </div>

      {selectedRangeId ? (
        <div className="panel">
          <h2>
            RangeExecution History for Range{" "}
            <span className="mono">{selectedRangeId.slice(0, 8)}</span>
          </h2>
          {execLoading ? (
            <p className="stat-meta">Loading RangeExecution windows...</p>
          ) : executions.length === 0 ? (
            <p className="stat-meta">No frozen RangeExecution windows recorded yet.</p>
          ) : (
            <ResponsiveTable
              caption="Immutable RangeExecution windows and continuous watermarks"
              headers={[
                "Execution ID",
                "(from, to]",
                "Watermark",
                "History Ingested",
                "Status",
                "Attempts / Error",
              ]}
            >
              {executions.map((exec) => (
                <tr key={exec.id}>
                  <th scope="row">{exec.id.slice(0, 8)}</th>
                  <td data-label="(from, to]" className="mono">
                    ({exec.from_message_id_exclusive}, {exec.to_message_id_inclusive}]
                  </td>
                  <td data-label="Watermark" className="mono">
                    {exec.watermark_message_id}
                  </td>
                  <td data-label="History Ingested">
                    {exec.history_ingested === undefined
                      ? "—"
                      : exec.history_ingested
                        ? "Yes"
                        : "No"}
                  </td>
                  <td data-label="Status">
                    <span className="badge">{exec.status}</span>
                  </td>
                  <td data-label="Attempts / Error" className="mono">
                    #{exec.attempt_count}{" "}
                    {exec.last_error_code ? `(${exec.last_error_code})` : ""}
                  </td>
                </tr>
              ))}
            </ResponsiveTable>
          )}
        </div>
      ) : null}
    </section>
  );
}
