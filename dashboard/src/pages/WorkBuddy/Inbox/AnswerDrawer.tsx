/**
 * WorkBuddy inbox → answer a question a run is parked on.
 *
 * The one place a form is filled: the inbox opens it for a question of the
 * caller's own queue and the run list opens it for whatever an execution asked,
 * so both entries render the same fields and submit through the same route.
 *
 * The form travels with the question — both list routes return it — and the
 * answer is built from those declared fields alone, because the server refuses
 * a key the form does not declare and reads a blank value exactly like an
 * absence. The checks in here only keep an obviously incomplete form from being
 * sent; the server stays authoritative and its own message is what the drawer
 * shows.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  DatePicker,
  Descriptions,
  Drawer,
  Form,
  Input,
  InputNumber,
  Select,
  Space,
  Switch,
  Tag,
  Typography,
} from "antd";
import dayjs, { type Dayjs } from "dayjs";
import { message } from "@/utils/antdMessage";
import { Send } from "lucide-react";
import { useTranslation } from "react-i18next";
import { apiErrorMessage } from "../../../utils/apiError";
import { useServerTimezone } from "../../../hooks/useServerTimezone";
import { formatServerIsoDateTime } from "../../../utils/formatMessageTime";
import {
  workbuddyRuntimeApi,
  type InputField,
  type InputRequest,
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
} from "../Workflows/consoleState";
import styles from "./index.module.less";

const { Text } = Typography;

/** What the form's controls hold: one entry per rendered field. */
interface AnswerFormValues {
  [name: string]: string | number | boolean | Dayjs | null | undefined;
}

/**
 * What a fresh form starts with. A required switch starts off, because an off
 * switch *is* the answer ``false``; an optional one starts unset, so leaving it
 * alone submits no value instead of an invented ``false``.
 */
function initialValues(fields: readonly InputField[]): AnswerFormValues {
  const values: AnswerFormValues = {};
  for (const field of fields) {
    if (field.type === "boolean" && field.required !== false) {
      values[field.name] = false;
    }
  }
  return values;
}

/**
 * The answer, built from the declared fields only and in the shape each type
 * promises: numbers stay numbers, a date control's ``Dayjs`` becomes the
 * ``YYYY-MM-DD`` text its field declares, and a blank stays out of the payload.
 */
function buildAnswer(
  fields: readonly InputField[],
  raw: AnswerFormValues,
): JsonObject {
  const values: JsonObject = {};
  for (const field of fields) {
    const value = raw[field.name];
    if (value === undefined || value === null) continue;
    if (typeof value === "string" && !value.trim()) continue;
    if (field.type === "integer" || field.type === "number") {
      if (typeof value === "number" && Number.isFinite(value)) {
        values[field.name] = value;
      }
      continue;
    }
    if (field.type === "date") {
      if (dayjs.isDayjs(value)) values[field.name] = value.format("YYYY-MM-DD");
      continue;
    }
    // string / text / boolean / select keep what their control produced; a type
    // this client does not know is sent as-is for the server to refuse by name.
    values[field.name] = value;
  }
  return values;
}

/** The id and ARIA wiring ``Form.Item`` injects into the control it wraps. */
interface FieldControlA11y {
  id?: string;
  "aria-describedby"?: string;
  "aria-invalid"?: boolean | "false" | "true";
  "aria-required"?: boolean | "false" | "true";
}

/**
 * One form control, chosen by the type the field declares.
 *
 * ``Form.Item`` injects the id its label points at and the ARIA wiring for the
 * rendered error into its direct child, so this wrapper passes them on to the
 * control itself: the label has to reach the input, and a failed rule has to be
 * announced.
 */
function FieldControl({
  field,
  ...a11y
}: { field: InputField } & FieldControlA11y) {
  switch (field.type) {
    case "text":
      return (
        <Input.TextArea
          autoSize={{ minRows: 3, maxRows: 10 }}
          placeholder={field.placeholder}
          {...a11y}
        />
      );
    case "integer":
      return (
        <InputNumber
          precision={0}
          step={1}
          style={{ width: "100%" }}
          placeholder={field.placeholder}
          {...a11y}
        />
      );
    case "number":
      return (
        <InputNumber
          style={{ width: "100%" }}
          placeholder={field.placeholder}
          {...a11y}
        />
      );
    case "boolean":
      return <Switch {...a11y} />;
    case "date":
      // The answer is sent as ``YYYY-MM-DD``, so the control shows exactly that
      // instead of a locale format the field would not accept back.
      return <DatePicker style={{ width: "100%" }} format="YYYY-MM-DD" {...a11y} />;
    case "select":
      return (
        <Select
          placeholder={field.placeholder}
          options={(field.options ?? []).map((option) => ({
            value: option,
            label: option,
          }))}
          {...a11y}
        />
      );
    default:
      return <Input placeholder={field.placeholder} {...a11y} />;
  }
}

