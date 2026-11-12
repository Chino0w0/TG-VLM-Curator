import { useCallback, useRef, useState } from "react";
export function useMutationGuard() {
  const locked = useRef(false);
  const [busy, setBusy] = useState(false);
  const runAction = useCallback(async (action: () => Promise<void>) => {
    if (locked.current) return;
    locked.current = true; setBusy(true);
    try { await action(); } finally { locked.current = false; setBusy(false); }
  }, []);
  return { busy, runAction };
}
