/**
 * WorkBuddy inbox → "questions waiting on me".
 *
 * The one list of questions the caller has to fill: the server returns the
 * questions they are an assignee of, in the scope and status asked for, and a
 * row opens the shared answer drawer — the same drawer the run list opens for a
 * run parked on ``waiting_input``. Nothing about the form is duplicated here.
 *
 * A question whose declared deadline has passed while it is still open gets an
 * explicit reminder on its row: the server keeps its own ``status`` as the
 * authority, so the row shows both facts instead of guessing.
 */

import { useCallback, useState } from "react";
import { Button, Segmented, Select, Space, Tag, Tooltip, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { Eye, PenLine, RefreshCw } from "lucide-react";
import { useTranslation } from "react-i18next";
import { ResizableTable } from "../../../components/ResizableTable";
import { useServerTimezone } from "../../../hooks/useServerTimezone";
import { formatServerIsoDateTime } from "../../../utils/formatMessageTime";
import {
  INPUT_REQUEST_STATUSES,
  workbuddyRuntimeApi,
  type InputRequest,
  type InputRequestStatus,
  type WorkBuddyScope,
} from "../../../api/modules/workbuddyRuntime";
import { TabPanelHeader } from "../../Settings/AdvancedSettings/TabPanelHeader";
import {
  ResourceState,
  statusLabel,
  useWorkBuddyResource,
} from "../Workflows/consoleState";
import AnswerDrawer from "./AnswerDrawer";
import styles from "./index.module.less";

const { Text } = Typography;

/** Tag colour per question status, mirroring the inbox's other panes. */
const STATUS_TAG_COLORS: Record<string, string> = {
  open: "gold",
  submitted: "green",
  expired: "red",
  invalidated: "default",
};

export default function InputInboxPanel() {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  const [scope, setScope] = useState<WorkBuddyScope>("self");
  const [status, setStatus] = useState<InputRequestStatus | "">("open");
  const [answerTarget, setAnswerTarget] = useState<{
    executionId: string;
    inputRequestId: string;
  } | null>(null);

  const requests = useWorkBuddyResource<InputRequest[]>(
    [],
    () =>
      workbuddyRuntimeApi.listInputRequests({
        scope,
        status,
        limit: 100,
      }),
    [scope, status],
  );

  const openAnswer = useCallback((row: InputRequest) => {
    setAnswerTarget({ executionId: row.execution_id, inputRequestId: row.id });
  }, []);

  const columns: ColumnsType<InputRequest> = [
    {
      title: t("workbuddy.inbox.inputs.column.status"),
      dataIndex: "status",
      key: "status",
      width: 140,
      render: (value: string) => (
        <Tag color={STATUS_TAG_COLORS[value]}>
          {statusLabel(t, "workbuddy.inbox.inputs.status", value)}
        </Tag>
      ),
    },
    {
      title: t("workbuddy.inbox.inputs.column.prompt"),
      dataIndex: "prompt",
      key: "prompt",
      width: 320,
      render: (value: string) => <Text>{value}</Text>,
    },
    {
      title: t("workbuddy.inbox.inputs.column.node"),
      dataIndex: "node_id",
      key: "node_id",
      width: 160,
      render: (value: string) => <Text code>{value}</Text>,
    },
    {
      title: t("workbuddy.inbox.inputs.column.execution"),
      dataIndex: "execution_id",
      key: "execution_id",
      width: 130,
      render: (value: string) => <Text code>{value.slice(0, 8)}</Text>,
    },
    {
      title: t("workbuddy.inbox.inputs.column.expiresAt"),
      dataIndex: "expires_at",
      key: "expires_at",
      width: 260,
      render: (value: string | null, row) => (
        <Space size={4} wrap>
          <span>
            {value ? formatServerIsoDateTime(value, timeZone) : "—"}
          </span>
          {row.status === "open" &&
            value !== null &&
            Date.parse(value) < Date.now() && (
              <Tooltip title={t("workbuddy.inbox.inputs.overdueHint")}>
                <Tag color="red">{t("workbuddy.inbox.inputs.overdue")}</Tag>
              </Tooltip>
            )}
        </Space>
      ),
    },
    {
      title: t("workbuddy.inbox.inputs.column.createdAt"),
      dataIndex: "created_at",
      key: "created_at",
      width: 170,
      render: (value: string) =>
        value ? formatServerIsoDateTime(value, timeZone) : "—",
    },
    {
      title: t("workbuddy.inbox.inputs.column.actions"),
      key: "actions",
      width: 140,
      fixed: "right",
      render: (_value, row) => (
        <Button
          type="link"
          size="small"
          icon={row.status === "open" ? <PenLine size={13} /> : <Eye size={13} />}
          onClick={() => openAnswer(row)}
        >
          {row.status === "open"
            ? t("workbuddy.inbox.inputs.answer")
            : t("workbuddy.inbox.inputs.view")}
        </Button>
      ),
    },
  ];

  const failed = requests.error !== null && requests.error !== undefined;
  const blocked = failed || requests.loading || requests.data.length === 0;

  return (
    <div className={styles.panel}>
      <TabPanelHeader
        icon={<PenLine size={16} />}
        title={t("workbuddy.inbox.inputs.title")}
        description={t("workbuddy.inbox.inputs.description")}
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
                  label: t("workbuddy.inbox.inputs.scope.self"),
                },
                {
                  value: "tenant",
                  label: t("workbuddy.inbox.inputs.scope.tenant"),
                },
              ]}
            />
            <Select<InputRequestStatus | "">
              size="small"
              style={{ minWidth: 160 }}
              value={status}
              onChange={setStatus}
              options={[
                {
                  value: "",
                  label: t("workbuddy.inbox.inputs.filterAll"),
                },
                ...INPUT_REQUEST_STATUSES.map((value) => ({
                  value,
                  label: statusLabel(
                    t,
                    "workbuddy.inbox.inputs.status",
                    value,
                  ),
                })),
              ]}
            />
            <Button
              size="small"
              icon={<RefreshCw size={14} />}
              onClick={() => void requests.reload()}
            >
              {t("common.refresh")}
            </Button>
          </Space>
        }
      />

      {blocked ? (
        <ResourceState
          resource={requests}
          isEmpty={!failed && !requests.loading && requests.data.length === 0}
          loadingLabel={t("common.loading")}
          errorTitle={t("workbuddy.inbox.inputs.loadFailed")}
          errorFallback={t("workbuddy.inbox.state.errorFallback")}
          unavailableTitle={t("workbuddy.shared.notMergedTitle")}
          unavailableHint={t("workbuddy.shared.notMergedHint")}
          emptyTitle={t("workbuddy.inbox.inputs.emptyTitle")}
          emptyHint={t("workbuddy.inbox.inputs.emptyHint")}
        />
      ) : (
        <ResizableTable<InputRequest>
          rowKey="id"
          size="small"
          columns={columns}
          dataSource={requests.data}
          pagination={false}
          scroll={{ x: 1320 }}
          storageKey="workbuddy-inbox-inputs"
        />
      )}

      <AnswerDrawer
        executionId={answerTarget?.executionId ?? null}
        inputRequestId={answerTarget?.inputRequestId ?? null}
        onClose={() => setAnswerTarget(null)}
        onAnswered={() => void requests.reload()}
      />
    </div>
  );
}
