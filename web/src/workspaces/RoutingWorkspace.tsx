import { RecordEditor } from "../components/RecordEditor";
import { ModalDialog } from "../components/ModalDialog";
import { useMutationGuard } from "../hooks/useMutationGuard";
import { useRequestGeneration } from "../hooks/useRequestGeneration";
import { ConfigReferences } from "../components/ConfigReferences";
import { publishedVersion, isDirtyJson } from "../api/config";
import { parseTelegramPeerId } from "../api/validation";
import { type FormEvent, useCallback, useEffect, useState } from "react";
import type { AdminApiClient } from "../api/client";
import type {
  ConfigEntity,
  ConfigVersion,
  DestinationChannel,
  RerouteResult,
  TelegramIdentity,
} from "../api/types";
import { ResponsiveTable } from "../components/ResponsiveTable";
import { Tabs } from "../components/Tabs";

export interface RoutingWorkspaceProps {
  client: AdminApiClient;
}

const DEFAULT_TEMPLATE_JSON = JSON.stringify(
  {
    template_text: "{{message.original_text}}",
  },
  null,
  2,
);

const DEFAULT_POLICY_JSON = JSON.stringify(
  {
    rules: [
      {
        id: "rule-curated-forward",
        priority: 100,
        stop_on_match: true,
        condition: {
          label: { namespace: "effective", scope: "global", key: "illustration" },
        },
        actions: [
          {
            destination_channel_id: "",
            publication_mode: "forward_only",
            rendering_template_version_id: null,
          },
        ],
      },
    ],
  },
  null,
  2,
);

