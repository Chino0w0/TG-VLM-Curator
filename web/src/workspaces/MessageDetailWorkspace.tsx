import { useRequestGeneration } from "../hooks/useRequestGeneration";
import { type FormEvent, useCallback, useEffect, useState } from "react";
import type { AdminApiClient } from "../api/client";
import type { MessageDetail } from "../api/types";
import { ResponsiveTable } from "../components/ResponsiveTable";

export interface MessageDetailWorkspaceProps {
  client: AdminApiClient;
  selectedMessageId: string | null;
  onSelectMessageId: (messageId: string) => void;
  onOpenReview: (messageId: string) => void;
}

export function MessageDetailWorkspace({
  client,
  selectedMessageId,
  onSelectMessageId,
  onOpenReview,
}: MessageDetailWorkspaceProps) {
  const reads = useRequestGeneration();
  const [lookupInput, setLookupInput] = useState(selectedMessageId ?? "");
  const [detail, setDetail] = useState<MessageDetail | null>(null);
  const [skipReason, setSkipReason] = useState("");
  const [loading, setLoading] = useState(false);
  const [statusMsg, setStatusMsg] = useState<string | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  useEffect(() => {
    if (selectedMessageId) {
      setLookupInput(selectedMessageId);
    }
  }, [selectedMessageId]);

  const loadDetail = useCallback(
    async (messageId: string) => {
      const request = reads.next();
      if (!messageId) return;
      setLoading(true);
      setErrorMsg(null);
      try {
        const res = await client.getMessageDetail(messageId);
        if (!reads.isCurrent(request)) return;
        setDetail(res);
      } catch (err) {
        if (!reads.isCurrent(request)) return;
        setErrorMsg(
          err instanceof Error ? err.message : "Failed to load message detail.",
        );
      } finally {
        if (reads.isCurrent(request)) setLoading(false);
      }
    },
    [client, reads],
  );

  useEffect(() => {
    if (selectedMessageId) {
      void loadDetail(selectedMessageId);
    }
  }, [selectedMessageId, loadDetail]);

  function handleLookupSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmed = lookupInput.trim();
    if (!trimmed) return;
    onSelectMessageId(trimmed);
    void loadDetail(trimmed);
  }

  async function handleSkipMessage(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!detail) return;
    setErrorMsg(null);
    setStatusMsg(null);
    try {
      await client.skipMessage(detail.message.id, skipReason.trim());
      setSkipReason("");
      setStatusMsg(
        "Message marked as skipped with audit trail; continuous watermark unblocked.",
      );
      await loadDetail(detail.message.id);
    } catch (err) {
      setErrorMsg(
        err instanceof Error ? err.message : "Failed to skip message.",
      );
    }
  }

  return (
    <section aria-labelledby="message-detail-heading">
      <div className="workspace-header">
        <div>
          <h1 id="message-detail-heading">
            Message Detail &amp; Causal Chain Inspector
          </h1>
          <p className="workspace-subtitle">
            End-to-end provenance from Telegram parts and perceptual hashes
            through StageRuns, InputManifests, InferenceCalls, layered
            Model/Manual/Effective labels, RoutingEvaluations, and
            PublishRecords.
          </p>
        </div>
        {detail ? (
          <div className="btn-row">
            <button
              type="button"
              className="btn"
              onClick={() => void loadDetail(detail.message.id)}
            >
              Refresh Chain
            </button>
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => onOpenReview(detail.message.id)}
            >
              Open in Review
            </button>
          </div>
        ) : null}
      </div>

      <div className="panel">
        <search aria-label="Lookup message by UUID">
          <form onSubmit={handleLookupSubmit} className="btn-row">
            <label htmlFor="detail-lookup-id">Message UUID:</label>
            <input
              id="detail-lookup-id"
              name="message_id"
              type="text"
              autoCorrect="off"
              autoCapitalize="none"
              spellCheck={false}
              placeholder="Paste logical message UUID..."
              value={lookupInput}
              onChange={(e) => setLookupInput(e.target.value)}
              style={{ flex: "1 1 20rem" }}
            />
            <button type="submit" className="btn btn-primary">
              Load Causal Chain
            </button>
          </form>
        </search>
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
      {loading ? <p>Loading causal chain...</p> : null}

      {detail ? (
        <>
          {/* 1. Telegram Snapshot & Parts */}
          <div className="panel">
            <h2>
              1. Telegram Logical Message (#
              {detail.message.primary_telegram_message_id})
            </h2>
            <p className="stat-meta">
              UUID: <span className="mono">{detail.message.id}</span> |
              Published:{" "}
              <span className="mono">{detail.message.published_at}</span> |
              Status:{" "}
              <span className="badge">{detail.message.processing_status}</span>{" "}
              | Review:{" "}
              <span className="badge">{detail.message.review_status}</span> |
              Source Deleted:{" "}
              <strong className="mono">
                {detail.message.source_deleted ? "yes" : "no"}
              </strong>
            </p>
            <pre className="code-block">
              {detail.message.original_text ?? "(no text caption)"}
            </pre>

            <ResponsiveTable
              caption="Telegram MessageParts (album / single message items)"
              headers={["Part ID", "Index", "Telegram Message ID", "Media Kind"]}
            >
              {detail.parts.map((part, index) => (
                <tr key={part.id}>
                  <th scope="row">{part.id.slice(0, 8)}</th>
                  <td data-label="Index" className="mono">
                    {index}
                  </td>
                  <td data-label="Telegram Message ID" className="mono">
                    {part.telegram_message_id}
                  </td>
                  <td data-label="Media Kind" className="mono">
                    {part.media?.map(item => String(item.kind ?? item.media_type ?? "media")).join(", ") || "text"}
                  </td>
                </tr>
              ))}
            </ResponsiveTable>
          </div>

          {/* 2. Media Assets, Hashes & Visual Deduplication */}
          <div className="panel">
            <h2>2. Normalized Media Assets &amp; Visual Deduplication</h2>
            {detail.duplicate_match ? (
              <div className="alert-banner alert-info">
                Duplicate Match: Canonical Message{" "}
                <strong className="mono">
                  {detail.duplicate_match.canonical_message_id}
                </strong>{" "}
                | Policy:{" "}
                <strong className="mono">
                  {detail.duplicate_match.duplicate_policy}
                </strong>{" "}
                | Hamming Distance:{" "}
                <strong className="mono">
                  {detail.duplicate_match.hamming_distance ?? "n/a"}
                </strong>
              </div>
            ) : (
              <p className="stat-meta">
                Duplicate Status:{" "}
                <span className="badge badge-ok">
                  {detail.message.duplicate_status}
                </span>
              </p>
            )}

            <ResponsiveTable
              caption="Normalized ImageAssets (SHA-256 & 64-bit pHash)"
              headers={[
                "Asset ID",
                "Archive State",
                "Dimensions",
                "pHash",
                "SHA-256",
              ]}
            >
              {detail.media.images.map((img) => (
                <tr key={img.id}>
                  <th scope="row">{img.id.slice(0, 8)}</th>
                  <td data-label="Archive State">
                    <span className="badge">{img.archive_state}</span>
                  </td>
                  <td data-label="Dimensions" className="mono">
                    {img.width ?? "?"}x{img.height ?? "?"}
                  </td>
                  <td data-label="pHash" className="mono">
                    {img.perceptual_hash ?? "n/a"}
                  </td>
                  <td data-label="SHA-256" className="mono">
                    {img.archive_sha256
                      ? `${img.archive_sha256.slice(0, 16)}...`
                      : "n/a"}
                  </td>
                </tr>
              ))}
            </ResponsiveTable>

            {detail.media.videos.length > 0 ? (
              <div style={{ marginBlockStart: "1rem" }}>
                <h3>
                  Sampled VideoAssets ({detail.media.videos.length})
                </h3>
                {detail.media.videos.map((vid) => (
                  <div key={vid.id} className="code-block">
                    Video {vid.id.slice(0, 8)} | archive_state=
                    {vid.archive_state} | cover_pHash=
                    {vid.source_cover_phash ?? "n/a"} | sampled_frames=
                    {vid.frames.length}
                  </div>
                ))}
              </div>
            ) : null}
          </div>

          {/* 3. Analysis Runs -> Stage Runs -> InputManifest -> InferenceCall */}
          <div className="panel">
            <h2>
              3. Analysis Runs, StageRuns, InputManifests &amp; InferenceCalls (
              {detail.analysis_runs.length})
            </h2>
            {detail.analysis_runs.map((run) => (
              <div
                key={run.id}
                style={{
                  border: "1px solid var(--border)",
                  borderRadius: "0.45rem",
                  padding: "0.85rem",
                  marginBlockEnd: "0.85rem",
                }}
              >
                <p className="stat-meta">
                  AnalysisRun <strong className="mono">{run.id}</strong> | Mode:{" "}
                  <span className="badge">{run.run_mode}</span> | Status:{" "}
                  <span className="badge">{run.status}</span> | Pipeline
                  Version:{" "}
                  <span className="mono">
                    {run.analysis_pipeline_version_id}
                  </span>
                </p>
                <ResponsiveTable
                  caption={`StageRuns for AnalysisRun ${run.id.slice(0, 8)}`}
                  headers={[
                    "StageRun",
                    "Node / Target",
                    "Status & Origin",
                    "Manifest Hash",
                    "Inference Call",
                  ]}
                >
                  {run.stage_runs.map((sr) => (
                    <tr key={sr.id}>
                      <th scope="row">{sr.id.slice(0, 8)}</th>
                      <td data-label="Node / Target" className="mono">
                        {sr.node_key ?? "stage"} / {sr.target_id ?? "message"}
                      </td>
                      <td data-label="Status & Origin">
                        <span className="badge">{sr.status}</span>{" "}
                        {sr.result_origin ? (
                          <span className="badge badge-accent">
                            {sr.result_origin}
                          </span>
                        ) : null}
                      </td>
                      <td data-label="Manifest Hash" className="mono">
                        {sr.input_manifest?.input_manifest_hash?.slice(0, 16) ?? "n/a"}
                        <details><summary>Manifest &amp; inference diagnostics</summary><pre className="code-block">{JSON.stringify({input_manifest: sr.input_manifest, inference_call: sr.inference_call, last_error_code: sr.last_error_code, last_error_type: sr.last_error_type}, null, 2)}</pre></details>
                      </td>
                      <td data-label="Inference Call" className="mono">
                        {sr.inference_call
                          ? `${sr.inference_call.provider_adapter} (${sr.inference_call.model_name}) HTTP ${sr.inference_call.http_status ?? "?"} · ${sr.inference_call.latency_ms ?? 0}ms`
                          : "Reused / Not invoked"}
                      </td>
                    </tr>
                  ))}
                </ResponsiveTable>
              </div>
            ))}
          </div>

          {/* 4. Layered Labels (Model vs Manual vs Effective) & Negative Gate */}
          <div className="panel">
            <h2>
              4. Layered Label Namespaces (Model Preserved vs Manual vs
              Effective)
            </h2>
            {detail.message.blocked_from_analysis ? (
              <div className="alert-banner alert-error">
                Negative Gate Triggered (`blocked_from_analysis=true`) by label{" "}
                <strong className="mono">
                  {detail.message.blocked_by_label_key ?? "negative_label"}
                </strong>{" "}
                — Media download &amp; heavy VLM bypassed; routing still
                evaluated.
              </div>
            ) : null}

            <div className="card-grid">
              <div>
                <h3>Model Namespace (Immutable VLM Output)</h3>
                <pre className="code-block">
                  {JSON.stringify(detail.labels.namespaces.model, null, 2)}
                </pre>
              </div>
              <div>
                <h3>Manual Namespace (Append-Only Overrides)</h3>
                <pre className="code-block">
                  {JSON.stringify(detail.labels.namespaces.manual, null, 2)}
                </pre>
              </div>
              <div>
                <h3>Effective Namespace (Manual ?? Model)</h3>
                <pre className="code-block">
                  {JSON.stringify(detail.labels.namespaces.effective, null, 2)}
                </pre>
              </div>
            </div>
          </div>

          {/* 5. Routing Evaluations -> PublicationIntents -> PublishAttempts -> PublishRecords */}
          <div className="panel">
            <h2>
              5. Routing Evaluations &amp; Publication Records (
              {detail.routing_evaluations.length})
            </h2>
            {detail.routing_evaluations.map((ev) => (
              <div
                key={ev.id}
                style={{
                  border: "1px solid var(--border)",
                  borderRadius: "0.45rem",
                  padding: "0.85rem",
                  marginBlockEnd: "0.85rem",
                }}
              >
                <p className="stat-meta">
                  Evaluation <strong className="mono">{ev.id}</strong> | Policy:{" "}
                  <span className="mono">{ev.routing_policy_version_id}</span> |
                  Facts Hash:{" "}
                  <span className="mono">{ev.facts_hash.slice(0, 16)}</span> |
                  Stopped At Rule:{" "}
                  <span className="mono">
                    {ev.stopped_at_rule_id ?? "none"}
                  </span>
                </p>
                <ResponsiveTable
                  caption={`PublicationIntents for RoutingEvaluation ${ev.id.slice(0, 8)}`}
                  headers={[
                    "Intent ID",
                    "Destination",
                    "Mode",
                    "Status",
                    "Attempts",
                    "Remote PublishRecords",
                  ]}
                >
                  {detail.publication_intents.filter((intent) => intent.routing_evaluation_id === ev.id).map((intent) => (
                    <tr key={intent.id}>
                      <th scope="row">{intent.id.slice(0, 8)}</th>
                      <td data-label="Destination" className="mono">
                        {intent.destination_channel_id}
                      </td>
                      <td data-label="Mode" className="mono">
                        {intent.publication_mode}
                      </td>
                      <td data-label="Status">
                        <span className="badge">{intent.status}</span>
                      </td>
                      <td data-label="Attempts" className="mono">
                        {intent.attempts.length}
                      </td>
                      <td data-label="Remote PublishRecords" className="mono">
                        {intent.records.length === 0
                          ? "none"
                          : intent.records
                              .map(
                                (r) => `${r.part}:${r.remote_message_ids.map(id => `#${id}`).join(", ")}`,
                              )
                              .join(", ")}
                      </td>
                    </tr>
                  ))}
                </ResponsiveTable>
              </div>
            ))}
          </div>

          {/* 6. Audit Trail & Audited Skip Control */}
          <div className="panel">
            <h2>
              6. Audit Events ({detail.audit_events.length}) &amp; Watermark
              Skip Control
            </h2>
            <ResponsiveTable
              caption="Message-scoped audit events"
              headers={["Timestamp", "Event Type", "Actor", "Payload"]}
            >
              {detail.audit_events.map((ae) => (
                <tr key={ae.id}>
                  <th scope="row">{ae.created_at}</th>
                  <td data-label="Event Type" className="mono">
                    {ae.event_type}
                  </td>
                  <td data-label="Actor" className="mono">
                    {ae.actor_admin_user_id ?? "system"}
                  </td>
                  <td data-label="Payload" className="mono">
                    {JSON.stringify(ae.payload)}
                  </td>
                </tr>
              ))}
            </ResponsiveTable>

            <form
              method="post"
              onSubmit={handleSkipMessage}
              style={{ marginBlockStart: "1rem" }}
            >
              <h3>Explicitly Skip Failed / Poison Message</h3>
              <p className="hint">
                Unblocks the gap-free continuous `processing_watermark` while
                recording an immutable AuditEvent.
              </p>
              <div className="field">
                <label htmlFor="skip-reason-input">Audit Reason *</label>
                <input
                  id="skip-reason-input"
                  name="reason"
                  type="text"
                  required
                  placeholder="Explain why this message is being skipped..."
                  value={skipReason}
                  onChange={(e) => setSkipReason(e.target.value)}
                />
              </div>
              <button type="submit" className="btn btn-danger">
                Skip Message &amp; Advance Watermark
              </button>
            </form>
          </div>
        </>
      ) : null}
    </section>
  );
}
