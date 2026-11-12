import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { AdminApiClient } from "../api/client";
import type {
  AdminSessionResponse,
  ConfigEntity,
  DashboardSummary,
  FailedStepItem,
  MessageDetail,
  OperationsStatusResponse,
  ProcessingRange,
  RangeExecution,
  TelegramIdentity,
} from "../api/types";
import { ModalDialog } from "../components/ModalDialog";
import { Tabs } from "../components/Tabs";
import { AnalysisConfigWorkspace } from "../workspaces/AnalysisConfigWorkspace";
import { ArchiveManagementWorkspace } from "../workspaces/ArchiveManagementWorkspace";
import { DashboardWorkspace } from "../workspaces/DashboardWorkspace";
import { OperationsWorkspace } from "../workspaces/OperationsWorkspace";
import { ProcessingRangesWorkspace } from "../workspaces/ProcessingRangesWorkspace";
import { ReviewWorkspace } from "../workspaces/ReviewWorkspace";
import { RoutingWorkspace } from "../workspaces/RoutingWorkspace";
import { SourceChannelsWorkspace } from "../workspaces/SourceChannelsWorkspace";
import { TelegramIdentitiesWorkspace } from "../workspaces/TelegramIdentitiesWorkspace";
import rawDetail from "./fixtures/message-detail.json";

const detailFixture = rawDetail as unknown as MessageDetail;

