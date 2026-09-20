/**
 * WorkBuddy console → Workflows → Versions.
 *
 * Version history of the selected workflow. Activation and rollback are
 * compare-and-swap writes: the panel re-reads the workflow immediately before
 * each write so the ``If-Match`` ETag is the revision the server will accept,
 * and a 409 is reported as "reload and retry" instead of being retried blindly.
 */

import { useCallback, useEffect, useState, type ReactNode } from "react";
import {
  Alert,
  Button,
  Collapse,
  Modal,
  Popconfirm,
  Space,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import type { TableProps } from "antd";
import type { ColumnsType } from "antd/es/table";
import type { TFunction } from "i18next";
import { message } from "@/utils/antdMessage";
import { History, RefreshCw } from "lucide-react";
import { useTranslation } from "react-i18next";
import { ResizableTable } from "../../../components/ResizableTable";
import { apiErrorMessage, parseApiError } from "../../../utils/apiError";
import { useServerTimezone } from "../../../hooks/useServerTimezone";
import { formatServerDateTime } from "../../../utils/formatMessageTime";
import {
  isWorkBuddyUnavailableError,
  isWorkflowManagerDetail,
  workbuddyWorkflowsApi,
  workflowEtag,
  type WorkflowActivationMode,
  type WorkflowDefinitionChange,
  type WorkflowVersion,
  type WorkflowVersionDiff,
  type WorkflowVersionDiffSide,
  type WorkflowVersionDiffSummary,
} from "../../../api/modules/workbuddyWorkflows";
import { TabPanelHeader } from "../../Settings/AdvancedSettings/TabPanelHeader";
import {
  JsonPreview,
  LoadError,
  LoadingBlock,
  ResourceState,
  UnavailableNotice,
  WorkflowPicker,
  statusLabel,
  useWorkBuddyResource,
  type WorkflowOption,
} from "./consoleState";
import styles from "./index.module.less";

const { Text } = Typography;

/** Tag colours that tell the three kinds apart at a glance. */
const CHANGE_KIND_COLOR: Record<string, string> = {
  added: "green",
  removed: "red",
  replaced: "gold",
};

const CHANGE_KINDS = ["added", "removed", "replaced"] as const;

const MONO = {
  fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
  fontSize: 12,
};

/**
 * Container pointer of a change: the JSON pointer with its last segment
 * dropped, so ``/nodes/grade/params/model`` groups under
 * ``/nodes/grade/params`` and the reader sees one block per place that changed.
 */
function containerPath(path: string): string {
  const trimmed =
    path.length > 1 && path.endsWith("/") ? path.slice(0, -1) : path;
  const cut = trimmed.lastIndexOf("/");
  return cut <= 0 ? "/" : trimmed.slice(0, cut);
}

interface ChangeGroup {
  path: string;
  changes: WorkflowDefinitionChange[];
}

/** Changes sharing a container path, in the order the server sent them. */
function groupChanges(changes: WorkflowDefinitionChange[]): ChangeGroup[] {
  const groups = new Map<string, WorkflowDefinitionChange[]>();
  for (const change of changes) {
    const key = containerPath(change.path);
    const bucket = groups.get(key);
    if (bucket === undefined) groups.set(key, [change]);
    else bucket.push(change);
  }
  return [...groups].map(([path, items]) => ({ path, changes: items }));
}

/** Counts for a change list, used only when the response carried no summary. */
function summarize(
  changes: WorkflowDefinitionChange[],
): WorkflowVersionDiffSummary {
  const summary: WorkflowVersionDiffSummary = {
    added: 0,
    removed: 0,
    replaced: 0,
  };
  for (const change of changes) {
    if (change.kind === "added") summary.added += 1;
    else if (change.kind === "removed") summary.removed += 1;
    else if (change.kind === "replaced") summary.replaced += 1;
  }
  return summary;
}

/**
 * One side of the comparison: what the response stated, falling back to the
 * row the list already holds. A digest the response did not carry stays
 * absent rather than being invented.
 */
function VersionSide({
  side,
  row,
  t,
}: {
  side: WorkflowVersionDiffSide | null;
  row: WorkflowVersion | undefined;
  t: TFunction;
}): ReactNode {
  const number = side?.version_number ?? row?.version_number;
  const origin = side?.origin ?? row?.origin;
  const sha = side?.definition_sha256 ?? null;
  return (
    <Space size={6} wrap>
      <Text code>{number === undefined ? "—" : `v${number}`}</Text>
      {origin ? (
        <Tag>
          {statusLabel(t, "workbuddy.workflows.versions.origin", origin)}
        </Tag>
      ) : null}
      {sha ? (
        <Tooltip title={sha}>
          <Text type="secondary" style={MONO}>
            {sha.slice(0, 12)}
          </Text>
        </Tooltip>
      ) : null}
    </Space>
  );
}

/**
 * The comparison itself: one block per container path, each change tagged with
 * its kind and holding both sides behind a collapsed JSON preview. The previews
 * are destroyed while closed, so a large definition costs nothing until the
 * reader opens the side they want.
 */
function VersionDiffBody({
  diff,
  pair,
}: {
  diff: WorkflowVersionDiff;
  pair: [WorkflowVersion, WorkflowVersion] | null;
}) {
  const { t } = useTranslation();
  const changes = diff.changes;
  if (changes === null) {
    return (
      <Alert
        type="warning"
        showIcon
        message={t("workbuddy.workflows.versions.diff.missingChanges")}
      />
    );
  }
  const summary = diff.summary ?? summarize(changes);
  return (
    <>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          flexWrap: "wrap",
          marginBottom: 12,
        }}
      >
        <VersionSide side={diff.from} row={pair?.[0]} t={t} />
        <span style={{ opacity: 0.6 }}>→</span>
        <VersionSide side={diff.to} row={pair?.[1]} t={t} />
      </div>
      {changes.length === 0 ? (
        <Alert
          type="info"
          showIcon
          message={t("workbuddy.workflows.versions.diff.empty")}
        />
      ) : (
        <>
          <Space size={4} wrap style={{ marginBottom: 12 }}>
            {CHANGE_KINDS.map((kind) => (
              <Tag key={kind} color={CHANGE_KIND_COLOR[kind]}>
                {t(`workbuddy.workflows.versions.diff.summary.${kind}`, {
                  count: summary[kind],
                })}
              </Tag>
            ))}
          </Space>
          {groupChanges(changes).map((group) => (
            <div key={group.path} style={{ marginBottom: 12 }}>
              <Space size={6} align="center">
                <Text code>{group.path}</Text>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {t("workbuddy.workflows.versions.diff.groupCount", {
                    count: group.changes.length,
                  })}
                </Text>
              </Space>
              {group.changes.map((change) => (
                <div
                  key={`${change.kind}:${change.path}`}
                  style={{
                    border: "1px solid var(--fn-border, rgba(0, 0, 0, 0.08))",
                    borderRadius: 6,
                    padding: "8px 10px",
                    marginTop: 6,
                  }}
                >
                  <Space size={6} wrap align="center">
                    <Tag color={CHANGE_KIND_COLOR[change.kind] ?? "default"}>
                      {statusLabel(
                        t,
                        "workbuddy.workflows.versions.diff.kind",
                        change.kind,
                      )}
                    </Tag>
                    <Text code>{change.path}</Text>
                  </Space>
                  <Collapse
                    size="small"
                    ghost
                    destroyOnHidden
                    items={[
                      {
                        key: "old",
                        label: (
                          <Text type="secondary" style={{ fontSize: 12 }}>
                            {t("workbuddy.workflows.versions.diff.old")}
                          </Text>
                        ),
                        children: (
                          <JsonPreview
                            value={change.old}
                            emptyLabel={t(
                              "workbuddy.workflows.versions.diff.noValue",
                            )}
                          />
                        ),
                      },
                      {
                        key: "new",
                        label: (
                          <Text type="secondary" style={{ fontSize: 12 }}>
                            {t("workbuddy.workflows.versions.diff.new")}
                          </Text>
                        ),
                        children: (
                          <JsonPreview
                            value={change.new}
                            emptyLabel={t(
                              "workbuddy.workflows.versions.diff.noValue",
                            )}
                          />
                        ),
                      },
                    ]}
                  />
                </div>
              ))}
            </div>
          ))}
        </>
      )}
    </>
  );
}

