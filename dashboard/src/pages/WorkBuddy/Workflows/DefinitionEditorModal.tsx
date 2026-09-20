/**
 * WorkBuddy console → Workflows → definition editor (create a draft, or append
 * an immutable version on top of the version the editor opened).
 *
 * Nothing is submitted before the platform compiler accepted the exact JSON
 * that will be stored: the local parse feeds ``/workflow-definitions/validate``
 * and the *normalized* definition from that response is what gets saved. A
 * stale revision surfaces as the server's 409 and is reported as a reload,
 * never retried blindly.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Form,
  Input,
  Modal,
  Segmented,
  Space,
  Typography,
} from "antd";
import { message } from "@/utils/antdMessage";
import { CheckCircle2, ShieldCheck } from "lucide-react";
import { useTranslation } from "react-i18next";
import { parseApiError, apiErrorMessage } from "../../../utils/apiError";
import {
  workbuddyWorkflowsApi,
  parseWorkflowDiagnostics,
  workflowEtag,
  type DefinitionValidation,
  type JsonObject,
  type WorkflowDiagnostic,
  type WorkflowManagerDetail,
  type WorkflowVersion,
  type WorkflowWithVersion,
} from "../../../api/modules/workbuddyWorkflows";
import DefinitionCanvas from "./DefinitionCanvas";
import type { GraphDefinition } from "./definitionGraph";
import styles from "./index.module.less";

const { Text } = Typography;

/**
 * One refused defect: the compiler's code, the location it points at, the
 * server's message, and — only when the key exists — the localized repair hint.
 * An untranslated code shows the location instead of an invented suggestion.
 */
function DiagnosticItem({ diagnostic }: { diagnostic: WorkflowDiagnostic }) {
  const { t, i18n } = useTranslation();
  const hintKey =
    diagnostic.hint_key ?? `workflowDiagnostics.${diagnostic.code}`;
  const hint = i18n.exists(hintKey) ? t(hintKey) : "";

  return (
    <Space direction="vertical" size={0}>
      <Space size={8} wrap>
        <Text code>{diagnostic.code}</Text>
        {diagnostic.node_id ? (
          <Text type="secondary">
            {t("workbuddy.workflows.editor.diagnosticNode", {
              node: diagnostic.node_id,
            })}
          </Text>
        ) : null}
        {diagnostic.path ? (
          <Text type="secondary">{diagnostic.path}</Text>
        ) : null}
      </Space>
      <Text>{diagnostic.message}</Text>
      {hint ? <Text type="warning">{hint}</Text> : null}
    </Space>
  );
}

export type DefinitionEditorTarget =
  | { mode: "create" }
  | {
      mode: "edit";
      workflow: WorkflowManagerDetail;
      baseVersion: WorkflowVersion;
    };

interface EditorFormValues {
  name: string;
  description?: string;
  change_summary?: string;
}

/**
 * Minimal definition the compiler accepts (same shape as the compiler's own
 * fixture): one transform node, one declared input, no edges.
 */
const MINIMAL_DEFINITION: JsonObject = {
  schema_version: 1,
  trigger: { type: "manual", config: {} },
  inputs: { who: { type: "string", required: true, default: "world" } },
  nodes: [
    {
      id: "hello",
      type: "transform",
      name: "Build greeting",
      config: {
        input: { greeting: "hello {{ inputs.who }}" },
        expression: "inputs",
      },
      save_as: "greeting",
    },
  ],
  edges: [],
};

