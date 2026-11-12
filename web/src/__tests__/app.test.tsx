import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { App } from "../App";
import { AdminApiClient } from "../api/client";
import type {
  AdminSessionResponse,
  ConfigEntity,
  MessageDetail,
  MessageSummary,
  SecretStatusRecord,
} from "../api/types";

interface RecordedCall {
  method: string;
  path: string;
  search: string;
  headers: Record<string, string>;
  body: Record<string, unknown> | null;
}

const TEST_SESSION: AdminSessionResponse = {
  user: { id: "admin-0001", username: "curator_admin" },
  session_id: "sess-1234",
  csrf_token: "csrf-secret-token-999",
  expires_at: "2026-12-31T23:59:59Z",
};

function createMockBackend(options?: { authenticatedInitially?: boolean }) {
  let authenticated = options?.authenticatedInitially ?? true;
  const calls: RecordedCall[] = [];

  const secrets: SecretStatusRecord[] = [
    {
      secret_id: "sec-11112222-3333",
      secret_type: "telethon_session",
      key_id: "master-v1",
      rotation_version: 1,
      configured: true,
      created_at: "2026-11-12T00:00:00Z",
    },
  ];

  const labelDefEntities: ConfigEntity[] = [
    {
      id: "ent-lbl-1",
      family: "label-definitions",
      name: "illustration",
      published_version: {
        id: "ver-lbl-pub-1",
        entity_id: "ent-lbl-1",
        version_number: 1,
        state: "published",
        content: {
          key: "illustration",
          scope: "global",
          value_type: "score",
          threshold: 0.75,
          negative: false,
        },
        created_at: "2026-11-12T00:00:00Z",
        published_at: "2026-11-12T00:05:00Z",
      },
      versions: [
        {
          id: "ver-lbl-pub-1",
          entity_id: "ent-lbl-1",
          version_number: 1,
          state: "published",
          content: {
            key: "illustration",
            scope: "global",
            value_type: "score",
            threshold: 0.75,
            negative: false,
          },
          created_at: "2026-11-12T00:00:00Z",
          published_at: "2026-11-12T00:05:00Z",
        },
      ],
    },
  ];

  const stageEntities: ConfigEntity[] = [
    {
      id: "ent-stage-1",
      family: "analysis-stages",
      name: "vlm-main-stage",
      published_version: {
        id: "ver-stage-pub-1",
        entity_id: "ent-stage-1",
        version_number: 1,
        state: "published",
        content: {
          execution_mode: "single_message",
          visual_composition_policy: "adaptive",
        },
        created_at: "2026-11-12T00:00:00Z",
      },
      versions: [
        {
          id: "ver-stage-pub-1",
          entity_id: "ent-stage-1",
          version_number: 1,
          state: "published",
          content: {
            execution_mode: "single_message",
            visual_composition_policy: "adaptive",
          },
          created_at: "2026-11-12T00:00:00Z",
        },
      ],
    },
  ];

  const messagesPage1: MessageSummary[] = [
    {
      id: "msg-0001-causal",
      source_channel_id: "src-0001",
      primary_telegram_message_id: 501,
      published_at: "2026-11-12T06:00:00Z",
      original_text: "Stunning anime landscape artwork",
      media_count: 1,
      processing_status: "completed",
      terminal_reason: "routed",
      duplicate_status: "unique",
      review_status: "unreviewed",
      blocked_from_analysis: false,
    },
  ];

  const messagesPage2: MessageSummary[] = [
    {
      id: "msg-0002-older",
      source_channel_id: "src-0001",
      primary_telegram_message_id: 500,
      published_at: "2026-11-12T05:00:00Z",
      original_text: "Sponsored promo post",
      media_count: 0,
      processing_status: "completed",
      terminal_reason: "blocked_by_negative_gate",
      duplicate_status: "unique",
      review_status: "unreviewed",
      blocked_from_analysis: true,
      blocked_by_label_key: "advertisement",
    },
  ];

  const detailState: MessageDetail = {
    message: {
      ...messagesPage1[0],
      source_deleted: false,
    },
    parts: [
      {
        id: "part-501-0",
        telegram_message_id: 501,
        part_index: 0,
        media_kind: "photo",
      },
    ],
    media: {
      images: [
        {
          id: "img-asset-9001",
          archive_state: "ready",
          storage_key: "archive/2026/11/img-asset-9001.webp",
          archive_sha256: "abcdef0123456789abcdef0123456789",
          perceptual_hash: "f0f0a5a5c3c30001",
          width: 1280,
          height: 720,
          archive_size_bytes: 184320,
        },
      ],
      videos: [],
    },
    duplicate_match: null,
    analysis_runs: [
      {
        id: "arun-7777",
        analysis_pipeline_version_id: "ver-pipe-1",
        run_mode: "production",
        status: "succeeded",
        created_at: "2026-11-12T06:00:05Z",
        stage_runs: [
          {
            id: "srun-8888",
            analysis_run_id: "arun-7777",
            node_key: "vlm_classify",
            target_id: "msg-0001-causal",
            status: "succeeded",
            result_origin: "fresh_inference",
            attempt_count: 1,
            input_manifest: {
              id: "man-1",
              manifest_hash: "99887766554433221100",
              content: { target_id: "msg-0001-causal" },
            },
            inference_call: {
              id: "icall-1",
              provider_adapter: "openai_compatible",
              model_name: "qwen2.5-vl-72b",
              status: "succeeded",
              http_status: 200,
              latency_ms: 412,
              structured_output_schema_hash: "schema-hash-1234",
              token_usage: { prompt_tokens: 320, completion_tokens: 48 },
            },
          },
        ],
      },
    ],
    labels: {
      model: [
        {
          message_id: "msg-0001-causal",
          target_scope: "global",
          target_kind: "message",
          target_id: "msg-0001-causal",
          label_definition_version_id: "ver-lbl-pub-1",
          label_key: "illustration",
          score: 0.62,
          activated: false,
        },
      ],
      manual_events: [],
      namespaces: {
        model: {
          global: { illustration: { score: 0.62, activated: false } },
          media: {},
        },
        manual: {
          global: {},
          media: {},
        },
        effective: {
          global: {
            illustration: { score: 0.62, activated: false, source: "model" },
          },
          media: {},
        },
      },
    },
    review_events: [],
    routing_evaluations: [
      {
        id: "reval-3001",
        routing_request_id: "rreq-3001",
        routing_policy_version_id: "ver-rp-1",
        facts_hash: "facts-hash-abcdef123456",
        facts_snapshot: {},
        rule_outcomes: [{ rule_id: "rule-art", matched: true }],
        stopped_at_rule_id: "rule-art",
        created_at: "2026-11-12T06:00:08Z",
        publication_intents: [
          {
            id: "pint-4001",
            destination_channel_id: "dst-curated-main",
            publish_identity_id: "id-bot-1",
            publication_mode: "copy_publish",
            status: "sent",
            attempt_count: 1,
            attempts: [{ id: "att-1", attempt_number: 1, status: "sent" }],
            records: [
              { id: "prec-1", part_kind: "primary", remote_message_id: 9201 },
            ],
          },
        ],
      },
    ],
    audit_events: [
      {
        id: "aud-1",
        event_type: "secret.created",
        entity_type: "secret",
        entity_id: "sec-11112222-3333",
        actor_admin_user_id: "admin-0001",
        payload: { secret_type: "telethon_session", plaintext: "[REDACTED]" },
        created_at: "2026-11-12T00:00:00Z",
      },
    ],
  };

  const cleanupJobs: Array<Record<string, unknown>> = [];
  const dryRuns: Array<Record<string, unknown>> = [];
  let dryRunSeq = 0;

  const fetchImpl: typeof fetch = async (input, init) => {
    const rawUrl = typeof input === "string" ? input : input.toString();
    const parsed = new URL(rawUrl, "http://localhost");
    const method = (init?.method ?? "GET").toUpperCase();
    const headers: Record<string, string> = {};
    if (init?.headers) {
      for (const [k, v] of Object.entries(
        init.headers as Record<string, string>,
      )) {
        headers[k.toLowerCase()] = v;
      }
    }
    const body =
      typeof init?.body === "string"
        ? (JSON.parse(init.body) as Record<string, unknown>)
        : null;

    calls.push({
      method,
      path: parsed.pathname,
      search: parsed.search,
      headers,
      body,
    });

    const json = (status: number, data: unknown) =>
      new Response(JSON.stringify(data), {
        status,
        headers: { "Content-Type": "application/json" },
      });

    // Enforce CSRF token on mutating endpoints except /auth/login
    if (
      ["POST", "PUT", "PATCH", "DELETE"].includes(method) &&
      parsed.pathname !== "/api/v1/auth/login"
    ) {
      if (headers["x-csrf-token"] !== TEST_SESSION.csrf_token) {
        return json(403, {
          error: { code: "csrf_invalid", message: "Missing or invalid CSRF" },
        });
      }
    }

    if (parsed.pathname === "/api/v1/auth/me") {
      return authenticated
        ? json(200, TEST_SESSION)
        : json(401, {
            error: { code: "unauthenticated", message: "Not logged in" },
          });
    }

    if (parsed.pathname === "/api/v1/auth/login" && method === "POST") {
      if (body?.username === "curator_admin" && body?.password === "s3cr3t!") {
        authenticated = true;
        return json(200, TEST_SESSION);
      }
      return json(401, {
        error: { code: "invalid_credentials", message: "Invalid credentials" },
      });
    }

    if (parsed.pathname === "/api/v1/auth/logout" && method === "POST") {
      authenticated = false;
      return json(200, { status: "logged_out" });
    }

    if (parsed.pathname === "/api/v1/dashboard/summary") {
      return json(200, {
        source_channels: { total: 2, enabled: 2, active_profiles: 2 },
        destination_channels: { total: 2, enabled: 2 },
        processing_ranges: {
          total: 2,
          active: 1,
          paused: 0,
          completed: 1,
          failed: 0,
        },
        messages: {
          total: 2,
          processed: 2,
          processing: 0,
          failed: 0,
          blocked_from_analysis: 1,
          unreviewed: 2,
        },
        identities: { total: 1, healthy: 1, degraded: 0, failed: 0 },
        archive: {
          ready_images: 1,
          ready_videos: 0,
          deleted_assets: 0,
          total_archive_bytes: 184320,
        },
        operations: {
          publication: {
            claimed: 1,
            sent: 1,
            retry_scheduled: 0,
            failed: 0,
            cancelled: 0,
            reconciled_parts: 0,
          },
          flood_wait: {
            events: 0,
            wait_seconds_total: 0,
            wait_seconds_max: 0,
            last_observed_at: null,
            last_retry_at: null,
          },
          backlog: {
            publication_pending: 0,
            publication_partial: 0,
            publication_retry_due: 0,
            cleanup_pending: 0,
            cleanup_retry_due: 0,
            durable_wakeups_due: 0,
            observed_at: "2026-11-12T06:05:00Z",
          },
          worker: {
            alive: true,
            ready: true,
            database_ready: true,
            broker_ready: true,
            last_heartbeat_at: "2026-11-12T06:05:00Z",
            blockers: [],
          },
          scheduler: {
            alive: true,
            ready: true,
            database_ready: true,
            broker_ready: true,
            last_heartbeat_at: "2026-11-12T06:05:00Z",
            blockers: [],
          },
        },
      });
    }

    if (parsed.pathname === "/api/v1/processing-ranges") {
      return json(200, {
        items: [
          {
            id: "range-latest-01",
            source_channel_id: "src-0001",
            start_at: "2026-11-01T00:00:00Z",
            end_mode: "latest",
            end_at: null,
            steady_after_seconds: 60,
            status: "active",
            active_high_watermark_message_id: 501,
            processing_watermark_message_id: 501,
          },
        ],
      });
    }

    if (parsed.pathname === "/api/v1/secrets") {
      if (method === "GET") {
        return json(200, { items: secrets });
      }
      if (method === "POST") {
        const created: SecretStatusRecord = {
          secret_id: "sec-new-99998888",
          secret_type: String(body?.secret_type ?? "bot_token"),
          key_id: "master-v1",
          rotation_version: 1,
          configured: true,
          created_at: "2026-11-12T06:10:00Z",
        };
        secrets.push(created);
        return json(201, created);
      }
    }

    if (parsed.pathname === "/api/v1/telegram-identities") {
      return json(200, {
        items: [
          {
            id: "id-user-1",
            name: "Primary Reader",
            identity_type: "mtproto_user",
            secret_id: "sec-11112222-3333",
            enabled: true,
            health_status: "healthy",
            capabilities: {
              identity_kind: "mtproto_user",
              health: "healthy",
              enabled: true,
              can_read_source: true,
              can_download_media: true,
              can_native_forward: true,
              can_copy_publish: true,
              can_metadata_publish: true,
              can_supplement_reply: true,
              restrictions: [],
            },
          },
        ],
      });
    }

    if (parsed.pathname === "/api/v1/source-channels") {
      return json(200, {
        items: [
          {
            id: "src-0001",
            telegram_peer_id: -100111222,
            display_name: "Illustration Feed",
            username: "illust_feed",
            enabled: true,
            protected_content: false,
            default_read_identity_id: "id-user-1",
            active_profile_version_id: "ver-prof-1",
          },
        ],
      });
    }

    if (parsed.pathname === "/api/v1/destination-channels") {
      return json(200, {
        items: [
          {
            id: "dst-curated-main",
            telegram_peer_id: -100999888,
            name: "Curated Main",
            enabled: true,
            default_publish_identity_id: "id-user-1",
          },
        ],
      });
    }

    if (parsed.pathname === "/api/v1/config/label-definitions") {
      return json(200, { items: labelDefEntities });
    }

    if (
      parsed.pathname === "/api/v1/config/label-definitions/ent-lbl-1/versions" &&
      method === "POST"
    ) {
      const nextDraft = {
        id: "ver-lbl-draft-2",
        entity_id: "ent-lbl-1",
        version_number: 2,
        state: "draft" as const,
        content: body ?? {},
        created_at: "2026-11-12T06:15:00Z",
      };
      labelDefEntities[0].versions.push(nextDraft);
      return json(201, nextDraft);
    }

    if (
      parsed.pathname ===
        "/api/v1/config/label-definitions/ent-lbl-1/versions/ver-lbl-draft-2" &&
      method === "PATCH"
    ) {
      const target = labelDefEntities[0].versions.find(
        (v) => v.id === "ver-lbl-draft-2",
      );
      if (target) {
        target.content = body ?? {};
      }
      return json(200, target);
    }

    if (parsed.pathname === "/api/v1/config/analysis-stages") {
      return json(200, { items: stageEntities });
    }

    if (
      parsed.pathname.startsWith("/api/v1/config/") &&
      method === "GET"
    ) {
      return json(200, { items: [] });
    }

    if (parsed.pathname === "/api/v1/analysis/test-bench" && method === "POST") {
      return json(200, {
        run_mode: "test",
        status: "succeeded",
        system_prompt: "Analyze Telegram visual content.",
        user_prompt: `Caption: ${String(body?.sample_text ?? "")}`,
        input_manifest: { stage_version_id: body?.stage_template_version_id },
        input_manifest_hash: "manifest-sha256-abcdef1234567890",
        structured_output_schema: { type: "object" },
        structured_output_schema_hash: "schema-sha256-1234567890abcdef",
        raw_response: { results: [] },
        parsed_results: [
          {
            target_id: "sample-target",
            reason: "Clean composition",
            evidence: ["linework"],
            labels: [
              {
                label_definition_version_id: "ver-lbl-pub-1",
                label_key: "illustration",
                scope: "global",
                score: 0.94,
                activated: true,
                negative: false,
              },
            ],
          },
        ],
        invalid_targets: {},
        missing_target_ids: [],
        negative_gate_would_trigger: false,
        token_usage: { prompt_tokens: 120, completion_tokens: 30 },
        latency_ms: 185,
        error_code: null,
        error_type: null,
      });
    }

    if (parsed.pathname === "/api/v1/messages" && method === "GET") {
      const cursor = parsed.searchParams.get("cursor");
      if (cursor === "cursor-page-2") {
        return json(200, {
          items: messagesPage2,
          next_cursor: null,
          has_more: false,
        });
      }
      return json(200, {
        items: messagesPage1,
        next_cursor: "cursor-page-2",
        has_more: true,
      });
    }

    if (
      parsed.pathname === "/api/v1/messages/msg-0001-causal" &&
      method === "GET"
    ) {
      return json(200, detailState);
    }

    if (
      parsed.pathname === "/api/v1/messages/msg-0001-causal/labels" &&
      method === "POST"
    ) {
      const lblKey = String(body?.label_key ?? "illustration");
      const scoreVal = Number(body?.score ?? 1.0);
      const actVal = Boolean(body?.activated ?? true);
      // Model namespace stays untouched; manual & effective update
      detailState.labels.namespaces.manual.global[lblKey] = {
        score: scoreVal,
        activated: actVal,
      };
      detailState.labels.namespaces.effective.global[lblKey] = {
        score: scoreVal,
        activated: actVal,
        source: "manual",
      };
      return json(200, { status: "recorded" });
    }

    if (
      parsed.pathname === "/api/v1/messages/msg-0001-causal/reroute" &&
      method === "POST"
    ) {
      return json(200, {
        evaluation_id: "reval-preview-99",
        routing_request_id: "rreq-preview-99",
        routing_policy_version_id: "ver-rp-2",
        dry_run: Boolean(body?.dry_run),
        persisted: !body?.dry_run,
        facts_hash: "facts-hash-reroute-99",
        matched_rule_ids: ["rule-art", "rule-vip-gallery"],
        stopped_at_rule_id: null,
        planned_intents: [
          {
            destination_channel_id: "dst-curated-main",
            publication_mode: "copy_publish",
            rendering_template_version_id: null,
          },
          {
            destination_channel_id: "dst-vip-gallery",
            publication_mode: "forward_plus_supplement",
            rendering_template_version_id: "ver-tpl-1",
          },
        ],
        comparison: {
          existing_destinations: ["dst-curated-main"],
          planned_destinations: ["dst-curated-main", "dst-vip-gallery"],
          added_destinations: ["dst-vip-gallery"],
        },
      });
    }

    if (parsed.pathname === "/api/v1/archive-cleanup/dry-runs") {
      if (method === "GET") {
        return json(200, { items: dryRuns });
      }
      if (method === "POST") {
        dryRunSeq += 1;
        const created = {
          id: `dryrun-5555-${dryRunSeq}`,
          actor_admin_user_id: "admin-0001",
          filter_snapshot: body ?? {},
          filter_hash: "fhash-7777888899990000",
          estimated_asset_count: 14,
          estimated_message_count: 9,
          estimated_bytes: 5242880,
          created_at: "2026-11-12T06:20:00Z",
        };
        dryRuns.unshift(created);
        return json(201, created);
      }
    }

    if (parsed.pathname === "/api/v1/archive-cleanup/jobs") {
      if (method === "GET") {
        return json(200, {
          items: cleanupJobs,
          next_cursor: null,
          has_more: false,
        });
      }
      if (method === "POST") {
        const createdJob = {
          id: "cjob-99990000",
          dry_run_id: String(body?.dry_run_id),
          filter_hash: String(body?.expected_filter_hash),
          estimated_asset_count: 14,
          estimated_message_count: 9,
          estimated_bytes: 5242880,
          deleted_count: 0,
          skipped_active_count: 0,
          status: "pending",
          created_at: "2026-11-12T06:21:00Z",
        };
        cleanupJobs.unshift(createdJob);
        return json(201, createdJob);
      }
    }

    if (parsed.pathname === "/api/v1/operations/status") {
      return json(200, {
        operations: {
          publication: {
            claimed: 1,
            sent: 1,
            retry_scheduled: 0,
            failed: 1,
            cancelled: 0,
            reconciled_parts: 0,
          },
          flood_wait: {
            events: 1,
            wait_seconds_total: 12,
            wait_seconds_max: 12,
            last_observed_at: "2026-11-12T06:00:00Z",
            last_retry_at: "2026-11-12T06:00:12Z",
          },
          backlog: {
            publication_pending: 0,
            publication_partial: 0,
            publication_retry_due: 0,
            cleanup_pending: 0,
            cleanup_retry_due: 0,
            durable_wakeups_due: 0,
            observed_at: "2026-11-12T06:05:00Z",
          },
          worker: {
            alive: true,
            ready: true,
            database_ready: true,
            broker_ready: true,
            last_heartbeat_at: "2026-11-12T06:05:00Z",
            blockers: [],
          },
          scheduler: {
            alive: true,
            ready: true,
            database_ready: true,
            broker_ready: true,
            last_heartbeat_at: "2026-11-12T06:05:00Z",
            blockers: [],
          },
        },
        queues: { analysis: 0, media: 0, publish: 1, maintenance: 0 },
        failed_count: 1,
      });
    }

    if (parsed.pathname === "/api/v1/operations/failed-items") {
      return json(200, {
        items: [
          {
            step_kind: "stage_run",
            target_id: "srun-failed-4040",
            message_id: "msg-0001-causal",
            status: "failed",
            error_code: "provider_timeout",
            attempt_count: 2,
          },
        ],
      });
    }

    if (parsed.pathname === "/api/v1/operations/retry" && method === "POST") {
      return json(200, { status: "retry_queued", ...body });
    }

    if (parsed.pathname === "/api/v1/audit-events") {
      return json(200, {
        items: detailState.audit_events,
        next_cursor: null,
        has_more: false,
      });
    }

    return json(404, {
      error: { code: "not_found", message: `Unhandled ${method} ${parsed.pathname}` },
    });
  };

  return {
    client: new AdminApiClient({ fetchImpl }),
    calls,
  };
}