export default function VersionsPanel({
  workflowId,
  onSelectWorkflow,
  options,
  optionsLoading,
  onWorkflowChanged,
}: {
  workflowId: string | null;
  onSelectWorkflow: (id: string | null) => void;
  options: WorkflowOption[];
  optionsLoading: boolean;
  onWorkflowChanged: () => void;
}) {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  const versions = useWorkBuddyResource<WorkflowVersion[]>(
    [],
    () =>
      workflowId
        ? workbuddyWorkflowsApi.listWorkflowVersions(workflowId)
        : Promise.resolve([]),
    [workflowId],
  );
  const [busyId, setBusyId] = useState<string | null>(null);
  const [definition, setDefinition] = useState<WorkflowVersion | null>(null);
  const [definitionLoading, setDefinitionLoading] = useState(false);
  const [compareIds, setCompareIds] = useState<string[]>([]);
  const [diffOpen, setDiffOpen] = useState(false);
  const [diffLoading, setDiffLoading] = useState(false);
  const [diffError, setDiffError] = useState<unknown>(null);
  const [diff, setDiff] = useState<WorkflowVersionDiff | null>(null);
  const [diffPair, setDiffPair] = useState<
    [WorkflowVersion, WorkflowVersion] | null
  >(null);

  // Another workflow is another history: a selection and a diff from the
  // previous one would be about versions this panel no longer lists.
  useEffect(() => {
    setCompareIds([]);
    setDiffOpen(false);
    setDiff(null);
    setDiffError(null);
    setDiffPair(null);
  }, [workflowId]);

  const openDefinition = useCallback(
    async (row: WorkflowVersion) => {
      if (!workflowId) return;
      setDefinitionLoading(true);
      try {
        setDefinition(
          await workbuddyWorkflowsApi.getWorkflowVersion(workflowId, row.id),
        );
      } catch (err) {
        message.error(
          apiErrorMessage(err, t("workbuddy.workflows.versions.loadFailed"), t),
        );
      } finally {
        setDefinitionLoading(false);
      }
    },
    [workflowId, t],
  );

  const runDiff = useCallback(
    async (pair: [WorkflowVersion, WorkflowVersion]) => {
      if (!workflowId) return;
      setDiffLoading(true);
      setDiffError(null);
      setDiff(null);
      try {
        setDiff(
          await workbuddyWorkflowsApi.compareWorkflowVersions(
            workflowId,
            pair[0].id,
            pair[1].id,
          ),
        );
      } catch (err) {
        setDiffError(err);
      } finally {
        setDiffLoading(false);
      }
    },
    [workflowId],
  );

  /**
   * Compare the two selected rows, the older version first — both in the pair
   * and in the query, so the arrows in the view always point forward in time.
   */
  const openDiff = useCallback(() => {
    if (!workflowId || compareIds.length !== 2) return;
    const selected = compareIds
      .map((id) => versions.data.find((row) => row.id === id))
      .filter((row): row is WorkflowVersion => row !== undefined);
    if (selected.length !== 2) return;
    const pair: [WorkflowVersion, WorkflowVersion] =
      selected[0].version_number <= selected[1].version_number
        ? [selected[0], selected[1]]
        : [selected[1], selected[0]];
    setDiffPair(pair);
    setDiffOpen(true);
    void runDiff(pair);
  }, [workflowId, compareIds, versions.data, runDiff]);

  const retryDiff = useCallback(() => {
    if (diffPair !== null) void runDiff(diffPair);
  }, [diffPair, runDiff]);

  const publish = useCallback(
    async (
      action: "activate" | "rollback",
      row: WorkflowVersion,
      mode: WorkflowActivationMode = "active",
    ) => {
      if (!workflowId) return;
      setBusyId(row.id);
      try {
        const detail = await workbuddyWorkflowsApi.getWorkflow(workflowId);
        if (!isWorkflowManagerDetail(detail)) {
          message.error(t("workbuddy.workflows.versions.onlyManagers"));
          return;
        }
        const etag = workflowEtag(detail);
        if (action === "activate") {
          await workbuddyWorkflowsApi.activateWorkflowVersion(
            detail.id,
            { version_id: row.id, mode },
            etag,
          );
          message.success(t("workbuddy.workflows.versions.activated"));
        } else {
          await workbuddyWorkflowsApi.rollbackWorkflowVersion(
            detail.id,
            { version_id: row.id },
            etag,
          );
          message.success(
            t("workbuddy.workflows.versions.rolledBack", {
              number: row.version_number,
            }),
          );
        }
        await versions.reload();
        onWorkflowChanged();
      } catch (err) {
        const code = parseApiError(err)?.code;
        if (
          code === "WORKBUDDY_WORKFLOW_REVISION_CONFLICT" ||
          code === "WORKBUDDY_PRECONDITION_REQUIRED"
        ) {
          message.error(t("workbuddy.workflows.versions.reloadRequired"));
        } else {
          message.error(
            apiErrorMessage(
              err,
              t(
                action === "activate"
                  ? "workbuddy.workflows.versions.activateFailed"
                  : "workbuddy.workflows.versions.rollbackFailed",
              ),
              t,
            ),
          );
        }
      } finally {
        setBusyId(null);
      }
    },
    [workflowId, versions, onWorkflowChanged, t],
  );

  // A comparison needs exactly two rows: the third checkbox is disabled rather
  // than silently dropping a choice the reader already made, and a select-all
  // header would tick a third version the view cannot compare.
  const rowSelection: TableProps<WorkflowVersion>["rowSelection"] = {
    selectedRowKeys: compareIds,
    onChange: (keys) => setCompareIds(keys.slice(0, 2).map(String)),
    getCheckboxProps: (row) => ({
      disabled: compareIds.length >= 2 && !compareIds.includes(row.id),
    }),
    hideSelectAll: true,
    columnWidth: 40,
  };

  const columns: ColumnsType<WorkflowVersion> = [
    {
      title: t("workbuddy.workflows.versions.column.number"),
      dataIndex: "version_number",
      key: "version_number",
      width: 90,
      render: (value: number) => <Text code>v{value}</Text>,
    },
    {
      title: t("workbuddy.workflows.versions.column.origin"),
      dataIndex: "origin",
      key: "origin",
      width: 130,
      render: (origin: string) => (
        <Tag>
          {statusLabel(t, "workbuddy.workflows.versions.origin", origin)}
        </Tag>
      ),
    },
    {
      title: t("workbuddy.workflows.versions.column.summary"),
      dataIndex: "change_summary",
      key: "change_summary",
      width: 300,
      render: (value: string | null) =>
        value?.trim() ? value : <Text type="secondary">—</Text>,
    },
    {
      title: t("workbuddy.workflows.versions.column.createdAt"),
      dataIndex: "created_at",
      key: "created_at",
      width: 170,
      render: (value: number | null) =>
        value ? formatServerDateTime(value, timeZone) : "—",
    },
    {
      title: t("workbuddy.workflows.versions.column.markers"),
      key: "markers",
      width: 200,
      render: (_value, row) => (
        <Space size={4} wrap>
          {row.is_active && (
            <Tag color="green">
              {t("workbuddy.workflows.versions.badge.active")}
            </Tag>
          )}
          {row.is_shadow && (
            <Tag color="purple">
              {t("workbuddy.workflows.versions.badge.shadow")}
            </Tag>
          )}
          {row.is_candidate && (
            <Tag color="gold">
              {t("workbuddy.workflows.versions.badge.candidate")}
            </Tag>
          )}
        </Space>
      ),
    },
    {
      title: t("workbuddy.workflows.versions.column.actions"),
      key: "actions",
      width: 340,
      fixed: "right",
      render: (_value, row) => (
        <Space size={4} wrap>
          <Button
            type="link"
            size="small"
            loading={definitionLoading}
            onClick={() => void openDefinition(row)}
          >
            {t("workbuddy.workflows.versions.viewDefinition")}
          </Button>
          {row.is_candidate ? (
            <Tooltip title={t("workbuddy.workflows.versions.candidateLocked")}>
              <Button type="link" size="small" disabled>
                {t("workbuddy.workflows.versions.activate")}
              </Button>
            </Tooltip>
          ) : (
            <>
              {!row.is_active && (
                <Popconfirm
                  title={t("workbuddy.workflows.versions.activateConfirm", {
                    number: row.version_number,
                    mode: t("workbuddy.workflows.versions.activateAsActive"),
                  })}
                  onConfirm={() => void publish("activate", row, "active")}
                >
                  <Button type="link" size="small" loading={busyId === row.id}>
                    {t("workbuddy.workflows.versions.activateAsActive")}
                  </Button>
                </Popconfirm>
              )}
              {!row.is_shadow && !row.is_active && (
                <Popconfirm
                  title={t("workbuddy.workflows.versions.activateConfirm", {
                    number: row.version_number,
                    mode: t("workbuddy.workflows.versions.activateAsShadow"),
                  })}
                  description={t("workbuddy.workflows.versions.shadowHint")}
                  onConfirm={() => void publish("activate", row, "shadow")}
                >
                  <Button type="link" size="small">
                    {t("workbuddy.workflows.versions.activateAsShadow")}
                  </Button>
                </Popconfirm>
              )}
              <Popconfirm
                title={t("workbuddy.workflows.versions.rollbackConfirm", {
                  number: row.version_number,
                })}
                onConfirm={() => void publish("rollback", row)}
              >
                <Button type="link" size="small">
                  {t("workbuddy.workflows.versions.rollback")}
                </Button>
              </Popconfirm>
            </>
          )}
        </Space>
      ),
    },
  ];

  if (!workflowId) {
    return (
      <div className={styles.panel}>
        <TabPanelHeader
          icon={<History size={16} />}
          title={t("workbuddy.workflows.versions.title")}
          description={t("workbuddy.workflows.versions.description")}
          actions={
            <WorkflowPicker
              options={options}
              value={workflowId}
              onChange={onSelectWorkflow}
              loading={optionsLoading}
            />
          }
        />
        <Alert
          type="info"
          showIcon
          message={t("workbuddy.workflows.state.noSelectionTitle")}
          description={t("workbuddy.workflows.state.noSelectionHint")}
        />
      </div>
    );
  }

  const failed = versions.error !== null && versions.error !== undefined;
  const blocked = failed || versions.loading || versions.data.length === 0;

  return (
    <div className={styles.panel}>
      <TabPanelHeader
        icon={<History size={16} />}
        title={t("workbuddy.workflows.versions.title")}
        description={t("workbuddy.workflows.versions.description")}
        actions={
          <Space size={8}>
            <WorkflowPicker
              options={options}
              value={workflowId}
              onChange={onSelectWorkflow}
              loading={optionsLoading}
            />
            <Button
              size="small"
              icon={<RefreshCw size={14} />}
              onClick={() => void versions.reload()}
            >
              {t("common.refresh")}
            </Button>
          </Space>
        }
      />

      {blocked ? (
        <ResourceState
          resource={versions}
          isEmpty={!failed && !versions.loading && versions.data.length === 0}
          loadingLabel={t("common.loading")}
          errorTitle={t("workbuddy.workflows.versions.loadFailed")}
          errorFallback={t("workbuddy.workflows.state.errorFallback")}
          unavailableTitle={t("workbuddy.workflows.state.unavailableTitle")}
          unavailableHint={t("workbuddy.workflows.state.unavailableHint")}
          emptyTitle={t("workbuddy.workflows.versions.emptyTitle")}
          emptyHint={t("workbuddy.workflows.versions.emptyHint")}
        />
      ) : (
        <>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 8,
              flexWrap: "wrap",
              marginBottom: 8,
            }}
          >
            <Text type="secondary" style={{ fontSize: 12 }}>
              {t("workbuddy.workflows.versions.diff.hint")}
            </Text>
            <Button
              size="small"
              type="primary"
              disabled={compareIds.length !== 2}
              onClick={openDiff}
            >
              {t("workbuddy.workflows.versions.diff.compare")}
            </Button>
          </div>
          <ResizableTable<WorkflowVersion>
            rowKey="id"
            size="small"
            columns={columns}
            dataSource={versions.data}
            rowSelection={rowSelection}
            pagination={false}
            scroll={{ x: 1270 }}
            storageKey="workbuddy-workflows-versions"
          />
        </>
      )}

      <Modal
        open={definition !== null}
        onCancel={() => setDefinition(null)}
        destroyOnHidden
        footer={null}
        width={760}
        title={t("workbuddy.workflows.versions.definitionTitle", {
          number: definition?.version_number ?? "",
        })}
      >
        {definition?.definition ? (
          <pre className={styles.jsonBlock}>
            {JSON.stringify(definition.definition, null, 2)}
          </pre>
        ) : (
          <Text type="secondary">
            {t("workbuddy.workflows.versions.noDefinition")}
          </Text>
        )}
      </Modal>

      <Modal
        open={diffOpen}
        onCancel={() => setDiffOpen(false)}
        destroyOnHidden
        footer={null}
        width={860}
        title={
          diffPair === null
            ? t("workbuddy.workflows.versions.diff.compare")
            : t("workbuddy.workflows.versions.diff.title", {
                from: `v${diffPair[0].version_number}`,
                to: `v${diffPair[1].version_number}`,
              })
        }
      >
        {diffLoading ? (
          <LoadingBlock label={t("common.loading")} />
        ) : diffError !== null && diffError !== undefined ? (
          // A refusal the server described — a version that is gone, a pair
          // from different workflows — is a real failure with the server's own
          // reason. Only an answer with no envelope at all (unmounted route,
          // missing control plane, dead connection) is "not available yet".
          isWorkBuddyUnavailableError(diffError) &&
          parseApiError(diffError) === null ? (
            <UnavailableNotice
              error={diffError}
              title={t("workbuddy.workflows.state.unavailableTitle")}
              hint={t("workbuddy.workflows.state.unavailableHint")}
              errorFallback={t("workbuddy.workflows.state.errorFallback")}
              onRetry={retryDiff}
            />
          ) : (
            <LoadError
              error={diffError}
              title={t("workbuddy.workflows.versions.diff.loadFailed")}
              fallback={t("workbuddy.workflows.state.errorFallback")}
              onRetry={retryDiff}
            />
          )
        ) : diff === null ? null : (
          <VersionDiffBody diff={diff} pair={diffPair} />
        )}
      </Modal>
    </div>
  );
}
