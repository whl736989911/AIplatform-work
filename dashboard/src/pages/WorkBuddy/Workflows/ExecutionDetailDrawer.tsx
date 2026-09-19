/**
 * WorkBuddy console → Workflows → one execution: steps, edge decisions, the
 * recorded outputs and the external-write reconciliation evidence.
 *
 * Reconciliation is a controlled admin action over a tool/model step; the form
 * sends exactly what the operator typed and shows the server's refusal
 * verbatim (never an invented success).
 */

import { useCallback, useState } from "react";
import {
  Alert,
  Button,
  Descriptions,
  Drawer,
  Form,
  Input,
  Modal,
  Select,
  Space,
  Table,
  Tag,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { message } from "@/utils/antdMessage";
import { FileCheck2, Plus } from "lucide-react";
import { useTranslation } from "react-i18next";
import { apiErrorMessage } from "../../../utils/apiError";
import { useServerTimezone } from "../../../hooks/useServerTimezone";
import { formatServerIsoDateTime } from "../../../utils/formatMessageTime";
import {
  RECONCILIATION_STATUSES,
  workbuddyRuntimeApi,
  type ExecutionDetail,
  type ExecutionEdge,
  type ExecutionStep,
  type Reconciliation,
  type ReconciliationStatus,
  type WorkflowNodeType,
} from "../../../api/modules/workbuddyRuntime";
import {
  isWorkBuddyUnavailableError,
  type JsonObject,
} from "../../../api/modules/workbuddyWorkflows";
import {
  JsonPreview,
  LoadingBlock,
  LoadError,
  UnavailableNotice,
  statusLabel,
  useWorkBuddyResource,
} from "./consoleState";
import styles from "./index.module.less";

const { Text } = Typography;

interface ReconciliationFormValues {
  node_id: string;
  status: ReconciliationStatus;
  evidence: string;
  external_ref?: string;
}

export default function ExecutionDetailDrawer({
  executionId,
  onClose,
}: {
  executionId: string | null;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  return (
    <Drawer
      open={executionId !== null}
      onClose={onClose}
      destroyOnHidden
      width={880}
      title={t("workbuddy.workflows.detail.title", {
        id: executionId?.slice(0, 8) ?? "",
      })}
    >
      {executionId ? <ExecutionDetailBody executionId={executionId} /> : null}
    </Drawer>
  );
}

function ExecutionDetailBody({ executionId }: { executionId: string }) {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  const execution = useWorkBuddyResource<ExecutionDetail | null>(
    null,
    () => workbuddyRuntimeApi.getExecution(executionId),
    [executionId],
  );

  const { data, loading, error } = execution;
  const failed = error !== null && error !== undefined;

  const stepColumns: ColumnsType<ExecutionStep> = [
    {
      title: t("workbuddy.workflows.detail.stepColumns.node"),
      dataIndex: "node_id",
      key: "node_id",
      width: 180,
      render: (value: string) => <Text code>{value}</Text>,
    },
    {
      title: t("workbuddy.workflows.detail.stepColumns.type"),
      dataIndex: "node_type",
      key: "node_type",
      width: 120,
      render: (value: WorkflowNodeType) =>
        statusLabel(t, "workbuddy.workflows.detail.nodeType", value),
    },
    {
      title: t("workbuddy.workflows.detail.stepColumns.attempt"),
      dataIndex: "attempt",
      key: "attempt",
      width: 90,
    },
    {
      title: t("workbuddy.workflows.detail.stepColumns.status"),
      dataIndex: "status",
      key: "status",
      width: 150,
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
          {statusLabel(t, "workbuddy.workflows.detail.stepStatus", value)}
        </Tag>
      ),
    },
    {
      title: t("workbuddy.workflows.detail.stepColumns.saveAs"),
      dataIndex: "save_as",
      key: "save_as",
      width: 140,
      render: (value: string | null) =>
        value ? <Text code>{value}</Text> : <Text type="secondary">—</Text>,
    },
    {
      title: t("workbuddy.workflows.detail.stepColumns.error"),
      dataIndex: "error_code",
      key: "error_code",
      width: 180,
      render: (value: string | null) =>
        value ? (
          <Text type="danger">{value}</Text>
        ) : (
          <Text type="secondary">—</Text>
        ),
    },
  ];

  const edgeColumns: ColumnsType<ExecutionEdge> = [
    {
      title: t("workbuddy.workflows.detail.edgeColumns.from"),
      dataIndex: "from",
      key: "from",
      render: (value: string) => <Text code>{value}</Text>,
    },
    {
      title: t("workbuddy.workflows.detail.edgeColumns.to"),
      dataIndex: "to",
      key: "to",
      render: (value: string) => <Text code>{value}</Text>,
    },
    {
      title: t("workbuddy.workflows.detail.edgeColumns.branch"),
      dataIndex: "branch",
      key: "branch",
      width: 100,
      render: (value: string | null) =>
        value ?? <Text type="secondary">—</Text>,
    },
    {
      title: t("workbuddy.workflows.detail.edgeColumns.taken"),
      dataIndex: "taken",
      key: "taken",
      width: 100,
      render: (value: boolean) =>
        value
          ? t("workbuddy.workflows.detail.takenYes")
          : t("workbuddy.workflows.detail.takenNo"),
    },
  ];

  if (failed) {
    return isWorkBuddyUnavailableError(error) ? (
      <UnavailableNotice
        error={error}
        errorFallback={t("workbuddy.workflows.state.errorFallback")}
        title={t("workbuddy.workflows.state.unavailableTitle")}
        hint={t("workbuddy.workflows.state.unavailableHint")}
        onRetry={() => void execution.reload()}
      />
    ) : (
      <LoadError
        error={error}
        title={t("workbuddy.workflows.detail.loadFailed")}
        fallback={t("workbuddy.workflows.state.errorFallback")}
        onRetry={() => void execution.reload()}
      />
    );
  }
  if (loading || !data) return <LoadingBlock label={t("common.loading")} />;

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <Descriptions
        size="small"
        column={2}
        bordered
        title={t("workbuddy.workflows.detail.overview")}
      >
        <Descriptions.Item
          label={t("workbuddy.workflows.detail.executionStatus")}
        >
          <Tag
            color={
              data.status === "succeeded"
                ? "green"
                : data.status === "failed"
                ? "red"
                : data.status === "waiting_approval"
                ? "gold"
                : "default"
            }
          >
            {statusLabel(
              t,
              "workbuddy.workflows.executions.status",
              data.status,
            )}
          </Tag>
        </Descriptions.Item>
        <Descriptions.Item label={t("workbuddy.workflows.detail.trigger")}>
          {statusLabel(
            t,
            "workbuddy.workflows.executions.trigger",
            data.trigger_type,
          )}
        </Descriptions.Item>
        <Descriptions.Item label={t("workbuddy.workflows.detail.version")}>
          <Text code>{data.workflow_version_id.slice(0, 8)}</Text>
        </Descriptions.Item>
        <Descriptions.Item label={t("workbuddy.workflows.detail.startedBy")}>
          {data.created_by_user_id ?? "—"}
        </Descriptions.Item>
        <Descriptions.Item label={t("workbuddy.workflows.detail.createdAt")}>
          {data.created_at
            ? formatServerIsoDateTime(data.created_at, timeZone)
            : "—"}
        </Descriptions.Item>
        <Descriptions.Item label={t("workbuddy.workflows.detail.startedAt")}>
          {data.started_at
            ? formatServerIsoDateTime(data.started_at, timeZone)
            : "—"}
        </Descriptions.Item>
        <Descriptions.Item label={t("workbuddy.workflows.detail.finishedAt")}>
          {data.finished_at
            ? formatServerIsoDateTime(data.finished_at, timeZone)
            : "—"}
        </Descriptions.Item>
      </Descriptions>

      {(data.error_code || data.error_message) && (
        <Alert
          type="error"
          showIcon
          message={data.error_code ?? t("workbuddy.workflows.detail.error")}
          description={data.error_message ?? ""}
        />
      )}

      <div>
        <Text strong>{t("workbuddy.workflows.detail.inputs")}</Text>
        <JsonPreview
          value={data.inputs}
          emptyLabel={t("workbuddy.workflows.detail.noPayload")}
        />
      </div>
      <div>
        <Text strong>{t("workbuddy.workflows.detail.outputs")}</Text>
        <JsonPreview
          value={data.outputs}
          emptyLabel={t("workbuddy.workflows.detail.noPayload")}
        />
      </div>

      <div>
        <Text strong>{t("workbuddy.workflows.detail.steps")}</Text>
        {data.steps.length === 0 ? (
          <Text type="secondary" className={styles.blockHint}>
            {t("workbuddy.workflows.detail.stepsEmpty")}
          </Text>
        ) : (
          <Table<ExecutionStep>
            rowKey={(row) => `${row.node_id}-${row.attempt}`}
            size="small"
            columns={stepColumns}
            dataSource={data.steps}
            pagination={false}
            scroll={{ x: 860 }}
          />
        )}
      </div>

      <div>
        <Text strong>{t("workbuddy.workflows.detail.edges")}</Text>
        {data.edges.length === 0 ? (
          <Text type="secondary" className={styles.blockHint}>
            {t("workbuddy.workflows.detail.edgesEmpty")}
          </Text>
        ) : (
          <Table<ExecutionEdge>
            rowKey={(row) => `${row.from}-${row.to}-${row.branch ?? ""}`}
            size="small"
            columns={edgeColumns}
            dataSource={data.edges}
            pagination={false}
            scroll={{ x: 640 }}
          />
        )}
      </div>

      <ReconciliationSection executionId={executionId} steps={data.steps} />
    </Space>
  );
}

