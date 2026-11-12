import type { ConfigEntity, ConfigVersion } from "./types";

export function publishedVersion(entity?: ConfigEntity | null): ConfigVersion | undefined {
  return entity?.versions.find(v => v.id === entity.published_version_id && v.state === "published");
}

export function isDirtyJson(editor: string, saved: Record<string, unknown>): boolean {
  try { return JSON.stringify(JSON.parse(editor)) !== JSON.stringify(saved); }
  catch { return true; }
}
