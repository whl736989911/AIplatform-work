/**
 * WorkBuddy console → Workflows → Jobs.
 *
 * Asynchronous jobs the runtime created on the caller's behalf (executions,
 * proposals, imports, trigger deliveries). The detail view re-reads the single
 * job from the server so a finished job is never shown from a stale list row.
 */

import { useCallback, useState } from "react";
import {
  Alert,
  Button,
  Modal,
  Progress,
  Select,
  Space,
  Tag,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { message } from "@/utils/antdMessage";
import { Eye, ListChecks, RefreshCw } from "lucide-react";
import { useTranslation } from "react-i18next";
import { ResizableTable } from "../../../components/ResizableTable";
import { apiErrorMessage } from "../../../utils/apiError";
import { useServerTimezone } from "../../../hooks/useServerTimezone";
import { formatServerIsoDateTime } from "../../../utils/formatMessageTime";
import {
  JOB_STATUSES,
  workbuddyWorkflowsApi,
  type Job,
  type JobStatus,
} from "../../../api/modules/workbuddyWorkflows";
import { TabPanelHeader } from "../../Settings/AdvancedSettings/TabPanelHeader";
import {
  JsonPreview,
  ResourceState,
  statusLabel,
  useWorkBuddyResource,
} from "./consoleState";
import styles from "./index.module.less";

const { Text } = Typography;

export default function JobsPanel() {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  const [status, setStatus] = useState<JobStatus | "">("");
  const [detail, setDetail] = useState<Job | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  const jobs = useWorkBuddyResource<Job[]>(
    [],
    () => workbuddyWorkflowsApi.listJobs({ status, limit: 100 }),
    [status],
  );

  const openDetail = useCallback(
    async (id: string) => {
      setDetailLoading(true);
      try {
        setDetail(await workbuddyWorkflowsApi.getJob(id));
      } catch (err) {
        message.error(
          apiErrorMessage(
            err,
            t("workbuddy.workflows.jobs.detailLoadFailed"),
            t,
          ),
        );
      } finally {
        setDetailLoading(false);
      }
    },
    [t],
  );

  const columns: ColumnsType<Job> = [
    {
      title: t("workbuddy.workflows.jobs.column.id"),
      dataIndex: "id",
      key: "id",
      width: 120,
      render: (value: string) => <Text code>{value.slice(0, 8)}</Text>,
    },
    {
      title: t("workbuddy.workflows.jobs.column.kind"),
      dataIndex: "kind",
      key: "kind",
      width: 180,
      render: (value: string) => (
        <Tag>{statusLabel(t, "workbuddy.workflows.jobs.kind", value)}</Tag>
      ),
    },
    {
      title: t("workbuddy.workflows.jobs.column.status"),
      dataIndex: "status",
      key: "status",
      width: 130,
      render: (value: string) => (
        <Tag
          color={
            value === "succeeded"
              ? "green"
              : value === "failed"
              ? "red"
              : "default"
          }
        >
          {statusLabel(t, "workbuddy.workflows.jobs.status", value)}
        </Tag>
      ),
    },
    {
      title: t("workbuddy.workflows.jobs.column.progress"),
      dataIndex: "progress",
      key: "progress",
      width: 160,
      render: (value: number) => (
        <Progress percent={value} size="small" status="normal" />
      ),
    },
    {
      title: t("workbuddy.workflows.jobs.column.execution"),
      dataIndex: "execution_id",
      key: "execution_id",
      width: 130,
      render: (value: string | null) =>
        value ? (
          <Text code>{value.slice(0, 8)}</Text>
        ) : (
          <Text type="secondary">—</Text>
        ),
    },
    {
      title: t("workbuddy.workflows.jobs.column.createdAt"),
      dataIndex: "created_at",
      key: "created_at",
      width: 170,
      render: (value: string | null) =>
        value ? formatServerIsoDateTime(value, timeZone) : "—",
    },
    {
      title: t("workbuddy.workflows.jobs.column.finishedAt"),
      dataIndex: "finished_at",
      key: "finished_at",
      width: 170,
      render: (value: string | null) =>
        value ? formatServerIsoDateTime(value, timeZone) : "—",
    },
    {
      title: t("workbuddy.workflows.jobs.column.actions"),
      key: "actions",
      width: 120,
      fixed: "right",
      render: (_value, row) => (
        <Button
          type="link"
          size="small"
          icon={<Eye size={13} />}
          loading={detailLoading}
          onClick={() => void openDetail(row.id)}
        >
          {t("workbuddy.workflows.jobs.detail")}
        </Button>
      ),
    },
  ];

  const failed = jobs.error !== null && jobs.error !== undefined;
  const blocked = failed || jobs.loading || jobs.data.length === 0;

  return (
    <div className={styles.panel}>
      <TabPanelHeader
        icon={<ListChecks size={16} />}
        title={t("workbuddy.workflows.jobs.title")}
        description={t("workbuddy.workflows.jobs.description")}
        actions={
          <Space size={8}>
            <Select<JobStatus | "">
              size="small"
              style={{ minWidth: 160 }}
              value={status}
              onChange={setStatus}
              options={[
                {
                  value: "",
                  label: t("workbuddy.workflows.jobs.filterAll"),
                },
                ...JOB_STATUSES.map((value) => ({
                  value,
                  label: statusLabel(
                    t,
                    "workbuddy.workflows.jobs.status",
                    value,
                  ),
                })),
              ]}
            />
            <Button
              size="small"
              icon={<RefreshCw size={14} />}
              onClick={() => void jobs.reload()}
            >
              {t("common.refresh")}
            </Button>
          </Space>
        }
      />

      {blocked ? (
        <ResourceState
          resource={jobs}
          isEmpty={!failed && !jobs.loading && jobs.data.length === 0}
          loadingLabel={t("common.loading")}
          errorTitle={t("workbuddy.workflows.jobs.loadFailed")}
          errorFallback={t("workbuddy.workflows.state.errorFallback")}
          unavailableTitle={t("workbuddy.workflows.state.unavailableTitle")}
          unavailableHint={t("workbuddy.workflows.state.unavailableHint")}
          emptyTitle={t("workbuddy.workflows.jobs.emptyTitle")}
          emptyHint={t("workbuddy.workflows.jobs.emptyHint")}
        />
      ) : (
        <ResizableTable<Job>
          rowKey="id"
          size="small"
          columns={columns}
          dataSource={jobs.data}
          pagination={false}
          scroll={{ x: 1180 }}
          storageKey="workbuddy-workflows-jobs"
        />
      )}

      <Modal
        open={detail !== null}
        onCancel={() => setDetail(null)}
        destroyOnHidden
        footer={null}
        width={720}
        title={t("workbuddy.workflows.jobs.detailTitle", {
          id: detail?.id.slice(0, 8) ?? "",
        })}
      >
        {detail && (
          <Space direction="vertical" size={12} style={{ width: "100%" }}>
            <Space size={8} wrap>
              <Tag>
                {statusLabel(t, "workbuddy.workflows.jobs.kind", detail.kind)}
              </Tag>
              <Tag
                color={
                  detail.status === "succeeded"
                    ? "green"
                    : detail.status === "failed"
                    ? "red"
                    : "default"
                }
              >
                {statusLabel(
                  t,
                  "workbuddy.workflows.jobs.status",
                  detail.status,
                )}
              </Tag>
              <Text type="secondary">
                {formatServerIsoDateTime(detail.created_at ?? "", timeZone)}
              </Text>
            </Space>
            <Progress percent={detail.progress} status="normal" />
            {(detail.error_code || detail.error_message) && (
              <Alert
                type="error"
                showIcon
                message={
                  detail.error_code ?? t("workbuddy.workflows.jobs.error")
                }
                description={detail.error_message ?? ""}
              />
            )}
            <div>
              <Text strong>{t("workbuddy.workflows.jobs.result")}</Text>
              <JsonPreview
                value={detail.result}
                emptyLabel={t("workbuddy.workflows.jobs.noResult")}
              />
            </div>
          </Space>
        )}
      </Modal>
    </div>
  );
}
