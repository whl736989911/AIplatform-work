/**
 * WorkBuddy console → Workflows → run one workflow with explicit inputs.
 *
 * The panel only updates after the server accepted the run (202) — the modal
 * returns the accepted execution and never pretends a run started.
 */

import { useEffect, useState } from "react";
import { Alert, Button, Form, Input, Modal, Typography } from "antd";
import { message } from "@/utils/antdMessage";
import { PlayCircle } from "lucide-react";
import { useTranslation } from "react-i18next";
import { apiErrorMessage } from "../../../utils/apiError";
import {
  workbuddyRuntimeApi,
  type Execution,
} from "../../../api/modules/workbuddyRuntime";
import type { JsonObject } from "../../../api/modules/workbuddyWorkflows";
import styles from "./index.module.less";

const { Text } = Typography;

interface RunFormValues {
  inputs: string;
  idempotency_key?: string;
}

export default function ExecuteModal({
  open,
  workflow,
  onClose,
  onStarted,
}: {
  open: boolean;
  workflow: { id: string; name: string } | null;
  onClose: () => void;
  onStarted: (execution: Execution) => void;
}) {
  const { t } = useTranslation();
  const [form] = Form.useForm<RunFormValues>();
  const [starting, setStarting] = useState(false);
  const [inputsError, setInputsError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setInputsError(null);
    form.setFieldsValue({ inputs: "{}", idempotency_key: "" });
  }, [open, form]);

  const submit = async () => {
    if (!workflow) return;
    let values: RunFormValues;
    try {
      values = await form.validateFields();
    } catch {
      return;
    }
    let inputs: JsonObject;
    try {
      const parsed: unknown = JSON.parse(values.inputs);
      if (
        typeof parsed !== "object" ||
        parsed === null ||
        Array.isArray(parsed)
      ) {
        setInputsError(t("workbuddy.workflows.run.inputsInvalid"));
        return;
      }
      inputs = parsed as JsonObject;
    } catch (err) {
      setInputsError(
        err instanceof Error
          ? err.message
          : t("workbuddy.workflows.run.inputsInvalid"),
      );
      return;
    }
    setInputsError(null);
    setStarting(true);
    try {
      const execution = await workbuddyRuntimeApi.executeWorkflow(workflow.id, {
        inputs,
        idempotency_key: values.idempotency_key?.trim() || null,
      });
      message.success(
        t("workbuddy.workflows.run.started", { id: execution.id.slice(0, 8) }),
      );
      onStarted(execution);
      onClose();
    } catch (err) {
      message.error(
        apiErrorMessage(err, t("workbuddy.workflows.run.failed"), t),
      );
    } finally {
      setStarting(false);
    }
  };

  return (
    <Modal
      open={open}
      onCancel={onClose}
      destroyOnHidden
      maskClosable={false}
      width={720}
      title={t("workbuddy.workflows.run.title", {
        name: workflow?.name ?? "",
      })}
      footer={
        <Button
          type="primary"
          icon={<PlayCircle size={14} />}
          loading={starting}
          onClick={() => void submit()}
        >
          {t("workbuddy.workflows.run.submit")}
        </Button>
      }
    >
      <Form form={form} layout="vertical" requiredMark={false}>
        <Form.Item
          name="inputs"
          label={t("workbuddy.workflows.run.inputs")}
          extra={t("workbuddy.workflows.run.inputsHint")}
        >
          <Input.TextArea
            autoSize={{ minRows: 8, maxRows: 18 }}
            spellCheck={false}
            className={styles.jsonEditor}
          />
        </Form.Item>
        <Form.Item
          name="idempotency_key"
          label={t("workbuddy.workflows.run.idempotency")}
          extra={t("workbuddy.workflows.run.idempotencyHint")}
        >
          <Input maxLength={200} placeholder="order-1234" />
        </Form.Item>
      </Form>
      {inputsError && (
        <Alert
          type="error"
          showIcon
          className={styles.notice}
          message={t("workbuddy.workflows.run.inputsInvalid")}
          description={<Text>{inputsError}</Text>}
        />
      )}
    </Modal>
  );
}
