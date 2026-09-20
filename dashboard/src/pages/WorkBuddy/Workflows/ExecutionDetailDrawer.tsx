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
  Collapse,
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
import { formatDurationMs } from "../../Chat/utils/trajectoryModel";
import {
  RECONCILIATION_DECISIONS,
  workbuddyRuntimeApi,
  type ExecutionDetail,
  type ExecutionEdge,
  type ExecutionStep,
  type Reconciliation,
  type ReconciliationDecision,
  type StepStatus,
  type TokenUsage,
  type WorkflowNodeType,
} from "../../../api/modules/workbuddyRuntime";
import { isWorkBuddyUnavailableError } from "../../../api/modules/workbuddyWorkflows";
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

/** Step status → tag colour, the same palette the executions list uses. */
const STEP_STATUS_COLORS: Record<StepStatus, string> = {
  queued: "default",
  running: "blue",
  waiting_approval: "gold",
  waiting_reconciliation: "orange",
  success: "green",
  failed: "red",
  skipped: "default",
  canceled: "default",
};

/** Adapter usage keys, read in order: the canonical names, then the aliases. */
const TOKEN_KEYS = {
  total: ["total_tokens"],
  input: ["input_tokens", "prompt_tokens"],
  output: ["output_tokens", "completion_tokens"],
} as const;

/**
 * Evidence is submitted by reference, and that reference is a payload row id —
 * migration 023 makes the column a uuid, so a malformed reference is refused
 * before the server has to answer for it.
 */
const EVIDENCE_REF_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function countFrom(usage: TokenUsage, keys: readonly string[]): number | null {
  for (const key of keys) {
    const value = usage[key];
    if (typeof value === "number" && Number.isFinite(value)) return value;
  }
  return null;
}

/**
 * The adapter usage one step recorded, as key/value pairs: the canonical total /
 * input / output, resolved through the aliases model adapters use. A counter the
 * adapter did not report renders as the console's dash, and a step that reported
 * no usage at all — every non-model node — says so instead of showing zeros.
 */
function TokenUsageBlock({
  usage,
  title,
}: {
  usage: TokenUsage | null | undefined;
  title: string;
}) {
  const { t } = useTranslation();
  return (
    <div>
      <Text strong>{title}</Text>
      {usage ? (
        <Descriptions size="small" column={3} bordered>
          <Descriptions.Item
            label={t("workbuddy.workflows.detail.tokensTotal")}
          >
            {countFrom(usage, TOKEN_KEYS.total) ?? "—"}
          </Descriptions.Item>
          <Descriptions.Item
            label={t("workbuddy.workflows.detail.tokensInput")}
          >
            {countFrom(usage, TOKEN_KEYS.input) ?? "—"}
          </Descriptions.Item>
          <Descriptions.Item
            label={t("workbuddy.workflows.detail.tokensOutput")}
          >
            {countFrom(usage, TOKEN_KEYS.output) ?? "—"}
          </Descriptions.Item>
        </Descriptions>
      ) : (
        <Text type="secondary" className={styles.blockHint}>
          —
        </Text>
      )}
    </div>
  );
}

interface ReconciliationFormValues {
  step_id: string;
  decision: ReconciliationDecision;
  evidence_ref: string;
  reason: string;
  external_reference?: string;
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
      render: (value: StepStatus) => (
        <Tag color={STEP_STATUS_COLORS[value]}>
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
        <Descriptions.Item
          label={t("workbuddy.workflows.detail.activeDuration")}
        >
          {data.active_duration_ms === null ||
          data.active_duration_ms === undefined
            ? "—"
            : formatDurationMs(data.active_duration_ms)}
        </Descriptions.Item>
        <Descriptions.Item label={t("workbuddy.workflows.detail.tokensTotal")}>
          {data.token_usage ?? "—"}
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

      <Collapse
        ghost
        className={styles.payloadPanels}
        defaultActiveKey={["inputs", "outputs"]}
        items={[
          {
            key: "inputs",
            label: <Text strong>{t("workbuddy.workflows.detail.inputs")}</Text>,
            children: (
              <JsonPreview
                value={data.inputs}
                emptyLabel={t("workbuddy.workflows.detail.noPayload")}
              />
            ),
          },
          {
            key: "outputs",
            label: (
              <Text strong>{t("workbuddy.workflows.detail.outputs")}</Text>
            ),
            children: (
              <JsonPreview
                value={data.outputs}
                emptyLabel={t("workbuddy.workflows.detail.noPayload")}
              />
            ),
          },
        ]}
      />

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
            expandable={{
              expandedRowRender: (row) => <StepBreakdown step={row} />,
            }}
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

      <ReconciliationSection
        executionId={executionId}
        steps={data.steps}
        onRecorded={() => void execution.reload()}
      />
    </Space>
  );
}

