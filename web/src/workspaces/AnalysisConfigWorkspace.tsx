import { type FormEvent, useCallback, useEffect, useState } from "react";
import type { AdminApiClient } from "../api/client";
import type {
  ConfigEntity,
  ConfigFamily,
  ConfigVersion,
  PromptTestBenchResponse,
} from "../api/types";
import { Tabs } from "../components/Tabs";

const ANALYSIS_FAMILIES: Array<{
  family: ConfigFamily;
  label: string;
  defaultContent: Record<string, unknown>;
}> = [
  {
    family: "label-definitions",
    label: "Label Definitions",
    defaultContent: {
      key: "illustration",
      scope: "global",
      value_type: "score",
      threshold: 0.7,
      negative: false,
      description: "High-quality illustration content",
    },
  },
  {
    family: "label-sets",
    label: "Label Sets",
    defaultContent: {
      label_definition_version_ids: [],
    },
  },
  {
    family: "prompt-templates",
    label: "Prompt Templates",
    defaultContent: {
      system_prompt: "Analyze the Telegram post and output structured JSON.",
      user_prompt_template: "Caption: {{text}}",
    },
  },
  {
    family: "inference-profiles",
    label: "Inference Profiles",
    defaultContent: {
      provider_adapter: "openai_compatible",
      base_url: "https://vlm.example.internal/v1",
      model_name: "qwen2.5-vl-72b",
      api_key_secret_id: null,
      timeout_seconds: 60,
      supports_structured_outputs: true,
    },
  },
  {
    family: "analysis-stages",
    label: "Analysis Stages",
    defaultContent: {
      execution_mode: "single_message",
      visual_composition_policy: "adaptive",
      prompt_template_version_id: "",
      inference_profile_version_id: "",
      label_set_version_id: "",
    },
  },
  {
    family: "analysis-pipelines",
    label: "Analysis Pipelines",
    defaultContent: {
      entry_nodes: ["prescreen"],
      nodes: [],
    },
  },
  {
    family: "media-processing-profiles",
    label: "Media Profiles",
    defaultContent: {
      max_image_edge: 1536,
      video_sampling_mode: "hybrid",
      max_video_frames: 6,
      duplicate_policy: "skip",
      phash_hamming_threshold: 6,
    },
  },
  {
    family: "source-channel-profiles",
    label: "Channel Profiles",
    defaultContent: {
      analysis_pipeline_version_id: "",
      media_processing_profile_version_id: "",
      routing_policy_version_id: "",
    },
  },
];

export interface AnalysisConfigWorkspaceProps {
  client: AdminApiClient;
}

