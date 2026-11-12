import { type FormEvent, useCallback, useEffect, useState } from "react";
import type { AdminApiClient } from "../api/client";
import type {
  SecretStatusRecord,
  TelegramCapabilities,
  TelegramIdentity,
} from "../api/types";
import { ResponsiveTable } from "../components/ResponsiveTable";

export interface TelegramIdentitiesWorkspaceProps {
  client: AdminApiClient;
}

export function TelegramIdentitiesWorkspace({
  client,
}: TelegramIdentitiesWorkspaceProps) {
  const [secrets, setSecrets] = useState<SecretStatusRecord[]>([]);
  const [identities, setIdentities] = useState<TelegramIdentity[]>([]);
  const [secretType, setSecretType] = useState("telethon_session");
  const [secretPlaintext, setSecretPlaintext] = useState("");
  const [rotateTargetId, setRotateTargetId] = useState<string | null>(null);
  const [rotatePlaintext, setRotatePlaintext] = useState("");

  const [identityName, setIdentityName] = useState("");
  const [identityType, setIdentityType] = useState<"mtproto_user" | "bot">(
    "mtproto_user",
  );
  const [identitySecretId, setIdentitySecretId] = useState("");

  const [evalIdentityId, setEvalIdentityId] = useState("");
  const [evalProtected, setEvalProtected] = useState(false);
  const [evalSourceAccess, setEvalSourceAccess] = useState(true);
  const [evalDestPost, setEvalDestPost] = useState(true);
  const [evaluatedCaps, setEvaluatedCaps] =
    useState<TelegramCapabilities | null>(null);

  const [statusMsg, setStatusMsg] = useState<string | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const loadAll = useCallback(async () => {
    setErrorMsg(null);
    try {
      const [secList, idList] = await Promise.all([
        client.listSecrets(),
        client.listIdentities(),
      ]);
      setSecrets(secList);
      setIdentities(idList);
      if (!identitySecretId && secList.length > 0) {
        setIdentitySecretId(secList[0].secret_id);
      }
      if (!evalIdentityId && idList.length > 0) {
        setEvalIdentityId(idList[0].id);
      }
    } catch (err) {
      setErrorMsg(
        err instanceof Error ? err.message : "Failed to load identities & secrets.",
      );
    }
  }, [client, identitySecretId, evalIdentityId]);

  useEffect(() => {
    void loadAll();
  }, [loadAll]);

  async function handleStoreSecret(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setErrorMsg(null);
    setStatusMsg(null);
    try {
      const created = await client.createSecret(secretType, secretPlaintext);
      // Immediately scrub plaintext from component state (write-only invariant)
      setSecretPlaintext("");
      setStatusMsg(
        `Encrypted secret ${created.secret_id.slice(0, 8)} stored (AES-256-GCM). Plaintext cleared.`,
      );
      await loadAll();
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : "Failed to store secret.");
    }
  }

  async function handleRotateSecret(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!rotateTargetId) return;
    setErrorMsg(null);
    try {
      await client.rotateSecret(rotateTargetId, rotatePlaintext);
      setRotatePlaintext("");
      setRotateTargetId(null);
      setStatusMsg("Secret rotated in-place; plaintext scrubbed.");
      await loadAll();
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : "Failed to rotate secret.");
    }
  }

  async function handleCreateIdentity(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setErrorMsg(null);
    try {
      await client.createIdentity({
        name: identityName.trim(),
        identity_type: identityType,
        secret_id: identitySecretId,
      });
      setIdentityName("");
      setStatusMsg("Telegram identity registered.");
      await loadAll();
    } catch (err) {
      setErrorMsg(
        err instanceof Error ? err.message : "Failed to register identity.",
      );
    }
  }

  async function handleEvaluateCaps(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!evalIdentityId) return;
    setErrorMsg(null);
    try {
      const result = await client.evaluateIdentityCapabilities(evalIdentityId, {
        source_protected_content: evalProtected,
        has_source_access: evalSourceAccess,
        has_destination_post_permission: evalDestPost,
      });
      setEvaluatedCaps(result.capabilities);
    } catch (err) {
      setErrorMsg(
        err instanceof Error ? err.message : "Failed to evaluate capabilities.",
      );
    }
  }

  return (
    <section aria-labelledby="identities-heading">
      <div className="workspace-header">
        <div>
          <h1 id="identities-heading">Telegram Identities & Write-Only Secrets</h1>
          <p className="workspace-subtitle">
            Manage MTProto User and Bot identities, store or rotate AES-256-GCM
            secrets (write-only; plaintext can never be retrieved), and inspect
            protected-content capability matrices.
          </p>
        </div>
        <button type="button" className="btn" onClick={() => void loadAll()}>
          Refresh
        </button>
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

      <div className="card-grid">
        <div className="panel">
          <h2 id="store-secret-title">Store Write-Only EncryptedSecret</h2>
          <form
            method="post"
            aria-labelledby="store-secret-title"
            onSubmit={handleStoreSecret}
          >
            <div className="field">
              <label htmlFor="secret-type">Credential Type *</label>
              <select
                id="secret-type"
                name="secret_type"
                value={secretType}
                onChange={(e) => setSecretType(e.target.value)}
              >
                <option value="telethon_session">telethon_session</option>
                <option value="bot_token">bot_token</option>
                <option value="telegram_api_hash">telegram_api_hash</option>
                <option value="inference_api_key">inference_api_key</option>
              </select>
            </div>

            <div className="field">
              <label htmlFor="secret-plaintext">
                Secret Value (write-only; never echoed back) *
              </label>
              <input
                id="secret-plaintext"
                name="plaintext"
                type="password"
                autoComplete="off"
                required
                value={secretPlaintext}
                onChange={(e) => setSecretPlaintext(e.target.value)}
              />
            </div>

            <button type="submit" className="btn btn-primary">
              Encrypt & Store Secret
            </button>
          </form>
        </div>

        <div className="panel">
          <h2 id="register-identity-title">Register TelegramIdentity</h2>
          <form
            method="post"
            aria-labelledby="register-identity-title"
            onSubmit={handleCreateIdentity}
          >
            <div className="field">
              <label htmlFor="identity-name">Identity Name *</label>
              <input
                id="identity-name"
                name="name"
                type="text"
                required
                value={identityName}
                onChange={(e) => setIdentityName(e.target.value)}
              />
            </div>

            <fieldset>
              <legend>Identity Kind</legend>
              <div className="field-inline">
                <input
                  id="id-kind-mtproto"
                  name="identity_type"
                  type="radio"
                  value="mtproto_user"
                  checked={identityType === "mtproto_user"}
                  onChange={() => setIdentityType("mtproto_user")}
                />
                <label htmlFor="id-kind-mtproto">
                  mtproto_user (Channel History + Read + Publish)
                </label>
              </div>
              <div className="field-inline" style={{ marginBlockEnd: 0 }}>
                <input
                  id="id-kind-bot"
                  name="identity_type"
                  type="radio"
                  value="bot"
                  checked={identityType === "bot"}
                  onChange={() => setIdentityType("bot")}
                />
                <label htmlFor="id-kind-bot">bot (Bot API Publisher)</label>
              </div>
            </fieldset>

            <div className="field">
              <label htmlFor="identity-secret-ref">Secret Reference *</label>
              <select
                id="identity-secret-ref"
                name="secret_id"
                required
                value={identitySecretId}
                onChange={(e) => setIdentitySecretId(e.target.value)}
              >
                <option value="">-- Choose configured secret --</option>
                {secrets.map((sec) => (
                  <option key={sec.secret_id} value={sec.secret_id}>
                    {sec.secret_type} ({sec.secret_id.slice(0, 8)} · Configured)
                  </option>
                ))}
              </select>
            </div>

            <button type="submit" className="btn btn-primary">
              Register Identity
            </button>
          </form>
        </div>
      </div>

      <div className="panel">
        <h2>Configured Secrets Metadata ({secrets.length})</h2>
        <p className="hint">
          Security Invariant: Plaintext values are never returned by the API or
          displayed in the UI. Secrets can only be replaced via rotation.
        </p>
        <ResponsiveTable
          caption="Encrypted secrets metadata and write-only replacement controls"
          headers={["Secret ID", "Type", "Status", "Key ID", "Replace / Rotate"]}
        >
          {secrets.map((sec) => (
            <tr key={sec.secret_id}>
              <th scope="row">{sec.secret_id.slice(0, 8)}</th>
              <td data-label="Type" className="mono">
                {sec.secret_type}
              </td>
              <td data-label="Status">
                <span className="badge badge-ok">
                  Configured
                  {sec.rotation_version ? ` (v${sec.rotation_version})` : ""}
                </span>
              </td>
              <td data-label="Key ID" className="mono">
                {sec.key_id}
              </td>
              <td data-label="Replace / Rotate">
                <button
                  type="button"
                  className="btn"
                  onClick={() => {
                    setRotateTargetId(sec.secret_id);
                    setRotatePlaintext("");
                  }}
                >
                  Replace Secret
                </button>
              </td>
            </tr>
          ))}
        </ResponsiveTable>

        {rotateTargetId ? (
          <form
            method="post"
            style={{ marginBlockStart: "1rem" }}
            onSubmit={handleRotateSecret}
          >
            <div className="field">
              <label htmlFor="rotate-secret-input">
                New Replacement Value for Secret{" "}
                <span className="mono">{rotateTargetId.slice(0, 8)}</span> *
              </label>
              <input
                id="rotate-secret-input"
                name="plaintext"
                type="password"
                autoComplete="off"
                required
                value={rotatePlaintext}
                onChange={(e) => setRotatePlaintext(e.target.value)}
              />
            </div>
            <div className="btn-row">
              <button type="submit" className="btn btn-primary">
                Confirm Secret Rotation
              </button>
              <button
                type="button"
                className="btn"
                onClick={() => setRotateTargetId(null)}
              >
                Cancel
              </button>
            </div>
          </form>
        ) : null}
      </div>

      <div className="panel">
        <h2>Telegram Identities & Capability Matrix ({identities.length})</h2>
        <ResponsiveTable
          caption="Telegram identities, health status, and baseline capabilities"
          headers={[
            "Name",
            "Kind",
            "Health",
            "Read Source",
            "Native Forward",
            "Copy Publish",
          ]}
        >
          {identities.map((idItem) => (
            <tr key={idItem.id}>
              <th scope="row">{idItem.name}</th>
              <td data-label="Kind" className="mono">
                {idItem.identity_type}
              </td>
              <td data-label="Health">
                <span className="badge">{idItem.health_status}</span>
              </td>
              <td data-label="Read Source">
                {idItem.capabilities.can_read_source ? "Allowed" : "No"}
              </td>
              <td data-label="Native Forward">
                {idItem.capabilities.can_native_forward ? "Allowed" : "Blocked"}
              </td>
              <td data-label="Copy Publish">
                {idItem.capabilities.can_copy_publish ? "Allowed" : "Blocked"}
              </td>
            </tr>
          ))}
        </ResponsiveTable>

        <form
          method="post"
          style={{ marginBlockStart: "1.25rem" }}
          onSubmit={handleEvaluateCaps}
        >
          <h3>Capability Simulator (Protected-Content Respect)</h3>
          <div className="form-grid">
            <div className="field">
              <label htmlFor="eval-identity-select">Identity</label>
              <select
                id="eval-identity-select"
                name="identity_id"
                value={evalIdentityId}
                onChange={(e) => setEvalIdentityId(e.target.value)}
              >
                {identities.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.name} ({item.identity_type})
                  </option>
                ))}
              </select>
            </div>
          </div>
          <div className="field-inline">
            <input
              id="eval-protected"
              name="source_protected_content"
              type="checkbox"
              checked={evalProtected}
              onChange={(e) => setEvalProtected(e.target.checked)}
            />
            <label htmlFor="eval-protected">
              Source has Telegram Protected Content (`noforwards`)
            </label>
          </div>
          <div className="field-inline">
            <input
              id="eval-source-access"
              name="has_source_access"
              type="checkbox"
              checked={evalSourceAccess}
              onChange={(e) => setEvalSourceAccess(e.target.checked)}
            />
            <label htmlFor="eval-source-access">Identity has source access</label>
          </div>
          <div className="field-inline">
            <input
              id="eval-dest-post"
              name="has_destination_post_permission"
              type="checkbox"
              checked={evalDestPost}
              onChange={(e) => setEvalDestPost(e.target.checked)}
            />
            <label htmlFor="eval-dest-post">
              Identity has destination post permission
            </label>
          </div>
          <button type="submit" className="btn">
            Evaluate Capabilities
          </button>
        </form>

        {evaluatedCaps ? (
          <pre className="code-block">
            {JSON.stringify(evaluatedCaps, null, 2)}
          </pre>
        ) : null}
      </div>
    </section>
  );
}
