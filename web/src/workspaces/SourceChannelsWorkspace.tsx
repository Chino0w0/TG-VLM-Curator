import { RecordEditor } from "../components/RecordEditor";
import { publishedVersion } from "../api/config";
import { parseTelegramPeerId } from "../api/validation";
import { type FormEvent, useCallback, useEffect, useState } from "react";
import type { AdminApiClient } from "../api/client";
import type {
  ConfigEntity,
  ProcessingRange,
  SourceChannel,
  TelegramIdentity,
} from "../api/types";
import { ResponsiveTable } from "../components/ResponsiveTable";
import { useMutationGuard } from "../hooks/useMutationGuard";
import { useRequestGeneration } from "../hooks/useRequestGeneration";

export interface SourceChannelsWorkspaceProps {
  client: AdminApiClient;
}

export function SourceChannelsWorkspace({ client }: SourceChannelsWorkspaceProps) {
  const reads = useRequestGeneration();
  const { busy, runAction } = useMutationGuard();
  const [loading, setLoading] = useState(true);
  const [channels, setChannels] = useState<SourceChannel[]>([]);
  const [identities, setIdentities] = useState<TelegramIdentity[]>([]);
  const [profiles, setProfiles] = useState<ConfigEntity[]>([]);
  const [ranges, setRanges] = useState<ProcessingRange[]>([]);
  const [peerId, setPeerId] = useState("-100");
  const [displayName, setDisplayName] = useState("");
  const [username, setUsername] = useState("");
  const [readIdentityId, setReadIdentityId] = useState("");
  const [statusMsg, setStatusMsg] = useState<string | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const loadAll = useCallback(async () => {
    const request = reads.next();
    setLoading(true);
    setErrorMsg(null);
    try {
      const [srcList, idList, profList, rangeList] = await Promise.all([
        client.listSourceChannels(),
        client.listIdentities(),
        client.listConfigEntities("source-channel-profiles"),
        client.listProcessingRanges(),
      ]);
      if (!reads.isCurrent(request)) return;
      setChannels(srcList);
      setIdentities(idList);
      setProfiles(profList);
      setRanges(rangeList);
      const defaultReaderId =
        idList.find((item) => item.identity_type === "mtproto_user")?.id ?? "";
      if (defaultReaderId) {
        setReadIdentityId((prev) => prev || defaultReaderId);
      }
    } catch (err) {
      if (!reads.isCurrent(request)) return;
      setErrorMsg(err instanceof Error ? err.message : "Failed to load source channels.");
    } finally {
      if (reads.isCurrent(request)) {
        setLoading(false);
      }
    }
  }, [client, reads]);

  useEffect(() => {
    void loadAll();
  }, [loadAll]);

  async function handleCreate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setErrorMsg(null);
    setStatusMsg(null);
    const parsedPeerId = parseTelegramPeerId(peerId);
    if (parsedPeerId === null) {
      setErrorMsg("Telegram Peer ID must be a non-zero integer.");
      return;
    }
    await runAction(async () => {
      try {
        await client.createSourceChannel({
          telegram_peer_id: parsedPeerId,
          display_name: displayName.trim(),
          username: username.trim() || null,
          default_read_identity_id: readIdentityId || null,
        });
        setDisplayName("");
        setUsername("");
        setStatusMsg("Source channel created.");
        await loadAll();
      } catch (err) {
        setErrorMsg(err instanceof Error ? err.message : "Failed to create source channel.");
      }
    });
  }

  async function handleAssignProfile(channelId: string, profileVersionId: string) {
    setErrorMsg(null);
    await runAction(async () => {
      try {
        await client.updateSourceChannel(channelId, {
          active_profile_version_id: profileVersionId || null,
        });
        setStatusMsg("Updated active SourceChannelProfile version.");
        await loadAll();
      } catch (err) {
        setErrorMsg(err instanceof Error ? err.message : "Failed to assign profile.");
      }
    });
  }

  async function handleToggleEnabled(channel: SourceChannel) {
    setErrorMsg(null);
    await runAction(async () => {
      try {
        await client.updateSourceChannel(channel.id, {
          enabled: !channel.enabled,
        });
        await loadAll();
      } catch (err) {
        setErrorMsg(err instanceof Error ? err.message : "Failed to update channel.");
      }
    });
  }

  const publishedVersions = profiles.flatMap(entity => {
    const version = publishedVersion(entity);
    return version ? [{ entityName: entity.name, sourceChannelId: entity.source_channel_id, version }] : [];
  });

  return (
    <section aria-labelledby="sources-heading">
      <div className="workspace-header">
        <div>
          <h1 id="sources-heading">Source Channels</h1>
          <p className="workspace-subtitle">
            Register Telegram sources, bind MTProto read identities, switch
            published SourceChannelProfile versions (Analysis Pipeline, Routing
            Policy, Media Processing Profile, Duplicate Policy), and inspect
            range counts.
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

      <div className="panel">
        <h2 id="create-source-title">Register Source Channel</h2>
        <form
          method="post"
          aria-labelledby="create-source-title"
          onSubmit={handleCreate}
        >
          <div className="form-grid">
            <div className="field">
              <label htmlFor="source-peer-id">Telegram Peer ID *</label>
              <input
                id="source-peer-id"
                name="telegram_peer_id"
                type="text"
                inputMode="numeric"
                required
                value={peerId}
                onChange={(e) => setPeerId(e.target.value)}
              />
            </div>

            <div className="field">
              <label htmlFor="source-display-name">Display Name *</label>
              <input
                id="source-display-name"
                name="display_name"
                type="text"
                required
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
              />
            </div>

            <div className="field">
              <label htmlFor="source-username">Channel Username (optional)</label>
              <input
                id="source-username"
                name="username"
                type="text"
                autoCorrect="off"
                autoCapitalize="none"
                spellCheck={false}
                value={username}
                onChange={(e) => setUsername(e.target.value)}
              />
            </div>

            <div className="field">
              <label htmlFor="source-read-identity">Read Identity</label>
              <select
                id="source-read-identity"
                name="default_read_identity_id"
                required
                value={readIdentityId}
                onChange={(e) => setReadIdentityId(e.target.value)}
              >
                <option value="">-- Select MTProto reader --</option>
                {identities.filter(identity => identity.identity_type === "mtproto_user").map((idItem) => (
                  <option key={idItem.id} value={idItem.id}>
                    {idItem.name} ({idItem.identity_type})
                  </option>
                ))}
              </select>
            </div>
          </div>

          <p className="hint">Protected content is observed from Telegram, not configured here.</p>

          <button type="submit" className="btn btn-primary" disabled={busy}>
            Register Source Channel
          </button>
        </form>
      </div>

      <div className="panel">
        <h2>Configured Source Channels ({channels.length})</h2>
        <ResponsiveTable
          caption="Source channels, active profile versions, duplicate policies, and ranges"
          headers={[
            "Channel",
            "Peer ID",
            "Read Identity",
            "Active Profile & Duplicate Policy",
            "Ranges",
            "Actions",
          ]}
          loading={loading}
          emptyMessage="No source channels registered yet."
        >
          {channels.map((channel) => {
            const activeProf = publishedVersions.find(
              (item) => item.version.id === channel.active_profile_version_id,
            );
            const dupPolicy = String(
              activeProf?.version.content?.duplicate_policy ?? "unassigned",
            );
            const phashDist =
              activeProf?.version.content?.phash_distance_threshold;
            const channelRanges = ranges.filter(
              (r) => r.source_channel_id === channel.id,
            );
            const reader = identities.find(
              (i) => i.id === channel.default_read_identity_id,
            );
            const seenId =
              channel.latest_seen_message_id ?? channel.last_seen_message_id;

            return (
              <tr key={channel.id}>
                <th scope="row">
                  <div>
                    {channel.display_name}{" "}
                    <span
                      className={
                        channel.enabled ? "badge badge-ok" : "badge badge-warn"
                      }
                    >
                      {channel.enabled ? "Enabled" : "Disabled"}
                    </span>
                  </div>
                  {channel.username || seenId != null ? (
                    <div className="hint">
                      {channel.username ? `@${channel.username}` : ""}
                      {channel.username && seenId != null ? " · " : ""}
                      {seenId != null ? `Latest msg #${seenId}` : ""}
                    </div>
                  ) : null}
                </th>
                <td data-label="Peer ID" className="mono">
                  {channel.telegram_peer_id}
                </td>
                <td data-label="Read Identity">
                  {reader ? `${reader.name} (${reader.identity_type})` : "None"}
                </td>
                <td data-label="Active Profile & Duplicate Policy">
                  <label
                    htmlFor={`assign-prof-${channel.id}`}
                    className="visually-hidden"
                  >
                    Active Profile for {channel.display_name}
                  </label>
                  <select
                    id={`assign-prof-${channel.id}`}
                    name="active_profile_version_id"
                    disabled={busy}
                    value={channel.active_profile_version_id ?? ""}
                    onChange={(e) =>
                      void handleAssignProfile(channel.id, e.target.value)
                    }
                  >
                    <option value="">-- Unassigned --</option>
                    {publishedVersions.filter(pv => pv.sourceChannelId === channel.id).map((pv) => (
                      <option key={pv.version.id} value={pv.version.id}>
                        {pv.entityName} v{pv.version.version_number}
                      </option>
                    ))}
                  </select>
                  <div className="hint" style={{ marginBlockStart: "0.25rem" }}>
                    Duplicate Policy: <strong>{dupPolicy}</strong>
                    {phashDist !== undefined
                      ? ` (pHash <= ${String(phashDist)})`
                      : ""}
                  </div>
                </td>
                <td data-label="Ranges" className="mono">
                  {channelRanges.length} total
                </td>
                <td data-label="Actions">
                  <RecordEditor label="Source" fields={[
                    {key: "display_name", label: "Display Name", value: channel.display_name, required: true},
                    {key: "username", label: "Username", value: channel.username},
                    {key: "default_read_identity_id", label: "Read Identity", value: channel.default_read_identity_id, required: true, options: identities.filter(i => i.identity_type === "mtproto_user").map(i => ({value: i.id, label: `${i.name}${i.enabled ? "" : " (disabled)"}`}))},
                  ]} onSave={async changes => { await client.updateSourceChannel(channel.id, changes); await loadAll(); }} />
                  <button
                    type="button"
                    className="btn"
                    disabled={busy}
                    onClick={() => void handleToggleEnabled(channel)}
                  >
                    {channel.enabled ? "Disable" : "Enable"}
                  </button>
                </td>
              </tr>
            );
          })}
        </ResponsiveTable>
      </div>
    </section>
  );
}