export function AnalysisConfigWorkspace({
  client,
}: AnalysisConfigWorkspaceProps) {
  const [activeTab, setActiveTab] = useState<string>("label-definitions");
  const [entities, setEntities] = useState<ConfigEntity[]>([]);
  const [selectedEntityId, setSelectedEntityId] = useState<string>("");
  const [selectedVersionId, setSelectedVersionId] = useState<string>("");
  const [editorJson, setEditorJson] = useState<string>("{}");

  const [newEntityName, setNewEntityName] = useState("");
  const [newEntityChannelId, setNewEntityChannelId] = useState("");
  const [newEntityJson, setNewEntityJson] = useState(
    JSON.stringify(ANALYSIS_FAMILIES[0].defaultContent, null, 2),
  );

  // Prompt Test Bench State
  const [stageEntities, setStageEntities] = useState<ConfigEntity[]>([]);
  const [benchStageVersionId, setBenchStageVersionId] = useState("");
  const [benchMessageId, setBenchMessageId] = useState("");
  const [benchSampleText, setBenchSampleText] = useState(
    "Sample Telegram post text for Prompt Test Bench validation.",
  );
  const [benchInvokeProvider, setBenchInvokeProvider] = useState(true);
  const [benchResult, setBenchResult] =
    useState<PromptTestBenchResponse | null>(null);

  const [statusMsg, setStatusMsg] = useState<string | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const activeFamilyMeta = ANALYSIS_FAMILIES.find(
    (f) => f.family === activeTab,
  );

  const loadFamily = useCallback(
    async (
      family: ConfigFamily,
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
          err instanceof Error
            ? err.message
            : "Failed to load configuration family.",
        );
      }
    },
    [client],
  );

  const loadStagesForBench = useCallback(async () => {
    try {
      const stages = await client.listConfigEntities("analysis-stages");
      setStageEntities(stages);
      if (!benchStageVersionId && stages.length > 0) {
        const firstVer =
          stages[0].published_version ?? stages[0].versions[0];
        if (firstVer) {
          setBenchStageVersionId(firstVer.id);
        }
      }
    } catch {
      // Handled when running bench if unavailable
    }
  }, [client, benchStageVersionId]);

  useEffect(() => {
    if (activeFamilyMeta) {
      setNewEntityJson(
        JSON.stringify(activeFamilyMeta.defaultContent, null, 2),
      );
      void loadFamily(activeFamilyMeta.family);
    } else if (activeTab === "prompt-test-bench") {
      void loadStagesForBench();
    }
  }, [activeTab, activeFamilyMeta, loadFamily, loadStagesForBench]);

  const selectedEntity = entities.find((e) => e.id === selectedEntityId) ?? null;
  const selectedVersion: ConfigVersion | null =
    selectedEntity?.versions.find((v) => v.id === selectedVersionId) ?? null;
  const isDraft = selectedVersion?.state === "draft";

  function handleSelectEntity(entityId: string) {
    setSelectedEntityId(entityId);
    const ent = entities.find((e) => e.id === entityId);
    const ver =
      ent?.published_version ?? ent?.versions[ent.versions.length - 1] ?? null;
    setSelectedVersionId(ver?.id ?? "");
    setEditorJson(ver ? JSON.stringify(ver.content, null, 2) : "{}");
  }

  function handleSelectVersion(versionId: string) {
    setSelectedVersionId(versionId);
    const ver = selectedEntity?.versions.find((v) => v.id === versionId);
    if (ver) {
      setEditorJson(JSON.stringify(ver.content, null, 2));
    }
  }

  async function handleCreateEntity(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!activeFamilyMeta) return;
    setErrorMsg(null);
    setStatusMsg(null);
    try {
      const parsed = JSON.parse(newEntityJson) as Record<string, unknown>;
      const payload: Record<string, unknown> = {
        name: newEntityName.trim(),
        content: parsed,
      };
      if (
        activeFamilyMeta.family === "source-channel-profiles" &&
        newEntityChannelId.trim()
      ) {
        payload.source_channel_id = newEntityChannelId.trim();
      }
      const created = await client.createConfigEntity(
        activeFamilyMeta.family,
        payload,
      );
      setNewEntityName("");
      const draftVer = created.versions[0];
      setStatusMsg(`Created ${created.name} with initial DRAFT v1.`);
      await loadFamily(activeFamilyMeta.family, created.id, draftVer?.id);
    } catch (err) {
      setErrorMsg(
        err instanceof Error ? err.message : "Invalid JSON or creation error.",
      );
    }
  }

  async function handleSaveDraft() {
    if (!activeFamilyMeta || !selectedEntity || !selectedVersion || !isDraft) {
      return;
    }
    setErrorMsg(null);
    setStatusMsg(null);
    try {
      const parsed = JSON.parse(editorJson) as Record<string, unknown>;
      await client.updateConfigVersion(
        activeFamilyMeta.family,
        selectedEntity.id,
        selectedVersion.id,
        parsed,
      );
      setStatusMsg(
        `Saved DRAFT v${selectedVersion.version_number} content.`,
      );
      await loadFamily(
        activeFamilyMeta.family,
        selectedEntity.id,
        selectedVersion.id,
      );
    } catch (err) {
      setErrorMsg(
        err instanceof Error ? err.message : "Failed to save draft version.",
      );
    }
  }

  async function handlePublishVersion() {
    if (!activeFamilyMeta || !selectedEntity || !selectedVersion || !isDraft) {
      return;
    }
    setErrorMsg(null);
    setStatusMsg(null);
    try {
      await client.publishConfigVersion(
        activeFamilyMeta.family,
        selectedEntity.id,
        selectedVersion.id,
        true,
      );
      setStatusMsg(
        `Published v${selectedVersion.version_number}; version is now immutable.`,
      );
      await loadFamily(
        activeFamilyMeta.family,
        selectedEntity.id,
        selectedVersion.id,
      );
    } catch (err) {
      setErrorMsg(
        err instanceof Error ? err.message : "Failed to publish version.",
      );
    }
  }

  async function handleCloneToDraft() {
    if (!activeFamilyMeta || !selectedEntity || !selectedVersion) return;
    setErrorMsg(null);
    setStatusMsg(null);
    try {
      const cloned = await client.createConfigVersion(
        activeFamilyMeta.family,
        selectedEntity.id,
        selectedVersion.content,
      );
      setStatusMsg(
        `Cloned immutable v${selectedVersion.version_number} into editable DRAFT v${cloned.version_number}.`,
      );
      await loadFamily(activeFamilyMeta.family, selectedEntity.id, cloned.id);
    } catch (err) {
      setErrorMsg(
        err instanceof Error ? err.message : "Failed to clone to draft.",
      );
    }
  }

  async function handleArchiveVersion() {
    if (!activeFamilyMeta || !selectedEntity || !selectedVersion) return;
    setErrorMsg(null);
    setStatusMsg(null);
    try {
      await client.archiveConfigVersion(
        activeFamilyMeta.family,
        selectedEntity.id,
        selectedVersion.id,
      );
      setStatusMsg(`Archived v${selectedVersion.version_number}.`);
      await loadFamily(
        activeFamilyMeta.family,
        selectedEntity.id,
        selectedVersion.id,
      );
    } catch (err) {
      setErrorMsg(
        err instanceof Error ? err.message : "Failed to archive version.",
      );
    }
  }

  async function handleRunTestBench(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setErrorMsg(null);
    setStatusMsg(null);
    try {
      const res = await client.runPromptTestBench({
        stage_template_version_id: benchStageVersionId.trim(),
        message_id: benchMessageId.trim() || undefined,
        sample_text: benchMessageId.trim() ? undefined : benchSampleText,
        invoke_provider: benchInvokeProvider,
      });
      setBenchResult(res);
      setStatusMsg(
        "Prompt Test Bench completed in side-effect-free run_mode='test'.",
      );
    } catch (err) {
      setErrorMsg(
        err instanceof Error ? err.message : "Prompt Test Bench failed.",
      );
    }
  }

  const versionManagerPanel = activeFamilyMeta ? (
    <div className="card-grid">
      <div className="panel">
        <h2>
          {activeFamilyMeta.label} — Version Lifecycle (DRAFT → PUBLISHED →
          ARCHIVED)
        </h2>
        <div className="form-grid">
          <div className="field">
            <label htmlFor="cfg-entity-select">Entity</label>
            <select
              id="cfg-entity-select"
              value={selectedEntityId}
              onChange={(e) => handleSelectEntity(e.target.value)}
            >
              {entities.length === 0 ? (
                <option value="">-- No entities created yet --</option>
              ) : null}
              {entities.map((ent) => (
                <option key={ent.id} value={ent.id}>
                  {ent.name}
                </option>
              ))}
            </select>
          </div>

          <div className="field">
            <label htmlFor="cfg-version-select">Version</label>
            <select
              id="cfg-version-select"
              value={selectedVersionId}
              onChange={(e) => handleSelectVersion(e.target.value)}
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
              Version UUID:{" "}
              <span className="mono">{selectedVersion.id}</span> | Lifecycle
              State:{" "}
              <span
                className={
                  isDraft ? "badge badge-warn" : "badge badge-ok"
                }
              >
                {isDraft
                  ? "DRAFT (Editable)"
                  : `IMMUTABLE (${selectedVersion.state.toUpperCase()})`}
              </span>
            </p>

            <div className="field">
              <label htmlFor="cfg-version-editor">
                Version Content JSON{" "}
                {!isDraft
                  ? "(Read-only — Clone to Draft to modify)"
                  : "*"}
              </label>
              <textarea
                id="cfg-version-editor"
                readOnly={!isDraft}
                value={editorJson}
                onChange={(e) => setEditorJson(e.target.value)}
                rows={10}
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
                onClick={() => void handlePublishVersion()}
              >
                Publish Version
              </button>
              <button
                type="button"
                className="btn"
                onClick={() => void handleCloneToDraft()}
              >
                Clone to New Draft
              </button>
              {selectedVersion.state === "published" ? (
                <button
                  type="button"
                  className="btn btn-danger"
                  onClick={() => void handleArchiveVersion()}
                >
                  Archive Version
                </button>
              ) : null}
            </div>
          </>
        ) : (
          <p className="stat-meta">
            Create a configuration entity to begin authoring versioned drafts.
          </p>
        )}
      </div>

      <div className="panel">
        <h2 id="create-cfg-heading">Create New {activeFamilyMeta.label}</h2>
        <form
          method="post"
          aria-labelledby="create-cfg-heading"
          onSubmit={handleCreateEntity}
        >
          <div className="field">
            <label htmlFor="new-cfg-name">Entity Name *</label>
            <input
              id="new-cfg-name"
              type="text"
              required
              autoCorrect="off"
              autoCapitalize="none"
              spellCheck={false}
              value={newEntityName}
              onChange={(e) => setNewEntityName(e.target.value)}
            />
          </div>

          {activeFamilyMeta.family === "source-channel-profiles" ? (
            <div className="field">
              <label htmlFor="new-cfg-channel">Source Channel UUID</label>
              <input
                id="new-cfg-channel"
                type="text"
                autoCorrect="off"
                autoCapitalize="none"
                spellCheck={false}
                value={newEntityChannelId}
                onChange={(e) => setNewEntityChannelId(e.target.value)}
              />
            </div>
          ) : null}

          <div className="field">
            <label htmlFor="new-cfg-json">Initial Draft v1 JSON *</label>
            <textarea
              id="new-cfg-json"
              required
              rows={8}
              value={newEntityJson}
              onChange={(e) => setNewEntityJson(e.target.value)}
            />
          </div>

          <button type="submit" className="btn btn-primary">
            Create Entity (DRAFT v1)
          </button>
        </form>
      </div>
    </div>
  ) : null;

  const promptTestBenchPanel = (
    <div className="panel">
      <h2>Prompt Test Bench (Side-Effect-Free `run_mode="test"`)</h2>
      <p className="hint">
        Renders prompt templates, builds the deterministic InputManifest +
        Structured Output JSON Schema, optionally invokes the external VLM/LLM
        endpoint, and previews parsed labels &amp; Negative Gate hits without
        creating formal ModelLabelAssignments or PublicationIntents.
      </p>

      <form method="post" onSubmit={handleRunTestBench}>
        <div className="form-grid">
          <div className="field">
            <label htmlFor="bench-stage-ver">
              Analysis Stage Version (Draft or Published) *
            </label>
            {stageEntities.length > 0 ? (
              <select
                id="bench-stage-ver"
                value={benchStageVersionId}
                onChange={(e) => setBenchStageVersionId(e.target.value)}
              >
                {stageEntities.flatMap((ent) =>
                  ent.versions.map((ver) => (
                    <option key={ver.id} value={ver.id}>
                      {ent.name} v{ver.version_number} [{ver.state}]
                    </option>
                  )),
                )}
              </select>
            ) : (
              <input
                id="bench-stage-ver"
                type="text"
                required
                autoCorrect="off"
                autoCapitalize="none"
                spellCheck={false}
                placeholder="Stage template version UUID"
                value={benchStageVersionId}
                onChange={(e) => setBenchStageVersionId(e.target.value)}
              />
            )}
          </div>

          <div className="field">
            <label htmlFor="bench-msg-id">
              Existing Message UUID (optional; overrides sample text)
            </label>
            <input
              id="bench-msg-id"
              type="text"
              autoCorrect="off"
              autoCapitalize="none"
              spellCheck={false}
              value={benchMessageId}
              onChange={(e) => setBenchMessageId(e.target.value)}
            />
          </div>
        </div>

        <div className="field">
          <label htmlFor="bench-sample-text">Sample Text Input</label>
          <textarea
            id="bench-sample-text"
            rows={3}
            value={benchSampleText}
            onChange={(e) => setBenchSampleText(e.target.value)}
          />
        </div>

        <div className="field-inline">
          <input
            id="bench-invoke-provider"
            type="checkbox"
            checked={benchInvokeProvider}
            onChange={(e) => setBenchInvokeProvider(e.target.checked)}
          />
          <label htmlFor="bench-invoke-provider">
            Invoke configured external InferenceProfile endpoint
          </label>
        </div>

        <button type="submit" className="btn btn-primary">
          Execute Prompt Test Bench
        </button>
      </form>

      {benchResult ? (
        <div style={{ marginBlockStart: "1.25rem" }}>
          <div className="alert-banner alert-info">
            Run Mode: <strong className="mono">{benchResult.run_mode}</strong> |
            Status: <strong className="mono">{benchResult.status}</strong> |
            Negative Gate Would Trigger:{" "}
            <strong className="mono">
              {benchResult.negative_gate_would_trigger ? "YES" : "NO"}
            </strong>{" "}
            | Latency:{" "}
            <strong className="mono">
              {benchResult.latency_ms ?? 0}ms
            </strong>
          </div>

          <h3>Rendered System &amp; User Prompts</h3>
          <pre className="code-block">
            {[
              `[SYSTEM]\n${benchResult.system_prompt ?? "(none)"}`,
              `[USER]\n${benchResult.user_prompt}`,
            ].join("\n\n")}
          </pre>

          <div className="card-grid">
            <div>
              <h3>
                InputManifest (`{benchResult.input_manifest_hash.slice(0, 16)}
                ...`)
              </h3>
              <pre className="code-block">
                {JSON.stringify(benchResult.input_manifest, null, 2)}
              </pre>
            </div>
            <div>
              <h3>
                Structured Schema (`
                {benchResult.structured_output_schema_hash.slice(0, 16)}...`)
              </h3>
              <pre className="code-block">
                {JSON.stringify(benchResult.structured_output_schema, null, 2)}
              </pre>
            </div>
          </div>

          <h3>Parsed Target Label Results</h3>
          <pre className="code-block">
            {JSON.stringify(benchResult.parsed_results, null, 2)}
          </pre>
        </div>
      ) : null}
    </div>
  );

  return (
    <section aria-labelledby="analysis-cfg-heading">
      <div className="workspace-header">
        <div>
          <h1 id="analysis-cfg-heading">
            Analysis Configuration &amp; Prompt Test Bench
          </h1>
          <p className="workspace-subtitle">
            Author immutable versioned Label Definitions, Label Sets, Prompt
            Templates, external Inference Profiles, Analysis Stages, Conditional
            DAG Pipelines, Media Profiles, Channel Profiles, and validate stages
            in the side-effect-free Prompt Test Bench.
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
        ariaLabel="Analysis Configuration Sections"
        activeKey={activeTab}
        onSelect={setActiveTab}
        tabs={[
          ...ANALYSIS_FAMILIES.map((item) => ({
            key: item.family,
            label: item.label,
            content: versionManagerPanel,
          })),
          {
            key: "prompt-test-bench",
            label: "Prompt Test Bench",
            content: promptTestBenchPanel,
          },
        ]}
      />
    </section>
  );
}
