/**
 * WorkBuddy workbench → 最近运行.
 *
 * The caller's own most recent executions. The only label the execution row does
 * not carry is the workflow name, so it is resolved from the workflow list the
 * workbench already loaded; when that list is unavailable (its own block shows
 * the reason) the row falls back to the workflow id — never to a made-up name.
 */

import { useCallback } from "react";
import { Button, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { PlayCircle } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import { ResizableTable } from "../../../components/ResizableTable";
import { useServerTimezone } from "../../../hooks/useServerTimezone";
import { formatServerIsoDateTime } from "../../../utils/formatMessageTime";
import type { Execution } from "../../../api/modules/workbuddyRuntime";
import {
  ResourceState,
  statusLabel,
  type WorkBuddyResource,
} from "../Workflows/consoleState";
import Section from "./Section";

const { Text } = Typography;

export default function RecentRunsBlock({
  resource,
  workflowNames,
}: {
  resource: WorkBuddyResource<Execution[]>;
  /** workflow id → name, from the workbench's workflow list. */
  workflowNames: Record<string, string>;
}) {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  const navigate = useNavigate();

  const workflowLabel = useCallback(
    (id: string): string => workflowNames[id] ?? id.slice(0, 8),
    [workflowNames],
  );

  const columns: ColumnsType<Execution> = [
    {
      title: t("workbuddy.runs.columnWorkflow"),
      dataIndex: "workflow_id",
      key: "workflow_id",
      width: 200,
      render: (value: string) => <Text>{workflowLabel(value)}</Text>,
    },
    {
      title: t("workbuddy.runs.columnStatus"),
      dataIndex: "status",
      key: "status",
      width: 110,
      render: (value: string) => (
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
      title: t("workbuddy.runs.columnTrigger"),
      dataIndex: "trigger_type",
      key: "trigger_type",
      width: 100,
      render: (value: string) =>
        statusLabel(t, "workbuddy.workflows.executions.trigger", value),
    },
    {
      title: t("workbuddy.runs.columnStarted"),
      dataIndex: "started_at",
      key: "started_at",
      width: 170,
      render: (value: string | null) =>
        value ? formatServerIsoDateTime(value, timeZone) : "—",
    },
    {
      title: t("workbuddy.runs.columnFinished"),
      dataIndex: "finished_at",
      key: "finished_at",
      width: 170,
      render: (value: string | null) =>
        value ? formatServerIsoDateTime(value, timeZone) : "—",
    },
    {
      title: t("common.actions"),
      key: "actions",
      width: 100,
      fixed: "right",
      render: () => (
        <Button
          type="link"
          size="small"
          onClick={() => navigate("/workbuddy/runs")}
        >
          {t("workbuddy.shared.open")}
        </Button>
      ),
    },
  ];

  const failed = resource.error !== null && resource.error !== undefined;
  const blocked = failed || resource.loading || resource.data.length === 0;

  return (
    <Section
      icon={<PlayCircle size={16} />}
      title={t("workbuddy.home.runsTitle")}
      wide
    >
      {blocked ? (
        <ResourceState
          resource={resource}
          isEmpty={!failed && !resource.loading && resource.data.length === 0}
          loadingLabel={t("common.loading")}
          errorTitle={t("workbuddy.shared.loadFailed")}
          errorFallback={t("common.unknownError")}
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
          dataSource={resource.data}
          pagination={false}
          scroll={{ x: 850 }}
          storageKey="workbuddy-home-runs"
        />
      )}
    </Section>
  );
}
