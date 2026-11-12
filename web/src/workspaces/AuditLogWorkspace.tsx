import { type FormEvent, useCallback, useEffect, useState } from "react";
import type { AdminApiClient } from "../api/client";
import type { AuditEventItem } from "../api/types";
import { ResponsiveTable } from "../components/ResponsiveTable";

export interface AuditLogWorkspaceProps {
  client: AdminApiClient;
}

export function AuditLogWorkspace({ client }: AuditLogWorkspaceProps) {
  const [entityType, setEntityType] = useState("");
  const [entityId, setEntityId] = useState("");
  const [eventType, setEventType] = useState("");
  const [appliedFilters, setAppliedFilters] = useState({
    entity_type: "",
    entity_id: "",
    event_type: "",
  });

  const [items, setItems] = useState<AuditEventItem[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [cursorStack, setCursorStack] = useState<Array<string | null>>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const loadAuditPage = useCallback(
    async (
      filters: { entity_type: string; entity_id: string; event_type: string },
      activeCursor: string | null,
    ) => {
      setErrorMsg(null);
      try {
        const page = await client.listAuditEvents({
          entity_type: filters.entity_type || undefined,
          entity_id: filters.entity_id || undefined,
          event_type: filters.event_type || undefined,
          cursor: activeCursor,
          limit: 30,
        });
        setItems(page.items);
        setNextCursor(page.next_cursor);
        setHasMore(page.has_more);
      } catch (err) {
        setErrorMsg(
          err instanceof Error ? err.message : "Failed to load audit events.",
        );
      }
    },
    [client],
  );

  useEffect(() => {
    void loadAuditPage(appliedFilters, cursor);
  }, [appliedFilters, cursor, loadAuditPage]);

  function handleSearchSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setCursorStack([]);
    setCursor(null);
    setAppliedFilters({
      entity_type: entityType.trim(),
      entity_id: entityId.trim(),
      event_type: eventType.trim(),
    });
  }

  return (
    <section aria-labelledby="audit-log-heading">
      <div className="workspace-header">
        <div>
          <h1 id="audit-log-heading">Audit Log</h1>
          <p className="workspace-subtitle">
            Immutable administrative and operational audit events with automatic
            secret redaction (`[REDACTED]`) and cursor pagination.
          </p>
        </div>
        <button
          type="button"
          className="btn"
          onClick={() => void loadAuditPage(appliedFilters, cursor)}
        >
          Refresh
        </button>
      </div>

      {errorMsg ? (
        <div role="alert" className="alert-banner alert-error">
          {errorMsg}
        </div>
      ) : null}

      <div className="panel">
        <h2 id="audit-search-heading">Filter Audit Events</h2>
        <search aria-labelledby="audit-search-heading">
          <form onSubmit={handleSearchSubmit}>
            <div className="form-grid">
              <div className="field">
                <label htmlFor="audit-entity-type">Entity Type</label>
                <input
                  id="audit-entity-type"
                  type="text"
                  autoCorrect="off"
                  autoCapitalize="none"
                  spellCheck={false}
                  placeholder="secret, message, cleanup_job..."
                  value={entityType}
                  onChange={(e) => setEntityType(e.target.value)}
                />
              </div>
              <div className="field">
                <label htmlFor="audit-entity-id">Entity ID</label>
                <input
                  id="audit-entity-id"
                  type="text"
                  autoCorrect="off"
                  autoCapitalize="none"
                  spellCheck={false}
                  value={entityId}
                  onChange={(e) => setEntityId(e.target.value)}
                />
              </div>
              <div className="field">
                <label htmlFor="audit-event-type">Event Type</label>
                <input
                  id="audit-event-type"
                  type="text"
                  autoCorrect="off"
                  autoCapitalize="none"
                  spellCheck={false}
                  placeholder="secret.created, message.manual_label..."
                  value={eventType}
                  onChange={(e) => setEventType(e.target.value)}
                />
              </div>
            </div>
            <div className="btn-row">
              <button type="submit" className="btn btn-primary">
                Apply Audit Filters
              </button>
              <button
                type="button"
                className="btn"
                onClick={() => {
                  setEntityType("");
                  setEntityId("");
                  setEventType("");
                  setCursorStack([]);
                  setCursor(null);
                  setAppliedFilters({
                    entity_type: "",
                    entity_id: "",
                    event_type: "",
                  });
                }}
              >
                Reset
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
            marginBlockEnd: "0.75rem",
          }}
        >
          <span className="stat-meta">Events loaded: {items.length}</span>
          <div className="btn-row">
            <button
              type="button"
              className="btn"
              disabled={cursorStack.length === 0}
              onClick={() => {
                const next = [...cursorStack];
                const prev = next.pop() ?? null;
                setCursorStack(next);
                setCursor(prev);
              }}
            >
              Previous Page
            </button>
            <button
              type="button"
              className="btn btn-primary"
              disabled={!hasMore || !nextCursor}
              onClick={() => {
                if (!nextCursor) return;
                setCursorStack((prev) => [...prev, cursor]);
                setCursor(nextCursor);
              }}
            >
              Next Page
            </button>
          </div>
        </div>

        <ResponsiveTable
          caption="Sanitized AuditEvents ordered newest first"
          headers={[
            "Timestamp",
            "Event Type",
            "Entity",
            "Actor",
            "Redacted Snapshot",
          ]}
        >
          {items.map((ev) => (
            <tr key={ev.id}>
              <th scope="row">{ev.created_at}</th>
              <td data-label="Event Type" className="mono">
                {ev.event_type}
              </td>
              <td data-label="Entity" className="mono">
                {ev.entity_type}
                {ev.entity_id ? `:${ev.entity_id.slice(0, 8)}` : ""}
              </td>
              <td data-label="Actor" className="mono">
                {ev.actor_admin_user_id ?? "system"}
              </td>
              <td data-label="Redacted Snapshot" className="mono">
                {JSON.stringify(ev.payload)}
              </td>
            </tr>
          ))}
        </ResponsiveTable>
      </div>
    </section>
  );
}
