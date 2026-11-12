import type {
  AdminSessionResponse,
  AuditEventItem,
  CleanupDryRun,
  CleanupFilterPayload,
  CleanupJob,
  ConfigEntity,
  ConfigFamily,
  ConfigVersion,
  CursorPageResponse,
  DashboardSummary,
  DestinationChannel,
  FailedStepItem,
  MessageDetail,
  MessageSearchFilters,
  MessageSummary,
  OperationsStatusResponse,
  ProcessingRange,
  PromptTestBenchResponse,
  RangeExecution,
  RerouteResult,
  SecretStatusRecord,
  SourceChannel,
  TelegramIdentity,
} from "./types";

export class ApiClientError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "ApiClientError";
    this.status = status;
    this.code = code;
  }
}

const MUTATING_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

export class AdminApiClient {
  private csrfToken: string | null = null;
  private readonly baseUrl: string;
  private readonly fetchImpl: typeof fetch;

  constructor(options?: { baseUrl?: string; fetchImpl?: typeof fetch }) {
    this.baseUrl = options?.baseUrl ?? "";
    this.fetchImpl = options?.fetchImpl ?? globalThis.fetch.bind(globalThis);
  }

  get currentCsrfToken(): string | null {
    return this.csrfToken;
  }

  setCsrfToken(token: string | null): void {
    this.csrfToken = token;
  }

  private async request<T>(
    path: string,
    options: {
      method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
      body?: unknown;
      params?: Record<string, string | number | boolean | undefined | null>;
      skipCsrf?: boolean;
    } = {},
  ): Promise<T> {
    const method = options.method ?? "GET";
    const url = new URL(`${this.baseUrl}${path}`, "http://localhost");
    if (options.params) {
      for (const [key, value] of Object.entries(options.params)) {
        if (value !== undefined && value !== null && value !== "") {
          url.searchParams.set(key, String(value));
        }
      }
    }
    const target = this.baseUrl
      ? url.toString()
      : `${url.pathname}${url.search}`;

    const headers: Record<string, string> = {
      Accept: "application/json",
    };
    if (options.body !== undefined) {
      headers["Content-Type"] = "application/json";
    }
    if (MUTATING_METHODS.has(method) && !options.skipCsrf && this.csrfToken) {
      headers["X-CSRF-Token"] = this.csrfToken;
    }

    const response = await this.fetchImpl(target, {
      method,
      credentials: "include",
      headers,
      body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
    });

    if (!response.ok) {
      if (response.status === 401) {
        this.csrfToken = null;
      }
      let code = "http_error";
      let message = `HTTP ${response.status}`;
      try {
        const errBody = (await response.json()) as {
          error?: { code?: string; message?: string };
          detail?: string;
        };
        if (errBody.error) {
          code = errBody.error.code ?? code;
          message = errBody.error.message ?? message;
        } else if (typeof errBody.detail === "string") {
          message = errBody.detail;
        }
      } catch {
        // Non-JSON error response
      }
      throw new ApiClientError(response.status, code, message);
    }

    return (await response.json()) as T;
  }

  // --- Auth ---

  async login(username: string, password: string): Promise<AdminSessionResponse> {
    const session = await this.request<AdminSessionResponse>("/api/v1/auth/login", {
      method: "POST",
      body: { username, password },
      skipCsrf: true,
    });
    this.csrfToken = session.csrf_token;
    return session;
  }

  async currentSession(): Promise<AdminSessionResponse> {
    const session = await this.request<AdminSessionResponse>("/api/v1/auth/me");
    this.csrfToken = session.csrf_token;
    return session;
  }

  async logout(): Promise<void> {
    await this.request<{ status: string }>("/api/v1/auth/logout", {
      method: "POST",
    });
    this.csrfToken = null;
  }

  // --- Dashboard, Operations & Audit ---

  async getDashboardSummary(): Promise<DashboardSummary> {
    return this.request<DashboardSummary>("/api/v1/dashboard/summary");
  }

  async getOperationsStatus(): Promise<OperationsStatusResponse> {
    return this.request<OperationsStatusResponse>("/api/v1/operations/status");
  }

  async listFailedItems(stepKind?: string, limit = 50): Promise<FailedStepItem[]> {
    const res = await this.request<{ items: FailedStepItem[] }>(
      "/api/v1/operations/failed-items",
      { params: { step_kind: stepKind, limit } },
    );
    return res.items;
  }

