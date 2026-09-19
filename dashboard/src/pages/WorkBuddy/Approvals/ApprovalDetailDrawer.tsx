/**
 * WorkBuddy console → Approvals → one request.
 *
 * Shows exactly what the decision applies to: the frozen parameter snapshot,
 * the collected-approval counters and the approver candidates. Only a pending
 * request offers the decision action.
 */

import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  Button,
  Descriptions,
  Drawer,
  Space,
  Table,
  Tag,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { Gavel } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useServerTimezone } from "../../../hooks/useServerTimezone";
import { formatServerIsoDateTime } from "../../../utils/formatMessageTime";
import {
  workbuddyRuntimeApi,
  type ApprovalCandidate,
  type ApprovalRequestDetail,
} from "../../../api/modules/workbuddyRuntime";
import { isWorkBuddyUnavailableError } from "../../../api/modules/workbuddyWorkflows";
import {
  JsonPreview,
  LoadingBlock,
  LoadError,
  UnavailableNotice,
  statusLabel,
} from "../Workflows/consoleState";

const { Text } = Typography;

export default function ApprovalDetailDrawer({
  approvalRequestId,
  onClose,
  onDecide,
}: {
  approvalRequestId: string | null;
  onClose: () => void;
  onDecide: (detail: ApprovalRequestDetail) => void;
}) {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  const [detail, setDetail] = useState<ApprovalRequestDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    if (!approvalRequestId) {
      setDetail(null);
      setError(null);
      return;
    }
    let active = true;
    setLoading(true);
    workbuddyRuntimeApi
      .getApprovalRequest(approvalRequestId)
      .then((row) => {
        if (!active) return;
        setDetail(row);
        setError(null);
      })
      .catch((err: unknown) => {
        if (!active) return;
        setDetail(null);
        setError(err);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [approvalRequestId, nonce]);

  const retry = useCallback(() => setNonce((value) => value + 1), []);

  const candidateColumns: ColumnsType<ApprovalCandidate> = [
    {
      title: t("workbuddy.approvals.inbox.candidates"),
      dataIndex: "user_id",
      key: "user_id",
      render: (value: number) => <Text code>#{value}</Text>,
    },
    {
      title: t("workbuddy.approvals.inbox.column.status"),
      dataIndex: "status",
      key: "status",
      width: 160,
      render: (value: string) => (
        <Tag>
          {statusLabel(t, "workbuddy.approvals.inbox.candidateStatus", value)}
        </Tag>
      ),
    },
  ];

  return (
    <Drawer
      open={approvalRequestId !== null}
      onClose={onClose}
      destroyOnHidden
      width={720}
      title={t("workbuddy.approvals.inbox.detailTitle", {
        id: approvalRequestId?.slice(0, 8) ?? "",
      })}
      extra={
        detail && detail.status === "pending" ? (
          <Button
            type="primary"
            size="small"
            icon={<Gavel size={14} />}
            onClick={() => onDecide(detail)}
          >
            {t("workbuddy.approvals.inbox.decide")}
          </Button>
        ) : null
      }
    >
      {approvalRequestId === null ? null : loading ? (
        <LoadingBlock label={t("common.loading")} />
      ) : error !== null && error !== undefined ? (
        isWorkBuddyUnavailableError(error) ? (
          <UnavailableNotice
            error={error}
            errorFallback={t("workbuddy.approvals.state.errorFallback")}
            title={t("workbuddy.approvals.state.unavailableTitle")}
            hint={t("workbuddy.approvals.state.unavailableHint")}
            onRetry={retry}
          />
        ) : (
          <LoadError
            error={error}
            title={t("workbuddy.approvals.inbox.detailLoadFailed")}
            fallback={t("workbuddy.approvals.state.errorFallback")}
            onRetry={retry}
          />
        )
      ) : detail ? (
        <Space direction="vertical" size={14} style={{ width: "100%" }}>
          <Descriptions
            size="small"
            column={2}
            bordered
            title={t("workbuddy.approvals.inbox.overview")}
          >
            <Descriptions.Item
              label={t("workbuddy.approvals.inbox.column.status")}
            >
              <Tag color={detail.status === "pending" ? "gold" : "default"}>
                {statusLabel(
                  t,
                  "workbuddy.approvals.inbox.status",
                  detail.status,
                )}
              </Tag>
            </Descriptions.Item>
            <Descriptions.Item label={t("workbuddy.approvals.inbox.node")}>
              <Text code>{detail.node_id}</Text>
            </Descriptions.Item>
            <Descriptions.Item label={t("workbuddy.approvals.inbox.execution")}>
              <Text code>{detail.execution_id.slice(0, 8)}</Text>
            </Descriptions.Item>
            <Descriptions.Item label={t("workbuddy.approvals.inbox.required")}>
              {detail.required_approvals}
            </Descriptions.Item>
            <Descriptions.Item label={t("workbuddy.approvals.inbox.decided")}>
              {t("workbuddy.approvals.inbox.approvalCount", {
                decided: detail.decided_approvals,
                required: detail.required_approvals,
              })}
            </Descriptions.Item>
            <Descriptions.Item label={t("workbuddy.approvals.inbox.decision")}>
              {detail.decision
                ? statusLabel(
                    t,
                    "workbuddy.approvals.inbox.decisionValue",
                    detail.decision,
                  )
                : t("workbuddy.approvals.inbox.none")}
            </Descriptions.Item>
            <Descriptions.Item label={t("workbuddy.approvals.inbox.createdAt")}>
              {detail.created_at
                ? formatServerIsoDateTime(detail.created_at, timeZone)
                : "—"}
            </Descriptions.Item>
            <Descriptions.Item label={t("workbuddy.approvals.inbox.decidedAt")}>
              {detail.decided_at
                ? formatServerIsoDateTime(detail.decided_at, timeZone)
                : "—"}
            </Descriptions.Item>
            <Descriptions.Item
              label={t("workbuddy.approvals.inbox.challengeExpires")}
            >
              {detail.token_expires_at
                ? formatServerIsoDateTime(detail.token_expires_at, timeZone)
                : t("workbuddy.approvals.inbox.none")}
            </Descriptions.Item>
          </Descriptions>

          <div>
            <Text strong>{t("workbuddy.approvals.inbox.params")}</Text>
            <JsonPreview
              value={detail.params}
              emptyLabel={t("workbuddy.approvals.inbox.noParams")}
            />
          </div>

          <div>
            <Text strong>{t("workbuddy.approvals.inbox.candidates")}</Text>
            {detail.candidates.length === 0 ? (
              <Alert
                type="info"
                showIcon
                message={t("workbuddy.approvals.inbox.candidatesEmpty")}
              />
            ) : (
              <Table<ApprovalCandidate>
                rowKey="user_id"
                size="small"
                columns={candidateColumns}
                dataSource={detail.candidates}
                pagination={false}
              />
            )}
          </div>
        </Space>
      ) : null}
    </Drawer>
  );
}
