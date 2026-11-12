import { Suspense, lazy, useEffect, useMemo, useState } from "react";
import { AdminApiClient } from "./api/client";
import type { AdminSessionResponse, WorkspaceKey } from "./api/types";
import { WorkspaceErrorBoundary } from "./components/WorkspaceErrorBoundary";
import { LoginView } from "./components/LoginView";

const AnalysisConfigWorkspace = lazy(() =>
  import("./workspaces/AnalysisConfigWorkspace").then((m) => ({
    default: m.AnalysisConfigWorkspace,
  })),
);
const ArchiveManagementWorkspace = lazy(() =>
  import("./workspaces/ArchiveManagementWorkspace").then((m) => ({
    default: m.ArchiveManagementWorkspace,
  })),
);
const AuditLogWorkspace = lazy(() =>
  import("./workspaces/AuditLogWorkspace").then((m) => ({
    default: m.AuditLogWorkspace,
  })),
);
const DashboardWorkspace = lazy(() =>
  import("./workspaces/DashboardWorkspace").then((m) => ({
    default: m.DashboardWorkspace,
  })),
);
const MessageDetailWorkspace = lazy(() =>
  import("./workspaces/MessageDetailWorkspace").then((m) => ({
    default: m.MessageDetailWorkspace,
  })),
);
const MessagesWorkspace = lazy(() =>
  import("./workspaces/MessagesWorkspace").then((m) => ({
    default: m.MessagesWorkspace,
  })),
);
const OperationsWorkspace = lazy(() =>
  import("./workspaces/OperationsWorkspace").then((m) => ({
    default: m.OperationsWorkspace,
  })),
);
const ProcessingRangesWorkspace = lazy(() =>
  import("./workspaces/ProcessingRangesWorkspace").then((m) => ({
    default: m.ProcessingRangesWorkspace,
  })),
);
const ReviewWorkspace = lazy(() =>
  import("./workspaces/ReviewWorkspace").then((m) => ({
    default: m.ReviewWorkspace,
  })),
);
const RoutingWorkspace = lazy(() =>
  import("./workspaces/RoutingWorkspace").then((m) => ({
    default: m.RoutingWorkspace,
  })),
);
const SourceChannelsWorkspace = lazy(() =>
  import("./workspaces/SourceChannelsWorkspace").then((m) => ({
    default: m.SourceChannelsWorkspace,
  })),
);
const TelegramIdentitiesWorkspace = lazy(() =>
  import("./workspaces/TelegramIdentitiesWorkspace").then((m) => ({
    default: m.TelegramIdentitiesWorkspace,
  })),
);

const NAV_ITEMS: Array<{ key: WorkspaceKey; label: string }> = [
  { key: "dashboard", label: "Dashboard" },
  { key: "source-channels", label: "Source Channels" },
  { key: "processing-ranges", label: "Processing Ranges" },
  { key: "telegram-identities", label: "Identities & Secrets" },
  { key: "messages", label: "Messages" },
  { key: "message-detail", label: "Message Detail" },
  { key: "review", label: "Review & Overrides" },
  { key: "analysis-config", label: "Analysis & Test Bench" },
  { key: "routing", label: "Routing & Destinations" },
  { key: "archive-management", label: "Archive Cleanup" },
  { key: "operations", label: "Operations & Retries" },
  { key: "audit-log", label: "Audit Log" },
];

export interface AppProps {
  client?: AdminApiClient;
  initialSession?: AdminSessionResponse | null;
  initialWorkspace?: WorkspaceKey;
}