function ReconciliationSection({
  executionId,
  steps,
}: {
  executionId: string;
  steps: ExecutionStep[];
}) {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  const reconciliations = useWorkBuddyResource<Reconciliation[]>(
    [],
    () => workbuddyRuntimeApi.listReconciliations(executionId),
    [executionId],
  );
  const [form] = Form.useForm<ReconciliationFormValues>();
  const [recording, setRecording] = useState(false);
  const [open, setOpen] = useState(false);

  const record = useCallback(async () => {
    let values: ReconciliationFormValues;
    try {
      values = await form.validateFields();
    } catch {
      return;
    }
    let evidence: JsonObject;
    try {
      const parsed: unknown = JSON.parse(values.evidence);
      if (
        typeof parsed !== "object" ||
        parsed === null ||
        Array.isArray(parsed)
      ) {
        message.error(t("workbuddy.workflows.detail.evidenceInvalid"));
        return;
      }
      evidence = parsed as JsonObject;
    } catch (err) {
      message.error(
        err instanceof Error
          ? err.message
          : t("workbuddy.workflows.detail.evidenceInvalid"),
      );
      return;
    }
    setRecording(true);
    try {
      await workbuddyRuntimeApi.recordReconciliation(executionId, {
        node_id: values.node_id,
        status: values.status,
        evidence,
        external_ref: values.external_ref?.trim() || null,
      });
      message.success(t("workbuddy.workflows.detail.recorded"));
      setOpen(false);
      form.resetFields();
      await reconciliations.reload();
    } catch (err) {
      message.error(
        apiErrorMessage(err, t("workbuddy.workflows.detail.recordFailed"), t),
      );
    } finally {
      setRecording(false);
    }
  }, [executionId, form, reconciliations, t]);

  const columns: ColumnsType<Reconciliation> = [
    {
      title: t("workbuddy.workflows.detail.reconcileNode"),
      dataIndex: "node_id",
      key: "node_id",
      render: (value: string) => <Text code>{value}</Text>,
    },
    {
      title: t("workbuddy.workflows.detail.reconcileStatus"),
      dataIndex: "status",
      key: "status",
      width: 140,
      render: (value: string) => (
        <Tag color={value === "matched" ? "green" : "default"}>
          {statusLabel(t, "workbuddy.workflows.detail.status", value)}
        </Tag>
      ),
    },
    {
      title: t("workbuddy.workflows.detail.reconcileExternalRef"),
      dataIndex: "external_ref",
      key: "external_ref",
      width: 200,
      render: (value: string | null) =>
        value ?? <Text type="secondary">—</Text>,
    },
    {
      title: t("workbuddy.workflows.detail.createdAt"),
      dataIndex: "created_at",
      key: "created_at",
      width: 170,
      render: (value: string | null) =>
        value ? formatServerIsoDateTime(value, timeZone) : "—",
    },
  ];

  const reconcilableSteps = steps
    .filter((step) => step.node_type === "tool" || step.node_type === "llm")
    .map((step) => ({ value: step.node_id, label: step.node_id }));

  return (
    <div>
      <Space size={8} align="center">
        <FileCheck2 size={14} />
        <Text strong>{t("workbuddy.workflows.detail.reconciliations")}</Text>
        <Button
          size="small"
          icon={<Plus size={13} />}
          disabled={reconcilableSteps.length === 0}
          onClick={() => {
            form.setFieldsValue({
              node_id: reconcilableSteps[0]?.value ?? "",
              status: "matched",
              evidence: "{}",
              external_ref: "",
            });
            setOpen(true);
          }}
        >
          {t("workbuddy.workflows.detail.record")}
        </Button>
      </Space>
      <Text type="secondary" className={styles.blockHint}>
        {t("workbuddy.workflows.detail.reconciliationHint")}
      </Text>

      {reconciliations.error !== null && reconciliations.error !== undefined ? (
        <LoadError
          error={reconciliations.error}
          title={t("workbuddy.workflows.detail.loadFailed")}
          fallback={t("workbuddy.workflows.state.errorFallback")}
          onRetry={() => void reconciliations.reload()}
        />
      ) : reconciliations.loading ? (
        <LoadingBlock label={t("common.loading")} />
      ) : reconciliations.data.length === 0 ? (
        <Text type="secondary" className={styles.blockHint}>
          {t("workbuddy.workflows.detail.reconciliationsEmpty")}
        </Text>
      ) : (
        <Table<Reconciliation>
          rowKey="id"
          size="small"
          columns={columns}
          dataSource={reconciliations.data}
          pagination={false}
          scroll={{ x: 700 }}
        />
      )}

      <Modal
        open={open}
        onCancel={() => setOpen(false)}
        destroyOnHidden
        maskClosable={false}
        title={t("workbuddy.workflows.detail.record")}
        footer={
          <Button
            type="primary"
            loading={recording}
            onClick={() => void record()}
          >
            {t("workbuddy.workflows.detail.reconcileSubmit")}
          </Button>
        }
      >
        <Form form={form} layout="vertical" requiredMark={false}>
          <Form.Item
            name="node_id"
            label={t("workbuddy.workflows.detail.reconcileNode")}
            rules={[
              {
                required: true,
                message: t("workbuddy.workflows.detail.nodeRequired"),
              },
            ]}
          >
            <Select options={reconcilableSteps} />
          </Form.Item>
          <Form.Item
            name="status"
            label={t("workbuddy.workflows.detail.reconcileStatus")}
          >
            <Select
              options={RECONCILIATION_STATUSES.map((status) => ({
                value: status,
                label: statusLabel(
                  t,
                  "workbuddy.workflows.detail.status",
                  status,
                ),
              }))}
            />
          </Form.Item>
          <Form.Item
            name="evidence"
            label={t("workbuddy.workflows.detail.reconcileEvidence")}
          >
            <Input.TextArea
              autoSize={{ minRows: 6, maxRows: 14 }}
              spellCheck={false}
              className={styles.jsonEditor}
            />
          </Form.Item>
          <Form.Item
            name="external_ref"
            label={t("workbuddy.workflows.detail.reconcileExternalRef")}
          >
            <Input maxLength={200} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
