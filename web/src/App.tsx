import { useEffect, useMemo, useState } from "react";
import { AdminApiClient } from "./api/client";
import type { AdminSessionResponse, WorkspaceKey } from "./api/types";
import { LoginView } from "./components/LoginView";
import { AnalysisConfigWorkspace } from "./workspaces/AnalysisConfigWorkspace";
import { ArchiveManagementWorkspace } from "./workspaces/ArchiveManagementWorkspace";
import { AuditLogWorkspace } from "./workspaces/AuditLogWorkspace";
import { DashboardWorkspace } from "./workspaces/DashboardWorkspace";
import { MessageDetailWorkspace } from "./workspaces/MessageDetailWorkspace";
import { MessagesWorkspace } from "./workspaces/MessagesWorkspace";
import { OperationsWorkspace } from "./workspaces/OperationsWorkspace";
import { ProcessingRangesWorkspace } from "./workspaces/ProcessingRangesWorkspace";
import { ReviewWorkspace } from "./workspaces/ReviewWorkspace";
import { RoutingWorkspace } from "./workspaces/RoutingWorkspace";
import { SourceChannelsWorkspace } from "./workspaces/SourceChannelsWorkspace";
import { TelegramIdentitiesWorkspace } from "./workspaces/TelegramIdentitiesWorkspace";

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

  async function handleSignOut() {
    try {
      await apiClient.logout();
    } finally {
      setSession(null);
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
            onClick={() => void handleSignOut()}
          >
            Sign Out
          </button>
        </div>
      </aside>

      <main className="app-main">
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
      </main>
    </div>
  );
}
