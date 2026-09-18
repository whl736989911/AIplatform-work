/**
 * Loading / error state for one knowledge API call.
 *
 * ``useAsyncResource`` reports failures through a toast only; the knowledge
 * panels must keep the failure on screen (the WorkBuddy knowledge slice may not
 * be mounted in this deployment), so this hook surfaces the error together with
 * an explicit "route not available yet" classification.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { parseApiError } from "../../../utils/apiError";

/** HTTP status parsed from a thrown ``request()`` error, or null. */
export function apiErrorStatus(error: unknown): number | null {
  if (!(error instanceof Error)) return null;
  const match = /^Request failed:\s*(\d{3})\b/.exec(error.message);
  return match ? Number(match[1]) : null;
}

/**
 * True when the endpoint itself is absent rather than the call failing.
 *
 * A missing route answers ``404`` with FastAPI's plain ``{"detail": "Not
 * Found"}``; a real WorkBuddy ``NOT_FOUND`` carries an error envelope with a
 * code. ``501`` is always "not implemented yet".
 */
export function isSliceUnavailable(error: unknown): boolean {
  const status = apiErrorStatus(error);
  if (status === 501) return true;
  if (status !== 404) return false;
  return parseApiError(error)?.code === undefined;
}

export interface KnowledgeResource<T> {
  data: T;
  loading: boolean;
  /** Last failure, or null. */
  error: unknown | null;
  /** The backend slice is not merged into this deployment (404 / 501). */
  unavailable: boolean;
  refresh: () => Promise<void>;
  setData: React.Dispatch<React.SetStateAction<T>>;
}

export interface KnowledgeResourceOptions {
  /** When false, skips the fetch and resets to the initial value. */
  enabled?: boolean;
}

export function useKnowledgeResource<T>(
  initialValue: T,
  fetcher: () => Promise<T>,
  deps: React.DependencyList,
  options?: KnowledgeResourceOptions,
): KnowledgeResource<T> {
  const [data, setData] = useState<T>(initialValue);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<unknown | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const initialRef = useRef(initialValue);
  initialRef.current = initialValue;
  const enabled = options?.enabled !== false;

  const refresh = useCallback(async () => {
    if (!enabled) {
      setData(initialRef.current);
      setError(null);
      setUnavailable(false);
      return;
    }
    setLoading(true);
    try {
      setData(await fetcher());
      setError(null);
      setUnavailable(false);
    } catch (err) {
      console.error("[workbuddy/knowledge]", err);
      setError(err);
      setUnavailable(isSliceUnavailable(err));
    } finally {
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, ...deps]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return { data, loading, error, unavailable, refresh, setData };
}