/**
 * One expanded step: everything the server recorded for that attempt, so a node
 * can be triaged in place — the payload it was handed, what it produced, how long
 * it took, what it cost and why it was skipped.
 */
function StepBreakdown({ step }: { step: ExecutionStep }) {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      <Descriptions size="small" column={3} bordered>
        <Descriptions.Item
          label={t("workbuddy.workflows.detail.stepColumns.status")}
        >
          <Tag color={STEP_STATUS_COLORS[step.status]}>
            {statusLabel(
              t,
              "workbuddy.workflows.detail.stepStatus",
              step.status,
            )}
          </Tag>
        </Descriptions.Item>
        <Descriptions.Item
          label={t("workbuddy.workflows.detail.stepColumns.attempt")}
        >
          {step.attempt}
        </Descriptions.Item>
        <Descriptions.Item
          label={t("workbuddy.workflows.detail.stepDuration")}
        >
          {step.duration_ms === null || step.duration_ms === undefined
            ? "—"
            : formatDurationMs(step.duration_ms)}
        </Descriptions.Item>
        <Descriptions.Item label={t("workbuddy.workflows.detail.startedAt")}>
          {step.started_at
            ? formatServerIsoDateTime(step.started_at, timeZone)
            : "—"}
        </Descriptions.Item>
        <Descriptions.Item label={t("workbuddy.workflows.detail.finishedAt")}>
          {step.finished_at
            ? formatServerIsoDateTime(step.finished_at, timeZone)
            : "—"}
        </Descriptions.Item>
        <Descriptions.Item
          label={t("workbuddy.workflows.detail.stepSkipReason")}
        >
          {step.skip_reason
            ? statusLabel(
                t,
                "workbuddy.workflows.detail.skipReason",
                step.skip_reason,
              )
            : "—"}
        </Descriptions.Item>
      </Descriptions>

      <div>
        <Text strong>{t("workbuddy.workflows.detail.stepInput")}</Text>
        <JsonPreview
          value={step.input}
          emptyLabel={t("workbuddy.workflows.detail.noPayload")}
        />
      </div>
      <div>
        <Text strong>{t("workbuddy.workflows.detail.stepOutput")}</Text>
        <JsonPreview
          value={step.output}
          emptyLabel={t("workbuddy.workflows.detail.noPayload")}
        />
      </div>

      <TokenUsageBlock
        usage={step.token_usage}
        title={t("workbuddy.workflows.detail.tokenUsage")}
      />
    </Space>
  );
}