export function App({
  client: providedClient,
  initialSession,
  initialWorkspace = "dashboard",
}: AppProps) {
  const apiClient = useMemo(
    () => providedClient ?? new AdminApiClient(),
    [providedClient],
  );
  const [session, setSession] = useState<AdminSessionResponse | null>(
    initialSession ?? null,
  );
  const [checkingSession, setCheckingSession] = useState(
    initialSession === undefined,
  );
  const [activeWorkspace, setActiveWorkspace] =
    useState<WorkspaceKey>(initialWorkspace);
  const [selectedMessageId, setSelectedMessageId] = useState<string | null>(
    null,
  );

  useEffect(() => {
    if (initialSession !== undefined) {
      if (initialSession) {
        apiClient.setCsrfToken(initialSession.csrf_token);
      }
      return;
    }
    let cancelled = false;
    void apiClient
      .currentSession()
      .then((res) => {
        if (!cancelled) {
          setSession(res);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setSession(null);
        }
      })
      .finally(() => {
        if (!cancelled) {
          setCheckingSession(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [apiClient, initialSession]);

  const [signingOut, setSigningOut] = useState(false);
  const [logoutError, setLogoutError] = useState<string | null>(null);
  useEffect(() => apiClient.onUnauthorized(() => {
    setSession(null);
    setSelectedMessageId(null);
    setLogoutError(null);
  }), [apiClient]);

  async function handleSignOut() {
    if (signingOut) return;
    setSigningOut(true);
    setLogoutError(null);
    try {
      await apiClient.logout();
      setSession(null);
      setSelectedMessageId(null);
    } catch (error) {
      setLogoutError(`Sign out failed; the server session may still be active. Retry Sign Out. ${error instanceof Error ? error.message : "Network error"}`);
    } finally {
      setSigningOut(false);
    }
  }

  if (checkingSession) {
    return (
      <main className="login-container">
        <p>Checking administrator session...</p>
      </main>
    );
  }

  if (!session) {
    return (
      <LoginView
        client={apiClient}
        onAuthenticated={(authenticated) => setSession(authenticated)}
      />
    );
  }

  return (
    <div className="app-shell">
      <aside className="app-sidebar">
        <div className="brand-header">
          <p className="brand-title">TG-VLM-Curator</p>
          <span className="brand-subtitle">
            Single-Tenant Administration Console
          </span>
        </div>

        <nav className="primary-nav" aria-label="Primary">
          <ul>
            {NAV_ITEMS.map((item) => (
              <li key={item.key}>
                <button
                  type="button"
                  className="nav-link-btn"
                  aria-current={
                    activeWorkspace === item.key ? "page" : undefined
                  }
                  onClick={() => setActiveWorkspace(item.key)}
                >
                  {item.label}
                </button>
              </li>
            ))}
          </ul>
        </nav>

        <div className="sidebar-footer">
          <div>
            Signed in as{" "}
            <strong className="mono">{session.user.username}</strong>
          </div>
          <button
            type="button"
            className="btn"
            disabled={signingOut}
            onClick={() => void handleSignOut()}
          >
            Sign Out
          </button>
        </div>
      </aside>

      <main className="app-main">
        {logoutError ? <div role="alert" className="alert-banner alert-error">{logoutError}</div> : null}
        <WorkspaceErrorBoundary key={`${activeWorkspace}:${selectedMessageId ?? ""}`}>
          <Suspense
            fallback={
              <p className="stat-meta" aria-live="polite">
                Loading workspace...
              </p>
            }
          >
            {activeWorkspace === "dashboard" ? (
              <DashboardWorkspace
                client={apiClient}
                onNavigate={setActiveWorkspace}
              />
            ) : null}
            {activeWorkspace === "source-channels" ? (
              <SourceChannelsWorkspace client={apiClient} />
            ) : null}
            {activeWorkspace === "processing-ranges" ? (
              <ProcessingRangesWorkspace client={apiClient} />
            ) : null}
            {activeWorkspace === "telegram-identities" ? (
              <TelegramIdentitiesWorkspace client={apiClient} />
            ) : null}
            {activeWorkspace === "messages" ? (
              <MessagesWorkspace
                client={apiClient}
                onInspectMessage={(messageId) => {
                  setSelectedMessageId(messageId);
                  setActiveWorkspace("message-detail");
                }}
                onReviewMessage={(messageId) => {
                  setSelectedMessageId(messageId);
                  setActiveWorkspace("review");
                }}
              />
            ) : null}
            {activeWorkspace === "message-detail" ? (
              <MessageDetailWorkspace
                client={apiClient}
                selectedMessageId={selectedMessageId}
                onSelectMessageId={setSelectedMessageId}
                onOpenReview={(messageId) => {
                  setSelectedMessageId(messageId);
                  setActiveWorkspace("review");
                }}
              />
            ) : null}
            {activeWorkspace === "review" ? (
              <ReviewWorkspace
                client={apiClient}
                selectedMessageId={selectedMessageId}
                onSelectMessageId={setSelectedMessageId}
              />
            ) : null}
            {activeWorkspace === "analysis-config" ? (
              <AnalysisConfigWorkspace client={apiClient} />
            ) : null}
            {activeWorkspace === "routing" ? (
              <RoutingWorkspace client={apiClient} />
            ) : null}
            {activeWorkspace === "archive-management" ? (
              <ArchiveManagementWorkspace client={apiClient} />
            ) : null}
            {activeWorkspace === "operations" ? (
              <OperationsWorkspace client={apiClient} />
            ) : null}
            {activeWorkspace === "audit-log" ? (
              <AuditLogWorkspace client={apiClient} />
            ) : null}
          </Suspense>
        </WorkspaceErrorBoundary>
      </main>
    </div>
  );
}
