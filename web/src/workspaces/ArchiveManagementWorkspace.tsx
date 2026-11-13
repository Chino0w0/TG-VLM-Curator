import { useRequestGeneration } from "../hooks/useRequestGeneration";
import { type FormEvent, useCallback, useEffect, useRef, useState } from "react";
import type { AdminApiClient } from "../api/client";
import type {
  CleanupDryRun,
  CleanupFilterPayload,
  CleanupJob,
  SourceChannel,
} from "../api/types";
import { ModalDialog } from "../components/ModalDialog";
import { ResponsiveTable } from "../components/ResponsiveTable";

export interface ArchiveManagementWorkspaceProps {
  client: AdminApiClient;
}

function parseCsvList(raw: string): string[] | undefined {
  const items = raw
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  return items.length > 0 ? items : undefined;
}

const ALLOWED_CLEANUP_DUPLICATE_STATUSES = new Set([
  "unknown",
  "unique",
  "canonical",
  "duplicate",
  "duplicate_skipped",
  "duplicate_reclassify",
  "duplicate_analyzed",
]);

const FILTER_LABELS: Record<string, string> = {
  archived_from: "Archived from (inclusive UTC)", archived_before: "Archived before (exclusive UTC)",
  source_channel_ids: "Source channels", message_published_from: "Messages published from (inclusive UTC)",
  message_published_before: "Messages published before (exclusive UTC)", model_label_keys: "Model labels",
  manual_label_keys: "Manual labels", effective_label_keys: "Effective labels", label_scopes: "Label scopes",
  negative_label: "Negative label", review_statuses: "Review statuses", processing_statuses: "Processing statuses",
  duplicate_statuses: "Duplicate statuses", media_types: "Media types", blocked_from_analysis: "Blocked from analysis",
};

function CleanupScope({ snapshot, sources }: { snapshot: Record<string, unknown>; sources: SourceChannel[] }) {
  const entries = Object.entries(snapshot).filter(([, value]) => value !== null && value !== undefined && value !== "" && (!Array.isArray(value) || value.length > 0));
  return <><h3>Exact confirmed filter scope (UTC)</h3><dl>{entries.map(([key, value]) => {
    const formatted = Array.isArray(value) ? value.map(item => key === "source_channel_ids" ? `${sources.find(source => source.id === item)?.display_name ?? "Source"} (${item})` : String(item)).join(", ") : String(value);
    return <div key={key}><dt><strong>{FILTER_LABELS[key] ?? key}</strong></dt><dd style={{marginInlineStart: 0, overflowWrap: "anywhere"}}>{formatted}</dd></div>;
  })}</dl><p className="hint">All other filter dimensions are unrestricted. Only the matched local archive blobs are queued for deletion.</p><details><summary>Canonical filter snapshot</summary><pre className="code-block">{JSON.stringify(snapshot, null, 2)}</pre></details></>;
}

