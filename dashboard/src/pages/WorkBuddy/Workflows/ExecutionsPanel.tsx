/**
 * WorkBuddy console → Workflows → Executions.
 *
 * Runs are accepted asynchronously by the server, so this panel lists what the
 * server reports (optionally scoped to one workflow), opens a real detail view
 * and only offers cancel / decide for the states the runtime accepts.
 */

import { useCallback, useMemo, useState } from "react";
import {
  Button,
  Popconfirm,
  Segmented,
  Select,
  Space,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { message } from "@/utils/antdMessage";
import { Eye, PlayCircle, RefreshCw, RotateCcw, XCircle } from "lucide-react";
import { useTranslation } from "react-i18next";
import { ResizableTable } from "../../../components/ResizableTable";
import { apiErrorMessage } from "../../../utils/apiError";
import { useServerTimezone } from "../../../hooks/useServerTimezone";
import { formatServerIsoDateTime } from "../../../utils/formatMessageTime";
import {
  CANCELLABLE_EXECUTION_STATUSES,
  EXECUTION_STATUSES,
  workbuddyRuntimeApi,
  type Execution,
  type ExecutionStatus,
  type WorkBuddyScope,
} from "../../../api/modules/workbuddyRuntime";
import { TabPanelHeader } from "../../Settings/AdvancedSettings/TabPanelHeader";
import {
  ResourceState,
  WorkflowPicker,
  statusLabel,
  useWorkBuddyResource,
  type WorkflowOption,
} from "./consoleState";
import ExecuteModal from "./ExecuteModal";
import ExecutionDetailDrawer from "./ExecutionDetailDrawer";
import DecisionModal from "../Approvals/DecisionModal";
import styles from "./index.module.less";

const { Text } = Typography;

export default function ExecutionsPanel({
  workflowId,
  onSelectWorkflow,
  options,
  optionsLoading,
  onExecutionAccepted,
}: {
  workflowId: string | null;
  onSelectWorkflow: (id: string | null) => void;
  options: WorkflowOption[];
  optionsLoading: boolean;
  onExecutionAccepted: () => void;
}) {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  const [scope, setScope] = useState<WorkBuddyScope>("self");
  const [status, setStatus] = useState<ExecutionStatus | "">("");
  const [runOpen, setRunOpen] = useState(false);
  const [detailId, setDetailId] = useState<string | null>(null);
  const [decideTarget, setDecideTarget] = useState<{
    approvalRequestId: string;
    executionId: string;
  } | null>(null);
  const [cancellingId, setCancellingId] = useState<string | null>(null);
  const [rerunningId, setRerunningId] = useState<string | null>(null);

  const executions = useWorkBuddyResource<Execution[]>(
    [],
    () =>
      workbuddyRuntimeApi.listExecutions({
        scope,
        status,
        workflow_id: workflowId ?? undefined,
        limit: 100,
      }),
    [scope, status, workflowId],
  );

  const workflowNames = useMemo<Record<string, string>>(
    () =>
      Object.fromEntries(options.map((option) => [option.value, option.label])),
    [options],
  );

  const workflowName = useCallback(
    (id: string): string => workflowNames[id] ?? id.slice(0, 8),
    [workflowNames],
  );

  const cancel = useCallback(
    async (row: Execution) => {
      setCancellingId(row.id);
      try {
        await workbuddyRuntimeApi.cancelExecution(row.id);
        message.success(t("workbuddy.workflows.executions.cancelled"));
        await executions.reload();
      } catch (err) {
        message.error(
          apiErrorMessage(
            err,
            t("workbuddy.workflows.executions.cancelFailed"),
            t,
          ),
        );
      } finally {
        setCancellingId(null);
      }
    },
    [executions, t],
  );

  /**
   * A re-run replays a row's recorded inputs through the execute route, which
   * starts the workflow's *active* version — that route has no version pin, so
   * the confirm text says so instead of promising the original definition. The
   * action is only offered when the row carries the workflow id, the version it
   * ran and the inputs it used; otherwise the operator is told which part is
   * missing rather than the panel guessing one.
   */
  const rerunBlocker = useCallback(
    (row: Execution): string | null => {
      if (!row.workflow_id) {
        return t("workbuddy.workflows.executions.rerunMissingWorkflow");
      }
      if (!row.workflow_version_id) {
        return t("workbuddy.workflows.executions.rerunMissingVersion");
      }
      const inputs = row.inputs;
      if (
        inputs === null ||
        typeof inputs !== "object" ||
        Array.isArray(inputs)
      ) {
        return t("workbuddy.workflows.executions.rerunMissingInputs");
      }
      return null;
    },
    [t],
  );

  const rerun = useCallback(
    async (row: Execution) => {
      setRerunningId(row.id);
      try {
        const execution = await workbuddyRuntimeApi.executeWorkflow(
          row.workflow_id,
          { inputs: row.inputs },
        );
        message.success(
          t("workbuddy.workflows.executions.rerunStarted", {
            id: execution.id.slice(0, 8),
          }),
        );
        await executions.reload();
        onExecutionAccepted();
      } catch (err) {
        message.error(
          apiErrorMessage(
            err,
            t("workbuddy.workflows.executions.rerunFailed"),
            t,
          ),
        );
      } finally {
        setRerunningId(null);
      }
    },
    [executions, onExecutionAccepted, t],
  );

  const columns: ColumnsType<Execution> = [
    {
      title: t("workbuddy.workflows.executions.column.id"),
      dataIndex: "id",
      key: "id",
      width: 120,
      render: (value: string) => <Text code>{value.slice(0, 8)}</Text>,
    },
    {
      title: t("workbuddy.workflows.executions.column.workflow"),
      dataIndex: "workflow_id",
      key: "workflow_id",
      width: 200,
      render: (value: string) => <Text>{workflowName(value)}</Text>,
    },
    {
      title: t("workbuddy.workflows.executions.column.status"),
      dataIndex: "status",
      key: "status",
      width: 160,
      render: (value: ExecutionStatus) => (
        <Tag
          color={
            value === "succeeded"
              ? "green"
              : value === "failed"
              ? "red"
              : value === "waiting_approval"
              ? "gold"
              : value === "partial"
              ? "orange"
              : "default"
          }
        >
          {statusLabel(t, "workbuddy.workflows.executions.status", value)}
        </Tag>
      ),
    },
    {
      title: t("workbuddy.workflows.executions.column.trigger"),
      dataIndex: "trigger_type",
      key: "trigger_type",
      width: 120,
      render: (value: string) =>
        statusLabel(t, "workbuddy.workflows.executions.trigger", value),
    },
    {
      title: t("workbuddy.workflows.executions.column.createdAt"),
      dataIndex: "created_at",
      key: "created_at",
      width: 170,
      render: (value: string | null) =>
        value ? formatServerIsoDateTime(value, timeZone) : "—",
    },
    {
      title: t("workbuddy.workflows.executions.column.finishedAt"),
      dataIndex: "finished_at",
      key: "finished_at",
      width: 170,
      render: (value: string | null) =>
        value ? formatServerIsoDateTime(value, timeZone) : "—",
    },
    {
      title: t("workbuddy.workflows.executions.column.actions"),
      key: "actions",
      width: 360,
      fixed: "right",
      render: (_value, row) => {
        const blocker = rerunBlocker(row);
        const rerunButton = (
          <Button
            type="link"
            size="small"
            icon={<RotateCcw size={13} />}
            disabled={blocker !== null}
            loading={rerunningId === row.id}
          >
            {t("workbuddy.workflows.executions.rerun")}
          </Button>
        );
        return (
          <Space size={4} wrap>
            <Button
              type="link"
              size="small"
              icon={<Eye size={13} />}
              onClick={() => setDetailId(row.id)}
            >
              {t("workbuddy.workflows.executions.detail")}
            </Button>
            {blocker !== null ? (
              <Tooltip title={blocker}>
                <span>{rerunButton}</span>
              </Tooltip>
            ) : (
              <Popconfirm
                title={t("workbuddy.workflows.executions.rerunConfirm", {
                  workflow: workflowName(row.workflow_id),
                  id: row.id.slice(0, 8),
                })}
                description={t(
                  "workbuddy.workflows.executions.rerunVersionNotice",
                  {
                    id: row.id.slice(0, 8),
                    version: row.workflow_version_id.slice(0, 8),
                  },
                )}
                onConfirm={() => void rerun(row)}
              >
                {rerunButton}
              </Popconfirm>
            )}
            {row.status === "waiting_approval" && (
              <Button
                type="link"
                size="small"
                onClick={() =>
                  setDecideTarget({
                    approvalRequestId: "",
                    executionId: row.id,
                  })
                }
              >
                {t("workbuddy.workflows.executions.decide")}
              </Button>
            )}
            {CANCELLABLE_EXECUTION_STATUSES.includes(row.status) && (
              <Popconfirm
                title={t("workbuddy.workflows.executions.cancelConfirm", {
                  id: row.id.slice(0, 8),
                })}
                onConfirm={() => void cancel(row)}
              >
                <Button
                  type="link"
                  size="small"
                  danger
                  icon={<XCircle size={13} />}
                  loading={cancellingId === row.id}
                >
                  {t("workbuddy.workflows.executions.cancel")}
                </Button>
              </Popconfirm>
            )}
          </Space>
        );
      },
    },
  ];

  const failed = executions.error !== null && executions.error !== undefined;
  const blocked = failed || executions.loading || executions.data.length === 0;

  return (
    <div className={styles.panel}>
      <TabPanelHeader
        icon={<PlayCircle size={16} />}
        title={t("workbuddy.workflows.executions.title")}
        description={t("workbuddy.workflows.executions.description")}
        actions={
          <Space size={8} wrap>
            <WorkflowPicker
              options={options}
              value={workflowId}
              onChange={onSelectWorkflow}
              loading={optionsLoading}
            />
            <Segmented
              size="small"
              value={scope}
              onChange={(value) =>
                setScope(value === "tenant" ? "tenant" : "self")
              }
              options={[
                {
                  value: "self",
                  label: t("workbuddy.workflows.executions.scope.self"),
                },
                {
                  value: "tenant",
                  label: t("workbuddy.workflows.executions.scope.tenant"),
                },
              ]}
            />
            <Select<ExecutionStatus | "">
              size="small"
              style={{ minWidth: 160 }}
              value={status}
              onChange={setStatus}
              options={[
                {
                  value: "",
                  label: t("workbuddy.workflows.executions.filterAll"),
                },
                ...EXECUTION_STATUSES.map((value) => ({
                  value,
                  label: statusLabel(
                    t,
                    "workbuddy.workflows.executions.status",
                    value,
                  ),
                })),
              ]}
            />
            <Button
              size="small"
              icon={<RefreshCw size={14} />}
              onClick={() => void executions.reload()}
            >
              {t("common.refresh")}
            </Button>
            {workflowId ? (
              <Button
                size="small"
                type="primary"
                icon={<PlayCircle size={14} />}
                onClick={() => setRunOpen(true)}
              >
                {t("workbuddy.workflows.executions.run")}
              </Button>
            ) : (
              <Tooltip
                title={t("workbuddy.workflows.executions.selectWorkflowToRun")}
              >
                <span>
                  <Button size="small" type="primary" disabled>
                    {t("workbuddy.workflows.executions.run")}
                  </Button>
                </span>
              </Tooltip>
            )}
          </Space>
        }
      />

      {blocked ? (
        <ResourceState
          resource={executions}
          isEmpty={
            !failed && !executions.loading && executions.data.length === 0
          }
          loadingLabel={t("common.loading")}
          errorTitle={t("workbuddy.workflows.executions.loadFailed")}
          errorFallback={t("workbuddy.workflows.state.errorFallback")}
          unavailableTitle={t("workbuddy.workflows.state.unavailableTitle")}
          unavailableHint={t("workbuddy.workflows.state.unavailableHint")}
          emptyTitle={t("workbuddy.workflows.executions.emptyTitle")}
          emptyHint={t("workbuddy.workflows.executions.emptyHint")}
        />
      ) : (
        <ResizableTable<Execution>
          rowKey="id"
          size="small"
          columns={columns}
          dataSource={executions.data}
          pagination={false}
          scroll={{ x: 1400 }}
          storageKey="workbuddy-workflows-executions"
        />
      )}

      <ExecuteModal
        open={runOpen}
        workflow={
          workflowId ? { id: workflowId, name: workflowName(workflowId) } : null
        }
        onClose={() => setRunOpen(false)}
        onStarted={(execution) => {
          void executions.reload();
          onExecutionAccepted();
          setDetailId(execution.id);
        }}
      />

      <ExecutionDetailDrawer
        executionId={detailId}
        onClose={() => setDetailId(null)}
      />

      <DecisionModal
        open={decideTarget !== null}
        executionId={decideTarget?.executionId ?? null}
        approvalRequestId={decideTarget?.approvalRequestId || null}
        onClose={() => setDecideTarget(null)}
        onDecided={() => void executions.reload()}
      />
    </div>
  );
}
