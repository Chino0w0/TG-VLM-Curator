import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { AdminApiClient, ApiClientError } from "../api/client";
import type { AdminSessionResponse, CleanupDryRun, ConfigEntity, ConfigVersion, MessageDetail } from "../api/types";
import { publishedVersion } from "../api/config";
import { PROCESSING_STATUSES, DUPLICATE_STATUSES } from "../api/messageStates";
import { WorkspaceErrorBoundary } from "../components/WorkspaceErrorBoundary";
import { ArchiveManagementWorkspace } from "../workspaces/ArchiveManagementWorkspace";
import { AnalysisConfigWorkspace } from "../workspaces/AnalysisConfigWorkspace";
import { RoutingWorkspace } from "../workspaces/RoutingWorkspace";
import { MessagesWorkspace } from "../workspaces/MessagesWorkspace";
import { ReviewWorkspace } from "../workspaces/ReviewWorkspace";
import { MessageDetailWorkspace } from "../workspaces/MessageDetailWorkspace";
import { AuditLogWorkspace } from "../workspaces/AuditLogWorkspace";
import rawReroute from "./fixtures/reroute.json";
import type { RerouteResult } from "../api/types";
import rawDetail from "./fixtures/message-detail.json";
import rawEntity from "./fixtures/config-entity.json";
import states from "./fixtures/message-states.json";

const detail = rawDetail as unknown as MessageDetail;
const entity = rawEntity as unknown as ConfigEntity;
const session: AdminSessionResponse = {user:{id:"test-admin", username:"test"}, session_id:"local", csrf_token:"test-csrf", expires_at:"2026-12-31T00:00:00Z"};
const pageOf = <T,>(items:T[], next_cursor:string|null=null) => ({items,next_cursor,has_more:!!next_cursor});
function deferred<T>() { let resolve!: (value:T)=>void; let reject!: (error:Error)=>void; const promise=new Promise<T>((res,rej)=>{resolve=res;reject=rej;}); return {promise,resolve,reject}; }
function client() {
  const c = new AdminApiClient({fetchImpl:vi.fn()});
  vi.spyOn(c,"listSourceChannels").mockResolvedValue([]);
  vi.spyOn(c,"listDestinationChannels").mockResolvedValue([]);
  vi.spyOn(c,"listIdentities").mockResolvedValue([]);
  vi.spyOn(c,"listSecrets").mockResolvedValue([]);
  vi.spyOn(c,"listConfigEntities").mockResolvedValue([]);
  vi.spyOn(c,"getMessageDetail").mockResolvedValue(structuredClone(detail));
  return c;
}
const preview: CleanupDryRun = {id:"dry-one",actor_admin_user_id:"test-admin",filter_snapshot:{archived_before:"2026-12-01T00:00:00Z"},filter_hash:"hash-one",estimated_asset_count:30,estimated_message_count:30,estimated_bytes:61440,created_at:"2026-11-12T00:00:00Z"};
function cleanupClient() { const c=client();vi.spyOn(c,"listCleanupDryRuns").mockResolvedValue([]);vi.spyOn(c,"listCleanupJobs").mockResolvedValue(pageOf([]));return c; }