const SESSION: AdminSessionResponse = {
  user: { id: "admin-hardening", username: "admin" },
  session_id: "sess-hardening",
  csrf_token: "csrf-hardening",
  expires_at: "2026-12-31T00:00:00Z",
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function baseClient() {
  const c = new AdminApiClient({ fetchImpl: vi.fn() });
  vi.spyOn(c, "listSourceChannels").mockResolvedValue([]);
  vi.spyOn(c, "listDestinationChannels").mockResolvedValue([]);
  vi.spyOn(c, "listIdentities").mockResolvedValue([]);
  vi.spyOn(c, "listSecrets").mockResolvedValue([]);
  vi.spyOn(c, "listProcessingRanges").mockResolvedValue([]);
  vi.spyOn(c, "listConfigEntities").mockResolvedValue([]);
  return c;
}

const SAMPLE_SUMMARY: DashboardSummary = {
  source_channels: { total: 3, enabled: 2, active_profiles: 2 },
  destination_channels: { total: 2, enabled: 2 },
  processing_ranges: { total: 4, active: 1, paused: 1, completed: 1, failed: 1 },
  messages: {
    total: 100,
    processed: 92,
    processing: 3,
    failed: 5,
    blocked_from_analysis: 10,
    unreviewed: 40,
  },
  identities: { total: 2, healthy: 2, degraded: 0, failed: 0 },
  archive: {
    ready_images: 50,
    ready_videos: 10,
    deleted_assets: 5,
    total_archive_bytes: 1048576,
  },
  operations: {
    publication: {
      claimed: 10,
      sent: 9,
      retry_scheduled: 1,
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
      publication_pending: 1,
      publication_partial: 0,
      publication_retry_due: 0,
      cleanup_pending: 0,
      cleanup_retry_due: 0,
      durable_wakeups_due: 0,
      observed_at: "2026-11-12T10:00:00Z",
    },
    worker: {
      alive: true,
      ready: true,
      database_ready: true,
      broker_ready: true,
      last_heartbeat_at: "2026-11-12T10:00:00Z",
      blockers: [],
    },
    scheduler: {
      alive: true,
      ready: true,
      database_ready: true,
      broker_ready: true,
      last_heartbeat_at: "2026-11-12T10:00:00Z",
      blockers: [],
    },
  },
};

function makeIdentity(
  id: string,
  name: string,
  kind: "mtproto_user" | "bot",
): TelegramIdentity {
  return {
    id,
    name,
    identity_type: kind,
    secret_id: "sec-1",
    enabled: true,
    health_status: "healthy",
    capabilities: {
      identity_kind: kind,
      health: "healthy",
      enabled: true,
      can_read_source: kind === "mtproto_user",
      can_download_media: kind === "mtproto_user",
      can_native_forward: true,
      can_copy_publish: true,
      can_metadata_publish: true,
      can_supplement_reply: true,
      restrictions: [],
    },
  };
}

describe("Phase 2 Web & API Hardening Coverage", () => {
  it("DashboardWorkspace loads telemetry without fetching inference-profiles and guards stale refreshes", async () => {
    const c = baseClient();
    const stale = deferred<DashboardSummary>();
    vi.spyOn(c, "getDashboardSummary")
      .mockReturnValueOnce(stale.promise)
      .mockResolvedValue({
        ...SAMPLE_SUMMARY,
        messages: { ...SAMPLE_SUMMARY.messages, total: 250 },
      });
    const listCfg = vi.spyOn(c, "listConfigEntities");
    const user = userEvent.setup();

    render(<DashboardWorkspace client={c} onNavigate={() => {}} />);
    await user.click(screen.getByRole("button", { name: "Refresh Telemetry" }));
    expect(await screen.findByText("250")).toBeInTheDocument();

    await act(async () => stale.resolve(SAMPLE_SUMMARY));
    expect(screen.getByText("250")).toBeInTheDocument();
    expect(listCfg).not.toHaveBeenCalled();
  });

  it("SourceChannelsWorkspace defaults to mtproto_user reader, loads once, validates peer_id, and clears optional username to null", async () => {
    const c = baseClient();
    const botId = makeIdentity("id-bot-first", "Bot Publisher", "bot");
    const readerId = makeIdentity("id-mtproto-second", "User Reader", "mtproto_user");
    const listSrc = vi.spyOn(c, "listSourceChannels").mockResolvedValue([
      {
        id: "src-1",
        telegram_peer_id: -100555,
        display_name: "Art Channel",
        username: "existing_handle",
        enabled: true,
        default_read_identity_id: readerId.id,
        active_profile_version_id: null,
        latest_seen_message_id: 42,
      },
    ]);
    vi.spyOn(c, "listIdentities").mockResolvedValue([botId, readerId]);
    const createSrc = vi.spyOn(c, "createSourceChannel").mockResolvedValue({
      id: "src-2",
      telegram_peer_id: -100777,
      display_name: "New",
      username: null,
      enabled: true,
      default_read_identity_id: readerId.id,
      active_profile_version_id: null,
    });
    const updateSrc = vi.spyOn(c, "updateSourceChannel").mockResolvedValue({
      id: "src-1",
      telegram_peer_id: -100555,
      display_name: "Art Channel",
      username: null,
      enabled: true,
      default_read_identity_id: readerId.id,
      active_profile_version_id: null,
    });

    const user = userEvent.setup();
    render(<SourceChannelsWorkspace client={c} />);
    await screen.findByText(/Latest msg #42/);

    // Single initial mount fetch and auto-selected mtproto_user (not leading bot)
    expect(listSrc).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText("Read Identity")).toHaveValue(readerId.id);

    // Reject zero/non-integer peer_id before calling API
    const peerInput = screen.getByLabelText("Telegram Peer ID *");
    await user.clear(peerInput);
    await user.type(peerInput, "0");
    await user.type(screen.getByLabelText("Display Name *"), "Invalid Peer");
    await user.click(
      screen.getByRole("button", { name: "Register Source Channel" }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /non-zero integer/i,
    );
    expect(createSrc).not.toHaveBeenCalled();

    // RecordEditor clearing a previously non-null optional username normalizes to null
    await user.click(screen.getByRole("button", { name: "Edit Source" }));
    const editForm = screen.getByRole("form", { name: "Edit Source" });
    await user.clear(within(editForm).getByLabelText("Username"));
    await user.click(
      within(editForm).getByRole("button", { name: "Save Source" }),
    );
    await waitFor(() =>
      expect(updateSrc).toHaveBeenCalledWith(
        "src-1",
        expect.objectContaining({ username: null }),
      ),
    );
  });

  it("ProcessingRangesWorkspace hides Pause/Resume on terminal ranges, validates steady_after_seconds, and guards execution races", async () => {
    const c = baseClient();
    vi.spyOn(c, "listSourceChannels").mockResolvedValue([
      {
        id: "src-1",
        telegram_peer_id: -1001,
        display_name: "Main Source",
        username: null,
        enabled: true,
        default_read_identity_id: "id-1",
        active_profile_version_id: null,
      },
    ]);
    const ranges: ProcessingRange[] = [
      {
        id: "aaaa1111-0000",
        source_channel_id: "src-1",
        start_at: "2026-11-01T00:00:00Z",
        end_mode: "latest",
        end_at: null,
        steady_after_seconds: 60,
        status: "active",
        active_high_watermark_message_id: 10,
        processing_watermark_message_id: 10,
      },
      {
        id: "bbbb2222-0000",
        source_channel_id: "src-1",
        start_at: "2026-10-01T00:00:00Z",
        end_mode: "fixed",
        end_at: "2026-10-02T00:00:00Z",
        steady_after_seconds: null,
        status: "completed",
        active_high_watermark_message_id: 5,
        processing_watermark_message_id: 5,
      },
    ];
    const listRanges = vi
      .spyOn(c, "listProcessingRanges")
      .mockResolvedValue(ranges);
    const createRange = vi.spyOn(c, "createProcessingRange");

    const staleExec = deferred<RangeExecution[]>();
    vi.spyOn(c, "listRangeExecutions")
      .mockReturnValueOnce(staleExec.promise)
      .mockResolvedValue([
        {
          id: "exec-b-current",
          from_message_id_exclusive: 0,
          to_message_id_inclusive: 5,
          watermark_message_id: 5,
          history_ingested: true,
          status: "completed",
          attempt_count: 1,
        },
      ]);

    const user = userEvent.setup();
    render(<ProcessingRangesWorkspace client={c} />);
    await screen.findByText("aaaa1111");
    expect(listRanges).toHaveBeenCalledTimes(1);

    // Active row has Pause; completed row shows Terminal and no Pause/Resume button
    const rows = screen.getAllByRole("row");
    const activeRow = rows.find((r) => r.textContent?.includes("aaaa1111"))!;
    const completedRow = rows.find((r) => r.textContent?.includes("bbbb2222"))!;
    expect(
      within(activeRow).getByRole("button", { name: "Pause" }),
    ).toBeInTheDocument();
    expect(
      within(completedRow).queryByRole("button", { name: /Pause|Resume/ }),
    ).not.toBeInTheDocument();
    expect(within(completedRow).getByText("Terminal")).toBeInTheDocument();

    // Invalid steady_after_seconds blocked client-side
    const steadyInput = screen.getByLabelText(
      /Steady Quiet Threshold \(seconds\)/i,
    );
    await user.clear(steadyInput);
    await user.type(steadyInput, "0");
    await user.click(
      screen.getByRole("button", { name: "Create ProcessingRange" }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /positive integer/i,
    );
    expect(createRange).not.toHaveBeenCalled();

    // Fast click Executions on A then B discards A's late response
    await user.click(
      within(activeRow).getByRole("button", { name: "Executions" }),
    );
    await user.click(
      within(completedRow).getByRole("button", { name: "Executions" }),
    );
    expect(await screen.findByText("exec-b-c")).toBeInTheDocument();
    expect(screen.getByText("Yes")).toBeInTheDocument();

    await act(async () =>
      staleExec.resolve([
        {
          id: "exec-a-stale",
          from_message_id_exclusive: 0,
          to_message_id_inclusive: 10,
          watermark_message_id: 10,
          history_ingested: false,
          status: "active",
          attempt_count: 1,
        },
      ]),
    );
    expect(screen.queryByText("exec-a-s")).not.toBeInTheDocument();
  });

  it("TelegramIdentitiesWorkspace loads once and clears stale evaluated capabilities on identity change", async () => {
    const c = baseClient();
    const id1 = makeIdentity("id-1", "Reader One", "mtproto_user");
    const id2 = makeIdentity("id-2", "Bot Two", "bot");
    const listIds = vi
      .spyOn(c, "listIdentities")
      .mockResolvedValue([id1, id2]);
    vi.spyOn(c, "listSecrets").mockResolvedValue([
      {
        secret_id: "sec-1",
        secret_type: "telethon_session",
        key_id: "k1",
        configured: true,
        created_at: "2026-11-12T00:00:00Z",
      },
    ]);
    vi.spyOn(c, "evaluateIdentityCapabilities").mockResolvedValue({
      identity_id: id1.id,
      capabilities: {
        ...id1.capabilities,
        restrictions: ["protected_content_blocked"],
      },
    });

    const user = userEvent.setup();
    render(<TelegramIdentitiesWorkspace client={c} />);
    await screen.findByText("Reader One");
    expect(listIds).toHaveBeenCalledTimes(1);

    await user.click(
      screen.getByRole("button", { name: "Evaluate Capabilities" }),
    );
    expect(
      await screen.findByText(/protected_content_blocked/),
    ).toBeInTheDocument();

    // Switching identity immediately clears stale evaluation result
    await user.selectOptions(screen.getByLabelText("Identity"), id2.id);
    expect(
      screen.queryByText(/protected_content_blocked/),
    ).not.toBeInTheDocument();
  });

  it("OperationsWorkspace uses image_archive/video_archive step kinds, discards stale filter reads, and guards retry", async () => {
    const c = baseClient();
    const opsStatus: OperationsStatusResponse = {
      operations: SAMPLE_SUMMARY.operations,
      queues: { publication_pending: 0 },
      failed_count: 1,
    };
    vi.spyOn(c, "getOperationsStatus").mockResolvedValue(opsStatus);
    const staleItems = deferred<FailedStepItem[]>();
    const listFailed = vi
      .spyOn(c, "listFailedItems")
      .mockResolvedValueOnce([
        {
          step_kind: "image_archive",
          target_id: "img-fail-0001",
          status: "failed",
          error_code: "download_failed",
          attempt_count: 2,
        },
      ])
      .mockReturnValueOnce(staleItems.promise)
      .mockResolvedValue([
        {
          step_kind: "video_archive",
          target_id: "vid-fail-0002",
          status: "failed",
          error_code: "ffmpeg_failed",
          attempt_count: 1,
        },
      ]);
    const retryGate = deferred<Record<string, unknown>>();
    const retrySpy = vi
      .spyOn(c, "retryFailedStep")
      .mockReturnValue(retryGate.promise);

    const user = userEvent.setup();
    render(<OperationsWorkspace client={c} />);
    await screen.findByText("img-fail-0001");

    const select = screen.getByLabelText("Filter by Step Kind");
    const optionValues = within(select)
      .getAllByRole("option")
      .map((o) => (o as HTMLOptionElement).value);
    expect(optionValues).toContain("image_archive");
    expect(optionValues).toContain("video_archive");
    expect(optionValues).not.toContain("image_asset");
    expect(optionValues).not.toContain("video_asset");

    await user.selectOptions(select, "image_archive");
    await user.selectOptions(select, "video_archive");
    expect(listFailed).toHaveBeenCalledWith("video_archive", 50);
    expect(await screen.findByText("vid-fail-0002")).toBeInTheDocument();

    await act(async () =>
      staleItems.resolve([
        {
          step_kind: "image_archive",
          target_id: "stale-img-9999",
          status: "failed",
          error_code: "old",
          attempt_count: 1,
        },
      ]),
    );
    expect(screen.queryByText("stale-img-9999")).not.toBeInTheDocument();

    // Mutation guard prevents duplicate retry calls on rapid double-click
    const retryBtn = screen.getByRole("button", { name: "Retry Exact Step" });
    fireEvent.click(retryBtn);
    fireEvent.click(retryBtn);
    expect(retrySpy).toHaveBeenCalledTimes(1);
    await act(async () => retryGate.resolve({ status: "queued" }));
  });

  it("ModalDialog suppresses onCancel on programmatic close, and Tabs supports roving keyboard navigation", async () => {
    const onCancel = vi.fn();
    const onConfirm = vi.fn();
    const { rerender } = render(
      <ModalDialog
        open={true}
        title="Danger Confirm"
        confirmLabel="Confirm Delete"
        destructive={true}
        onConfirm={onConfirm}
        onCancel={onCancel}
      >
        <p>Body</p>
      </ModalDialog>,
    );

    // Destructive dialog focuses Cancel button on open
    expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();

    // Programmatic close via prop update must NOT call onCancel
    rerender(
      <ModalDialog
        open={false}
        title="Danger Confirm"
        confirmLabel="Confirm Delete"
        destructive={true}
        onConfirm={onConfirm}
        onCancel={onCancel}
      >
        <p>Body</p>
      </ModalDialog>,
    );
    expect(onCancel).not.toHaveBeenCalled();

    // Tabs keyboard navigation (ArrowRight, End, Home, ArrowLeft)
    let active = "one";
    const onSelect = vi.fn((next: string) => {
      active = next;
    });
    const tabs = [
      { key: "one", label: "First", content: <div>Panel 1</div> },
      { key: "two", label: "Second", content: <div>Panel 2</div> },
      { key: "three", label: "Third", content: <div>Panel 3</div> },
    ];
    const { rerender: rerenderTabs } = render(
      <Tabs
        ariaLabel="Demo Tabs"
        tabs={tabs}
        activeKey={active}
        onSelect={onSelect}
      />,
    );

    const firstTab = screen.getByRole("tab", { name: "First" });
    const secondTab = screen.getByRole("tab", { name: "Second" });
    expect(firstTab).toHaveAttribute("tabindex", "0");
    expect(secondTab).toHaveAttribute("tabindex", "-1");

    fireEvent.keyDown(firstTab, { key: "ArrowRight" });
    expect(onSelect).toHaveBeenLastCalledWith("two");
    rerenderTabs(
      <Tabs
        ariaLabel="Demo Tabs"
        tabs={tabs}
        activeKey={active}
        onSelect={onSelect}
      />,
    );
    fireEvent.keyDown(secondTab, { key: "End" });
    expect(onSelect).toHaveBeenLastCalledWith("three");
    fireEvent.keyDown(screen.getByRole("tab", { name: "Third" }), {
      key: "Home",
    });
    expect(onSelect).toHaveBeenLastCalledWith("one");
  });

  it("ReviewWorkspace filters published scope-matched labels, validates score, and blocks no-op review transitions", async () => {
    const c = baseClient();
    const reviewedMessage = {
      ...detailFixture.message,
      review_status: "reviewed",
    };
    const listMsgs = vi
      .spyOn(c, "listMessages")
      .mockResolvedValue({ items: [reviewedMessage], next_cursor: null, has_more: false });
    vi.spyOn(c, "getMessageDetail").mockResolvedValue({
      ...structuredClone(detailFixture),
      message: reviewedMessage,
    });

    const labelEntities: ConfigEntity[] = [
      {
        id: "ent-global-draft",
        family: "label-definitions",
        name: "Draft Global",
        published_version_id: null,
        versions: [
          {
            id: "ver-draft-only",
            entity_id: "ent-global-draft",
            version_number: 1,
            state: "draft",
            content: { key: "draft_lbl", scope: "global" },
            created_at: "2026-11-12T00:00:00Z",
          },
        ],
      },
      {
        id: "ent-global-pub",
        family: "label-definitions",
        name: "Published Global",
        published_version_id: "ver-global-pub",
        versions: [
          {
            id: "ver-global-pub",
            entity_id: "ent-global-pub",
            version_number: 1,
            state: "published",
            content: { key: "pub_global", scope: "global" },
            created_at: "2026-11-12T00:00:00Z",
          },
        ],
      },
      {
        id: "ent-media-pub",
        family: "label-definitions",
        name: "Published Media",
        published_version_id: "ver-media-pub",
        versions: [
          {
            id: "ver-media-pub",
            entity_id: "ent-media-pub",
            version_number: 1,
            state: "published",
            content: { key: "pub_media", scope: "media" },
            created_at: "2026-11-12T00:00:00Z",
          },
        ],
      },
    ];
    vi.spyOn(c, "listConfigEntities").mockImplementation(async (family) =>
      family === "label-definitions" ? labelEntities : [],
    );
    const applySpy = vi.spyOn(c, "applyManualLabel").mockResolvedValue({});

    const user = userEvent.setup();
    render(
      <ReviewWorkspace
        client={c}
        selectedMessageId={reviewedMessage.id}
        onSelectMessageId={() => {}}
      />,
    );

    await screen.findByRole("button", { name: "Record Manual Label Event" });
    expect(listMsgs).toHaveBeenCalledTimes(1);

    // Only published global version appears under global scope (draft & media excluded)
    const defSelect = screen.getByLabelText("Label Definition Version *");
    expect(
      within(defSelect)
        .getAllByRole("option")
        .map((o) => (o as HTMLOptionElement).value),
    ).toEqual(["ver-global-pub"]);

    // Switching to media scope swaps options to ver-media-pub and updates Label Key
    await user.selectOptions(screen.getByLabelText("Target Scope"), "media");
    expect(
      within(screen.getByLabelText("Label Definition Version *"))
        .getAllByRole("option")
        .map((o) => (o as HTMLOptionElement).value),
    ).toEqual(["ver-media-pub"]);
    expect(screen.getByLabelText("Label Key *")).toHaveValue("pub_media");

    // Out-of-range score rejected client-side
    fireEvent.change(screen.getByLabelText(/Score/), {
      target: { value: "1.5" },
    });
    fireEvent.submit(
      screen
        .getByRole("button", { name: "Record Manual Label Event" })
        .closest("form")!,
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /between 0\.0 and 1\.0/i,
    );
    expect(applySpy).not.toHaveBeenCalled();

    // Update Review Status disabled when nextReviewStatus === detail.message.review_status ("reviewed")
    expect(
      screen.getByRole("button", { name: "Update Review Status" }),
    ).toBeDisabled();
  });

  it("MessageDetailWorkspace performs a single detail fetch on lookup in App and disables Skip for processed messages", async () => {
    const c = baseClient();
    const getDetail = vi
      .spyOn(c, "getMessageDetail")
      .mockResolvedValue(structuredClone(detailFixture));

    const user = userEvent.setup();
    render(
      <App
        client={c}
        initialSession={SESSION}
        initialWorkspace="message-detail"
      />,
    );

    const input = await screen.findByLabelText("Message UUID:");
    await user.type(input, detailFixture.message.id);
    await user.click(
      screen.getByRole("button", { name: "Load Causal Chain" }),
    );

    expect(
      await screen.findByRole("button", {
        name: "Skip Message & Advance Watermark",
      }),
    ).toBeDisabled();
    expect(getDetail).toHaveBeenCalledTimes(1);
  });

  it("guards unsaved dirty draft edits on tab switch in AnalysisConfigWorkspace and RoutingWorkspace, and validates cleanup duplicate_statuses", async () => {
    const c = baseClient();
    vi.spyOn(c, "listConfigEntities").mockImplementation(async (family) => [
      {
        id: `ent-${family}`,
        family,
        name: `Draft ${family}`,
        published_version_id: null,
        versions: [
          {
            id: `ver-${family}`,
            entity_id: `ent-${family}`,
            version_number: 1,
            state: "draft",
            content:
              family === "rendering-templates"
                ? { template_text: "hello" }
                : { key: "k", display_name: "K", scope: "global" },
            created_at: "2026-11-12T00:00:00Z",
          },
        ],
      },
    ]);

    const user = userEvent.setup();
    const { unmount } = render(<AnalysisConfigWorkspace client={c} />);
    const editor = await screen.findByLabelText(/Version Content JSON/i);
    fireEvent.change(editor, {
      target: { value: '{"key":"edited","display_name":"K","scope":"global"}' },
    });

    await user.click(screen.getByRole("tab", { name: "Prompt Test Bench" }));
    expect(
      screen.getByRole("heading", { name: "Discard Unsaved Draft Changes?" }),
    ).toBeInTheDocument();

    // Cancel keeps current tab and dirty edits intact
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByLabelText(/Version Content JSON/i)).toHaveValue(
      '{"key":"edited","display_name":"K","scope":"global"}',
    );

    // Confirm discard navigates to Prompt Test Bench
    await user.click(screen.getByRole("tab", { name: "Prompt Test Bench" }));
    await user.click(
      screen.getByRole("button", { name: "Discard Unsaved Changes" }),
    );
    expect(
      await screen.findByRole("button", { name: "Execute Prompt Test Bench" }),
    ).toBeInTheDocument();
    unmount();

    // RoutingWorkspace dirty guard + ArchiveManagementWorkspace duplicate_statuses validation
    vi.spyOn(c, "listCleanupDryRuns").mockResolvedValue([]);
    vi.spyOn(c, "listCleanupJobs").mockResolvedValue({
      items: [],
      next_cursor: null,
      has_more: false,
    });
    const dryRunSpy = vi.spyOn(c, "createCleanupDryRun");
    const { unmount: unmountRouting } = render(<RoutingWorkspace client={c} />);
    await user.click(screen.getByRole("tab", { name: "Rendering Templates" }));
    const rtEditor = await screen.findByLabelText(
      /Non-Executable JSON Specification/i,
    );
    fireEvent.change(rtEditor, {
      target: { value: '{"template_text":"unsaved"}' },
    });
    await user.click(screen.getByRole("tab", { name: "Routing Policies" }));
    expect(
      screen.getByRole("heading", { name: "Discard Unsaved Draft Changes?" }),
    ).toBeInTheDocument();
    unmountRouting();

    render(<ArchiveManagementWorkspace client={c} />);
    await user.type(
      screen.getByLabelText("Duplicate Statuses (CSV)"),
      "bogus_status",
    );
    await user.click(
      screen.getByRole("button", { name: "Step 1: Run Dry-Run Preview" }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /Invalid duplicate_statuses: bogus_status/,
    );
    expect(dryRunSpy).not.toHaveBeenCalled();
  });
});
