import { useEffect, useMemo, useRef } from "react";

/** Latest-only commits, including unmount and synchronous form invalidation. */
export function useRequestGeneration() {
  const generation = useRef(0);
  useEffect(() => () => { generation.current += 1; }, []);
  return useMemo(() => ({
    next: () => ++generation.current,
    isCurrent: (request: number) => request === generation.current,
    invalidate: () => { generation.current += 1; },
  }), []);
}