describe("PR30 regressions grounded in current backend serializers/HTTP DTOs",()=>{
  it("keeps enums and published version resolution aligned to backend-generated fixtures",()=>{
    expect([...PROCESSING_STATUSES]).toEqual(states.processing);
    expect([...DUPLICATE_STATUSES]).toEqual(states.duplicate);
    expect(publishedVersion(entity)?.id).toBe(entity.published_version_id);
    expect(publishedVersion({...entity,published_version_id:null})).toBeUndefined();
    expect(detail.analysis_runs[0].stage_runs[0].input_manifest).toHaveProperty("input_manifest_hash");
    expect(detail.routing_evaluations[0]).not.toHaveProperty("publication_intents");
  });
  it("renders actual serializer manifests and top-level intents without losing the shell",async()=>{
    const c=client();render(<MessageDetailWorkspace client={c} selectedMessageId={detail.message.id} onSelectMessageId={()=>{}} onOpenReview={()=>{}} />);
    expect(await screen.findByText("primary:#9001, #9002")).toBeInTheDocument();
    expect(screen.getAllByText(/aaaaaaaaaaaaaaaa/).length).toBeGreaterThan(0);
    expect(screen.getByText(/"input_tokens": 123/)).toBeInTheDocument();
  });
  it("renders the real reroute HTTP DTO without nonexistent comparison fields", async () => {
    const c = client();
    vi.spyOn(c, "listMessages").mockResolvedValue(pageOf([detail.message]));
    vi.spyOn(c, "rerouteMessage").mockResolvedValue(rawReroute as RerouteResult);
    const user = userEvent.setup();
    render(<ReviewWorkspace client={c} selectedMessageId={detail.message.id} onSelectMessageId={() => {}} />);
    await user.click(await screen.findByRole("button", {name: "Preview Dry-Run Re-route"}));
    expect(await screen.findByText(/Previous Evaluation:/)).toBeInTheDocument();
    expect(rawReroute.intents[0].rendered_text).toBe("Test message 01");
  });
  it("contains an unexpected render failure while navigation remains usable",()=>{
    vi.spyOn(console,"error").mockImplementation(()=>{});
    const Throw=():never=>{throw new Error("fixture-render-failure");};
    render(<><nav>Preserved navigation</nav><WorkspaceErrorBoundary><Throw /></WorkspaceErrorBoundary></>);
    expect(screen.getByRole("navigation")).toHaveTextContent("Preserved navigation");expect(screen.getByRole("alert")).toHaveTextContent("Workspace could not be displayed");
    vi.restoreAllMocks();
  });
  it("discards a pending cleanup preview after filter change and its late error cannot replace newer success",async()=>{
    const c=cleanupClient(),old=deferred<CleanupDryRun>();
    vi.spyOn(c,"createCleanupDryRun").mockReturnValueOnce(old.promise).mockResolvedValue({...preview,id:"dry-new"});
    const user=userEvent.setup();render(<ArchiveManagementWorkspace client={c}/>);
    fireEvent.change(screen.getByLabelText("Archived Before (ISO UTC)"),{target:{value:"2026-12-01T00:00:00Z"}});
    await user.click(screen.getByRole("button",{name:"Step 1: Run Dry-Run Preview"}));
    fireEvent.change(screen.getByLabelText("Archived Before (ISO UTC)"),{target:{value:"2026-10-01T00:00:00Z"}});
    await user.click(screen.getByRole("button",{name:"Step 1: Run Dry-Run Preview"}));
    await waitFor(()=>expect(screen.getByRole("button",{name:"Step 2: Confirm Cleanup Job..."})).toBeEnabled());
    await act(async()=>old.reject(new Error("stale failure")));
    expect(screen.queryByText("stale failure")).not.toBeInTheDocument();expect(screen.getByRole("button",{name:"Step 2: Confirm Cleanup Job..."})).toBeEnabled();
  });
  it("late successful cleanup response cannot unlock changed scope",async()=>{
    const c=cleanupClient(),old=deferred<CleanupDryRun>();vi.spyOn(c,"createCleanupDryRun").mockReturnValue(old.promise);
    render(<ArchiveManagementWorkspace client={c}/>);fireEvent.change(screen.getByLabelText("Archived Before (ISO UTC)"),{target:{value:"2026-12-01T00:00:00Z"}});fireEvent.click(screen.getByRole("button",{name:"Step 1: Run Dry-Run Preview"}));
    fireEvent.change(screen.getByLabelText("Archived Before (ISO UTC)"),{target:{value:"2026-10-01T00:00:00Z"}});await act(async()=>old.resolve(preview));
    expect(screen.getByRole("button",{name:"Step 2: Confirm Cleanup Job..."})).toBeDisabled();
  });
  it("shows cleanup scope and blocks duplicate confirmations, requiring new preview after unknown failure",async()=>{
    const c=cleanupClient(),pending=deferred<never>();vi.spyOn(c,"createCleanupDryRun").mockResolvedValue(preview);const confirm=vi.spyOn(c,"confirmCleanupJob").mockReturnValue(pending.promise);
    const user=userEvent.setup();render(<ArchiveManagementWorkspace client={c}/>);fireEvent.change(screen.getByLabelText("Archived Before (ISO UTC)"),{target:{value:"2026-12-01T00:00:00Z"}});await user.click(screen.getByRole("button",{name:"Step 1: Run Dry-Run Preview"}));await user.click(screen.getByRole("button",{name:"Step 2: Confirm Cleanup Job..."}));expect(screen.getByRole("dialog")).toHaveTextContent("2026-12-01");
    const button=screen.getByRole("button",{name:"Delete Matched Archive Blobs"});fireEvent.click(button);fireEvent.click(button);expect(confirm).toHaveBeenCalledTimes(1);await act(async()=>pending.reject(new Error("response lost")));expect(screen.getByRole("button",{name:"Step 2: Confirm Cleanup Job..."})).toBeDisabled();expect(screen.getByRole("alert")).toHaveTextContent("Run a new preview");
  });
  it("Messages ignores an older page response after applying a newer query",async()=>{
    const c=client(),old=deferred<ReturnType<typeof pageOf<typeof detail.message>>>();
    vi.spyOn(c,"listMessages").mockResolvedValueOnce(pageOf([{...detail.message,original_text:"first page"}],"cursor-two")).mockReturnValueOnce(old.promise).mockResolvedValue(pageOf([{...detail.message,original_text:"new query result"}]));
    const user=userEvent.setup();render(<MessagesWorkspace client={c} onInspectMessage={()=>{}} onReviewMessage={()=>{}}/>);await screen.findByText("first page");await user.click(screen.getByRole("button",{name:"Next Page"}));fireEvent.change(screen.getByLabelText("Text Query"),{target:{value:"new query"}});await user.click(screen.getByRole("button",{name:"Apply Filters"}));await screen.findByText("new query result");await act(async()=>old.resolve(pageOf([{...detail.message,original_text:"STALE PAGE"}],"stale-cursor")));expect(screen.queryByText("STALE PAGE")).not.toBeInTheDocument();expect(screen.getByRole("button",{name:"Next Page"})).toBeDisabled();
  });
  it.each(["image_asset","video_asset"])("MEDIA %s set and clear serialize matching asset IDs",async kind=>{
    const c=client();vi.mocked(c.listConfigEntities).mockResolvedValue([entity]);vi.spyOn(c,"listMessages").mockResolvedValue(pageOf([detail.message]));const submit=vi.spyOn(c,"applyManualLabel").mockResolvedValue({});
    const user=userEvent.setup();render(<ReviewWorkspace client={c} selectedMessageId={detail.message.id} onSelectMessageId={()=>{}}/>);
    await screen.findByRole("button",{name:"Record Manual Label Event"});
    const assetId=(kind==="image_asset"?detail.media.images:detail.media.videos)[0].id;
    for(const operation of ["set","clear"]){await user.selectOptions(screen.getByLabelText("Target Scope"),"media");await user.selectOptions(screen.getByLabelText("Target ID *"),`${kind}:${assetId}`);await user.selectOptions(screen.getByLabelText("Operation"),operation);await user.click(screen.getByRole("button",{name:"Record Manual Label Event"}));await waitFor(()=>expect(submit).toHaveBeenLastCalledWith(detail.message.id,expect.objectContaining({operation,target_id:assetId,target_kind:kind,[kind==="image_asset"?"image_asset_id":"video_asset_id"]:assetId})));await screen.findByRole("button",{name:"Record Manual Label Event"});}
  });  it("Review retains cursor pagination and discards a late page after queue change",async()=>{
    const c=client(),old=deferred<ReturnType<typeof pageOf<typeof detail.message>>>();vi.spyOn(c,"listMessages").mockResolvedValueOnce(pageOf([{...detail.message,original_text:"queue first"}],"cursor-two")).mockReturnValueOnce(old.promise).mockResolvedValue(pageOf([{...detail.message,original_text:"reviewed queue"}]));
    const user=userEvent.setup();render(<ReviewWorkspace client={c} selectedMessageId={detail.message.id} onSelectMessageId={()=>{}}/>);await screen.findByText("queue first");await user.click(screen.getByRole("button",{name:"Next Queue Page"}));await user.selectOptions(screen.getByLabelText("Queue:"),"reviewed");await screen.findByText("reviewed queue");await act(async()=>old.resolve(pageOf([{...detail.message,original_text:"stale queue"}])));expect(screen.queryByText("stale queue")).not.toBeInTheDocument();expect(screen.getByRole("button",{name:"Previous Queue Page"})).toBeDisabled();
  });
  it("Audit ignores late filtered pages and keeps current pagination",async()=>{
    const c=client(),old=deferred<ReturnType<typeof pageOf<typeof detail.audit_events[number]>>>();
    const event={id:"audit-one",event_type:"old audit",entity_type:"message",entity_id:detail.message.id,actor_admin_user_id:null,payload:{},created_at:"2026-11-12"};
    vi.spyOn(c,"listAuditEvents").mockResolvedValueOnce(pageOf([event],"cursor-two")).mockReturnValueOnce(old.promise).mockResolvedValue(pageOf([{...event,event_type:"current audit"}]));
    const user=userEvent.setup();render(<AuditLogWorkspace client={c}/>);await screen.findByText("old audit");await user.click(screen.getByRole("button",{name:"Next Page"}));fireEvent.change(screen.getByLabelText("Event Type"),{target:{value:"current audit"}});fireEvent.submit(screen.getByLabelText("Event Type").closest("form")!);await screen.findByText("current audit");await act(async()=>old.resolve(pageOf([{...event,event_type:"stale audit"}])));expect(screen.queryByText("stale audit")).not.toBeInTheDocument();expect(screen.getByRole("button",{name:"Previous Page"})).toBeDisabled();
  });
  it.each(["analysis","routing"])("%s dirty drafts cannot publish before save",async kind=>{
    const c=client();const version: ConfigVersion={...entity.versions[0],state:"draft" as const,content:kind==="analysis"?{key:"quality",display_name:"Quality",scope:"global"}:{template_text:"old text"}};const draft={...entity,family:kind==="analysis"?"label-definitions" as const:"rendering-templates" as const,published_version_id:null,versions:[version]};
    vi.mocked(c.listConfigEntities).mockResolvedValue([draft]);const publish=vi.spyOn(c,"publishConfigVersion").mockResolvedValue(version);
    const pending=deferred<typeof version>();vi.spyOn(c,"updateConfigVersion").mockImplementation((_f,_e,_v,content)=>{draft.versions[0]={...version,content};return pending.promise;});
    const user=userEvent.setup();render(kind==="analysis"?<AnalysisConfigWorkspace client={c}/>:<RoutingWorkspace client={c}/>);if(kind==="routing")await user.click(screen.getByRole("tab",{name:"Rendering Templates"}));await screen.findByRole("button",{name:"Save Draft"});
    const editor=screen.getByLabelText(kind==="analysis"?/Version Content JSON/:/Non-Executable JSON Specification/);fireEvent.change(editor,{target:{value:JSON.stringify({...version.content,description:"new unsaved text"})}});expect(screen.getByRole("button",{name:"Publish Version"})).toBeDisabled();await user.click(screen.getByRole("button",{name:"Save Draft"}));expect(editor).toBeDisabled();expect(screen.getByRole("button",{name:"Publish Version"})).toBeDisabled();expect(publish).not.toHaveBeenCalled();await act(async()=>pending.resolve(draft.versions[0]));await waitFor(()=>expect(screen.getByRole("button",{name:"Publish Version"})).toBeEnabled());
  });
  it("creates the first version for a legacy empty entity rather than claiming a nonexistent v1",async()=>{
    const c=client(),empty={...entity,published_version_id:null,versions:[]};vi.mocked(c.listConfigEntities).mockResolvedValue([empty]);const create=vi.spyOn(c,"createConfigVersion").mockResolvedValue(entity.versions[0]);const user=userEvent.setup();render(<AnalysisConfigWorkspace client={c}/>);await user.click(await screen.findByRole("button",{name:"Create First Draft"}));expect(create).toHaveBeenCalledWith("label-definitions",entity.id,expect.objectContaining({display_name:"Illustration"}));
  });
  it("a failed logout retains the authenticated UI and reports retry without false success",async()=>{
    const c=client();vi.spyOn(c,"listAuditEvents").mockResolvedValue(pageOf([]));vi.spyOn(c,"logout").mockRejectedValue(new ApiClientError(503,"unavailable","service unavailable"));const user=userEvent.setup();render(<App client={c} initialSession={session} initialWorkspace="audit-log"/>);await user.click(screen.getByRole("button",{name:"Sign Out"}));expect(await screen.findByRole("alert")).toHaveTextContent("server session may still be active");expect(screen.getByRole("navigation")).toBeInTheDocument();expect(screen.queryByLabelText(/Username/)).not.toBeInTheDocument();
  });
  it("every API 401 resets App authentication, and stale pre-login 401 cannot end a new login",async()=>{
    let fail=false;const old=deferred<Response>();const fetcher=vi.fn<typeof fetch>().mockImplementation(async input=>{
      const url=String(input);if(url.includes("/auth/login"))return new Response(JSON.stringify(session));
      if(url.includes("/messages/old"))return old.promise;
      return fail?new Response(JSON.stringify({detail:"expired"}),{status:401}):new Response(JSON.stringify(pageOf([])));
    });const c=new AdminApiClient({fetchImpl:fetcher});const user=userEvent.setup();render(<App client={c} initialSession={session} initialWorkspace="audit-log"/>);await screen.findByRole("heading",{name:"Audit Log"});const listener=vi.fn();c.onUnauthorized(listener);const pending=c.getMessageDetail("old").catch(()=>{});await c.login("test","test");await act(async()=>old.resolve(new Response("{}",{status:401})));await pending;expect(listener).not.toHaveBeenCalled();fail=true;await user.click(within(screen.getByRole("main")).getByRole("button",{name:"Refresh"}));await screen.findByRole("button",{name:"Sign In"});expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
  });
});