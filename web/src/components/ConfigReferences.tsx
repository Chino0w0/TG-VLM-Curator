import { useEffect, useId, useState } from "react";
import type { AdminApiClient } from "../api/client";
import type { ConfigFamily } from "../api/types";

type Reference = { field: string; family?: ConfigFamily; kind?: "secret" | "destination"; path?: string; optional?: boolean };
const REFERENCES: Partial<Record<ConfigFamily, Reference[]>> = {
  "label-sets": [{ field: "label_definition_version_id", family: "label-definitions", path: "bindings" }],
  "inference-profiles": [{ field: "api_secret_reference", kind: "secret" }],
  "analysis-stages": [
    { field: "prompt_template_version_id", family: "prompt-templates" },
    { field: "inference_profile_version_id", family: "inference-profiles" },
    { field: "label_set_version_id", family: "label-sets" },
  ],
  "analysis-pipelines": [{ field: "analysis_stage_template_version_id", family: "analysis-stages", path: "nodes" }],
  "source-channel-profiles": [
    { field: "analysis_pipeline_version_id", family: "analysis-pipelines", optional: true },
    { field: "routing_policy_version_id", family: "routing-policies", optional: true },
    { field: "media_processing_profile_version_id", family: "media-processing-profiles", optional: true },
  ],
  "routing-policies": [{ field: "destination_channel_id", kind: "destination", path: "action" }, { field: "rendering_template_version_id", family: "rendering-templates", path: "action", optional: true }],
};
type Option = { id: string; label: string };

/** Writes canonical API fields, never guessed UUIDs. Advanced bindings/rules remain editable JSON. */
export function ConfigReferences({ client, family, json, onChange }: {
  client: AdminApiClient; family: ConfigFamily; json: string; onChange: (json: string) => void;
}) {
  const id = useId();
  const [options, setOptions] = useState<Record<string, Option[]>>({});
  const [error, setError] = useState("");
  const refs = REFERENCES[family];
  useEffect(() => {
    let cancelled = false;
    setOptions({}); setError("");
    void Promise.all((refs ?? []).map(async ref => {
      let items: Option[];
      if (ref.kind === "secret") items = (await client.listSecrets()).filter(s => s.secret_type === "api_key" || s.secret_type === "inference_api_key").map(s => ({ id: s.secret_id, label: `${s.secret_type} / ${s.secret_id}` }));
      else if (ref.kind === "destination") items = (await client.listDestinationChannels()).map(d => ({ id: d.id, label: d.name }));
      else items = (await client.listConfigEntities(ref.family!)).flatMap(e => e.versions.filter(v => v.state === "published").map(v => ({ id: v.id, label: `${e.name} v${v.version_number}` })));
      return [ref.field, items] as const;
    })).then(entries => { if (!cancelled) setOptions(Object.fromEntries(entries)); }).catch(e => { if (!cancelled) setError(e instanceof Error ? e.message : "Reference lookup failed"); });
    return () => { cancelled = true; };
  }, [client, refs]);
  if (!refs?.length) return null;
  let content: Record<string, unknown>;
  try { content = JSON.parse(json); if (!content || Array.isArray(content) || typeof content !== "object") throw new Error(); }
  catch { return <p className="hint">Fix JSON syntax before choosing references.</p>; }
  function target(data: Record<string, unknown>, ref: Reference): Record<string, unknown> | undefined {
    if (ref.path === "action") return ((data.rules as Array<{actions: Record<string, unknown>[]}> | undefined)?.[0]?.actions)?.[0];
    if (ref.path) return (data[ref.path] as Record<string, unknown>[] | undefined)?.[0];
    return data;
  }
  return <details open className="config-references"><summary>Published references (first binding / node / action)</summary>
    <p className="hint">Create and publish prerequisites first. Choose actual references below; edit JSON for additional bindings or rules. No secret values are exposed.</p>
    {error ? <p role="alert">{error}</p> : null}
    {refs.map(ref => {
      const value = String(target(content, ref)?.[ref.field] ?? "");
      const items = options[ref.field] ?? [];
      return <div className="field" key={ref.field}><label htmlFor={`${id}-${ref.field}`}>{ref.field}</label>
        <select id={`${id}-${ref.field}`} value={value} onChange={e => {
          const next = JSON.parse(json) as Record<string, unknown>;
          const dest = target(next, ref);
          if (!dest) { setError("Add the first binding, node or action in JSON before selecting a reference."); return; }
          dest[ref.field] = e.target.value || (ref.optional ? null : "");
          onChange(JSON.stringify(next, null, 2));
        }}>
          <option value="">{ref.optional ? "-- None --" : "-- Select published prerequisite --"}</option>
          {value && !items.some(x => x.id === value) ? <option value={value}>{value} (not in published options)</option> : null}
          {items.map(x => <option key={x.id} value={x.id}>{x.label}</option>)}
        </select>
      </div>;
    })}
  </details>;
}
