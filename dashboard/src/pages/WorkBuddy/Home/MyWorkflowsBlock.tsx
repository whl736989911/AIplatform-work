/**
 * WorkBuddy workbench → 我的工作流.
 *
 * The first five workflows the server lists for this caller — the published
 * catalogue for a member, the managed set for a creator / tenant admin. Every
 * row links into the workflows console with that workflow selected.
 */

import { useMemo } from "react";
import { Button, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { Workflow } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import { ResizableTable } from "../../../components/ResizableTable";
import { useServerTimezone } from "../../../hooks/useServerTimezone";
import { formatServerDateTime } from "../../../utils/formatMessageTime";
import type { WorkflowListItem } from "../../../api/modules/workbuddyWorkflows";
import {
  ResourceState,
  statusLabel,
  type WorkBuddyResource,
} from "../Workflows/consoleState";
import Section from "./Section";

const { Text } = Typography;

/** The workbench is a summary: five rows are enough to recognise the set. */
const WORKFLOW_LIMIT = 5;

export default function MyWorkflowsBlock({
  resource,
}: {
  resource: WorkBuddyResource<WorkflowListItem[]>;
}) {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  const navigate = useNavigate();

  const rows = useMemo(
    () => resource.data.slice(0, WORKFLOW_LIMIT),
    [resource.data],
  );

  const columns: ColumnsType<WorkflowListItem> = [
    {
      title: t("workbuddy.workflows.definitions.column.name"),
      dataIndex: "name",
      key: "name",
      width: 200,
      render: (value: string) => <Text strong>{value}</Text>,
    },
    {
      title: t("workbuddy.workflows.definitions.column.status"),
      dataIndex: "status",
      key: "status",
      width: 100,
      render: (value: string) => (
        <Tag color={value === "active" ? "green" : "default"}>
          {statusLabel(t, "workbuddy.workflows.definitions.status", value)}
        </Tag>
      ),
    },
    {
      title: t("workbuddy.workflows.definitions.column.updatedAt"),
      dataIndex: "updated_at",
      key: "updated_at",
      width: 170,
      // Epoch seconds on this projection, unlike the ISO-8601 runtime rows.
      render: (value: number | null) =>
        value ? formatServerDateTime(value, timeZone) : "—",
    },
    {
      title: t("common.actions"),
      key: "actions",
      width: 100,
      fixed: "right",
      render: (_value, row) => (
        <Button
          type="link"
          size="small"
          onClick={() =>
            navigate(
              `/workbuddy/workflows?workflow=${encodeURIComponent(row.id)}`,
            )
          }
        >
          {t("workbuddy.shared.open")}
        </Button>
      ),
    },
  ];

  const failed = resource.error !== null && resource.error !== undefined;
  const blocked = failed || resource.loading || rows.length === 0;

  return (
    <Section
      icon={<Workflow size={16} />}
      title={t("workbuddy.home.workflowsTitle")}
    >
      {blocked ? (
        <ResourceState
          resource={resource}
          isEmpty={!failed && !resource.loading && rows.length === 0}
          loadingLabel={t("common.loading")}
          errorTitle={t("workbuddy.shared.loadFailed")}
          errorFallback={t("common.unknownError")}
          unavailableTitle={t("workbuddy.shared.notMergedTitle")}
          unavailableHint={t("workbuddy.shared.notMergedHint")}
          emptyTitle={t("workbuddy.shared.empty")}
          emptyHint={t("workbuddy.workflows.definitions.emptyHint")}
        />
      ) : (
        <ResizableTable<WorkflowListItem>
          rowKey="id"
          size="small"
          columns={columns}
          dataSource={rows}
          pagination={false}
          scroll={{ x: 570 }}
          storageKey="workbuddy-home-workflows"
        />
      )}
    </Section>
  );
}