  async retryFailedStep(payload: {
    step_kind: string;
    target_id: string;
    reason?: string;
  }): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>("/api/v1/operations/retry", {
      method: "POST",
      body: payload,
    });
  }

  async listAuditEvents(params?: {
    entity_type?: string;
    entity_id?: string;
    event_type?: string;
    cursor?: string | null;
    limit?: number;
  }): Promise<CursorPageResponse<AuditEventItem>> {
    return this.request<CursorPageResponse<AuditEventItem>>("/api/v1/audit-events", {
      params,
    });
  }

  // --- Write-Only Secrets ---

  async listSecrets(): Promise<SecretStatusRecord[]> {
    const res = await this.request<{ items: SecretStatusRecord[] }>("/api/v1/secrets");
    return res.items;
  }

  async createSecret(secretType: string, plaintext: string): Promise<SecretStatusRecord> {
    return this.request<SecretStatusRecord>("/api/v1/secrets", {
      method: "POST",
      body: { secret_type: secretType, plaintext },
    });
  }

  async rotateSecret(secretId: string, plaintext: string): Promise<SecretStatusRecord> {
    return this.request<SecretStatusRecord>(`/api/v1/secrets/${secretId}`, {
      method: "PUT",
      body: { plaintext },
    });
  }

  // --- Telegram Identities ---

  async listIdentities(): Promise<TelegramIdentity[]> {
    const res = await this.request<{ items: TelegramIdentity[] }>(
      "/api/v1/telegram-identities",
    );
    return res.items;
  }

  async createIdentity(payload: {
    name: string;
    identity_type: "mtproto_user" | "bot";
    secret_id: string;
    enabled?: boolean;
  }): Promise<TelegramIdentity> {
    return this.request<TelegramIdentity>("/api/v1/telegram-identities", {
      method: "POST",
      body: payload,
    });
  }

  async updateIdentity(
    identityId: string,
    changes: Record<string, unknown>,
  ): Promise<TelegramIdentity> {
    return this.request<TelegramIdentity>(
      `/api/v1/telegram-identities/${identityId}`,
      { method: "PATCH", body: changes },
    );
  }

  async evaluateIdentityCapabilities(
    identityId: string,
    payload: {
      source_protected_content: boolean;
      has_source_access: boolean;
      has_destination_post_permission: boolean;
    },
  ): Promise<{ identity_id: string; capabilities: TelegramIdentity["capabilities"] }> {
    return this.request(
      `/api/v1/telegram-identities/${identityId}/evaluate-capabilities`,
      { method: "POST", body: payload },
    );
  }

  // --- Channels ---

  async listSourceChannels(): Promise<SourceChannel[]> {
    const res = await this.request<{ items: SourceChannel[] }>("/api/v1/source-channels");
    return res.items;
  }

  async createSourceChannel(payload: Record<string, unknown>): Promise<SourceChannel> {
    return this.request<SourceChannel>("/api/v1/source-channels", {
      method: "POST",
      body: payload,
    });
  }

  async updateSourceChannel(
    channelId: string,
    changes: Record<string, unknown>,
  ): Promise<SourceChannel> {
    return this.request<SourceChannel>(`/api/v1/source-channels/${channelId}`, {
      method: "PATCH",
      body: changes,
    });
  }

  async listDestinationChannels(): Promise<DestinationChannel[]> {
    const res = await this.request<{ items: DestinationChannel[] }>(
      "/api/v1/destination-channels",
    );
    return res.items;
  }

  async createDestinationChannel(
    payload: Record<string, unknown>,
  ): Promise<DestinationChannel> {
    return this.request<DestinationChannel>("/api/v1/destination-channels", {
      method: "POST",
      body: payload,
    });
  }

  async updateDestinationChannel(
    channelId: string,
    changes: Record<string, unknown>,
  ): Promise<DestinationChannel> {
    return this.request<DestinationChannel>(
      `/api/v1/destination-channels/${channelId}`,
      { method: "PATCH", body: changes },
    );
  }

  // --- Processing Ranges ---

  async listProcessingRanges(params?: {
    source_channel_id?: string;
    status?: string;
  }): Promise<ProcessingRange[]> {
    const res = await this.request<{ items: ProcessingRange[] }>(
      "/api/v1/processing-ranges",
      { params },
    );
    return res.items;
  }

  async createProcessingRange(
    payload: Record<string, unknown>,
  ): Promise<ProcessingRange> {
    return this.request<ProcessingRange>("/api/v1/processing-ranges", {
      method: "POST",
      body: payload,
    });
  }

  async pauseProcessingRange(rangeId: string): Promise<ProcessingRange> {
    return this.request<ProcessingRange>(
      `/api/v1/processing-ranges/${rangeId}/pause`,
      { method: "POST" },
    );
  }

  async resumeProcessingRange(rangeId: string): Promise<ProcessingRange> {
    return this.request<ProcessingRange>(
      `/api/v1/processing-ranges/${rangeId}/resume`,
      { method: "POST" },
    );
  }

  async listRangeExecutions(rangeId: string): Promise<RangeExecution[]> {
    const res = await this.request<{ items: RangeExecution[] }>(
      `/api/v1/processing-ranges/${rangeId}/executions`,
    );
    return res.items;
  }

  // --- Versioned Configurations & Prompt Test Bench ---

  async listConfigEntities(family: ConfigFamily): Promise<ConfigEntity[]> {
    const res = await this.request<{ items: ConfigEntity[] }>(
      `/api/v1/config/${family}`,
    );
    return res.items;
  }

  async createConfigEntity(
    family: ConfigFamily,
    payload: Record<string, unknown>,
  ): Promise<ConfigEntity> {
    return this.request<ConfigEntity>(`/api/v1/config/${family}`, {
      method: "POST",
      body: payload,
    });
  }

  async createConfigVersion(
    family: ConfigFamily,
    entityId: string,
    content: Record<string, unknown>,
  ): Promise<ConfigVersion> {
    return this.request<ConfigVersion>(
      `/api/v1/config/${family}/${entityId}/versions`,
      { method: "POST", body: content },
    );
  }

  async updateConfigVersion(
    family: ConfigFamily,
    entityId: string,
    versionId: string,
    content: Record<string, unknown>,
  ): Promise<ConfigVersion> {
    return this.request<ConfigVersion>(
      `/api/v1/config/${family}/${entityId}/versions/${versionId}`,
      { method: "PATCH", body: content },
    );
  }

  async publishConfigVersion(
    family: ConfigFamily,
    entityId: string,
    versionId: string,
    supersedePublished = true,
  ): Promise<ConfigVersion> {
    return this.request<ConfigVersion>(
      `/api/v1/config/${family}/${entityId}/versions/${versionId}/publish`,
      { method: "POST", body: { supersede_published: supersedePublished } },
    );
  }

  async archiveConfigVersion(
    family: ConfigFamily,
    entityId: string,
    versionId: string,
  ): Promise<ConfigVersion> {
    return this.request<ConfigVersion>(
      `/api/v1/config/${family}/${entityId}/versions/${versionId}/archive`,
      { method: "POST" },
    );
  }

  async runPromptTestBench(
    payload: Record<string, unknown>,
  ): Promise<PromptTestBenchResponse> {
    return this.request<PromptTestBenchResponse>("/api/v1/analysis/test-bench", {
      method: "POST",
      body: payload,
    });
  }

  // --- Messages, Review, Reanalyze, Reroute & Skip ---

  async listMessages(
    filters: MessageSearchFilters,
    cursor?: string | null,
    limit = 25,
  ): Promise<CursorPageResponse<MessageSummary>> {
    return this.request<CursorPageResponse<MessageSummary>>("/api/v1/messages", {
      params: { ...filters, cursor, limit },
    });
  }

  async getMessageDetail(messageId: string): Promise<MessageDetail> {
    return this.request<MessageDetail>(`/api/v1/messages/${messageId}`);
  }

  async applyManualLabel(
    messageId: string,
    payload: {
      operation: "set" | "clear";
      target_scope: "global" | "media";
      target_kind: string;
      target_id: string;
      label_definition_version_id: string;
      label_key: string;
      score?: number;
      activated?: boolean;
      reason?: string;
    },
  ): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>(
      `/api/v1/messages/${messageId}/labels`,
      { method: "POST", body: payload },
    );
  }

  async transitionReviewStatus(
    messageId: string,
    newStatus: string,
    reason?: string,
  ): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>(
      `/api/v1/messages/${messageId}/review-status`,
      { method: "POST", body: { new_status: newStatus, reason } },
    );
  }

  async reanalyzeMessage(
    messageId: string,
    payload: { pipeline_version_id?: string; reason?: string },
  ): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>(
      `/api/v1/messages/${messageId}/reanalyze`,
      { method: "POST", body: payload },
    );
  }

  async rerouteMessage(
    messageId: string,
    payload: {
      routing_policy_version_id?: string;
      dry_run: boolean;
      reason?: string;
    },
  ): Promise<RerouteResult> {
    return this.request<RerouteResult>(`/api/v1/messages/${messageId}/reroute`, {
      method: "POST",
      body: payload,
    });
  }

  async skipMessage(
    messageId: string,
    reason: string,
  ): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>(
      `/api/v1/messages/${messageId}/skip`,
      { method: "POST", body: { reason } },
    );
  }

  // --- Two-Step Archive Cleanup ---

  async listCleanupDryRuns(limit = 20): Promise<CleanupDryRun[]> {
    const res = await this.request<{ items: CleanupDryRun[] }>(
      "/api/v1/archive-cleanup/dry-runs",
      { params: { limit } },
    );
    return res.items;
  }

  async createCleanupDryRun(filterPayload: CleanupFilterPayload): Promise<CleanupDryRun> {
    return this.request<CleanupDryRun>("/api/v1/archive-cleanup/dry-runs", {
      method: "POST",
      body: filterPayload,
    });
  }

  async listCleanupJobs(
    cursor?: string | null,
    limit = 25,
  ): Promise<CursorPageResponse<CleanupJob>> {
    return this.request<CursorPageResponse<CleanupJob>>(
      "/api/v1/archive-cleanup/jobs",
      { params: { cursor, limit } },
    );
  }

  async confirmCleanupJob(payload: {
    dry_run_id: string;
    expected_filter_hash: string;
  }): Promise<CleanupJob> {
    return this.request<CleanupJob>("/api/v1/archive-cleanup/jobs", {
      method: "POST",
      body: payload,
    });
  }
}
