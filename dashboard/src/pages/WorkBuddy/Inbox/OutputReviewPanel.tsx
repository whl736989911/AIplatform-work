/**
 * WorkBuddy inbox → "outputs waiting on my review".
 *
 * The one queue of reviews the caller was asked to do: the server returns the
 * reviews they are a reviewer of, in the scope and status asked for, and a row
 * opens the shared review drawer — the same drawer the runs page opens for a
 * settled execution. Nothing about the decision is duplicated here.
 *
 * A review never holds a run. The execution settled before the review existed
 * and its outputs were copied into it, so this pane offers no "release" and no
 * "block": a row is a result somebody has to read, and the drawer records what
 * was said about it.
 *
 * A review the route lists carries no reviewer rows — the queue answers without
 * them — so an open review shows as "assigned to me" in the self scope (which is
 * exactly what that scope is), and the drawer reads the full roster.
 */

import { useCallback, useState } from "react";
import { Alert, Button, Segmented, Select, Space, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { BadgeCheck, Eye, RefreshCw } from "lucide-react";
import { useTranslation } from "react-i18next";
import { ResizableTable } from "../../../components/ResizableTable";
import { useServerTimezone } from "../../../hooks/useServerTimezone";
import { formatServerIsoDateTime } from "../../../utils/formatMessageTime";
import {
  OUTPUT_REVIEW_STATUSES,
  workbuddyRuntimeApi,
  type OutputReview,
  type OutputReviewStatus,
  type WorkBuddyScope,
} from "../../../api/modules/workbuddyRuntime";
import { TabPanelHeader } from "../../Settings/AdvancedSettings/TabPanelHeader";
import {
  ResourceState,
  statusLabel,
  useWorkBuddyResource,
} from "../Workflows/consoleState";
import OutputReviewDrawer from "./OutputReviewDrawer";
import styles from "./index.module.less";

const { Text } = Typography;

/** Tag colour per review status, mirroring the inbox's other panes. */
const STATUS_TAG_COLORS: Record<string, string> = {
  open: "gold",
  accepted: "green",
  corrected: "blue",
  rerun: "purple",
};

export default function OutputReviewPanel() {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  const [scope, setScope] = useState<WorkBuddyScope>("self");
  const [status, setStatus] = useState<OutputReviewStatus | "">("open");
  const [reviewExecutionId, setReviewExecutionId] = useState<string | null>(
    null,
  );

  const reviews = useWorkBuddyResource<OutputReview[]>(
    [],
    () =>
      workbuddyRuntimeApi.listOutputReviews({
        scope,
        status,
        limit: 100,
      }),
    [scope, status],
  );

  const openReview = useCallback((row: OutputReview) => {
    setReviewExecutionId(row.execution_id);
  }, []);

  const columns: ColumnsType<OutputReview> = [
    {
      title: t("workbuddy.inbox.reviews.column.status"),
      dataIndex: "status",
      key: "status",
      width: 200,
      render: (value: string) => (
        <Space size={4} wrap>
          <Tag color={STATUS_TAG_COLORS[value]}>
            {statusLabel(t, "workbuddy.inbox.reviews.status", value)}
          </Tag>
          {scope === "self" && (
            <Tag color="blue">{t("workbuddy.inbox.reviews.assignedToMe")}</Tag>
          )}
        </Space>
      ),
    },
    {
      title: t("workbuddy.inbox.reviews.column.execution"),
      dataIndex: "execution_id",
      key: "execution_id",
      width: 130,
      render: (value: string) => <Text code>{value.slice(0, 8)}</Text>,
    },
    {
      title: t("workbuddy.inbox.reviews.column.produced"),
      key: "produced",
      width: 320,
      // The keys the run produced, not their values: the row says what is under
      // review, and the drawer shows the payload itself.
      render: (_value, row) => {
        const keys = Object.keys(row.produced);
        if (keys.length === 0) {
          return (
            <Text type="secondary">
              {t("workbuddy.inbox.reviews.producedEmpty")}
            </Text>
          );
        }
        return (
          <Space size={4} wrap>
            {keys.map((key) => (
              <Tag key={key}>{key}</Tag>
            ))}
          </Space>
        );
      },
    },
    {
      title: t("workbuddy.inbox.reviews.column.requestedBy"),
      dataIndex: "requested_by_user_id",
      key: "requested_by_user_id",
      width: 120,
      render: (value: number | null) =>
        value === null ? (
          <Text type="secondary">{t("workbuddy.inbox.reviews.none")}</Text>
        ) : (
          `#${value}`
        ),
    },
    {
      title: t("workbuddy.inbox.reviews.column.createdAt"),
      dataIndex: "created_at",
      key: "created_at",
      width: 180,
      render: (value: string) =>
        value ? formatServerIsoDateTime(value, timeZone) : "—",
    },
    {
      title: t("workbuddy.inbox.reviews.column.actions"),
      key: "actions",
      width: 130,
      fixed: "right",
      render: (_value, row) => (
        <Button
          type="link"
          size="small"
          icon={
            row.status === "open" ? (
              <BadgeCheck size={13} />
            ) : (
              <Eye size={13} />
            )
          }
          onClick={() => openReview(row)}
        >
          {row.status === "open"
            ? t("workbuddy.inbox.reviews.review")
            : t("workbuddy.inbox.reviews.view")}
        </Button>
      ),
    },
  ];

  const failed = reviews.error !== null && reviews.error !== undefined;
  const blocked = failed || reviews.loading || reviews.data.length === 0;

  return (
    <div className={styles.panel}>
      <TabPanelHeader
        icon={<BadgeCheck size={16} />}
        title={t("workbuddy.inbox.reviews.title")}
        description={t("workbuddy.inbox.reviews.description")}
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
                  label: t("workbuddy.inbox.reviews.scope.self"),
                },
                {
                  value: "tenant",
                  label: t("workbuddy.inbox.reviews.scope.tenant"),
                },
              ]}
            />
            <Select<OutputReviewStatus | "">
              size="small"
              style={{ minWidth: 160 }}
              value={status}
              onChange={setStatus}
              options={[
                {
                  value: "",
                  label: t("workbuddy.inbox.reviews.filterAll"),
                },
                ...OUTPUT_REVIEW_STATUSES.map((value) => ({
                  value,
                  label: statusLabel(
                    t,
                    "workbuddy.inbox.reviews.status",
                    value,
                  ),
                })),
              ]}
            />
            <Button
              size="small"
              icon={<RefreshCw size={14} />}
              onClick={() => void reviews.reload()}
            >
              {t("common.refresh")}
            </Button>
          </Space>
        }
      />

      {/* The tenant scope answers for an admin only and is not limited to this
          caller's own queue; say so rather than let a wider list read as mine. */}
      {scope === "tenant" && (
        <Alert
          className={styles.notice}
          type="info"
          showIcon
          message={t("workbuddy.inbox.reviews.tenantHint")}
        />
      )}

      {blocked ? (
        <ResourceState
          resource={reviews}
          isEmpty={!failed && !reviews.loading && reviews.data.length === 0}
          loadingLabel={t("common.loading")}
          errorTitle={t("workbuddy.inbox.reviews.loadFailed")}
          errorFallback={t("workbuddy.inbox.state.errorFallback")}
          unavailableTitle={t("workbuddy.shared.notMergedTitle")}
          unavailableHint={t("workbuddy.shared.notMergedHint")}
          emptyTitle={t("workbuddy.inbox.reviews.emptyTitle")}
          emptyHint={t("workbuddy.inbox.reviews.emptyHint")}
        />
      ) : (
        <ResizableTable<OutputReview>
          rowKey="id"
          size="small"
          columns={columns}
          dataSource={reviews.data}
          pagination={false}
          scroll={{ x: 1200 }}
          storageKey="workbuddy-inbox-reviews"
        />
      )}

      <OutputReviewDrawer
        executionId={reviewExecutionId}
        onClose={() => setReviewExecutionId(null)}
        onDecided={() => void reviews.reload()}
      />
    </div>
  );
}