describe("TG-VLM-Curator M9 Web Administration Console", () => {
  it("authenticates with autofill-compliant login form, attaches X-CSRF-Token on mutations, and logs out", async () => {
    const user = userEvent.setup();
    const { client, calls } = createMockBackend({
      authenticatedInitially: false,
    });

    render(<App client={client} />);

    const usernameInput = await screen.findByLabelText(/Username/i);
    const passwordInput = screen.getByLabelText(/^Password/i);
    expect(usernameInput).toHaveAttribute("autocomplete", "username");
    expect(passwordInput).toHaveAttribute("autocomplete", "current-password");
    expect(passwordInput).toHaveAttribute("type", "password");

    const revealBtn = screen.getByRole("button", { name: /Show password/i });
    await user.click(revealBtn);
    expect(passwordInput).toHaveAttribute("type", "text");

    await user.type(usernameInput, "curator_admin");
    await user.type(passwordInput, "s3cr3t!");
    await user.click(screen.getByRole("button", { name: /^Sign In$/i }));

    expect(
      await screen.findByRole("heading", { name: "Dashboard" }),
    ).toBeInTheDocument();

    const nav = screen.getByRole("navigation", { name: "Primary" });
    expect(
      within(nav).getByRole("button", { name: "Dashboard" }),
    ).toHaveAttribute("aria-current", "page");

    await user.click(screen.getByRole("button", { name: "Sign Out" }));
    expect(
      await screen.findByRole("heading", { name: /TG-VLM-Curator Sign In/i }),
    ).toBeInTheDocument();

    const logoutCall = calls.find((c) => c.path === "/api/v1/auth/logout");
    expect(logoutCall?.headers["x-csrf-token"]).toBe(TEST_SESSION.csrf_token);
  });

  it("enforces write-only EncryptedSecret storage and scrubs plaintext immediately", async () => {
    const user = userEvent.setup();
    const { client, calls } = createMockBackend();

    render(
      <App
        client={client}
        initialSession={TEST_SESSION}
        initialWorkspace="telegram-identities"
      />,
    );

    expect(
      await screen.findByRole("heading", {
        name: /Telegram Identities & Write-Only Secrets/i,
      }),
    ).toBeInTheDocument();

    const secretInput = screen.getByLabelText(/Secret Value/i);
    await user.selectOptions(
      screen.getByLabelText(/Credential Type/i),
      "bot_token",
    );
    await user.type(secretInput, "123456:SUPER_SECRET_BOT_TOKEN");
    await user.click(
      screen.getByRole("button", { name: /Encrypt & Store Secret/i }),
    );

    await waitFor(() => {
      expect(secretInput).toHaveValue("");
    });
    expect(
      screen.queryByText(/SUPER_SECRET_BOT_TOKEN/),
    ).not.toBeInTheDocument();
    expect(screen.getAllByText(/Configured/).length).toBeGreaterThanOrEqual(2);

    const postSecret = calls.find(
      (c) => c.path === "/api/v1/secrets" && c.method === "POST",
    );
    expect(postSecret?.body).toEqual({
      secret_type: "bot_token",
      plaintext: "123456:SUPER_SECRET_BOT_TOKEN",
    });
    expect(postSecret?.headers["x-csrf-token"]).toBe(TEST_SESSION.csrf_token);
  });

  it("enforces PUBLISHED config immutability, clones to editable DRAFT, and runs Prompt Test Bench", async () => {
    const user = userEvent.setup();
    const { client, calls } = createMockBackend();

    render(
      <App
        client={client}
        initialSession={TEST_SESSION}
        initialWorkspace="analysis-config"
      />,
    );

    const editor = await screen.findByLabelText(/Version Content JSON/i);
    expect(editor).toHaveAttribute("readonly");
    expect(screen.getByRole("button", { name: "Save Draft" })).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "Publish Version" }),
    ).toBeDisabled();

    await user.click(
      screen.getByRole("button", { name: "Clone to New Draft" }),
    );

    await waitFor(() => {
      expect(
        screen.getByLabelText(/Version Content JSON/i),
      ).not.toHaveAttribute("readonly");
    });
    const saveDraftBtn = screen.getByRole("button", { name: "Save Draft" });
    expect(saveDraftBtn).not.toBeDisabled();
    await user.click(saveDraftBtn);

    expect(
      calls.some(
        (c) =>
          c.path ===
            "/api/v1/config/label-definitions/ent-lbl-1/versions/ver-lbl-draft-2" &&
          c.method === "PATCH",
      ),
    ).toBe(true);

    // Switch to Prompt Test Bench tab
    await user.click(screen.getByRole("tab", { name: "Prompt Test Bench" }));
    await user.click(
      await screen.findByRole("button", {
        name: "Execute Prompt Test Bench",
      }),
    );

    expect(
      await screen.findByText(/manifest-sha256-/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/Clean composition/i)).toBeInTheDocument();
  });

  it("navigates cursor-paginated Messages into the 6-stage Causal Chain Inspector", async () => {
    const user = userEvent.setup();
    const { client, calls } = createMockBackend();

    render(
      <App
        client={client}
        initialSession={TEST_SESSION}
        initialWorkspace="messages"
      />,
    );

    expect(
      await screen.findByText(/Stunning anime landscape artwork/i),
    ).toBeInTheDocument();

    // Advance cursor page and return
    await user.click(screen.getByRole("button", { name: "Next Page" }));
    expect(
      await screen.findByText(/Sponsored promo post/i),
    ).toBeInTheDocument();
    expect(
      calls.some((c) => c.search.includes("cursor=cursor-page-2")),
    ).toBe(true);

    await user.click(screen.getByRole("button", { name: "Previous Page" }));
    expect(
      await screen.findByText(/Stunning anime landscape artwork/i),
    ).toBeInTheDocument();

    // Inspect causal chain
    await user.click(screen.getByRole("button", { name: "Causal Chain" }));
    expect(
      await screen.findByRole("heading", {
        name: /Message Detail & Causal Chain Inspector/i,
      }),
    ).toBeInTheDocument();
    expect(screen.getByText(/f0f0a5a5c3c30001/)).toBeInTheDocument();
    expect(screen.getByText(/qwen2\.5-vl-72b/)).toBeInTheDocument();
    expect(screen.getByText(/primary:#9201/)).toBeInTheDocument();
  });

  it("records append-only manual label overrides without overwriting Model labels and previews Dry-Run Re-route", async () => {
    const user = userEvent.setup();
    const { client } = createMockBackend();

    render(
      <App
        client={client}
        initialSession={TEST_SESSION}
        initialWorkspace="review"
      />,
    );

    expect(
      await screen.findByRole("heading", { name: /Active Message #501/i }),
    ).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "Record Manual Label Event" }),
    );
    expect(
      await screen.findByText(/ModelLabelAssignment preserved untouched/i),
    ).toBeInTheDocument();
    // Model score 0.62 remains intact while Effective shows source: manual
    expect(screen.getByText(/0\.62/)).toBeInTheDocument();
    expect(screen.getByText(/"source": "manual"/)).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "Preview Dry-Run Re-route" }),
    );
    expect(
      (await screen.findAllByText(/dst-vip-gallery/)).length,
    ).toBeGreaterThanOrEqual(1);
  });

  it("enforces two-step Dry-Run -> Confirm lock in Archive Cleanup and invalidates on filter edit", async () => {
    const user = userEvent.setup();
    const { client, calls } = createMockBackend();

    render(
      <App
        client={client}
        initialSession={TEST_SESSION}
        initialWorkspace="archive-management"
      />,
    );

    const confirmTrigger = await screen.findByRole("button", {
      name: /Step 2: Confirm Cleanup Job/i,
    });
    expect(confirmTrigger).toBeDisabled();

    const effLabelsInput = screen.getByLabelText(
      /Effective Label Keys \(CSV\)/i,
    );
    await user.type(effLabelsInput, "advertisement");
    await user.click(
      screen.getByRole("button", { name: /Step 1: Run Dry-Run Preview/i }),
    );

    await waitFor(() => {
      expect(confirmTrigger).not.toBeDisabled();
    });

    // Mutating any filter must immediately re-lock Step 2 confirmation!
    await user.type(effLabelsInput, ", spam");
    expect(confirmTrigger).toBeDisabled();

    // Run Step 1 again for the modified filter and confirm via ModalDialog
    await user.click(
      screen.getByRole("button", { name: /Step 1: Run Dry-Run Preview/i }),
    );
    await waitFor(() => {
      expect(confirmTrigger).not.toBeDisabled();
    });

    await user.click(confirmTrigger);
    const modalConfirmBtn = await screen.findByRole("button", {
      name: "Delete Matched Archive Blobs",
    });
    await user.click(modalConfirmBtn);

    await waitFor(() => {
      expect(
        calls.some(
          (c) =>
            c.path === "/api/v1/archive-cleanup/jobs" && c.method === "POST",
        ),
      ).toBe(true);
    });
    const confirmCall = calls.find(
      (c) => c.path === "/api/v1/archive-cleanup/jobs" && c.method === "POST",
    );
    expect(confirmCall?.body).toEqual({
      dry_run_id: "dryrun-5555-2",
      expected_filter_hash: "fhash-7777888899990000",
    });
  });

  it("executes targeted step-level retry in Operations and renders redacted Audit Log", async () => {
    const user = userEvent.setup();
    const { client, calls } = createMockBackend();

    render(
      <App
        client={client}
        initialSession={TEST_SESSION}
        initialWorkspace="operations"
      />,
    );

    const retryBtn = await screen.findByRole("button", {
      name: "Retry Exact Step",
    });
    await user.click(retryBtn);

    await waitFor(() => {
      expect(
        calls.some(
          (c) => c.path === "/api/v1/operations/retry" && c.method === "POST",
        ),
      ).toBe(true);
    });

    // Navigate via Primary sidebar to Audit Log
    const nav = screen.getByRole("navigation", { name: "Primary" });
    await user.click(within(nav).getByRole("button", { name: "Audit Log" }));
    expect(
      await screen.findByText(/"plaintext":"\[REDACTED\]"/),
    ).toBeInTheDocument();
  });
});
