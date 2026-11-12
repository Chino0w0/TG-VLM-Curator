import { type FormEvent, useCallback, useEffect, useState } from "react";
import type { AdminApiClient } from "../api/client";
import type {
  DestinationChannel,
  MessageSearchFilters,
  MessageSummary,
  SourceChannel,
} from "../api/types";
import { ResponsiveTable } from "../components/ResponsiveTable";

export interface MessagesWorkspaceProps {
  client: AdminApiClient;
  onInspectMessage: (messageId: string) => void;
  onReviewMessage: (messageId: string) => void;
}

const EMPTY_FILTERS: MessageSearchFilters = {
  source_channel_id: "",
  published_from: "",
  published_before: "",
  processing_status: "",
  review_status: "",
  duplicate_status: "",
  blocked_from_analysis: "",
  negative_label: "",
  label_key: "",
  label_namespace: "",
  label_scope: "",
  pipeline_version_id: "",
  routing_policy_version_id: "",
  destination_channel_id: "",
  archive_state: "",
  query: "",
};

export function MessagesWorkspace({
  client,
  onInspectMessage,
  onReviewMessage,
}: MessagesWorkspaceProps) {
  const [sources, setSources] = useState<SourceChannel[]>([]);
  const [destinations, setDestinations] = useState<DestinationChannel[]>([]);
  const [draftFilters, setDraftFilters] =
    useState<MessageSearchFilters>(EMPTY_FILTERS);
  const [appliedFilters, setAppliedFilters] =
    useState<MessageSearchFilters>(EMPTY_FILTERS);

  const [items, setItems] = useState<MessageSummary[]>([]);
  const [currentCursor, setCurrentCursor] = useState<string | null>(null);
  const [cursorStack, setCursorStack] = useState<Array<string | null>>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const loadChannels = useCallback(async () => {
    try {
      const [srcList, dstList] = await Promise.all([
        client.listSourceChannels(),
        client.listDestinationChannels(),
      ]);
      setSources(srcList);
      setDestinations(dstList);
    } catch {
      // Channels are optional convenience selectors for filters
    }
  }, [client]);

  const loadPage = useCallback(
    async (filters: MessageSearchFilters, cursor: string | null) => {
      setLoading(true);
      setErrorMsg(null);
      try {
        const page = await client.listMessages(filters, cursor, 25);
        setItems(page.items);
        setNextCursor(page.next_cursor);
        setHasMore(page.has_more);
      } catch (err) {
        setErrorMsg(
          err instanceof Error ? err.message : "Failed to load messages.",
        );
      } finally {
        setLoading(false);
      }
    },
    [client],
  );

  useEffect(() => {
    void loadChannels();
  }, [loadChannels]);

  useEffect(() => {
    void loadPage(appliedFilters, currentCursor);
  }, [appliedFilters, currentCursor, loadPage]);

  function updateDraft<K extends keyof MessageSearchFilters>(
    key: K,
    value: string,
  ) {
    setDraftFilters((prev) => ({ ...prev, [key]: value }));
  }

  function handleApplySearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setCursorStack([]);
    setCurrentCursor(null);
    setAppliedFilters({ ...draftFilters });
  }

  function handleClearFilters() {
    setDraftFilters(EMPTY_FILTERS);
    setCursorStack([]);
    setCurrentCursor(null);
    setAppliedFilters(EMPTY_FILTERS);
  }

  function handleNextPage() {
    if (!nextCursor) return;
    setCursorStack((prev) => [...prev, currentCursor]);
    setCurrentCursor(nextCursor);
  }

  function handlePreviousPage() {
    if (cursorStack.length === 0) return;
    const nextStack = [...cursorStack];
    const previousCursor = nextStack.pop() ?? null;
    setCursorStack(nextStack);
    setCurrentCursor(previousCursor);
  }

  function handleResetToLatest() {
    setCursorStack([]);
    setCurrentCursor(null);
  }

  return (
    <section aria-labelledby="messages-heading">
      <div className="workspace-header">
        <div>
          <h1 id="messages-heading">Messages Browser</h1>
          <p className="workspace-subtitle">
            Multi-dimensional search across source channels, processing &amp;
            review states, Negative Gate blocks, layered GLOBAL/MEDIA labels,
            routing destinations, and archive states using strict cursor
            pagination.
          </p>
        </div>
        <button
          type="button"
          className="btn"
          onClick={() => void loadPage(appliedFilters, currentCursor)}
        >
          Refresh Page
        </button>
      </div>

      {errorMsg ? (
        <div role="alert" className="alert-banner alert-error">
          {errorMsg}
        </div>
      ) : null}

      <div className="panel">
        <h2 id="messages-filter-heading">Search &amp; Filter Messages</h2>
        <search aria-labelledby="messages-filter-heading">
          <form onSubmit={handleApplySearch}>
            <div className="form-grid">
              <div className="field">
                <label htmlFor="msg-filter-source">Source Channel</label>
                <select
                  id="msg-filter-source"
                  name="source_channel_id"
                  value={draftFilters.source_channel_id ?? ""}
                  onChange={(e) =>
                    updateDraft("source_channel_id", e.target.value)
                  }
                >
                  <option value="">All source channels</option>
                  {sources.map((src) => (
                    <option key={src.id} value={src.id}>
                      {src.display_name} ({src.telegram_peer_id})
                    </option>
                  ))}
                </select>
              </div>

              <div className="field">
                <label htmlFor="msg-filter-dest">Destination Channel</label>
                <select
                  id="msg-filter-dest"
                  name="destination_channel_id"
                  value={draftFilters.destination_channel_id ?? ""}
                  onChange={(e) =>
                    updateDraft("destination_channel_id", e.target.value)
                  }
                >
                  <option value="">All destinations</option>
                  {destinations.map((dst) => (
                    <option key={dst.id} value={dst.id}>
                      {dst.name} ({dst.telegram_peer_id})
                    </option>
                  ))}
                </select>
              </div>

              <div className="field">
                <label htmlFor="msg-filter-proc">Processing Status</label>
                <select
                  id="msg-filter-proc"
                  name="processing_status"
                  value={draftFilters.processing_status ?? ""}
                  onChange={(e) =>
                    updateDraft("processing_status", e.target.value)
                  }
                >
                  <option value="">Any status</option>
                  <option value="pending">pending</option>
                  <option value="processing">processing</option>
                  <option value="completed">completed</option>
                  <option value="failed">failed</option>
                  <option value="skipped">skipped</option>
                </select>
              </div>

              <div className="field">
                <label htmlFor="msg-filter-review">Review Status</label>
                <select
                  id="msg-filter-review"
                  name="review_status"
                  value={draftFilters.review_status ?? ""}
                  onChange={(e) => updateDraft("review_status", e.target.value)}
                >
                  <option value="">Any review state</option>
                  <option value="unreviewed">unreviewed</option>
                  <option value="in_review">in_review</option>
                  <option value="reviewed">reviewed</option>
                  <option value="needs_attention">needs_attention</option>
                </select>
              </div>

              <div className="field">
                <label htmlFor="msg-filter-dedup">Duplicate Status</label>
                <select
                  id="msg-filter-dedup"
                  name="duplicate_status"
                  value={draftFilters.duplicate_status ?? ""}
                  onChange={(e) =>
                    updateDraft("duplicate_status", e.target.value)
                  }
                >
                  <option value="">Any duplicate state</option>
                  <option value="unique">unique</option>
                  <option value="duplicate_skipped">duplicate_skipped</option>
                  <option value="duplicate_reclassified">
                    duplicate_reclassified
                  </option>
                </select>
              </div>

              <div className="field">
                <label htmlFor="msg-filter-blocked">
                  Blocked by Negative Gate
                </label>
                <select
                  id="msg-filter-blocked"
                  name="blocked_from_analysis"
                  value={draftFilters.blocked_from_analysis ?? ""}
                  onChange={(e) =>
                    updateDraft("blocked_from_analysis", e.target.value)
                  }
                >
                  <option value="">Any</option>
                  <option value="true">Blocked (true)</option>
                  <option value="false">Not blocked (false)</option>
                </select>
              </div>

              <div className="field">
                <label htmlFor="msg-filter-neg-label">Negative Label Hit</label>
                <select
                  id="msg-filter-neg-label"
                  name="negative_label"
                  value={draftFilters.negative_label ?? ""}
                  onChange={(e) =>
                    updateDraft("negative_label", e.target.value)
                  }
                >
                  <option value="">Any</option>
                  <option value="true">Has negative label</option>
                  <option value="false">No negative label</option>
                </select>
              </div>

              <div className="field">
                <label htmlFor="msg-filter-archive">Archive State</label>
                <select
                  id="msg-filter-archive"
                  name="archive_state"
                  value={draftFilters.archive_state ?? ""}
                  onChange={(e) => updateDraft("archive_state", e.target.value)}
                >
                  <option value="">Any archive state</option>
                  <option value="ready">ready</option>
                  <option value="deleted">deleted</option>
                  <option value="pending">pending</option>
                  <option value="failed">failed</option>
                </select>
              </div>

              <div className="field">
                <label htmlFor="msg-filter-label-key">Label Key</label>
                <input
                  id="msg-filter-label-key"
                  name="label_key"
                  type="text"
                  autoCorrect="off"
                  autoCapitalize="none"
                  spellCheck={false}
                  placeholder="e.g. anime_illustration"
                  value={draftFilters.label_key ?? ""}
                  onChange={(e) => updateDraft("label_key", e.target.value)}
                />
              </div>

              <div className="field">
                <label htmlFor="msg-filter-label-ns">Label Namespace</label>
                <select
                  id="msg-filter-label-ns"
                  name="label_namespace"
                  value={draftFilters.label_namespace ?? ""}
                  onChange={(e) =>
                    updateDraft("label_namespace", e.target.value)
                  }
                >
                  <option value="">effective (default)</option>
                  <option value="effective">effective</option>
                  <option value="model">model</option>
                  <option value="manual">manual</option>
                </select>
              </div>

              <div className="field">
                <label htmlFor="msg-filter-label-scope">Label Scope</label>
                <select
                  id="msg-filter-label-scope"
                  name="label_scope"
                  value={draftFilters.label_scope ?? ""}
                  onChange={(e) => updateDraft("label_scope", e.target.value)}
                >
                  <option value="">Any scope</option>
                  <option value="global">global</option>
                  <option value="media">media</option>
                </select>
              </div>

              <div className="field">
                <label htmlFor="msg-filter-query">Text Query</label>
                <input
                  id="msg-filter-query"
                  name="query"
                  type="search"
                  placeholder="Search original_text..."
                  value={draftFilters.query ?? ""}
                  onChange={(e) => updateDraft("query", e.target.value)}
                />
              </div>

              <div className="field">
                <label htmlFor="msg-filter-pub-from">
                  Published From (ISO UTC)
                </label>
                <input
                  id="msg-filter-pub-from"
                  name="published_from"
                  type="text"
                  autoCorrect="off"
                  autoCapitalize="none"
                  spellCheck={false}
                  placeholder="2026-01-01T00:00:00Z"
                  value={draftFilters.published_from ?? ""}
                  onChange={(e) =>
                    updateDraft("published_from", e.target.value)
                  }
                />
              </div>

              <div className="field">
                <label htmlFor="msg-filter-pub-before">
                  Published Before (ISO UTC)
                </label>
                <input
                  id="msg-filter-pub-before"
                  name="published_before"
                  type="text"
                  autoCorrect="off"
                  autoCapitalize="none"
                  spellCheck={false}
                  placeholder="2026-12-31T23:59:59Z"
                  value={draftFilters.published_before ?? ""}
                  onChange={(e) =>
                    updateDraft("published_before", e.target.value)
                  }
                />
              </div>

              <div className="field">
                <label htmlFor="msg-filter-pipeline-ver">
                  Pipeline Version UUID
                </label>
                <input
                  id="msg-filter-pipeline-ver"
                  name="pipeline_version_id"
                  type="text"
                  autoCorrect="off"
                  autoCapitalize="none"
                  spellCheck={false}
                  value={draftFilters.pipeline_version_id ?? ""}
                  onChange={(e) =>
                    updateDraft("pipeline_version_id", e.target.value)
                  }
                />
              </div>

              <div className="field">
                <label htmlFor="msg-filter-routing-ver">
                  Routing Policy Version UUID
                </label>
                <input
                  id="msg-filter-routing-ver"
                  name="routing_policy_version_id"
                  type="text"
                  autoCorrect="off"
                  autoCapitalize="none"
                  spellCheck={false}
                  value={draftFilters.routing_policy_version_id ?? ""}
                  onChange={(e) =>
                    updateDraft("routing_policy_version_id", e.target.value)
                  }
                />
              </div>
            </div>

            <div className="btn-row">
              <button type="submit" className="btn btn-primary">
                Apply Filters
              </button>
              <button
                type="button"
                className="btn"
                onClick={handleClearFilters}
              >
                Clear All Filters
              </button>
            </div>
          </form>
        </search>
      </div>

      <div className="panel">
        <div
          className="btn-row"
          style={{
            justifyContent: "space-between",
            marginBlockEnd: "0.85rem",
          }}
        >
          <span className="stat-meta">
            Showing {items.length} message(s) | Cursor depth:{" "}
            <strong className="mono">{cursorStack.length}</strong>
          </span>
          <div className="btn-row">
            <button
              type="button"
              className="btn"
              disabled={cursorStack.length === 0 && currentCursor === null}
              onClick={handleResetToLatest}
            >
              Reset to Latest
            </button>
            <button
              type="button"
              className="btn"
              disabled={cursorStack.length === 0}
              onClick={handlePreviousPage}
            >
              Previous Page
            </button>
            <button
              type="button"
              className="btn btn-primary"
              disabled={!hasMore || !nextCursor}
              onClick={handleNextPage}
            >
              Next Page
            </button>
          </div>
        </div>

        {loading ? <p>Loading messages...</p> : null}

        <ResponsiveTable
          caption="Logical messages ordered by cursor pagination"
          headers={[
            "TG Msg ID",
            "Published",
            "Processing",
            "Review",
            "Dedup / Gate",
            "Media",
            "Excerpt",
            "Actions",
          ]}
        >
          {items.map((msg) => (
            <tr key={msg.id}>
              <th scope="row">#{msg.primary_telegram_message_id}</th>
              <td data-label="Published" className="mono">
                {msg.published_at}
              </td>
              <td data-label="Processing">
                <span
                  className={
                    msg.processing_status === "completed"
                      ? "badge badge-ok"
                      : msg.processing_status === "failed"
                        ? "badge badge-danger"
                        : "badge"
                  }
                >
                  {msg.processing_status}
                </span>
              </td>
              <td data-label="Review">
                <span className="badge">{msg.review_status}</span>
              </td>
              <td data-label="Dedup / Gate">
                <span className="badge">{msg.duplicate_status}</span>{" "}
                {msg.blocked_from_analysis ? (
                  <span className="badge badge-warn">
                    Negative Gate ({msg.blocked_by_label_key ?? "blocked"})
                  </span>
                ) : null}
              </td>
              <td data-label="Media" className="mono">
                {msg.media_count}
              </td>
              <td data-label="Excerpt">
                {msg.original_text
                  ? msg.original_text.slice(0, 80)
                  : "(media-only)"}
              </td>
              <td data-label="Actions">
                <div className="btn-row">
                  <button
                    type="button"
                    className="btn"
                    onClick={() => onInspectMessage(msg.id)}
                  >
                    Causal Chain
                  </button>
                  <button
                    type="button"
                    className="btn"
                    onClick={() => onReviewMessage(msg.id)}
                  >
                    Review
                  </button>
                </div>
              </td>
            </tr>
          ))}
        </ResponsiveTable>
      </div>
    </section>
  );
}
