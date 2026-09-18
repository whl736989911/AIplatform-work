/**
 * Shared console plumbing for the two WorkBuddy operator pages
 * (``Workflows`` and ``Approvals``).
 *
 * It lives beside the larger page because the console has exactly two pages and
 * both must render *identical* load / empty / error / not-available states and
 * the same status-vocabulary lookup — a second copy of either would drift.
 *
 * Nothing here fabricates data: on an unmounted (404) or unimplemented (501)
 * backend the caller gets an explicit informational state, and every state
 * component is a pure function of what the hook actually observed.
 */

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type DependencyList,
  type Dispatch,
  type ReactNode,
  type SetStateAction,
} from "react";
import { Alert, Button, Select, Spin, Typography } from "antd";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";
import { RefreshCw } from "lucide-react";
import { EmptyState } from "../../../components/EmptyState";
import { apiErrorMessage } from "../../../utils/apiError";
import { isWorkBuddyUnavailableError } from "../../../api/modules/workbuddyWorkflows";

const { Text } = Typography;

export interface WorkBuddyResource<T> {
  data: T;
  loading: boolean;
  error: unknown;
  reload: () => Promise<void>;
  setData: Dispatch<SetStateAction<T>>;
}

/**
 * Load-state machine for one console resource: ``loading`` starts true so a
 * panel never flashes its empty state before the first response, and a failed
 * load keeps the previous data with the error alongside instead of pretending
 * the resource is empty.
 */
export function useWorkBuddyResource<T>(
  initialValue: T,
  fetcher: () => Promise<T>,
  deps: DependencyList,
): WorkBuddyResource<T> {
  const [data, setData] = useState<T>(initialValue);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const reload = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await fetcherRef.current());
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  useEffect(() => {
    void reload();
  }, [reload]);

  return { data, loading, error, reload, setData };
}

/**
 * Label for a backend status / kind code, falling back to the raw code so a
 * newly added server state stays visible instead of rendering an i18n key.
 */
export function statusLabel(
  t: TFunction,
  keyPrefix: string,
  value: string,
): string {
  const key = `${keyPrefix}.${value}`;
  const label = t(key);
  return key === label ? value : label;
}

/** Explicit loading state with a translated label (never a blank panel). */
export function LoadingBlock({ label }: { label: string }) {
  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        gap: 12,
        padding: "40px 0",
      }}
    >
      <Spin size="large" />
      <Text type="secondary" style={{ fontSize: 12 }}>
        {label}
      </Text>
    </div>
  );
}

/**
 * The backend slice for this console page did not answer for this caller
 * (unmounted router → 404, unimplemented → 501, missing control plane → 503).
 * Informational, retryable, and never a substitute for real data: the server's
 * own message is shown underneath so an operator can tell a missing module from
 * a route that exists but is out of this caller's reach.
 */
export function UnavailableNotice({
  error,
  title,
  hint,
  errorFallback,
  onRetry,
}: {
  error: unknown;
  title: string;
  hint: string;
  errorFallback: string;
  onRetry: () => void;
}) {
  const { t } = useTranslation();
  return (
    <Alert
      type="info"
      showIcon
      message={title}
      description={
        <>
          {hint}
          <div style={{ marginTop: 4, fontSize: 12, opacity: 0.75 }}>
            {apiErrorMessage(error, errorFallback, t)}
          </div>
        </>
      }
      action={
        <Button size="small" icon={<RefreshCw size={14} />} onClick={onRetry}>
          {t("common.refresh")}
        </Button>
      }
    />
  );
}

/** A real failure (validation, conflict, permission, …) with the server text. */
export function LoadError({
  error,
  title,
  fallback,
  onRetry,
}: {
  error: unknown;
  title: string;
  fallback: string;
  onRetry: () => void;
}) {
  const { t } = useTranslation();
  return (
    <EmptyState
      variant="error"
      title={title}
      description={apiErrorMessage(error, fallback, t)}
      actionLabel={t("common.refresh")}
      onAction={onRetry}
    />
  );
}

/**
 * Renders the one state a resource is in, or nothing when the panel should
 * render its own body. Keeping the four states in a single place is what makes
 * "not available yet" impossible to mistake for an empty result.
 */
export function ResourceState<T>({
  resource,
  isEmpty,
  loadingLabel,
  errorTitle,
  errorFallback,
  unavailableTitle,
  unavailableHint,
  emptyTitle,
  emptyHint,
  emptyActionLabel,
  onEmptyAction,
}: {
  resource: WorkBuddyResource<T>;
  isEmpty: boolean;
  loadingLabel: string;
  errorTitle: string;
  errorFallback: string;
  unavailableTitle: string;
  unavailableHint: string;
  emptyTitle: string;
  emptyHint: string;
  emptyActionLabel?: string;
  onEmptyAction?: () => void;
}): ReactNode {
  const { loading, error, reload } = resource;
  if (error !== null && error !== undefined) {
    return isWorkBuddyUnavailableError(error) ? (
      <UnavailableNotice
        error={error}
        title={unavailableTitle}
        hint={unavailableHint}
        errorFallback={errorFallback}
        onRetry={() => void reload()}
      />
    ) : (
      <LoadError
        error={error}
        title={errorTitle}
        fallback={errorFallback}
        onRetry={() => void reload()}
      />
    );
  }
  if (loading) return <LoadingBlock label={loadingLabel} />;
  if (isEmpty) {
    return (
      <EmptyState
        title={emptyTitle}
        description={emptyHint}
        actionLabel={emptyActionLabel}
        onAction={onEmptyAction}
      />
    );
  }
  return null;
}

/** Compact scrollable preview for a JSON payload the server returned. */
export function JsonPreview({
  value,
  emptyLabel,
}: {
  value: unknown;
  emptyLabel: string;
}) {
  if (value === null || value === undefined) {
    return <Text type="secondary">{emptyLabel}</Text>;
  }
  const text =
    typeof value === "string" ? value : JSON.stringify(value, null, 2);
  if (!text.trim()) return <Text type="secondary">{emptyLabel}</Text>;
  return (
    <pre
      style={{
        margin: 0,
        padding: 12,
        borderRadius: 6,
        background: "var(--fn-bg-layout, rgba(0,0,0,0.03))",
        fontFamily:
          "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
        fontSize: 12,
        lineHeight: 1.6,
        maxHeight: 280,
        overflow: "auto",
        whiteSpace: "pre-wrap",
        wordBreak: "break-word",
      }}
    >
      {text}
    </pre>
  );
}

/** One selectable workflow: id plus the label the pickers display. */
export interface WorkflowOption {
  value: string;
  label: string;
}

/**
 * Workflow selector shared by the version, execution and trigger tabs so the
 * page-level selection means the same thing everywhere.
 */
export function WorkflowPicker({
  options,
  value,
  onChange,
  loading,
}: {
  options: WorkflowOption[];
  value: string | null;
  onChange: (value: string | null) => void;
  loading: boolean;
}) {
  const { t } = useTranslation();
  return (
    <Select
      style={{ minWidth: 240, maxWidth: 360 }}
      size="small"
      showSearch
      optionFilterProp="label"
      loading={loading}
      value={value ?? undefined}
      placeholder={t("workbuddy.workflows.selectWorkflowPlaceholder")}
      options={options}
      onChange={(next) => onChange(next ?? null)}
      allowClear
    />
  );
}
