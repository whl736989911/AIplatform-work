/**
 * Runs (/workbuddy/runs) — every execution the caller may see, across
 * workflows.
 *
 * The fourth daily entry: one flat, scope-filtered list over the same server
 * slice the Workflows page lists per workflow, so a row here opens the identical
 * detail drawer (steps, edge decisions, reconciliation evidence) instead of a
 * second, thinner rendering of the same execution.
 *
 * Nothing on this page mutates an execution — the row action is a read. The
 * workflow filter can arrive in the URL (`?workflow=<id>`, e.g. from a link on
 * the dashboard) and the picker writes it back, so a filtered view is
 * shareable, visibly labelled and always clearable.
 *
 * No data is fabricated: an unmerged slice (404/501) or a real failure renders
 * the shared informational / error state, and a workflow the name lookup does
 * not cover falls back to its short id instead of an invented label.
 */

import { useCallback, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Button, Segmented, Select, Space, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { Eye, RefreshCw } from "lucide-react";
import { useTranslation } from "react-i18next";
import PageShell from "../../../layouts/PageShell";
import { ResizableTable } from "../../../components/ResizableTable";
import { useServerTimezone } from "../../../hooks/useServerTimezone";
import { formatServerIsoDateTime } from "../../../utils/formatMessageTime";
import {
  EXECUTION_STATUSES,
  workbuddyRuntimeApi,
  type Execution,
  type ExecutionStatus,
  type WorkBuddyScope,
} from "../../../api/modules/workbuddyRuntime";
import {
  workbuddyWorkflowsApi,
  type WorkflowListItem,
} from "../../../api/modules/workbuddyWorkflows";
import {
  ResourceState,
  WorkflowPicker,
  statusLabel,
  useWorkBuddyResource,
  type WorkflowOption,
} from "../Workflows/consoleState";
import ExecutionDetailDrawer from "../Workflows/ExecutionDetailDrawer";

const { Text } = Typography;

/** Chip colour per execution status, matching the Workflows executions tab. */
function statusColor(status: ExecutionStatus): string {
  if (status === "succeeded") return "green";
  if (status === "failed") return "red";
  if (status === "waiting_approval") return "gold";
  if (status === "partial") return "orange";
  return "default";
}

export default function RunsPage() {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  const [searchParams, setSearchParams] = useSearchParams();
  const [scope, setScope] = useState<WorkBuddyScope>("self");
  const [status, setStatus] = useState<ExecutionStatus | "">("");
  const [detailId, setDetailId] = useState<string | null>(null);

  const workflowId = searchParams.get("workflow");

  const selectWorkflow = useCallback(
    (id: string | null) => {
      const next = new URLSearchParams(searchParams);
      if (id) next.set("workflow", id);
      else next.delete("workflow");
      setSearchParams(next, { replace: true });
    },
    [searchParams, setSearchParams],
  );

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

  // Name lookup only: the workflow list is the one source for what a workflow
  // is called, so the picker and the name column cannot disagree.
  const workflows = useWorkBuddyResource<WorkflowListItem[]>(
    [],
    () => workbuddyWorkflowsApi.listWorkflows(),
    [],
  );

  const options = useMemo<WorkflowOption[]>(
    () => workflows.data.map((row) => ({ value: row.id, label: row.name })),
    [workflows.data],
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

  const columns: ColumnsType<Execution> = [
    {
      title: t("workbuddy.runs.columnWorkflow"),
      dataIndex: "workflow_id",
      key: "workflow_id",
      width: 240,
      render: (value: string) => <Text>{workflowName(value)}</Text>,
    },
    {
      title: t("workbuddy.runs.columnStatus"),
      dataIndex: "status",
      key: "status",
      width: 160,
      render: (value: ExecutionStatus) => (
        <Tag color={statusColor(value)}>
          {statusLabel(t, "workbuddy.workflows.executions.status", value)}
        </Tag>
      ),
    },
    {
      title: t("workbuddy.runs.columnTrigger"),
      dataIndex: "trigger_type",
      key: "trigger_type",
      width: 120,
      render: (value: string) =>
        statusLabel(t, "workbuddy.workflows.executions.trigger", value),
    },
    {
      title: t("workbuddy.runs.columnStarted"),
      dataIndex: "started_at",
      key: "started_at",
      width: 180,
      render: (value: string | null) =>
        value ? formatServerIsoDateTime(value, timeZone) : "—",
    },
    {
      title: t("workbuddy.runs.columnFinished"),
      dataIndex: "finished_at",
      key: "finished_at",
      width: 180,
      render: (value: string | null) =>
        value ? formatServerIsoDateTime(value, timeZone) : "—",
    },
    {
      title: t("workbuddy.workflows.executions.column.actions"),
      key: "actions",
      width: 120,
      fixed: "right",
      render: (_value, row) => (
        <Button
          type="link"
          size="small"
          icon={<Eye size={13} />}
          onClick={() => setDetailId(row.id)}
        >
          {t("workbuddy.shared.open")}
        </Button>
      ),
    },
  ];

  const failed = executions.error !== null && executions.error !== undefined;
  const blocked = failed || executions.loading || executions.data.length === 0;

  return (
    <PageShell
      title={t("workbuddy.runs.title")}
      subtitle={t("workbuddy.runs.subtitle")}
      actions={
        <Space size={8} wrap>
          <WorkflowPicker
            options={options}
            value={workflowId}
            onChange={selectWorkflow}
            loading={workflows.loading}
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
            onClick={() => {
              void executions.reload();
              void workflows.reload();
            }}
          >
            {t("common.refresh")}
          </Button>
        </Space>
      }
    >
      {blocked ? (
        <ResourceState
          resource={executions}
          isEmpty={
            !failed && !executions.loading && executions.data.length === 0
          }
          loadingLabel={t("common.loading")}
          errorTitle={t("workbuddy.shared.loadFailed")}
          errorFallback={t("workbuddy.workflows.state.errorFallback")}
          unavailableTitle={t("workbuddy.shared.notMergedTitle")}
          unavailableHint={t("workbuddy.shared.notMergedHint")}
          emptyTitle={t("workbuddy.runs.empty")}
          emptyHint={t("workbuddy.workflows.executions.emptyHint")}
        />
      ) : (
        <ResizableTable<Execution>
          rowKey="id"
          size="small"
          columns={columns}
          dataSource={executions.data}
          pagination={false}
          scroll={{ x: 1000 }}
          storageKey="workbuddy-runs"
        />
      )}

      <ExecutionDetailDrawer
        executionId={detailId}
        onClose={() => setDetailId(null)}
      />
    </PageShell>
  );
}
