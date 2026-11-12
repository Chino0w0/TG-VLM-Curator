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
    header_template: "Curated from {{source_channel_name}}",
    body_template: "{{original_text}}",
    footer_template: "Tags: {{effective_labels}}",
    supplement_template: "Source link: {{source_message_link}}",
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
          op: "label_activated",
          namespace: "effective",
          scope: "global",
          key: "illustration",
        },
        actions: [
          {
            destination_channel_id: "",
            publication_mode: "copy_publish",
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

  const loadDestinations = useCallback(async () => {
    setErrorMsg(null);
    try {
      const [dstList, idList] = await Promise.all([
        client.listDestinationChannels(),
        client.listIdentities(),
      ]);
      setDestinations(dstList);
      setIdentities(idList);
    } catch (err) {
      setErrorMsg(
        err instanceof Error
          ? err.message
          : "Failed to load destination channels.",
      );
    }
  }, [client]);

  const loadConfigFamily = useCallback(
    async (
      family: "rendering-templates" | "routing-policies",
      preferredEntityId?: string,
      preferredVersionId?: string,
    ) => {
      setErrorMsg(null);
      try {
        const list = await client.listConfigEntities(family);
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
            ent.published_version ??
            ent.versions[ent.versions.length - 1];
          if (ver) {
            setSelectedVersionId(ver.id);
            setEditorJson(JSON.stringify(ver.content, null, 2));
          }
        } else {
          setSelectedEntityId("");
          setSelectedVersionId("");
          setEditorJson("{}");
        }
      } catch (err) {
        setErrorMsg(
          err instanceof Error ? err.message : "Failed to load config family.",
        );
      }
    },
    [client],
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
    try {
      await client.createDestinationChannel({
        telegram_peer_id: Number(destPeerId),
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
  }

  async function handleToggleDestination(dst: DestinationChannel) {
    setErrorMsg(null);
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
  }

  const activeConfigFamily =
    activeTab === "rendering-templates" || activeTab === "routing-policies"
      ? activeTab
      : null;
  const selectedEntity = entities.find((e) => e.id === selectedEntityId) ?? null;
  const selectedVersion: ConfigVersion | null =
    selectedEntity?.versions.find((v) => v.id === selectedVersionId) ?? null;
  const isDraft = selectedVersion?.state === "draft";

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
        content,
      });
      setNewEntityName("");
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
              type="number"
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
              value={destIdentityId}
              onChange={(e) => setDestIdentityId(e.target.value)}
            >
              <option value="">-- Automatic / Unassigned --</option>
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
                setSelectedEntityId(e.target.value);
                const ent = entities.find((x) => x.id === e.target.value);
                const ver =
                  ent?.published_version ??
                  ent?.versions[ent.versions.length - 1];
                setSelectedVersionId(ver?.id ?? "");
                setEditorJson(ver ? JSON.stringify(ver.content, null, 2) : "{}");
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
                setSelectedVersionId(e.target.value);
                const ver = selectedEntity?.versions.find(
                  (v) => v.id === e.target.value,
                );
                if (ver) {
                  setEditorJson(JSON.stringify(ver.content, null, 2));
                }
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
            <div className="btn-row">
              <button
                type="button"
                className="btn btn-primary"
                disabled={!isDraft}
                onClick={() => void handleSaveDraft()}
              >
                Save Draft
              </button>
              <button
                type="button"
                className="btn btn-primary"
                disabled={!isDraft}
                onClick={() => void handlePublish()}
              >
                Publish Version
              </button>
              <button
                type="button"
                className="btn"
                onClick={() => void handleCloneDraft()}
              >
                Clone to New Draft
              </button>
            </div>
          </>
        ) : null}
      </div>

      <div className="panel">
        <h2>
          Create New{" "}
          {activeTab === "rendering-templates"
            ? "Rendering Template"
            : "Routing Policy"}
        </h2>
        <form method="post" onSubmit={handleCreateVersionedEntity}>
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
        snapshot (`native_forward`, `copy_publish`, `metadata_only`,
        `forward_plus_supplement`) without creating PublicationIntents.
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
        onSelect={setActiveTab}
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
    </section>
  );
}
