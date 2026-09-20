/**
 * WorkBuddy workbench → 待我处理.
 *
 * Exactly the pending approval requests the server returns for the caller's own
 * queue, capped at the five most recent. Nothing is decided here: the row link
 * hands off to the inbox, which owns the decision challenge and the resume.
 */

import { Button, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { Gavel } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import { ResizableTable } from "../../../components/ResizableTable";
import { useServerTimezone } from "../../../hooks/useServerTimezone";
import { formatServerIsoDateTime } from "../../../utils/formatMessageTime";
import type { ApprovalRequest } from "../../../api/modules/workbuddyRuntime";
import {
  ResourceState,
  type WorkBuddyResource,
} from "../Workflows/consoleState";
import Section from "./Section";

const { Text } = Typography;

export default function PendingApprovalsBlock({
  resource,
}: {
  resource: WorkBuddyResource<ApprovalRequest[]>;
}) {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  const navigate = useNavigate();

  const columns: ColumnsType<ApprovalRequest> = [
    {
      title: t("workbuddy.approvals.inbox.column.node"),
      dataIndex: "node_id",
      key: "node_id",
      width: 150,
      render: (value: string) => <Text code>{value}</Text>,
    },
    {
      title: t("workbuddy.approvals.inbox.column.execution"),
      dataIndex: "execution_id",
      key: "execution_id",
      width: 110,
      render: (value: string) => <Text code>{value.slice(0, 8)}</Text>,
    },
    {
      title: t("workbuddy.approvals.inbox.column.approvals"),
      key: "approvals",
      width: 110,
      render: (_value, row) =>
        t("workbuddy.approvals.inbox.approvalCount", {
          decided: row.decided_approvals,
          required: row.required_approvals,
        }),
    },
    {
      title: t("workbuddy.approvals.inbox.column.createdAt"),
      dataIndex: "created_at",
      key: "created_at",
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
          onClick={() => navigate("/workbuddy/inbox")}
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
      icon={<Gavel size={16} />}
      title={t("workbuddy.home.pendingTitle")}
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
          emptyTitle={t("workbuddy.home.pendingEmpty")}
          emptyHint={t("workbuddy.approvals.inbox.emptyHint")}
        />
      ) : (
        <ResizableTable<ApprovalRequest>
          rowKey="id"
          size="small"
          columns={columns}
          dataSource={resource.data}
          pagination={false}
          scroll={{ x: 640 }}
          storageKey="workbuddy-home-approvals"
        />
      )}
    </Section>
  );
}
