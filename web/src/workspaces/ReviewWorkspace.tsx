import { useMutationGuard } from "../hooks/useMutationGuard";
import { useRequestGeneration } from "../hooks/useRequestGeneration";
import {
  type FormEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { AdminApiClient } from "../api/client";
import type {
  ConfigEntity,
  MessageDetail,
  MessageSummary,
  RerouteResult,
} from "../api/types";
import { ResponsiveTable } from "../components/ResponsiveTable";

function getEligibleLabelOptions(
  entities: ConfigEntity[],
  scope: "global" | "media",
) {
  return entities.flatMap((ent) =>
    ent.versions
      .filter(
        (ver) =>
          ver.state === "published" &&
          ver.content.enabled !== false &&
          String(ver.content.scope ?? "global") === scope,
      )
      .map((ver) => ({
        entityName: ent.name,
        version: ver,
        key: String(ver.content.key ?? ent.name),
      })),
  );
}

export interface ReviewWorkspaceProps {
  client: AdminApiClient;
  selectedMessageId: string | null;
  onSelectMessageId: (messageId: string) => void;
}

export function ReviewWorkspace({
  client,
  selectedMessageId,
  onSelectMessageId,
}: ReviewWorkspaceProps) {
  const { busy, runAction } = useMutationGuard();
  const selectedId = useRef(selectedMessageId);
  selectedId.current = selectedMessageId;
  const onSelectMessageIdRef = useRef(onSelectMessageId);
  onSelectMessageIdRef.current = onSelectMessageId;
  const loadedMessageIdRef = useRef<string | null>(null);
  const queueRequests = useRequestGeneration();
  const detailRequests = useRequestGeneration();
  const [queueCursor, setQueueCursor] = useState<string | null>(null);
  const [queueNextCursor, setQueueNextCursor] = useState<string | null>(null);
  const [queueHasMore, setQueueHasMore] = useState(false);
  const [queueStack, setQueueStack] = useState<Array<string | null>>([]);
  const [queueLoading, setQueueLoading] = useState(false);
  const [queueFilter, setQueueFilter] = useState("unreviewed");
  const [queueItems, setQueueItems] = useState<MessageSummary[]>([]);
  const [labelDefs, setLabelDefs] = useState<ConfigEntity[]>([]);
  const [pipelines, setPipelines] = useState<ConfigEntity[]>([]);
  const [routingPolicies, setRoutingPolicies] = useState<ConfigEntity[]>([]);
  const [detail, setDetail] = useState<MessageDetail | null>(null);

  // Manual Label Override Form
  const [operation, setOperation] = useState<"set" | "clear">("set");
  const [targetScope, setTargetScope] = useState<"global" | "media">("global");
  const [targetKind, setTargetKind] = useState("message");
  const [targetId, setTargetId] = useState("");
  const [labelVersionId, setLabelVersionId] = useState("");
  const [labelKey, setLabelKey] = useState("");
  const [score, setScore] = useState("1.0");
  const [activated, setActivated] = useState(true);
  const [labelReason, setLabelReason] = useState("");

  // Review Status Form
  const [nextReviewStatus, setNextReviewStatus] = useState("reviewed");
  const [reviewReason, setReviewReason] = useState("");

  // Reanalyze & Reroute Forms
  const [reanalyzePipelineVersionId, setReanalyzePipelineVersionId] =
    useState("");
  const [reanalyzeReason, setReanalyzeReason] = useState("");
  const [reroutePolicyVersionId, setReroutePolicyVersionId] = useState("");
  const [rerouteReason, setRerouteReason] = useState("");
  const [reroutePreview, setReroutePreview] = useState<RerouteResult | null>(
    null,
  );

  const [statusMsg, setStatusMsg] = useState<string | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const matchingLabelOptions = useMemo(
    () => getEligibleLabelOptions(labelDefs, targetScope),
    [labelDefs, targetScope],
  );

  useEffect(() => {
    if (matchingLabelOptions.length === 0) return;
    if (
      !matchingLabelOptions.some((opt) => opt.version.id === labelVersionId)
    ) {
      setLabelVersionId(matchingLabelOptions[0].version.id);
      setLabelKey(matchingLabelOptions[0].key);
    }
  }, [matchingLabelOptions, labelVersionId]);

  const loadQueueAndMetadata = useCallback(async () => {
    const request = queueRequests.next();
    setQueueLoading(true);
    setErrorMsg(null);
    try {
      const [page, lbls, pipes, policies] = await Promise.all([
        client.listMessages(
          queueFilter ? { review_status: queueFilter } : {},
          queueCursor,
          25,
        ),
        client.listConfigEntities("label-definitions"),
        client.listConfigEntities("analysis-pipelines"),
        client.listConfigEntities("routing-policies"),
      ]);
      if (!queueRequests.isCurrent(request)) return;
      setQueueNextCursor(page.next_cursor);
      setQueueHasMore(page.has_more);
      setQueueItems(page.items);
      setLabelDefs(lbls);
      setPipelines(pipes);
      setRoutingPolicies(policies);
      if (!selectedId.current && page.items.length > 0) {
        onSelectMessageIdRef.current(page.items[0].id);
      }
    } catch (err) {
      if (queueRequests.isCurrent(request)) setErrorMsg(err instanceof Error ? err.message : "Failed to load review queue.");
    } finally {
      if (queueRequests.isCurrent(request)) setQueueLoading(false);
    }
  }, [client, queueFilter, queueCursor, queueRequests]);

  const loadSelectedDetail = useCallback(
    async (msgId: string) => {
      if (!msgId || msgId !== selectedId.current) return;
      const request = detailRequests.next();
      const isSwitchingMessage = loadedMessageIdRef.current !== msgId;
      if (isSwitchingMessage) {
        setDetail(null);
      }
      try {
        const loaded = await client.getMessageDetail(msgId);
        if (!detailRequests.isCurrent(request) || msgId !== selectedId.current) return;
        const isSameMessage = loadedMessageIdRef.current === loaded.message.id;
        loadedMessageIdRef.current = loaded.message.id;
        setDetail(loaded);
        if (!isSameMessage) {
          setTargetScope("global");
          setTargetKind("message");
          setTargetId(loaded.message.id);
        }
      } catch (err) {
        if (!detailRequests.isCurrent(request) || msgId !== selectedId.current) return;
        setErrorMsg(
          err instanceof Error
            ? err.message
            : "Failed to load selected message for review.",
        );
      }
    },
    [client, detailRequests],
  );

  useEffect(() => {
    void loadQueueAndMetadata();
  }, [loadQueueAndMetadata]);

  useEffect(() => {
    if (selectedMessageId) {
      setReroutePreview(null);
      void loadSelectedDetail(selectedMessageId);
    }
  }, [selectedMessageId, loadSelectedDetail]);

  async function handleManualLabelSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!detail) return;
    setErrorMsg(null);
    setStatusMsg(null);
    try {
      if (targetScope === "media" && !(targetKind === "image_asset" ? detail.media.images : detail.media.videos).some(asset => asset.id === targetId)) {
        throw new Error("Select an image or video belonging to this message.");
      }
      let parsedScore: number | undefined;
      if (operation === "set") {
        const rawScore = score.trim();
        const numericScore = Number(rawScore);
        if (
          rawScore === "" ||
          !Number.isFinite(numericScore) ||
          numericScore < 0 ||
          numericScore > 1
        ) {
          throw new Error(
            "Score must be a finite number between 0.0 and 1.0.",
          );
        }
        parsedScore = numericScore;
      }
      await client.applyManualLabel(detail.message.id, {
        operation,
        target_scope: targetScope,
        target_kind: targetKind,
        target_id: targetScope === "global" ? detail.message.id : targetId,
        image_asset_id: targetScope === "media" && targetKind === "image_asset" ? targetId : undefined,
        video_asset_id: targetScope === "media" && targetKind === "video_asset" ? targetId : undefined,
        label_definition_version_id: labelVersionId,
        label_key: labelKey.trim(),
        score: parsedScore,
        activated: operation === "set" ? activated : undefined,
        reason: labelReason.trim() || undefined,
      });
      setStatusMsg(
        `Append-only manual label event (${operation} ${labelKey}) recorded; ModelLabelAssignment preserved untouched.`,
      );
      await loadSelectedDetail(detail.message.id);
    } catch (err) {
      setErrorMsg(
        err instanceof Error ? err.message : "Failed to apply manual label.",
      );
    }
  }

  async function handleReviewTransition(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!detail) return;
    setErrorMsg(null);
    setStatusMsg(null);
    if (nextReviewStatus === detail.message.review_status) {
      setErrorMsg(
        `Review status is already '${detail.message.review_status}'. Choose a different target status.`,
      );
      return;
    }
    try {
      await client.transitionReviewStatus(
        detail.message.id,
        nextReviewStatus,
        reviewReason.trim() || undefined,
      );
      setStatusMsg(`Review status updated to '${nextReviewStatus}'.`);
      await Promise.all([
        loadSelectedDetail(detail.message.id),
        loadQueueAndMetadata(),
      ]);
    } catch (err) {
      setErrorMsg(
        err instanceof Error
          ? err.message
          : "Failed to transition review status.",
      );
    }
  }

  async function handleReanalyze(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!detail) return;
    setErrorMsg(null);
    setStatusMsg(null);
    try {
      await client.reanalyzeMessage(detail.message.id, {
        pipeline_version_id: reanalyzePipelineVersionId || undefined,
        reason: reanalyzeReason.trim() || undefined,
      });
      setStatusMsg(
        "Explicit Reanalysis triggered without overwriting manual overrides.",
      );
      await loadSelectedDetail(detail.message.id);
    } catch (err) {
      setErrorMsg(
        err instanceof Error ? err.message : "Failed to trigger reanalysis.",
      );
    }
  }

  async function runReroute(dryRun: boolean) {
    if (!detail) return;
    setErrorMsg(null);
    setStatusMsg(null);
    try {
      const result = await client.rerouteMessage(detail.message.id, {
        routing_policy_version_id: reroutePolicyVersionId || undefined,
        dry_run: dryRun,
        reason: rerouteReason.trim() || undefined,
      });
      setReroutePreview(result);
      setStatusMsg(
        dryRun
          ? `Dry-Run Re-route evaluated: ${result.comparison.added_destinations.length} added destination(s); existing PublishRecords untouched.`
          : `Formal Re-route executed: ${result.comparison.added_destinations.length} incremental destination(s) queued.`,
      );
      if (!dryRun) {
        await loadSelectedDetail(detail.message.id);
      }
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : "Re-route failed.");
    }
  }

  return (
    <section aria-labelledby="review-heading">
      <fieldset disabled={busy} style={{border: 0, padding: 0, margin: 0, minWidth: 0}}>
      <div className="workspace-header">
        <div>
          <h1 id="review-heading">Human Review &amp; Label Override</h1>
          <p className="workspace-subtitle">
            Append-only GLOBAL &amp; MEDIA manual label overrides (preserving
            raw ModelLabelAssignments), review status workflow, explicit
            reanalysis, and incremental re-route destination diffs.
          </p>
        </div>
        <div className="btn-row">
          <label htmlFor="review-queue-select">Queue:</label>
          <select
            id="review-queue-select"
            value={queueFilter}
            onChange={(e) => { queueRequests.invalidate(); setQueueCursor(null); setQueueStack([]); setQueueFilter(e.target.value); }}
          >
            <option value="unreviewed">unreviewed</option>
            <option value="needs_attention">needs_attention</option>
            <option value="in_review">in_review</option>
            <option value="reviewed">reviewed</option>
            <option value="">All messages</option>
          </select>
        </div>
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
        <h2>Review Queue ({queueItems.length})</h2>
        <p className="hint">The inspected message stays selected when paging, filtering, or recording a review. Select a queue row to inspect another message.</p>
        <div className="btn-row">
          <button className="btn" disabled={queueLoading || !queueStack.length} onClick={() => { const stack = [...queueStack]; setQueueCursor(stack.pop() ?? null); setQueueStack(stack); }}>Previous Queue Page</button>
          <button className="btn" disabled={queueLoading || !queueHasMore || !queueNextCursor} onClick={() => { setQueueStack(s => [...s, queueCursor]); setQueueCursor(queueNextCursor); }}>Next Queue Page</button>
        </div>
        <ResponsiveTable
          caption="Messages available for human review"
          headers={["TG Msg ID", "Review Status", "Excerpt", "Action"]}
          loading={queueLoading}
          emptyMessage="No messages in this review queue."
        >
          {queueItems.map((item) => (
            <tr key={item.id}>
              <th scope="row">#{item.primary_telegram_message_id}</th>
              <td data-label="Review Status">
                <span className="badge">{item.review_status}</span>
              </td>
              <td data-label="Excerpt">
                {item.original_text?.slice(0, 70) ?? "(media-only)"}
              </td>
              <td data-label="Action">
                <button
                  type="button"
                  className={
                    item.id === selectedMessageId
                      ? "btn btn-primary"
                      : "btn"
                  }
                  onClick={() => onSelectMessageId(item.id)}
                >
                  {item.id === selectedMessageId ? "Inspecting" : "Select"}
                </button>
              </td>
            </tr>
          ))}
        </ResponsiveTable>
      </div>

      {detail ? (
        <>
          <div className="card-grid">
            {/* Layered Labels View */}
            <div className="panel">
              <h2>
                Active Message #{detail.message.primary_telegram_message_id}
              </h2>
              <p className="stat-meta">
                Review Status:{" "}
                <span className="badge badge-accent">
                  {detail.message.review_status}
                </span>
              </p>
              <h3>Model Labels (Never Overwritten)</h3>
              <pre className="code-block">
                {JSON.stringify(detail.labels.namespaces.model, null, 2)}
              </pre>
              <h3>Effective Labels (Manual ?? Model)</h3>
              <pre className="code-block">
                {JSON.stringify(detail.labels.namespaces.effective, null, 2)}
              </pre>
            </div>

            {/* Append-Only Manual Label Form */}
            <div className="panel">
              <h2 id="manual-label-form-title">
                Append-Only Manual Label Override
              </h2>
              <form
                method="post"
                aria-labelledby="manual-label-form-title"
                onSubmit={event => { event.preventDefault(); void runAction(() => handleManualLabelSubmit(event)); }}
              >
                <div className="form-grid">
                  <div className="field">
                    <label htmlFor="rev-op">Operation</label>
                    <select
                      id="rev-op"
                      value={operation}
                      onChange={(e) =>
                        setOperation(e.target.value as "set" | "clear")
                      }
                    >
                      <option value="set">set (override)</option>
                      <option value="clear">
                        clear (revert to model output)
                      </option>
                    </select>
                  </div>

                  <div className="field">
                    <label htmlFor="rev-scope">Target Scope</label>
                    <select
                      id="rev-scope"
                      value={targetScope}
                      onChange={(e) => {
                        const nextScope = e.target.value as "global" | "media";
                        setTargetScope(nextScope);
                        if (nextScope === "global") {
                          setTargetKind("message");
                          setTargetId(detail.message.id);
                        } else {
                          const image = detail.media.images[0];
                          const video = detail.media.videos[0];
                          setTargetKind(image ? "image_asset" : "video_asset");
                          setTargetId(image?.id ?? video?.id ?? "");
                        }
                        const nextOpts = getEligibleLabelOptions(
                          labelDefs,
                          nextScope,
                        );
                        if (
                          nextOpts.length > 0 &&
                          !nextOpts.some(
                            (opt) => opt.version.id === labelVersionId,
                          )
                        ) {
                          setLabelVersionId(nextOpts[0].version.id);
                          setLabelKey(nextOpts[0].key);
                        } else if (nextOpts.length === 0) {
                          setLabelVersionId("");
                          setLabelKey("");
                        }
                      }}
                    >
                      <option value="global">global (message)</option>
                      <option value="media">media (per-asset)</option>
                    </select>
                  </div>

                  <div className="field">
                    <label htmlFor="rev-target-id">Target ID *</label>
                    <select id="rev-target-id" required value={`${targetKind}:${targetId}`} onChange={e => {
                      const [kind, id] = e.target.value.split(":"); setTargetKind(kind); setTargetId(id);
                    }}>
                      {targetScope === "global" ? <option value={`message:${detail.message.id}`}>Message {detail.message.id}</option> : <>
                        <option value="">-- Select media asset --</option>
                        {detail.media.images.map(asset => <option key={asset.id} value={`image_asset:${asset.id}`}>Image {asset.id}</option>)}
                        {detail.media.videos.map(asset => <option key={asset.id} value={`video_asset:${asset.id}`}>Video {asset.id}</option>)}
                      </>}
                    </select>
                  </div>

                  <div className="field">
                    <label htmlFor="rev-label-def">
                      Label Definition Version *
                    </label>
                    {matchingLabelOptions.length > 0 ? (
                      <select
                        id="rev-label-def"
                        value={labelVersionId}
                        onChange={(e) => {
                          setLabelVersionId(e.target.value);
                          const found = matchingLabelOptions.find(
                            (opt) => opt.version.id === e.target.value,
                          );
                          if (found) {
                            setLabelKey(found.key);
                          }
                        }}
                      >
                        {matchingLabelOptions.map((opt) => (
                          <option key={opt.version.id} value={opt.version.id}>
                            {opt.entityName} v{opt.version.version_number} (published · {targetScope})
                          </option>
                        ))}
                      </select>
                    ) : (
                      <input
                        id="rev-label-def"
                        type="text"
                        required
                        placeholder={`Published ${targetScope} label version UUID`}
                        value={labelVersionId}
                        onChange={(e) => setLabelVersionId(e.target.value)}
                      />
                    )}
                  </div>

                  <div className="field">
                    <label htmlFor="rev-label-key">Label Key *</label>
                    <input
                      id="rev-label-key"
                      type="text"
                      required
                      autoCorrect="off"
                      autoCapitalize="none"
                      spellCheck={false}
                      value={labelKey}
                      onChange={(e) => setLabelKey(e.target.value)}
                    />
                  </div>

                  {operation === "set" ? (
                    <div className="field">
                      <label htmlFor="rev-score">Score (0.0 – 1.0)</label>
                      <input
                        id="rev-score"
                        type="number"
                        step="0.01"
                        min="0"
                        max="1"
                        value={score}
                        onChange={(e) => setScore(e.target.value)}
                      />
                    </div>
                  ) : null}
                </div>

                {operation === "set" ? (
                  <div className="field-inline">
                    <input
                      id="rev-activated"
                      type="checkbox"
                      checked={activated}
                      onChange={(e) => setActivated(e.target.checked)}
                    />
                    <label htmlFor="rev-activated">Label Activated</label>
                  </div>
                ) : null}

                <div className="field">
                  <label htmlFor="rev-label-reason">Override Reason</label>
                  <input
                    id="rev-label-reason"
                    type="text"
                    value={labelReason}
                    onChange={(e) => setLabelReason(e.target.value)}
                  />
                </div>

                <button type="submit" className="btn btn-primary">
                  Record Manual Label Event
                </button>
              </form>
            </div>
          </div>

          <div className="card-grid">
            {/* Review Status & Explicit Reanalysis */}
            <div className="panel">
              <h2>Review Status &amp; Explicit Reanalysis</h2>
              <form method="post" onSubmit={event => { event.preventDefault(); void runAction(() => handleReviewTransition(event)); }}>
                <div className="form-grid">
                  <div className="field">
                    <label htmlFor="rev-next-status">New Review Status</label>
                    <select
                      id="rev-next-status"
                      value={nextReviewStatus}
                      onChange={(e) => setNextReviewStatus(e.target.value)}
                    >
                      <option value="unreviewed">unreviewed</option>
                      <option value="in_review">in_review</option>
                      <option value="reviewed">reviewed</option>
                      <option value="needs_attention">needs_attention</option>
                    </select>
                  </div>
                  <div className="field">
                    <label htmlFor="rev-status-reason">Transition Note</label>
                    <input
                      id="rev-status-reason"
                      type="text"
                      value={reviewReason}
                      onChange={(e) => setReviewReason(e.target.value)}
                    />
                  </div>
                </div>
                <button
                  type="submit"
                  className="btn btn-primary"
                  disabled={
                    busy || nextReviewStatus === detail.message.review_status
                  }
                >
                  Update Review Status
                </button>
                {nextReviewStatus === detail.message.review_status ? (
                  <p className="hint" style={{ marginBlockStart: "0.4rem" }}>
                    Message is already in &apos;{detail.message.review_status}
                    &apos; status.
                  </p>
                ) : null}
              </form>

              <hr
                style={{
                  borderColor: "var(--border)",
                  marginBlock: "1.1rem",
                }}
              />

              <form method="post" onSubmit={event => { event.preventDefault(); void runAction(() => handleReanalyze(event)); }}>
                <h3>Trigger Explicit Reanalysis</h3>
                <div className="form-grid">
                  <div className="field">
                    <label htmlFor="rev-reanalyze-pipe">
                      Pipeline Version (optional override)
                    </label>
                    <select
                      id="rev-reanalyze-pipe"
                      value={reanalyzePipelineVersionId}
                      onChange={(e) =>
                        setReanalyzePipelineVersionId(e.target.value)
                      }
                    >
                      <option value="">Active channel pipeline</option>
                      {pipelines.flatMap((p) =>
                        p.versions
                          .filter((v) => v.state === "published")
                          .map((v) => (
                            <option key={v.id} value={v.id}>
                              {p.name} v{v.version_number}
                            </option>
                          )),
                      )}
                    </select>
                  </div>
                  <div className="field">
                    <label htmlFor="rev-reanalyze-reason">Reason</label>
                    <input
                      id="rev-reanalyze-reason"
                      type="text"
                      value={reanalyzeReason}
                      onChange={(e) => setReanalyzeReason(e.target.value)}
                    />
                  </div>
                </div>
                <button type="submit" className="btn">
                  Trigger Reanalysis
                </button>
              </form>
            </div>

            {/* Re-route Dry-Run & Incremental Execution */}
            <div className="panel">
              <h2>Re-route &amp; Destination Diff</h2>
              <p className="hint">
                Dry-run previews destination diffs without creating intents;
                formal re-route queues only newly added destinations and never
                deletes prior PublishRecords.
              </p>
              <div className="form-grid">
                <div className="field">
                  <label htmlFor="rev-reroute-policy">
                    Routing Policy Version
                  </label>
                  <select
                    id="rev-reroute-policy"
                    value={reroutePolicyVersionId}
                    onChange={(e) => setReroutePolicyVersionId(e.target.value)}
                  >
                    <option value="">Active channel routing policy</option>
                    {routingPolicies.flatMap((rp) =>
                      rp.versions
                        .filter((v) => v.state === "published")
                        .map((v) => (
                          <option key={v.id} value={v.id}>
                            {rp.name} v{v.version_number}
                          </option>
                        )),
                    )}
                  </select>
                </div>
                <div className="field">
                  <label htmlFor="rev-reroute-reason">Reason</label>
                  <input
                    id="rev-reroute-reason"
                    type="text"
                    value={rerouteReason}
                    onChange={(e) => setRerouteReason(e.target.value)}
                  />
                </div>
              </div>

              <div className="btn-row">
                <button
                  type="button"
                  className="btn"
                  onClick={() => void runAction(() => runReroute(true))}
                >
                  Preview Dry-Run Re-route
                </button>
                <button
                  type="button"
                  className="btn btn-primary"
                  onClick={() => void runAction(() => runReroute(false))}
                >
                  Execute Formal Re-route
                </button>
              </div>

              {reroutePreview ? (
                <div style={{ marginBlockStart: "1rem" }}>
                  <p className="stat-meta">
                    Mode:{" "}
                    <span className="badge">
                      {reroutePreview.dry_run ? "DRY-RUN" : "FORMAL"}
                    </span>{" "}
                    | Previous Evaluation:{" "}
                    <strong className="mono">
                      {reroutePreview.comparison.previous_evaluation_id ?? "none"}
                    </strong>{" "}
                    | Newly Added Destinations:{" "}
                    <strong className="mono">
                      {reroutePreview.comparison.added_destinations.join(
                        ", ",
                      ) || "none"}
                    </strong>
                  </p>
                  <pre className="code-block">
                    {JSON.stringify(reroutePreview, null, 2)}
                  </pre>
                </div>
              ) : null}
            </div>
          </div>
        </>
      ) : null}
      </fieldset>
    </section>
  );
}