function formatBytes(bytes: number): string {
  if (bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const idx = Math.min(
    Math.floor(Math.log(bytes) / Math.log(1024)),
    units.length - 1,
  );
  return `${(bytes / 1024 ** idx).toFixed(idx === 0 ? 0 : 2)} ${units[idx]}`;
}

export function ArchiveManagementWorkspace({
  client,
}: ArchiveManagementWorkspaceProps) {
  const reads = useRequestGeneration();
  const [historyLoading, setHistoryLoading] = useState(false);
  const [sources, setSources] = useState<SourceChannel[]>([]);
  const [dryRuns, setDryRuns] = useState<CleanupDryRun[]>([]);
  const [jobs, setJobs] = useState<CleanupJob[]>([]);
  const [jobCursor, setJobCursor] = useState<string | null>(null);
  const [jobCursorStack, setJobCursorStack] = useState<Array<string | null>>(
    [],
  );
  const [jobNextCursor, setJobNextCursor] = useState<string | null>(null);
  const [jobHasMore, setJobHasMore] = useState(false);

  // 15-dimension CleanupFilter form fields
  const [archivedFrom, setArchivedFrom] = useState("");
  const [archivedBefore, setArchivedBefore] = useState("");
  const [sourceChannelId, setSourceChannelId] = useState("");
  const [msgPubFrom, setMsgPubFrom] = useState("");
  const [msgPubBefore, setMsgPubBefore] = useState("");
  const [modelLabelKeys, setModelLabelKeys] = useState("");
  const [manualLabelKeys, setManualLabelKeys] = useState("");
  const [effectiveLabelKeys, setEffectiveLabelKeys] = useState("");
  const [labelScopes, setLabelScopes] = useState("");
  const [negativeLabel, setNegativeLabel] = useState("");
  const [reviewStatuses, setReviewStatuses] = useState("");
  const [processingStatuses, setProcessingStatuses] = useState("");
  const [duplicateStatuses, setDuplicateStatuses] = useState("");
  const [mediaTypes, setMediaTypes] = useState("");
  const [blockedFromAnalysis, setBlockedFromAnalysis] = useState("");

  const previews = useRequestGeneration();
  const previewSnapshot = useRef<string | null>(null);
  const confirming = useRef(false);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [confirmLoading, setConfirmLoading] = useState(false);
  // Two-Step Safety Lock State
  const [activeDryRun, setActiveDryRun] = useState<CleanupDryRun | null>(null);
  const [confirmDialogOpen, setConfirmDialogOpen] = useState(false);

  const [statusMsg, setStatusMsg] = useState<string | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const loadHistory = useCallback(
    async (cursor: string | null) => {
      const request = reads.next();
      setHistoryLoading(true);
      setErrorMsg(null);
      try {
        const [srcList, dryList, jobPage] = await Promise.all([
          client.listSourceChannels(),
          client.listCleanupDryRuns(15),
          client.listCleanupJobs(cursor, 20),
        ]);
        if (!reads.isCurrent(request)) return;
        setSources(srcList);
        setDryRuns(dryList);
        setJobs(jobPage.items);
        setJobNextCursor(jobPage.next_cursor);
        setJobHasMore(jobPage.has_more);
      } catch (err) {
        if (!reads.isCurrent(request)) return;
        setErrorMsg(
          err instanceof Error
            ? err.message
            : "Failed to load archive cleanup history.",
        );
      } finally {
        if (reads.isCurrent(request)) setHistoryLoading(false);
      }
    },
    [client, reads],
  );

  useEffect(() => {
    void loadHistory(jobCursor);
  }, [loadHistory, jobCursor]);

  function invalidateDryRun() {
    previews.invalidate();
    previewSnapshot.current = null;
    setActiveDryRun(null);
    setPreviewLoading(false);
    setConfirmDialogOpen(false);
    setStatusMsg(null);
  }

  function buildFilterPayload(): CleanupFilterPayload {
    return {
      archived_from: archivedFrom.trim() || null,
      archived_before: archivedBefore.trim() || null,
      source_channel_ids: sourceChannelId ? [sourceChannelId] : undefined,
      message_published_from: msgPubFrom.trim() || null,
      message_published_before: msgPubBefore.trim() || null,
      model_label_keys: parseCsvList(modelLabelKeys),
      manual_label_keys: parseCsvList(manualLabelKeys),
      effective_label_keys: parseCsvList(effectiveLabelKeys),
      label_scopes: parseCsvList(labelScopes),
      negative_label:
        negativeLabel === "" ? null : negativeLabel === "true",
      review_statuses: parseCsvList(reviewStatuses),
      processing_statuses: parseCsvList(processingStatuses),
      duplicate_statuses: parseCsvList(duplicateStatuses),
      media_types: parseCsvList(mediaTypes),
      blocked_from_analysis:
        blockedFromAnalysis === "" ? null : blockedFromAnalysis === "true",
    };
  }

  async function handleRunDryRun(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setErrorMsg(null);
    setStatusMsg(null);
    if (confirming.current) return;
    const filter = buildFilterPayload();
    const invalidDedup = (filter.duplicate_statuses ?? []).filter(
      (item) => !ALLOWED_CLEANUP_DUPLICATE_STATUSES.has(item),
    );
    if (invalidDedup.length > 0) {
      setErrorMsg(
        `Invalid duplicate_statuses: ${invalidDedup.join(", ")}. Allowed values: duplicate, unique, canonical, unknown (or duplicate_skipped, duplicate_reclassify, duplicate_analyzed).`,
      );
      return;
    }
    const request = previews.next();
    previewSnapshot.current = JSON.stringify(filter);
    setActiveDryRun(null);
    setPreviewLoading(true);
    try {
      const created = await client.createCleanupDryRun(filter);
      if (!previews.isCurrent(request)) return;
      setActiveDryRun(created);
      setStatusMsg(
        `Step 1 Dry-Run ${created.id.slice(0, 8)} completed: ${created.estimated_asset_count} asset(s), ${formatBytes(created.estimated_bytes)}. Step 2 Confirm is now unlocked for this exact filter hash.`,
      );
      await loadHistory(jobCursor);
    } catch (err) {
      if (!previews.isCurrent(request)) return;
      setActiveDryRun(null);
      setErrorMsg(
        err instanceof Error ? err.message : "Cleanup Dry-Run failed.",
      );
    } finally {
      if (previews.isCurrent(request)) setPreviewLoading(false);
    }
  }

  async function handleConfirmCleanup() {
    setConfirmDialogOpen(false);
    if (!activeDryRun || confirming.current || previewSnapshot.current !== JSON.stringify(buildFilterPayload())) return;
    confirming.current = true;
    setConfirmLoading(true);
    const confirmedPreview = activeDryRun;
    invalidateDryRun();
    setErrorMsg(null);
    setStatusMsg(null);
    try {
      const job = await client.confirmCleanupJob({
        dry_run_id: confirmedPreview.id,
        expected_filter_hash: confirmedPreview.filter_hash,
      });
      setActiveDryRun(null);
      setStatusMsg(
        `CleanupJob ${job.id.slice(0, 8)} created (${job.status}). Normalized archive blobs queued for deletion; PostgreSQL metadata retained.`,
      );
      await loadHistory(jobCursor);
    } catch (err) {
      setErrorMsg(
        err instanceof Error ? `${err.message} Run a new preview before retrying.` : "Failed to confirm CleanupJob. Run a new preview before retrying.",
      );
    } finally {
      confirming.current = false;
      setConfirmLoading(false);
    }
  }

  return (
    <section aria-labelledby="archive-mgmt-heading">
      <div className="workspace-header">
        <div>
          <h1 id="archive-mgmt-heading">
            Archive Management &amp; Two-Step Cleanup
          </h1>
          <p className="workspace-subtitle">
            Reclaim normalized local archive disk space using 15-dimension
            filters and a mandatory `Dry-Run → Confirm` lock. Only local media
            blobs are deleted; PostgreSQL messages, labels, routing, and
            publication history are retained permanently.
          </p>
        </div>
        <button
          type="button"
          className="btn"
          onClick={() => void loadHistory(jobCursor)}
        >
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
        <h2 id="cleanup-filter-heading">
          Step 1: Configure Cleanup Filter &amp; Run Dry-Run Preview
        </h2>
        <div role="search" aria-labelledby="cleanup-filter-heading">
          <form onSubmit={handleRunDryRun}>
            <div className="form-grid">
              <div className="field">
                <label htmlFor="cln-source">Source Channel</label>
                <select
                  id="cln-source"
                  value={sourceChannelId}
                  onChange={(e) => {
                    setSourceChannelId(e.target.value);
                    invalidateDryRun();
                  }}
                >
                  <option value="">Any source channel</option>
                  {sources.map((src) => (
                    <option key={src.id} value={src.id}>
                      {src.display_name}
                    </option>
                  ))}
                </select>
              </div>

              <div className="field">
                <label htmlFor="cln-archived-before">
                  Archived Before (ISO UTC)
                </label>
                <input
                  id="cln-archived-before"
                  type="text"
                  autoCorrect="off"
                  autoCapitalize="none"
                  spellCheck={false}
                  placeholder="2026-12-01T00:00:00Z"
                  value={archivedBefore}
                  onChange={(e) => {
                    setArchivedBefore(e.target.value);
                    invalidateDryRun();
                  }}
                />
              </div>

              <div className="field">
                <label htmlFor="cln-archived-from">
                  Archived From (ISO UTC)
                </label>
                <input
                  id="cln-archived-from"
                  type="text"
                  autoCorrect="off"
                  autoCapitalize="none"
                  spellCheck={false}
                  value={archivedFrom}
                  onChange={(e) => {
                    setArchivedFrom(e.target.value);
                    invalidateDryRun();
                  }}
                />
              </div>

              <div className="field">
                <label htmlFor="cln-eff-labels">
                  Effective Label Keys (CSV)
                </label>
                <input
                  id="cln-eff-labels"
                  type="text"
                  autoCorrect="off"
                  autoCapitalize="none"
                  spellCheck={false}
                  placeholder="low_quality, advertisement"
                  value={effectiveLabelKeys}
                  onChange={(e) => {
                    setEffectiveLabelKeys(e.target.value);
                    invalidateDryRun();
                  }}
                />
              </div>

              <div className="field">
                <label htmlFor="cln-model-labels">Model Label Keys (CSV)</label>
                <input
                  id="cln-model-labels"
                  type="text"
                  autoCorrect="off"
                  autoCapitalize="none"
                  spellCheck={false}
                  value={modelLabelKeys}
                  onChange={(e) => {
                    setModelLabelKeys(e.target.value);
                    invalidateDryRun();
                  }}
                />
              </div>

              <div className="field">
                <label htmlFor="cln-manual-labels">
                  Manual Label Keys (CSV)
                </label>
                <input
                  id="cln-manual-labels"
                  type="text"
                  autoCorrect="off"
                  autoCapitalize="none"
                  spellCheck={false}
                  value={manualLabelKeys}
                  onChange={(e) => {
                    setManualLabelKeys(e.target.value);
                    invalidateDryRun();
                  }}
                />
              </div>

              <div className="field">
                <label htmlFor="cln-scopes">Label Scopes (CSV)</label>
                <input
                  id="cln-scopes"
                  type="text"
                  autoCorrect="off"
                  autoCapitalize="none"
                  spellCheck={false}
                  placeholder="global, media"
                  value={labelScopes}
                  onChange={(e) => {
                    setLabelScopes(e.target.value);
                    invalidateDryRun();
                  }}
                />
              </div>

              <div className="field">
                <label htmlFor="cln-neg-label">Negative Label</label>
                <select
                  id="cln-neg-label"
                  value={negativeLabel}
                  onChange={(e) => {
                    setNegativeLabel(e.target.value);
                    invalidateDryRun();
                  }}
                >
                  <option value="">Any</option>
                  <option value="true">Has negative label</option>
                  <option value="false">No negative label</option>
                </select>
              </div>

              <div className="field">
                <label htmlFor="cln-blocked">Blocked by Negative Gate</label>
                <select
                  id="cln-blocked"
                  value={blockedFromAnalysis}
                  onChange={(e) => {
                    setBlockedFromAnalysis(e.target.value);
                    invalidateDryRun();
                  }}
                >
                  <option value="">Any</option>
                  <option value="true">Blocked (true)</option>
                  <option value="false">Not blocked (false)</option>
                </select>
              </div>

              <div className="field">
                <label htmlFor="cln-review-statuses">
                  Review Statuses (CSV)
                </label>
                <input
                  id="cln-review-statuses"
                  type="text"
                  autoCorrect="off"
                  autoCapitalize="none"
                  spellCheck={false}
                  placeholder="reviewed, unreviewed"
                  value={reviewStatuses}
                  onChange={(e) => {
                    setReviewStatuses(e.target.value);
                    invalidateDryRun();
                  }}
                />
              </div>

              <div className="field">
                <label htmlFor="cln-proc-statuses">
                  Processing Statuses (CSV)
                </label>
                <input
                  id="cln-proc-statuses"
                  type="text"
                  autoCorrect="off"
                  autoCapitalize="none"
                  spellCheck={false}
                  placeholder="processed, failed"
                  value={processingStatuses}
                  onChange={(e) => {
                    setProcessingStatuses(e.target.value);
                    invalidateDryRun();
                  }}
                />
              </div>

              <div className="field">
                <label htmlFor="cln-dedup-statuses">
                  Duplicate Statuses (CSV)
                </label>
                <input
                  id="cln-dedup-statuses"
                  type="text"
                  autoCorrect="off"
                  autoCapitalize="none"
                  spellCheck={false}
                  placeholder="duplicate, unique, canonical, unknown"
                  value={duplicateStatuses}
                  onChange={(e) => {
                    setDuplicateStatuses(e.target.value);
                    invalidateDryRun();
                  }}
                />
                <span className="hint">
                  Values: duplicate, unique, canonical, unknown (or
                  duplicate_skipped, duplicate_reclassify)
                </span>
              </div>

              <div className="field">
                <label htmlFor="cln-media-types">Media Types (CSV)</label>
                <input
                  id="cln-media-types"
                  type="text"
                  autoCorrect="off"
                  autoCapitalize="none"
                  spellCheck={false}
                  placeholder="image, video"
                  value={mediaTypes}
                  onChange={(e) => {
                    setMediaTypes(e.target.value);
                    invalidateDryRun();
                  }}
                />
              </div>

              <div className="field">
                <label htmlFor="cln-msg-from">
                  Message Published From (ISO UTC)
                </label>
                <input
                  id="cln-msg-from"
                  type="text"
                  autoCorrect="off"
                  autoCapitalize="none"
                  spellCheck={false}
                  value={msgPubFrom}
                  onChange={(e) => {
                    setMsgPubFrom(e.target.value);
                    invalidateDryRun();
                  }}
                />
              </div>

              <div className="field">
                <label htmlFor="cln-msg-before">
                  Message Published Before (ISO UTC)
                </label>
                <input
                  id="cln-msg-before"
                  type="text"
                  autoCorrect="off"
                  autoCapitalize="none"
                  spellCheck={false}
                  value={msgPubBefore}
                  onChange={(e) => {
                    setMsgPubBefore(e.target.value);
                    invalidateDryRun();
                  }}
                />
              </div>
            </div>

            <div className="btn-row">
              <button type="submit" className="btn btn-primary" disabled={previewLoading || confirmLoading}>
                Step 1: Run Dry-Run Preview
              </button>
              <button
                type="button"
                className="btn btn-danger"
                disabled={!activeDryRun || previewLoading || confirmLoading}
                onClick={() => setConfirmDialogOpen(true)}
              >
                Step 2: Confirm Cleanup Job...
              </button>
            </div>
          </form>
        </div>

        {activeDryRun ? (
          <div
            className="alert-banner alert-info"
            style={{ marginBlockStart: "1rem" }}
          >
            <strong>Active Dry-Run Preview:</strong> ID{" "}
            <span className="mono">{activeDryRun.id}</span> | Filter Hash{" "}
            <span className="mono">{activeDryRun.filter_hash}</span> | Matched
            Assets:{" "}
            <strong className="mono">
              {activeDryRun.estimated_asset_count}
            </strong>{" "}
            across{" "}
            <strong className="mono">
              {activeDryRun.estimated_message_count}
            </strong>{" "}
            message(s) | Estimated Reclaimed Bytes:{" "}
            <strong className="mono">
              {formatBytes(activeDryRun.estimated_bytes)}
            </strong>
            . Modifying any filter above automatically invalidates this preview.
          </div>
        ) : (
          <p className="hint" style={{ marginBlockStart: "0.75rem" }}>
            Safety Lock Active: Run a Dry-Run Preview with at least one filter
            criterion to unlock Step 2 confirmation.
          </p>
        )}
      </div>

      <ModalDialog
        open={confirmDialogOpen}
        title="Confirm Archive Blob Cleanup"
        confirmLabel="Delete Matched Archive Blobs"
        destructive={true}
        confirmDisabled={!activeDryRun || previewLoading || confirmLoading}
        onConfirm={() => void handleConfirmCleanup()}
        onCancel={() => setConfirmDialogOpen(false)}
      >
        {activeDryRun ? (
          <><CleanupScope snapshot={activeDryRun.filter_snapshot} sources={sources} /><p>
            Permanently delete{" "}
            <strong className="mono">
              {activeDryRun.estimated_asset_count}
            </strong>{" "}
            normalized archive media blob(s) (
            <strong className="mono">
              {formatBytes(activeDryRun.estimated_bytes)}
            </strong>
            ) matching filter hash{" "}
            <span className="mono">
              {activeDryRun.filter_hash.slice(0, 16)}...
            </span>
            ? Assets referenced by active AnalysisRun or PublicationIntent rows
            are skipped automatically, and all PostgreSQL metadata is preserved.
          </p></>
        ) : null}
      </ModalDialog>

      <div className="card-grid">
        <div className="panel">
          <h2>Recent Dry-Run Previews ({dryRuns.length})</h2>
          <ResponsiveTable
            caption="Recorded CleanupDryRun previews"
            headers={["Dry-Run ID", "Assets", "Messages", "Bytes", "Hash"]}
            loading={historyLoading}
            emptyMessage="No cleanup dry-run previews recorded yet."
          >
            {dryRuns.map((dr) => (
              <tr key={dr.id}>
                <th scope="row">{dr.id.slice(0, 8)}</th>
                <td data-label="Assets" className="mono">
                  {dr.estimated_asset_count}
                </td>
                <td data-label="Messages" className="mono">
                  {dr.estimated_message_count}
                </td>
                <td data-label="Bytes" className="mono">
                  {formatBytes(dr.estimated_bytes)}
                </td>
                <td data-label="Hash" className="mono">
                  {dr.filter_hash.slice(0, 12)}
                </td>
              </tr>
            ))}
          </ResponsiveTable>
        </div>

        <div className="panel">
          <div
            className="btn-row"
            style={{
              justifyContent: "space-between",
              marginBlockEnd: "0.75rem",
            }}
          >
            <h2 style={{ margin: 0 }}>Cleanup Jobs ({jobs.length})</h2>
            <div className="btn-row">
              <button
                type="button"
                className="btn"
                disabled={historyLoading || jobCursorStack.length === 0}
                onClick={() => {
                  const stack = [...jobCursorStack];
                  const prev = stack.pop() ?? null;
                  setJobCursorStack(stack);
                  setJobCursor(prev);
                }}
              >
                Previous
              </button>
              <button
                type="button"
                className="btn"
                disabled={historyLoading || !jobHasMore || !jobNextCursor}
                onClick={() => {
                  if (!jobNextCursor) return;
                  setJobCursorStack((prev) => [...prev, jobCursor]);
                  setJobCursor(jobNextCursor);
                }}
              >
                Next
              </button>
            </div>
          </div>
          <ResponsiveTable
            caption="Confirmed CleanupJob executions"
            headers={["Job ID", "Status", "Est. Assets", "Deleted", "Skipped"]}
            loading={historyLoading}
            emptyMessage="No confirmed cleanup jobs executed yet."
          >
            {jobs.map((job) => (
              <tr key={job.id}>
                <th scope="row">{job.id.slice(0, 8)}</th>
                <td data-label="Status">
                  <span className="badge">{job.status}</span>
                </td>
                <td data-label="Est. Assets" className="mono">
                  {job.estimated_asset_count}
                </td>
                <td data-label="Deleted" className="mono">
                  {job.deleted_count ?? 0}
                </td>
                <td data-label="Skipped" className="mono">
                  {job.skipped_active_count ?? 0}
                </td>
              </tr>
            ))}
          </ResponsiveTable>
        </div>
      </div>
    </section>
  );
}