function ReconciliationSection({
  executionId,
  steps,
  onRecorded,
}: {
  executionId: string;
  steps: ExecutionStep[];
  /** The decision settles a step and re-queues the execution, so the detail is stale. */
  onRecorded: () => void;
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

  // The route settles the step parked on an unknown external write, by node id,
  // and refuses every other step — so only those are offered here.
  const parkedSteps = steps
    .filter((step) => step.status === "waiting_reconciliation")
    .map((step) => ({ value: step.node_id, label: step.node_id }));

  const record = useCallback(async () => {
    let values: ReconciliationFormValues;
    try {
      values = await form.validateFields();
    } catch {
      return;
    }
    setRecording(true);
    try {
      await workbuddyRuntimeApi.recordReconciliation(executionId, {
        step_id: values.step_id,
        decision: values.decision,
        evidence_ref: values.evidence_ref.trim(),
        reason: values.reason.trim(),
        external_reference: values.external_reference?.trim() || null,
      });
      message.success(t("workbuddy.workflows.detail.recorded"));
      setOpen(false);
      form.resetFields();
      await reconciliations.reload();
      onRecorded();
    } catch (err) {
      message.error(
        apiErrorMessage(err, t("workbuddy.workflows.detail.recordFailed"), t),
      );
    } finally {
      setRecording(false);
    }
  }, [executionId, form, onRecorded, reconciliations, t]);

  const columns: ColumnsType<Reconciliation> = [
    {
      title: t("workbuddy.workflows.detail.reconcileDecision"),
      dataIndex: "decision",
      key: "decision",
      width: 170,
      render: (value: ReconciliationDecision) => (
        <Tag color={value === "confirmed_success" ? "green" : "red"}>
          {statusLabel(
            t,
            "workbuddy.workflows.detail.reconcileDecisionValue",
            value,
          )}
        </Tag>
      ),
    },
    {
      title: t("workbuddy.workflows.detail.reconcileStepRun"),
      dataIndex: "step_run_id",
      key: "step_run_id",
      width: 120,
      render: (value: string) => <Text code>{value.slice(0, 8)}</Text>,
    },
    {
      title: t("workbuddy.workflows.detail.reconcileEvidenceRef"),
      dataIndex: "evidence_ref",
      key: "evidence_ref",
      width: 140,
      render: (value: string) => <Text code>{value.slice(0, 8)}</Text>,
    },
    {
      title: t("workbuddy.workflows.detail.reconcileNote"),
      dataIndex: "note",
      key: "note",
    },
    {
      title: t("workbuddy.workflows.detail.reconcileExternalRequest"),
      dataIndex: "external_request_id",
      key: "external_request_id",
      width: 180,
      render: (value: string | null) =>
        value ?? <Text type="secondary">—</Text>,
    },
    {
      title: t("workbuddy.workflows.detail.reconcileDecidedBy"),
      dataIndex: "decided_by_user_id",
      key: "decided_by_user_id",
      width: 120,
      render: (value: number | null) =>
        value === null ? <Text type="secondary">—</Text> : `#${value}`,
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

  return (
    <div>
      <Space size={8} align="center">
        <FileCheck2 size={14} />
        <Text strong>{t("workbuddy.workflows.detail.reconciliations")}</Text>
        <Button
          size="small"
          icon={<Plus size={13} />}
          disabled={parkedSteps.length === 0}
          onClick={() => {
            form.resetFields();
            form.setFieldsValue({ step_id: parkedSteps[0]?.value ?? "" });
            setOpen(true);
          }}
        >
          {t("workbuddy.workflows.detail.record")}
        </Button>
      </Space>
      <Text type="secondary" className={styles.blockHint}>
        {parkedSteps.length === 0
          ? t("workbuddy.workflows.detail.reconcileNoSteps")
          : t("workbuddy.workflows.detail.reconciliationHint")}
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
          scroll={{ x: 980 }}
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
            name="step_id"
            label={t("workbuddy.workflows.detail.reconcileNode")}
            rules={[
              {
                required: true,
                message: t("workbuddy.workflows.detail.nodeRequired"),
              },
            ]}
          >
            <Select options={parkedSteps} />
          </Form.Item>
          <Form.Item
            name="decision"
            label={t("workbuddy.workflows.detail.reconcileDecision")}
            rules={[
              {
                required: true,
                message: t("workbuddy.workflows.detail.decisionRequired"),
              },
            ]}
          >
            <Select
              options={RECONCILIATION_DECISIONS.map((decision) => ({
                value: decision,
                label: statusLabel(
                  t,
                  "workbuddy.workflows.detail.reconcileDecisionValue",
                  decision,
                ),
              }))}
            />
          </Form.Item>
          <Form.Item
            name="evidence_ref"
            label={t("workbuddy.workflows.detail.reconcileEvidenceRef")}
            extra={t("workbuddy.workflows.detail.reconcileEvidenceRefHint")}
            rules={[
              {
                required: true,
                message: t("workbuddy.workflows.detail.evidenceRefRequired"),
              },
              {
                pattern: EVIDENCE_REF_PATTERN,
                message: t("workbuddy.workflows.detail.evidenceRefInvalid"),
              },
            ]}
          >
            <Input maxLength={64} spellCheck={false} />
          </Form.Item>
          <Form.Item
            name="reason"
            label={t("workbuddy.workflows.detail.reconcileReason")}
            rules={[
              {
                required: true,
                whitespace: true,
                message: t("workbuddy.workflows.detail.reasonRequired"),
              },
            ]}
          >
            <Input.TextArea
              autoSize={{ minRows: 3, maxRows: 8 }}
              maxLength={4000}
            />
          </Form.Item>
          <Form.Item
            name="external_reference"
            label={t("workbuddy.workflows.detail.reconcileExternalRequest")}
          >
            <Input maxLength={200} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