export default function AnswerDrawer({
  executionId,
  inputRequestId,
  onClose,
  onAnswered,
}: {
  /** The run that asked; ``null`` keeps the drawer closed. */
  executionId: string | null;
  /** The exact question to answer; ``null`` means "this run's open one". */
  inputRequestId: string | null;
  onClose: () => void;
  onAnswered: () => void;
}) {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  const [form] = Form.useForm<AnswerFormValues>();
  const [requests, setRequests] = useState<InputRequest[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [nonce, setNonce] = useState(0);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  useEffect(() => {
    if (!executionId) {
      setRequests([]);
      setError(null);
      return;
    }
    let active = true;
    setLoading(true);
    workbuddyRuntimeApi
      .listExecutionInputRequests(executionId)
      .then((rows) => {
        if (!active) return;
        setRequests(rows);
        setError(null);
      })
      .catch((err: unknown) => {
        if (!active) return;
        setRequests([]);
        setError(err);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [executionId, nonce]);

  // A named question is answered by that id or not at all: falling back to
  // another open one would submit an answer to a question nobody opened.
  const target = inputRequestId
    ? (requests.find((row) => row.id === inputRequestId) ?? null)
    : (requests.find((row) => row.status === "open") ?? null);
  const fields = useMemo(() => target?.form.fields ?? [], [target]);

  useEffect(() => {
    // A new question starts clean: no half-typed answer, no earlier refusal.
    setSubmitError(null);
    if (!target) return;
    form.resetFields();
    form.setFieldsValue(initialValues(target.form.fields));
  }, [form, target]);

  const retry = useCallback(() => setNonce((value) => value + 1), []);

  const submit = useCallback(async () => {
    if (!executionId || !target) return;
    let raw: AnswerFormValues;
    try {
      raw = await form.validateFields();
    } catch {
      // antd marked the missing fields inline; nothing is sent.
      return;
    }
    setSubmitting(true);
    setSubmitError(null);
    try {
      await workbuddyRuntimeApi.answerInputRequest(executionId, target.id, {
        values: buildAnswer(fields, raw),
      });
      message.success(t("workbuddy.inbox.inputs.submitted"));
      form.resetFields();
      onAnswered();
      onClose();
    } catch (err) {
      setSubmitError(
        apiErrorMessage(err, t("workbuddy.inbox.inputs.submitFailed"), t),
      );
    } finally {
      setSubmitting(false);
    }
  }, [executionId, target, fields, form, onAnswered, onClose, t]);

  return (
    <Drawer
      open={executionId !== null}
      onClose={onClose}
      destroyOnHidden
      width={720}
      title={
        target
          ? t("workbuddy.inbox.inputs.detailTitle", {
              id: target.id.slice(0, 8),
            })
          : t("workbuddy.inbox.inputs.answerTitle")
      }
      extra={
        target && target.status === "open" ? (
          <Button
            type="primary"
            size="small"
            icon={<Send size={14} />}
            loading={submitting}
            disabled={fields.length === 0}
            onClick={() => void submit()}
          >
            {t("workbuddy.inbox.inputs.submit")}
          </Button>
        ) : null
      }
    >
      {executionId === null ? null : loading ? (
        <LoadingBlock label={t("common.loading")} />
      ) : error !== null && error !== undefined ? (
        isWorkBuddyUnavailableError(error) ? (
          <UnavailableNotice
            error={error}
            errorFallback={t("workbuddy.inbox.state.errorFallback")}
            title={t("workbuddy.shared.notMergedTitle")}
            hint={t("workbuddy.shared.notMergedHint")}
            onRetry={retry}
          />
        ) : (
          <LoadError
            error={error}
            title={t("workbuddy.inbox.inputs.detailLoadFailed")}
            fallback={t("workbuddy.inbox.state.errorFallback")}
            onRetry={retry}
          />
        )
      ) : target === null ? (
        <Alert
          type="info"
          showIcon
          message={t("workbuddy.inbox.inputs.noOpenTitle")}
          description={t("workbuddy.inbox.inputs.noOpenHint")}
        />
      ) : (
        <Space direction="vertical" size={14} style={{ width: "100%" }}>
          <Descriptions
            size="small"
            column={2}
            bordered
            title={t("workbuddy.inbox.inputs.overview")}
          >
            <Descriptions.Item
              label={t("workbuddy.inbox.inputs.column.status")}
            >
              <Tag color={target.status === "open" ? "gold" : "default"}>
                {statusLabel(t, "workbuddy.inbox.inputs.status", target.status)}
              </Tag>
            </Descriptions.Item>
            <Descriptions.Item label={t("workbuddy.inbox.inputs.node")}>
              <Text code>{target.node_id}</Text>
            </Descriptions.Item>
            <Descriptions.Item label={t("workbuddy.inbox.inputs.execution")}>
              <Text code>{target.execution_id.slice(0, 8)}</Text>
            </Descriptions.Item>
            <Descriptions.Item label={t("workbuddy.inbox.inputs.expiresAt")}>
              {target.expires_at
                ? formatServerIsoDateTime(target.expires_at, timeZone)
                : t("workbuddy.inbox.inputs.none")}
            </Descriptions.Item>
            <Descriptions.Item label={t("workbuddy.inbox.inputs.createdAt")}>
              {target.created_at
                ? formatServerIsoDateTime(target.created_at, timeZone)
                : t("workbuddy.inbox.inputs.none")}
            </Descriptions.Item>
            <Descriptions.Item label={t("workbuddy.inbox.inputs.submittedAt")}>
              {target.submitted_at
                ? formatServerIsoDateTime(target.submitted_at, timeZone)
                : t("workbuddy.inbox.inputs.none")}
            </Descriptions.Item>
          </Descriptions>

          <div>
            <Text strong>{t("workbuddy.inbox.inputs.prompt")}</Text>
            <div className={styles.promptText}>{target.prompt}</div>
          </div>

          {target.status !== "open" ? (
            <div>
              <Alert
                type="info"
                showIcon
                message={t("workbuddy.inbox.inputs.closedTitle")}
                description={t("workbuddy.inbox.inputs.closedHint")}
              />
              <Text strong>{t("workbuddy.inbox.inputs.submittedValues")}</Text>
              <JsonPreview
                value={target.values}
                emptyLabel={t("workbuddy.inbox.inputs.noValues")}
              />
            </div>
          ) : fields.length === 0 ? (
            <Alert
              type="warning"
              showIcon
              message={t("workbuddy.inbox.inputs.noFieldsTitle")}
              description={t("workbuddy.inbox.inputs.noFieldsHint")}
            />
          ) : (
            <>
              {/* The deadline the run declared has passed and nobody answered:
                  a reminder before submitting, not a verdict on the answer. */}
              {target.expires_at !== null &&
                Date.parse(target.expires_at) < Date.now() && (
                  <Alert
                    type="warning"
                    showIcon
                    message={t("workbuddy.inbox.inputs.overdue")}
                    description={t("workbuddy.inbox.inputs.overdueHint")}
                  />
                )}
              <Form form={form} layout="vertical">
                {fields.map((field) => {
                  const required = field.required !== false;
                  return (
                    <Form.Item
                      key={field.name}
                      name={field.name}
                      label={field.label}
                      required={required}
                      valuePropName={
                        field.type === "boolean" ? "checked" : "value"
                      }
                      rules={
                        required
                          ? [
                              {
                                required: true,
                                message: t(
                                  "workbuddy.inbox.inputs.fieldRequired",
                                  { label: field.label },
                                ),
                              },
                            ]
                          : undefined
                      }
                    >
                      <FieldControl field={field} />
                    </Form.Item>
                  );
                })}
              </Form>
              {submitError !== null && (
                <Alert
                  type="error"
                  showIcon
                  message={t("workbuddy.inbox.inputs.submitFailed")}
                  description={<Text>{submitError}</Text>}
                />
              )}
            </>
          )}
        </Space>
      )}
    </Drawer>
  );
}
