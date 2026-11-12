import { useId, useState } from "react";

export interface EditField {
  key: string;
  label: string;
  value: string | boolean | null;
  options?: { value: string; label: string }[];
  required?: boolean;
}

export function RecordEditor({
  label,
  fields,
  onSave,
}: {
  label: string;
  fields: EditField[];
  onSave: (changes: Record<string, unknown>) => Promise<void>;
}) {
  const id = useId();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<Record<string, string | boolean | null>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  if (!editing) {
    return (
      <button
        type="button"
        className="btn"
        onClick={() => {
          setDraft(Object.fromEntries(fields.map((f) => [f.key, f.value])));
          setError("");
          setEditing(true);
        }}
      >
        Edit {label}
      </button>
    );
  }

  return (
    <form
      aria-label={`Edit ${label}`}
      onSubmit={async (e) => {
        e.preventDefault();
        if (busy) return;
        setBusy(true);
        setError("");
        try {
          const normalized: Record<string, unknown> = {};
          for (const field of fields) {
            const raw = draft[field.key];
            if (typeof field.value === "boolean") {
              normalized[field.key] = Boolean(raw);
            } else if (field.options) {
              const selected = String(raw ?? "");
              normalized[field.key] = selected || null;
            } else {
              const trimmed = String(raw ?? "").trim();
              if (field.required && !trimmed) {
                throw new Error(`${field.label} is required.`);
              }
              normalized[field.key] = trimmed || (field.required ? "" : null);
            }
          }
          await onSave(normalized);
          setEditing(false);
        } catch (err) {
          setError(err instanceof Error ? err.message : "Update failed");
        } finally {
          setBusy(false);
        }
      }}
    >
      <fieldset disabled={busy} style={{ border: 0, padding: 0 }}>
        {fields.map((field) => (
          <div className="field" key={field.key}>
            <label htmlFor={`${id}-${field.key}`}>{field.label}</label>
            {typeof field.value === "boolean" ? (
              <input
                id={`${id}-${field.key}`}
                type="checkbox"
                checked={Boolean(draft[field.key])}
                onChange={(e) =>
                  setDraft({ ...draft, [field.key]: e.target.checked })
                }
              />
            ) : field.options ? (
              <select
                id={`${id}-${field.key}`}
                value={String(draft[field.key] ?? "")}
                required={field.required}
                onChange={(e) =>
                  setDraft({ ...draft, [field.key]: e.target.value || null })
                }
              >
                <option value="">-- Unassigned --</option>
                {field.options.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
            ) : (
              <input
                id={`${id}-${field.key}`}
                required={field.required}
                value={String(draft[field.key] ?? "")}
                onChange={(e) =>
                  setDraft({ ...draft, [field.key]: e.target.value })
                }
              />
            )}
          </div>
        ))}
        {error ? <p role="alert">{error}</p> : null}
        <div className="btn-row">
          <button className="btn btn-primary" type="submit">
            Save {label}
          </button>
          <button
            className="btn"
            type="button"
            onClick={() => setEditing(false)}
          >
            Cancel Edit
          </button>
        </div>
      </fieldset>
    </form>
  );
}
