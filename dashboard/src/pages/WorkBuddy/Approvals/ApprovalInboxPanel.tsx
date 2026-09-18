/**
 * WorkBuddy console → Approvals → inbox.
 *
 * The list is whatever the server returns for the requested scope and status;
 * a decision is only ever offered for a pending row and is submitted through
 * the shared decision modal (challenge + resume).
 */

import { useCallback, useState } from "react";
import { Button, Segmented, Select, Space, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { Eye, Gavel, RefreshCw } from "lucide-react";
import { useTranslation } from "react-i18next";
import { ResizableTable } from "../../../components/ResizableTable";
import { useServerTimezone } from "../../../hooks/useServerTimezone";
import { formatServerIsoDateTime } from "../../../utils/formatMessageTime";
import {
  APPROVAL_STATUSES,
  workbuddyRuntimeApi,
  type ApprovalRequest,
  type ApprovalRequestDetail,
  type ApprovalRequestStatus,
  type WorkBuddyScope,
} from "../../../api/modules/workbuddyRuntime";
import { TabPanelHeader } from "../../Settings/AdvancedSettings/TabPanelHeader";
import {
  ResourceState,
  statusLabel,
  useWorkBuddyResource,
} from "../Workflows/consoleState";
import ApprovalDetailDrawer from "./ApprovalDetailDrawer";
import DecisionModal from "./DecisionModal";
import styles from "./index.module.less";

const { Text } = Typography;

export default function ApprovalInboxPanel() {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  const [scope, setScope] = useState<WorkBuddyScope>("self");
  const [status, setStatus] = useState<ApprovalRequestStatus | "">("pending");
  const [detailId, setDetailId] = useState<string | null>(null);
  const [decideTarget, setDecideTarget] = useState<{
    approvalRequestId: string;
    executionId: string;
  } | null>(null);

  const approvals = useWorkBuddyResource<ApprovalRequest[]>(
    [],
    () =>
      workbuddyRuntimeApi.listApprovalRequests({
        scope,
        status: status === "" ? undefined : status,
        limit: 100,
      }),
    [scope, status],
  );

  const openDecide = useCallback(
    (row: ApprovalRequestDetail | ApprovalRequest) => {
      setDetailId(null);
      setDecideTarget({
        approvalRequestId: row.id,
        executionId: row.execution_id,
      });
    },
    [],
  );

  const columns: ColumnsType<ApprovalRequest> = [
    {
      title: t("workbuddy.approvals.inbox.column.status"),
      dataIndex: "status",
      key: "status",
      width: 140,
      render: (value: string) => (
        <Tag color={value === "pending" ? "gold" : "default"}>
          {statusLabel(t, "workbuddy.approvals.inbox.status", value)}
        </Tag>
      ),
    },
    {
      title: t("workbuddy.approvals.inbox.column.node"),
      dataIndex: "node_id",
      key: "node_id",
      width: 180,
      render: (value: string) => <Text code>{value}</Text>,
    },
    {
      title: t("workbuddy.approvals.inbox.column.execution"),
      dataIndex: "execution_id",
      key: "execution_id",
      width: 130,
      render: (value: string) => <Text code>{value.slice(0, 8)}</Text>,
    },
    {
      title: t("workbuddy.approvals.inbox.column.approvals"),
      key: "approvals",
      width: 120,
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
      title: t("workbuddy.approvals.inbox.column.decidedAt"),
      dataIndex: "decided_at",
      key: "decided_at",
      width: 170,
      render: (value: string | null) =>
        value ? formatServerIsoDateTime(value, timeZone) : "—",
    },
    {
      title: t("workbuddy.approvals.inbox.column.actions"),
      key: "actions",
      width: 200,
      fixed: "right",
      render: (_value, row) => (
        <Space size={4}>
          <Button
            type="link"
            size="small"
            icon={<Eye size={13} />}
            onClick={() => setDetailId(row.id)}
          >
            {t("workbuddy.approvals.inbox.detail")}
          </Button>
          {row.status === "pending" && (
            <Button
              type="link"
              size="small"
              icon={<Gavel size={13} />}
              onClick={() => openDecide(row)}
            >
              {t("workbuddy.approvals.inbox.decide")}
            </Button>
          )}
        </Space>
      ),
    },
  ];

  const failed = approvals.error !== null && approvals.error !== undefined;
  const blocked = failed || approvals.loading || approvals.data.length === 0;

  return (
    <div className={styles.panel}>
      <TabPanelHeader
        icon={<Gavel size={16} />}
        title={t("workbuddy.approvals.inbox.title")}
        description={t("workbuddy.approvals.inbox.description")}
        actions={
          <Space size={8} wrap>
            <Segmented
              size="small"
              value={scope}
              onChange={(value) =>
                setScope(value === "tenant" ? "tenant" : "self")
              }
              options={[
                {
                  value: "self",
                  label: t("workbuddy.approvals.inbox.scope.self"),
                },
                {
                  value: "tenant",
                  label: t("workbuddy.approvals.inbox.scope.tenant"),
                },
              ]}
            />
            <Select<ApprovalRequestStatus | "">
              size="small"
              style={{ minWidth: 160 }}
              value={status}
              onChange={setStatus}
              options={[
                {
                  value: "",
                  label: t("workbuddy.approvals.inbox.filterAll"),
                },
                ...APPROVAL_STATUSES.map((value) => ({
                  value,
                  label: statusLabel(
                    t,
                    "workbuddy.approvals.inbox.status",
                    value,
                  ),
                })),
              ]}
            />
            <Button
              size="small"
              icon={<RefreshCw size={14} />}
              onClick={() => void approvals.reload()}
            >
              {t("common.refresh")}
            </Button>
          </Space>
        }
      />

      {blocked ? (
        <ResourceState
          resource={approvals}
          isEmpty={!failed && !approvals.loading && approvals.data.length === 0}
          loadingLabel={t("common.loading")}
          errorTitle={t("workbuddy.approvals.inbox.loadFailed")}
          errorFallback={t("workbuddy.approvals.state.errorFallback")}
          unavailableTitle={t("workbuddy.approvals.state.unavailableTitle")}
          unavailableHint={t("workbuddy.approvals.state.unavailableHint")}
          emptyTitle={t("workbuddy.approvals.inbox.emptyTitle")}
          emptyHint={t("workbuddy.approvals.inbox.emptyHint")}
        />
      ) : (
        <ResizableTable<ApprovalRequest>
          rowKey="id"
          size="small"
          columns={columns}
          dataSource={approvals.data}
          pagination={false}
          scroll={{ x: 1120 }}
          storageKey="workbuddy-approvals-inbox"
        />
      )}

      <ApprovalDetailDrawer
        approvalRequestId={detailId}
        onClose={() => setDetailId(null)}
        onDecide={openDecide}
      />

      <DecisionModal
        open={decideTarget !== null}
        executionId={decideTarget?.executionId ?? null}
        approvalRequestId={decideTarget?.approvalRequestId ?? null}
        onClose={() => setDecideTarget(null)}
        onDecided={() => void approvals.reload()}
      />
    </div>
  );
}