export default function DefinitionEditorModal({
  open,
  target,
  onClose,
  onSaved,
  onStale,
}: {
  open: boolean;
  target: DefinitionEditorTarget | null;
  onClose: () => void;
  onSaved: (saved: WorkflowWithVersion) => void;
  onStale: () => void;
}) {
  const { t } = useTranslation();
  const [form] = Form.useForm<EditorFormValues>();
  const [definition, setDefinition] = useState("");
  const [view, setView] = useState<"json" | "canvas">("json");
  // The canvas draws a definition, so it is only offered when the text parses:
  // silently showing a stale graph while the JSON is mid-edit would be worse
  // than making the reader fix the JSON first.
  const parsed = useMemo<GraphDefinition | null>(() => {
    try {
      const value: unknown = JSON.parse(definition);
      return value !== null && typeof value === "object"
        ? (value as GraphDefinition)
        : null;
    } catch {
      return null;
    }
  }, [definition]);
  const [validation, setValidation] = useState<DefinitionValidation | null>(
    null,
  );
  const [validationError, setValidationError] = useState<unknown>(null);
  const [validating, setValidating] = useState(false);
  const [saving, setSaving] = useState(false);

  const editing = target?.mode === "edit" ? target : null;

  useEffect(() => {
    if (!open) return;
    const initialDefinition =
      editing?.baseVersion.definition ?? MINIMAL_DEFINITION;
    setDefinition(JSON.stringify(initialDefinition, null, 2));
    setValidation(null);
    setValidationError(null);
    form.setFieldsValue({
      name: editing?.workflow.name ?? "",
      description: editing?.workflow.description ?? "",
      change_summary: "",
    });
  }, [open, editing, form]);

  const runValidation = useCallback(async () => {
    let parsed: unknown;
    try {
      parsed = JSON.parse(definition);
    } catch (err) {
      setValidation(null);
      setValidationError(
        new Error(
          t("workbuddy.workflows.editor.jsonInvalid", {
            message: err instanceof Error ? err.message : String(err),
          }),
        ),
      );
      return;
    }
    if (
      typeof parsed !== "object" ||
      parsed === null ||
      Array.isArray(parsed)
    ) {
      setValidation(null);
      setValidationError(
        new Error(t("workbuddy.workflows.editor.jsonObjectRequired")),
      );
      return;
    }
    setValidating(true);
    try {
      const result = await workbuddyWorkflowsApi.validateDefinition(
        parsed as JsonObject,
      );
      setValidation(result);
      setValidationError(null);
      setDefinition(JSON.stringify(result.definition, null, 2));
    } catch (err) {
      setValidation(null);
      setValidationError(err);
    } finally {
      setValidating(false);
    }
  }, [definition, t]);

  const submit = async () => {
    if (!validation) {
      message.warning(t("workbuddy.workflows.editor.validateFirst"));
      return;
    }
    let values: EditorFormValues;
    try {
      values = await form.validateFields();
    } catch {
      return;
    }
    setSaving(true);
    try {
      if (editing) {
        const saved = await workbuddyWorkflowsApi.saveWorkflowVersion(
          editing.workflow.id,
          {
            definition: validation.definition,
            base_version_id: editing.baseVersion.id,
            change_summary: values.change_summary?.trim() || null,
          },
          workflowEtag(editing.workflow),
        );
        message.success(t("workbuddy.workflows.editor.saved"));
        onSaved(saved);
      } else {
        const created = await workbuddyWorkflowsApi.createWorkflow({
          name: values.name.trim(),
          description: values.description?.trim() || null,
          definition: validation.definition,
        });
        message.success(t("workbuddy.workflows.editor.created"));
        onSaved(created);
      }
      onClose();
    } catch (err) {
      const code = parseApiError(err)?.code;
      if (
        code === "WORKBUDDY_WORKFLOW_REVISION_CONFLICT" ||
        code === "WORKBUDDY_PRECONDITION_REQUIRED"
      ) {
        message.error(t("workbuddy.workflows.editor.revisionConflict"));
        onStale();
      } else {
        message.error(
          apiErrorMessage(err, t("workbuddy.workflows.editor.saveFailed"), t),
        );
      }
    } finally {
      setSaving(false);
    }
  };

  const validationDiagnostics = parseWorkflowDiagnostics(validationError);

  return (
    <Modal
      open={open}
      onCancel={onClose}
      destroyOnHidden
      width={880}
      maskClosable={false}
      title={t(
        editing
          ? "workbuddy.workflows.editor.editTitle"
          : "workbuddy.workflows.editor.createTitle",
      )}
      footer={
        <Space>
          <Text type="secondary" className={styles.footerHint}>
            {t("workbuddy.workflows.editor.closeWarning")}
          </Text>
          <Button onClick={onClose}>{t("common.cancel")}</Button>
          <Button
            icon={<ShieldCheck size={14} />}
            loading={validating}
            onClick={() => void runValidation()}
          >
            {validating
              ? t("workbuddy.workflows.editor.validating")
              : t("workbuddy.workflows.editor.validate")}
          </Button>
          <Button type="primary" loading={saving} onClick={() => void submit()}>
            {t(
              editing
                ? "workbuddy.workflows.editor.submitSave"
                : "workbuddy.workflows.editor.submitCreate",
            )}
          </Button>
        </Space>
      }
    >
      <Form form={form} layout="vertical" requiredMark={false}>
        <Form.Item
          name="name"
          label={t("workbuddy.workflows.editor.name")}
          rules={[
            {
              required: true,
              whitespace: true,
              message: t("workbuddy.workflows.editor.nameRequired"),
            },
          ]}
        >
          <Input
            disabled={editing !== null}
            placeholder={t("workbuddy.workflows.editor.namePlaceholder")}
            maxLength={200}
          />
        </Form.Item>
        {editing === null ? (
          <Form.Item
            name="description"
            label={t("workbuddy.workflows.editor.description")}
          >
            <Input
              placeholder={t(
                "workbuddy.workflows.editor.descriptionPlaceholder",
              )}
              maxLength={2000}
            />
          </Form.Item>
        ) : (
          <Form.Item
            name="change_summary"
            label={t("workbuddy.workflows.editor.changeSummary")}
          >
            <Input
              placeholder={t(
                "workbuddy.workflows.editor.changeSummaryPlaceholder",
              )}
              maxLength={500}
            />
          </Form.Item>
        )}

        <Form.Item
          label={t("workbuddy.workflows.editor.definition")}
          extra={t("workbuddy.workflows.editor.definitionHint")}
        >
          <Segmented
            size="small"
            style={{ marginBottom: 8 }}
            value={view}
            onChange={(value) =>
              setView(value === "canvas" ? "canvas" : "json")
            }
            options={[
              {
                label: t("workbuddy.workflows.editor.viewJson"),
                value: "json",
              },
              {
                label: t("workbuddy.workflows.editor.viewCanvas"),
                value: "canvas",
                disabled: parsed === null,
              },
            ]}
          />
          {view === "canvas" && parsed !== null ? (
            <DefinitionCanvas
              definition={parsed}
              layoutKey={target?.mode === "edit" ? target.workflow.id : "draft"}
              onChange={(next) => {
                // The one structural edit the canvas makes: connections. It is
                // written straight back into the JSON, which stays the source of
                // truth the routes receive.
                setDefinition(JSON.stringify(next, null, 2));
                setValidation(null);
                setValidationError(null);
              }}
            />
          ) : (
            <Input.TextArea
              value={definition}
              onChange={(event) => {
                setDefinition(event.target.value);
                setValidation(null);
                setValidationError(null);
              }}
              autoSize={{ minRows: 12, maxRows: 22 }}
              spellCheck={false}
              className={styles.jsonEditor}
            />
          )}
        </Form.Item>
        {editing === null && (
          <Text type="secondary" className={styles.hint}>
            {t("workbuddy.workflows.editor.templateHint")}
          </Text>
        )}

        {validation && (
          <Alert
            type="success"
            showIcon
            icon={<CheckCircle2 size={16} />}
            className={styles.notice}
            message={t("workbuddy.workflows.editor.validTitle")}
            description={
              <Space direction="vertical" size={2}>
                <Text>
                  {t("workbuddy.workflows.editor.summary", {
                    nodes: validation.node_count,
                    edges: validation.edge_count,
                    entry: validation.entry_node_id,
                  })}
                </Text>
                <Text type="secondary">
                  {t("workbuddy.workflows.editor.exitNodes", {
                    nodes: validation.exit_node_ids.join(", ") || "—",
                  })}
                </Text>
                <Text type="secondary">
                  {t("workbuddy.workflows.editor.semanticChecks", {
                    value: validation.semantic_checks || "—",
                  })}
                </Text>
                <Text type="secondary">
                  {t("workbuddy.workflows.editor.compiler", {
                    version: validation.compiler_version,
                  })}
                </Text>
              </Space>
            }
          />
        )}
        {validationError !== null && validationError !== undefined && (
          <Alert
            type="error"
            showIcon
            className={styles.notice}
            message={t("workbuddy.workflows.editor.invalidTitle")}
            description={
              validationDiagnostics.length > 0 ? (
                <Space direction="vertical" size={4}>
                  <Text type="secondary">
                    {t("workbuddy.workflows.editor.diagnosticCount", {
                      count: validationDiagnostics.length,
                    })}
                  </Text>
                  {validationDiagnostics.map((diagnostic, index) => (
                    <DiagnosticItem
                      // The compiler emits one entry per defect, in a stable
                      // order, so the index is a stable identity for the list.
                      key={`${index}-${diagnostic.code}-${
                        diagnostic.path ?? ""
                      }`}
                      diagnostic={diagnostic}
                    />
                  ))}
                </Space>
              ) : (
                apiErrorMessage(
                  validationError,
                  t("workbuddy.workflows.editor.validateFailed"),
                  t,
                )
              )
            }
          />
        )}
      </Form>
    </Modal>
  );
}