export function RoutingWorkspace({ client }: RoutingWorkspaceProps) {
  const { busy, runAction } = useMutationGuard();
  const familyRequests = useRequestGeneration();
  const destRequests = useRequestGeneration();
  const [destLoading, setDestLoading] = useState(false);
  const [activeTab, setActiveTab] = useState("destination-channels");
  const [destinations, setDestinations] = useState<DestinationChannel[]>([]);
  const [identities, setIdentities] = useState<TelegramIdentity[]>([]);

  // Destination creation form
  const [destPeerId, setDestPeerId] = useState("");
  const [destName, setDestName] = useState("");
  const [destUsername, setDestUsername] = useState("");
  const [destIdentityId, setDestIdentityId] = useState("");

  // Versioned family state (rendering-templates / routing-policies)
  const [entities, setEntities] = useState<ConfigEntity[]>([]);
  const [selectedEntityId, setSelectedEntityId] = useState("");
  const [selectedVersionId, setSelectedVersionId] = useState("");
  const [editorJson, setEditorJson] = useState("{}");
  const [newEntityName, setNewEntityName] = useState("");
  const [newEntityJson, setNewEntityJson] = useState(DEFAULT_TEMPLATE_JSON);

  // Dry-run historical evaluator
  const [policyEntities, setPolicyEntities] = useState<ConfigEntity[]>([]);
  const [dryRunMessageId, setDryRunMessageId] = useState("");
  const [dryRunPolicyVerId, setDryRunPolicyVerId] = useState("");
  const [dryRunResult, setDryRunResult] = useState<RerouteResult | null>(null);

  const [statusMsg, setStatusMsg] = useState<string | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [pendingDiscardAction, setPendingDiscardAction] = useState<
    (() => void) | null
  >(null);

  const loadDestinations = useCallback(async () => {
    const request = destRequests.next();
    setDestLoading(true);
    setErrorMsg(null);
    try {
      const [dstList, idList] = await Promise.all([
        client.listDestinationChannels(),
        client.listIdentities(),
      ]);
      if (!destRequests.isCurrent(request)) return;
      setDestinations(dstList);
      setIdentities(idList);
    } catch (err) {
      if (!destRequests.isCurrent(request)) return;
      setErrorMsg(
        err instanceof Error
          ? err.message
          : "Failed to load destination channels.",
      );
    } finally {
      if (destRequests.isCurrent(request)) {
        setDestLoading(false);
      }
    }
  }, [client, destRequests]);

  const loadConfigFamily = useCallback(
    async (
      family: "rendering-templates" | "routing-policies",
      preferredEntityId?: string,
      preferredVersionId?: string,
    ) => {
      const request = familyRequests.next();
      setErrorMsg(null);
      try {
        const list = await client.listConfigEntities(family);
        if (!familyRequests.isCurrent(request)) return;
        setEntities(list);
        if (list.length > 0) {
          const ent =
            (preferredEntityId
              ? list.find((e) => e.id === preferredEntityId)
              : undefined) ?? list[0];
          setSelectedEntityId(ent.id);
          const ver =
            (preferredVersionId
              ? ent.versions.find((v) => v.id === preferredVersionId)
              : undefined) ??
            publishedVersion(ent) ??
            ent.versions[ent.versions.length - 1];
          setSelectedVersionId(ver?.id ?? "");
          setEditorJson(ver ? JSON.stringify(ver.content, null, 2) : "{}");
        } else {
          setSelectedEntityId("");
          setSelectedVersionId("");
          setEditorJson("{}");
        }
      } catch (err) {
        if (!familyRequests.isCurrent(request)) return;
        setErrorMsg(
          err instanceof Error ? err.message : "Failed to load config family.",
        );
      }
    },
    [client, familyRequests],
  );

  const loadPoliciesForDryRun = useCallback(async () => {
    try {
      const list = await client.listConfigEntities("routing-policies");
      setPolicyEntities(list);
    } catch {
      // Handled on submit if needed
    }
  }, [client]);

  useEffect(() => {
    if (activeTab === "destination-channels") {
      void loadDestinations();
    } else if (activeTab === "rendering-templates") {
      setNewEntityJson(DEFAULT_TEMPLATE_JSON);
      void loadConfigFamily("rendering-templates");
    } else if (activeTab === "routing-policies") {
      setNewEntityJson(DEFAULT_POLICY_JSON);
      void loadConfigFamily("routing-policies");
    } else if (activeTab === "dry-run-evaluator") {
      void loadPoliciesForDryRun();
    }
  }, [
    activeTab,
    loadDestinations,
    loadConfigFamily,
    loadPoliciesForDryRun,
  ]);

  async function handleCreateDestination(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setErrorMsg(null);
    setStatusMsg(null);
    const parsedPeerId = parseTelegramPeerId(destPeerId);
    if (parsedPeerId === null) {
      setErrorMsg("Telegram Peer ID must be a non-zero integer.");
      return;
    }
    await runAction(async () => {
      try {
        await client.createDestinationChannel({
          telegram_peer_id: parsedPeerId,
          name: destName.trim(),
          username: destUsername.trim() || null,
          default_publish_identity_id: destIdentityId || null,
          enabled: true,
        });
        setDestPeerId("");
        setDestName("");
        setDestUsername("");
        setStatusMsg("Destination channel created.");
        await loadDestinations();
      } catch (err) {
        setErrorMsg(
          err instanceof Error
            ? err.message
            : "Failed to create destination channel.",
        );
      }
    });
  }

  async function handleToggleDestination(dst: DestinationChannel) {
    setErrorMsg(null);
    await runAction(async () => {
      try {
        await client.updateDestinationChannel(dst.id, {
          enabled: !dst.enabled,
        });
        await loadDestinations();
      } catch (err) {
        setErrorMsg(
          err instanceof Error
            ? err.message
            : "Failed to update destination channel.",
        );
      }
    });
  }

  const activeConfigFamily =
    activeTab === "rendering-templates" || activeTab === "routing-policies"
      ? activeTab
      : null;
  const selectedEntity = entities.find((e) => e.id === selectedEntityId) ?? null;
  const selectedVersion: ConfigVersion | null =
    selectedEntity?.versions.find((v) => v.id === selectedVersionId) ?? null;
  const isDraft = selectedVersion?.state === "draft";
  const dirty = selectedVersion ? isDirtyJson(editorJson, selectedVersion.content) : false;

  function guardDirtyNavigation(action: () => void) {
    if (isDraft && dirty) {
      setPendingDiscardAction(() => action);
      return;
    }
    action();
  }

  async function handleCreateVersionedEntity(
    event: FormEvent<HTMLFormElement>,
  ) {
    event.preventDefault();
    if (!activeConfigFamily) return;
    setErrorMsg(null);
    setStatusMsg(null);
    try {
      const content = JSON.parse(newEntityJson) as Record<string, unknown>;
      const created = await client.createConfigEntity(activeConfigFamily, {
        name: newEntityName.trim(),
        initial_version: content,
      });
      setNewEntityName("");
      if (!created.versions[0]) { await loadConfigFamily(activeConfigFamily, created.id); throw new Error("Entity created without a version. Use Create First Draft to recover."); }
      setStatusMsg(`Created ${created.name} (DRAFT v1).`);
      await loadConfigFamily(
        activeConfigFamily,
        created.id,
        created.versions[0]?.id,
      );
    } catch (err) {
      setErrorMsg(
        err instanceof Error ? err.message : "Failed to create entity.",
      );
    }
  }

  async function handleSaveDraft() {
    if (!activeConfigFamily || !selectedEntity || !selectedVersion || !isDraft) {
      return;
    }
    setErrorMsg(null);
    try {
      const content = JSON.parse(editorJson) as Record<string, unknown>;
      await client.updateConfigVersion(
        activeConfigFamily,
        selectedEntity.id,
        selectedVersion.id,
        content,
      );
      setStatusMsg("Draft updated.");
      await loadConfigFamily(
        activeConfigFamily,
        selectedEntity.id,
        selectedVersion.id,
      );
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : "Failed to save draft.");
    }
  }

  async function handlePublish() {
    if (dirty) { setErrorMsg("Save Draft before publishing unsaved changes."); return; }
    if (!activeConfigFamily || !selectedEntity || !selectedVersion || !isDraft) {
      return;
    }
    setErrorMsg(null);
    try {
      await client.publishConfigVersion(
        activeConfigFamily,
        selectedEntity.id,
        selectedVersion.id,
        true,
      );
      setStatusMsg("Version published and locked immutable.");
      await loadConfigFamily(
        activeConfigFamily,
        selectedEntity.id,
        selectedVersion.id,
      );
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : "Publish failed.");
    }
  }

  async function handleCreateFirstDraft() {
    if (!activeConfigFamily || !selectedEntity || selectedEntity.versions.length) return;
    setErrorMsg(null);
    try {
      const created = await client.createConfigVersion(activeConfigFamily, selectedEntity.id, JSON.parse(newEntityJson));
      await loadConfigFamily(activeConfigFamily, selectedEntity.id, created.id);
      setStatusMsg("First draft created from Initial Draft JSON.");
    } catch (error) { setErrorMsg(error instanceof Error ? error.message : "First draft creation failed."); }
  }

  async function handleArchiveVersion() {
    if (!activeConfigFamily || !selectedEntity || !selectedVersion) return;
    setErrorMsg(null);
    try {
      await client.archiveConfigVersion(activeConfigFamily, selectedEntity.id, selectedVersion.id);
      await loadConfigFamily(activeConfigFamily, selectedEntity.id, selectedVersion.id);
      setStatusMsg("Version archived.");
    } catch (error) { setErrorMsg(error instanceof Error ? error.message : "Archive failed."); }
  }

  async function handleCloneDraft() {
    if (!activeConfigFamily || !selectedEntity || !selectedVersion) return;
    setErrorMsg(null);
    try {
      const cloned = await client.createConfigVersion(
        activeConfigFamily,
        selectedEntity.id,
        selectedVersion.content,
      );
      setStatusMsg(`Cloned to DRAFT v${cloned.version_number}.`);
      await loadConfigFamily(activeConfigFamily, selectedEntity.id, cloned.id);
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : "Clone failed.");
    }
  }

  async function handleDryRunEvaluate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setErrorMsg(null);
    setStatusMsg(null);
    try {
      const res = await client.rerouteMessage(dryRunMessageId.trim(), {
        routing_policy_version_id: dryRunPolicyVerId || undefined,
        dry_run: true,
        reason: "Routing workspace historical dry-run",
      });
      setDryRunResult(res);
      setStatusMsg(
        "Historical routing dry-run completed with zero PublicationIntents created.",
      );
    } catch (err) {
      setErrorMsg(
        err instanceof Error ? err.message : "Historical dry-run failed.",
      );
    }
  }

  const destinationPanel = (
    <div className="card-grid">
      <div className="panel">
        <h2 id="create-dest-heading">Register Destination Channel</h2>
        <form
          method="post"
          aria-labelledby="create-dest-heading"
          onSubmit={handleCreateDestination}
        >
          <div className="field">
            <label htmlFor="dest-peer-id">Telegram Peer ID *</label>
            <input
              id="dest-peer-id"
              type="text"
              inputMode="numeric"
              required
              placeholder="-100987654321"
              value={destPeerId}
              onChange={(e) => setDestPeerId(e.target.value)}
            />
          </div>
          <div className="field">
            <label htmlFor="dest-name">Display Name *</label>
            <input
              id="dest-name"
              type="text"
              required
              value={destName}
              onChange={(e) => setDestName(e.target.value)}
            />
          </div>
          <div className="field">
            <label htmlFor="dest-username">Channel Username (optional)</label>
            <input
              id="dest-username"
              type="text"
              autoCorrect="off"
              autoCapitalize="none"
              spellCheck={false}
              value={destUsername}
              onChange={(e) => setDestUsername(e.target.value)}
            />
          </div>
          <div className="field">
            <label htmlFor="dest-identity">Default Publish Identity</label>
            <select
              id="dest-identity"
              required
              value={destIdentityId}
              onChange={(e) => setDestIdentityId(e.target.value)}
            >
              <option value="">-- Select publish identity --</option>
              {identities.map((idItem) => (
                <option key={idItem.id} value={idItem.id}>
                  {idItem.name} ({idItem.identity_type})
                </option>
              ))}
            </select>
          </div>
          <button type="submit" className="btn btn-primary">
            Create Destination Channel
          </button>
        </form>

      </div>

      <div className="panel">
        <h2>Destination Channels ({destinations.length})</h2>
        <ResponsiveTable
          caption="Configured Telegram destination channels"
          headers={["Name", "Peer ID", "Enabled", "Publish Identity", "Action"]}
          loading={destLoading}
          emptyMessage="No destination channels registered yet."
        >
          {destinations.map((dst) => (
            <tr key={dst.id}>
              <th scope="row">{dst.name}</th>
              <td data-label="Peer ID" className="mono">
                {dst.telegram_peer_id}
              </td>
              <td data-label="Enabled">
                <span
                  className={dst.enabled ? "badge badge-ok" : "badge badge-warn"}
                >
                  {dst.enabled ? "Enabled" : "Disabled"}
                </span>
              </td>
              <td data-label="Publish Identity" className="mono">
                {dst.default_publish_identity_id
                  ? dst.default_publish_identity_id.slice(0, 8)
                  : "auto"}
              </td>
              <td data-label="Action">
                <RecordEditor label="Destination" fields={[
                  {key: "name", label: "Display Name", value: dst.name, required: true},
                  {key: "username", label: "Username", value: dst.username ?? null},
                  {key: "default_publish_identity_id", label: "Publish Identity", value: dst.default_publish_identity_id, required: true, options: identities.map(i => ({value: i.id, label: `${i.name}${i.enabled ? "" : " (disabled)"}`}))},
                ]} onSave={async changes => { await client.updateDestinationChannel(dst.id, changes); await loadDestinations(); }} />
                <button
                  type="button"
                  className="btn"
                  onClick={() => void handleToggleDestination(dst)}
                >
                  {dst.enabled ? "Disable" : "Enable"}
                </button>
              </td>
            </tr>
          ))}
        </ResponsiveTable>
      </div>
    </div>
  );

  const configEditorPanel = (
    <div className="card-grid">
      <div className="panel">
        <h2>
          {activeTab === "rendering-templates"
            ? "Rendering Templates"
            : "Routing Policies (`priority DESC` + `stop_on_match`)"}
        </h2>
        <div className="form-grid">
          <div className="field">
            <label htmlFor="rt-entity-select">Entity</label>
            <select
              id="rt-entity-select"
              value={selectedEntityId}
              onChange={(e) => {
                const nextId = e.target.value;
                if (nextId === selectedEntityId) return;
                guardDirtyNavigation(() => {
                  setSelectedEntityId(nextId);
                  const ent = entities.find((x) => x.id === nextId);
                  const ver =
                    publishedVersion(ent) ??
                    ent?.versions[ent.versions.length - 1];
                  setSelectedVersionId(ver?.id ?? "");
                  setEditorJson(
                    ver ? JSON.stringify(ver.content, null, 2) : "{}",
                  );
                });
              }}
            >
              {entities.map((ent) => (
                <option key={ent.id} value={ent.id}>
                  {ent.name}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="rt-ver-select">Version</label>
            <select
              id="rt-ver-select"
              value={selectedVersionId}
              onChange={(e) => {
                const nextVerId = e.target.value;
                if (nextVerId === selectedVersionId) return;
                guardDirtyNavigation(() => {
                  setSelectedVersionId(nextVerId);
                  const ver = selectedEntity?.versions.find(
                    (v) => v.id === nextVerId,
                  );
                  if (ver) {
                    setEditorJson(JSON.stringify(ver.content, null, 2));
                  }
                });
              }}
            >
              {(selectedEntity?.versions ?? []).map((ver) => (
                <option key={ver.id} value={ver.id}>
                  v{ver.version_number} [{ver.state.toUpperCase()}]
                </option>
              ))}
            </select>
          </div>
        </div>

        {selectedVersion ? (
          <>
            <p className="stat-meta">
              State:{" "}
              <span className={isDraft ? "badge badge-warn" : "badge badge-ok"}>
                {isDraft
                  ? "DRAFT (Editable)"
                  : `IMMUTABLE (${selectedVersion.state.toUpperCase()})`}
              </span>
            </p>
            {isDraft && activeConfigFamily ? <ConfigReferences client={client} family={activeConfigFamily} json={editorJson} onChange={setEditorJson} /> : null}
            <div className="field">
              <label htmlFor="rt-json-editor">
                Non-Executable JSON Specification{" "}
                {!isDraft ? "(Read-only)" : "*"}
              </label>
              <textarea
                id="rt-json-editor"
                rows={10}
                readOnly={!isDraft}
                value={editorJson}
                onChange={(e) => setEditorJson(e.target.value)}
              />
            </div>
            {dirty ? <p role="status" className="hint">Unsaved changes — Save Draft before publishing.</p> : null}
            <div className="btn-row">
              <button
                type="button"
                className="btn btn-primary"
                disabled={!isDraft}
                onClick={() => void runAction(handleSaveDraft)}
              >
                Save Draft
              </button>
              <button
                type="button"
                className="btn btn-primary"
                disabled={!isDraft || dirty || busy}
                onClick={() => void runAction(handlePublish)}
              >
                Publish Version
              </button>
              <button
                type="button"
                className="btn"
                onClick={() => void runAction(handleCloneDraft)}
              >
                Clone to New Draft
              </button>
              {selectedVersion.state === "published" ? <button className="btn btn-danger" onClick={() => void runAction(handleArchiveVersion)}>Archive Version</button> : null}
            </div>
          </>
        ) : null}
        {selectedEntity && !selectedEntity.versions.length ? <><p className="hint">This entity has no versions. Fill Initial Draft JSON, then recover it here.</p><button className="btn" onClick={() => void runAction(handleCreateFirstDraft)}>Create First Draft</button></> : null}
      </div>

      <div className="panel">
        <h2>
          Create New{" "}
          {activeTab === "rendering-templates"
            ? "Rendering Template"
            : "Routing Policy"}
        </h2>
        <form method="post" onSubmit={event => { event.preventDefault(); void runAction(() => handleCreateVersionedEntity(event)); }}>
          <div className="field">
            <label htmlFor="rt-new-name">Name *</label>
            <input
              id="rt-new-name"
              type="text"
              required
              autoCorrect="off"
              autoCapitalize="none"
              spellCheck={false}
              value={newEntityName}
              onChange={(e) => setNewEntityName(e.target.value)}
            />
          </div>
          {activeConfigFamily ? <ConfigReferences client={client} family={activeConfigFamily} json={newEntityJson} onChange={setNewEntityJson} /> : null}
          <div className="field">
            <label htmlFor="rt-new-json">Initial Draft JSON *</label>
            <textarea
              id="rt-new-json"
              rows={9}
              required
              value={newEntityJson}
              onChange={(e) => setNewEntityJson(e.target.value)}
            />
          </div>
          <button type="submit" className="btn btn-primary">
            Create Draft v1
          </button>
        </form>
      </div>
    </div>
  );

  const dryRunPanel = (
    <div className="panel">
      <h2>Historical Message Routing Dry-Run Evaluator</h2>
      <p className="hint">
        Test any published RoutingPolicyVersion against a historical message
        snapshot (`forward_only`, `copy_with_caption`, `metadata_only`,
        `native_forward_with_supplement`) without creating PublicationIntents.
      </p>
      <form method="post" onSubmit={handleDryRunEvaluate}>
        <div className="form-grid">
          <div className="field">
            <label htmlFor="dryrun-msg-id">Historical Message UUID *</label>
            <input
              id="dryrun-msg-id"
              type="text"
              required
              autoCorrect="off"
              autoCapitalize="none"
              spellCheck={false}
              value={dryRunMessageId}
              onChange={(e) => setDryRunMessageId(e.target.value)}
            />
          </div>
          <div className="field">
            <label htmlFor="dryrun-policy-ver">
              Routing Policy Version (optional override)
            </label>
            <select
              id="dryrun-policy-ver"
              value={dryRunPolicyVerId}
              onChange={(e) => setDryRunPolicyVerId(e.target.value)}
            >
              <option value="">Active channel routing policy</option>
              {policyEntities.flatMap((p) =>
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
        </div>
        <button type="submit" className="btn btn-primary">
          Evaluate Dry-Run Routing
        </button>
      </form>

      {dryRunResult ? (
        <pre className="code-block" style={{ marginBlockStart: "1rem" }}>
          {JSON.stringify(dryRunResult, null, 2)}
        </pre>
      ) : null}
    </div>
  );

  return (
    <section aria-labelledby="routing-heading">
      <fieldset disabled={busy} style={{border: 0, padding: 0, margin: 0, minWidth: 0}}>
      <div className="workspace-header">
        <div>
          <h1 id="routing-heading">Routing &amp; Destination Channels</h1>
          <p className="workspace-subtitle">
            Manage DestinationChannels, versioned RenderingTemplates,
            deterministic RoutingPolicies (`priority` + `stop_on_match`), and
            side-effect-free historical dry-run evaluations.
          </p>
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

      <Tabs
        ariaLabel="Routing Configuration Sections"
        activeKey={activeTab}
        onSelect={(tab) => {
          if (tab === activeTab) return;
          guardDirtyNavigation(() => {
            familyRequests.invalidate();
            setEntities([]);
            setSelectedEntityId("");
            setSelectedVersionId("");
            setEditorJson("{}");
            setActiveTab(tab);
          });
        }}
        tabs={[
          {
            key: "destination-channels",
            label: "Destination Channels",
            content: destinationPanel,
          },
          {
            key: "rendering-templates",
            label: "Rendering Templates",
            content: configEditorPanel,
          },
          {
            key: "routing-policies",
            label: "Routing Policies",
            content: configEditorPanel,
          },
          {
            key: "dry-run-evaluator",
            label: "Dry-Run Evaluator",
            content: dryRunPanel,
          },
        ]}
      />
      <ModalDialog
        open={pendingDiscardAction !== null}
        title="Discard Unsaved Draft Changes?"
        confirmLabel="Discard Unsaved Changes"
        destructive={true}
        onConfirm={() => {
          const action = pendingDiscardAction;
          setPendingDiscardAction(null);
          action?.();
        }}
        onCancel={() => setPendingDiscardAction(null)}
      >
        <p>
          You have unsaved edits in the current DRAFT JSON specification.
          Discard your unsaved changes?
        </p>
      </ModalDialog>
      </fieldset>
    </section>
  );
}
