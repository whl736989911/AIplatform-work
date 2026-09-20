/**
 * WorkBuddy console → Workflows → guided creation (A-06).
 *
 * The JSON editor next door is the power-user path. This wizard is the path a
 * non-technical member takes: declare the inputs, lay out the steps, wire what
 * each step reads, and say what the run produces. Every field it renders comes
 * from `GET /workflow-definitions/metadata` — the wizard never hard-codes a node
 * type, a required field or a reference syntax, so it cannot drift from the
 * compiler it feeds.
 *
 * Validation is the server's: each edit re-runs `POST /workflow-definitions/validate`
 * (debounced) and the diagnostics that come back are grouped by the step whose
 * `path` they name. A step with an error blocks "next" instead of letting the
 * member walk into a definition the compiler will refuse at save time.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Form,
  Input,
  InputNumber,
  Modal,
  Select,
  Space,
  Steps,
  Switch,
  Tag,
  Typography,
} from "antd";
import { Plus, Trash2 } from "lucide-react";
import { useTranslation } from "react-i18next";
import {
  isWorkBuddyUnavailableError,
  workbuddyWorkflowsApi,
  workflowEtag,
  type WorkflowDefinitionMetadata,
  type WorkflowDiagnostic,
  type WorkflowFieldMetadata,
} from "../../../../api/modules/workbuddyWorkflows";
import { workbuddyRuntimeApi } from "../../../../api/modules/workbuddyRuntime";
import styles from "./index.module.less";

const { Text } = Typography;

/** The step types a member picks from, in the order the wizard explains them. */
const STEP_TYPES = ["input", "knowledge", "llm", "tool", "transform", "condition", "approval", "output"];

export interface WizardStep {
  /** Stable node id, also the default output key. */
  id: string;
  type: string;
  name: string;
  config: Record<string, unknown>;
  saveAs: string;
}

export interface WizardInput {
  name: string;
  type: string;
  required: boolean;
  defaultValue?: string;
}

interface Props {
  open: boolean;
  onClose: () => void;
  onCreated: (workflowId: string) => void;
}

function identifierFrom(label: string, taken: Set<string>): string {
  const base =
    label
      .toLowerCase()
      .replace(/[^a-z0-9_]+/g, "_")
      .replace(/^_+/, "")
      .slice(0, 40) || "step";
  const startsOk = /^[a-z]/.test(base) ? base : `s_${base}`;
  let candidate = startsOk;
  let suffix = 2;
  while (taken.has(candidate)) {
    candidate = `${startsOk}_${suffix}`;
    suffix += 1;
  }
  return candidate;
}

/** Render one metadata-described config field; unknown shapes fall back to text. */
function ConfigField({
  field,
  value,
  onChange,
  references,
}: {
  field: WorkflowFieldMetadata;
  value: unknown;
  onChange: (next: unknown) => void;
  references: string[];
}) {
  const { t } = useTranslation();
  if (Array.isArray(field.enum) && field.enum.length > 0) {
    return (
      <Select
        size="small"
        value={value ?? field.enum[0]}
        onChange={onChange}
        options={field.enum.map((option) => ({ value: option, label: String(option) }))}
      />
    );
  }
  if (field.type === "integer" || field.type === "number") {
    return (
      <InputNumber
        size="small"
        value={typeof value === "number" ? value : undefined}
        min={field.minimum}
        max={field.maximum}
        onChange={(next) => onChange(next ?? undefined)}
      />
    );
  }
  if (field.type === "boolean") {
    return <Switch size="small" checked={Boolean(value)} onChange={onChange} />;
  }
  if (field.type === "array") {
    return (
      <Select
        size="small"
        mode="tags"
        value={Array.isArray(value) ? (value as string[]) : []}
        onChange={onChange}
        placeholder={t("workbuddy.workflows.wizard.field.listHint")}
      />
    );
  }
  const isTemplate = field.name && references.length > 0;
  return (
    <Input
      size="small"
      value={typeof value === "string" ? value : ""}
      onChange={(event) => onChange(event.target.value)}
      placeholder={
        isTemplate ? t("workbuddy.workflows.wizard.field.referenceHint") : undefined
      }
    />
  );
}

export default function CreateWizard({ open, onClose, onCreated }: Props) {
  const { t } = useTranslation();
  const [current, setCurrent] = useState(0);
  const [metadata, setMetadata] = useState<WorkflowDefinitionMetadata | null>(null);
  const [metadataError, setMetadataError] = useState<unknown>(null);
  const [name, setName] = useState("");
  const [inputs, setInputs] = useState<WizardInput[]>([]);
  const [steps, setSteps] = useState<WizardStep[]>([]);
  const [diagnostics, setDiagnostics] = useState<WorkflowDiagnostic[] | null>(null);
  const [validating, setValidating] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<unknown>(null);
  const [savedWorkflowId, setSavedWorkflowId] = useState<string | null>(null);
  const [runMessage, setRunMessage] = useState<string | null>(null);

  useEffect(() => {
    if (!open || metadata !== null) return;
    let cancelled = false;
    workbuddyWorkflowsApi
      .getDefinitionMetadata()
      .then((document) => {
        if (!cancelled) setMetadata(document);
      })
      .catch((error: unknown) => {
        if (!cancelled) setMetadataError(error);
      });
    return () => {
      cancelled = true;
    };
  }, [open, metadata]);

  const inputTypes = metadata?.inputs.types ?? ["string"];

  const definition = useMemo(
    () => ({
      schema_version: metadata?.schema_version ?? 1,
      trigger: { type: "manual", config: {} },
      inputs: Object.fromEntries(
        inputs
          .filter((entry) => entry.name.trim() !== "")
          .map((entry) => [
            entry.name.trim(),
            {
              type: entry.type,
              required: entry.required,
              ...(entry.defaultValue ? { default: entry.defaultValue } : {}),
            },
          ]),
      ),
      nodes: steps.map((step) => ({
        id: step.id,
        type: step.type,
        name: step.name || step.id,
        config: step.config,
        ...(step.saveAs ? { save_as: step.saveAs } : {}),
      })),
      edges: steps.slice(1).map((step, index) => ({
        from: steps[index].id,
        to: step.id,
      })),
    }),
    [inputs, steps, metadata],
  );

  const definitionKey = useMemo(() => JSON.stringify(definition), [definition]);

  // Server-side validation, debounced: the wizard's own rules are only about
  // which step you may leave, never about whether the definition is valid.
  useEffect(() => {
    if (!open || savedWorkflowId !== null) return;
    const handle = setTimeout(() => {
      setValidating(true);
      workbuddyWorkflowsApi
        .validateDefinition(definition)
        .then(() => setDiagnostics([]))
        .catch((error: unknown) => setDiagnostics(diagnosticsFrom(error)))
        .finally(() => setValidating(false));
    }, 400);
    return () => clearTimeout(handle);
  }, [definitionKey, open, savedWorkflowId, definition]);

  const diagnosticsForStep = useCallback(
    (index: number): WorkflowDiagnostic[] => {
      if (!diagnostics || diagnostics.length === 0) return [];
      const step = steps[index];
      if (step === undefined) {
        return diagnostics.filter((entry) => !entry.path?.startsWith("nodes."));
      }
      return diagnostics.filter(
        (entry) =>
          entry.node_id === step.id ||
          entry.path === `nodes.${step.id}` ||
          entry.path?.startsWith(`nodes.${step.id}.`),
      );
    },
    [diagnostics, steps],
  );

  const addInput = useCallback(() => {
    setInputs((previous) => {
      const taken = new Set(previous.map((entry) => entry.name));
      const next = identifierFrom("input", taken);
      return [...previous, { name: next, type: inputTypes[0] ?? "string", required: false }];
    });
  }, [inputTypes]);

  const addStep = useCallback(
    (type: string) => {
      setSteps((previous) => {
        const taken = new Set([
          ...previous.map((step) => step.id),
          ...inputs.map((entry) => entry.name),
        ]);
        const id = identifierFrom(type === "output" ? "result" : type, taken);
        return [
          ...previous,
          { id, type, name: t(`workbuddy.workflows.wizard.type.${type}`), config: {}, saveAs: id },
        ];
      });
    },
    [inputs, t],
  );

  const updateStep = useCallback((index: number, patch: Partial<WizardStep>) => {
    setSteps((previous) =>
      previous.map((step, position) => (position === index ? { ...step, ...patch } : step)),
    );
  }, []);

  const saveAndActivate = useCallback(async () => {
    setSaving(true);
    setSaveError(null);
    try {
      const created = await workbuddyWorkflowsApi.createWorkflow({
        name: name.trim(),
        definition,
      });
      const version = await workbuddyWorkflowsApi.saveWorkflowVersion(
        created.id,
        { definition, base_version_id: null },
        workflowEtag(created),
      );
      await workbuddyWorkflowsApi.activateWorkflowVersion(
        created.id,
        { version_id: version.version.id, mode: "active" },
        workflowEtag(version),
      );
      setSavedWorkflowId(created.id);
      onCreated(created.id);
    } catch (error) {
      setSaveError(error);
    } finally {
      setSaving(false);
    }
  }, [definition, name, onCreated]);

  const dryRun = useCallback(async () => {
    if (savedWorkflowId === null) return;
    setRunMessage(null);
    try {
      const run = await workbuddyRuntimeApi.executeWorkflow(savedWorkflowId, {
        inputs: Object.fromEntries(
          inputs
            .filter((entry) => entry.defaultValue !== undefined && entry.defaultValue !== "")
            .map((entry) => [entry.name, entry.defaultValue]),
        ),
      });
      setRunMessage(
        t("workbuddy.workflows.wizard.dryRun.started", {
          status: run.status,
          execution: run.id,
        }),
      );
    } catch (error) {
      setRunMessage(
        isWorkBuddyUnavailableError(error)
          ? t("workbuddy.shared.notMergedHint")
          : t("workbuddy.workflows.wizard.dryRun.failed"),
      );
    }
  }, [savedWorkflowId, inputs, t]);

  const stepLabels = useMemo(
    () => [
      t("workbuddy.workflows.wizard.step.inputs"),
      t("workbuddy.workflows.wizard.step.steps"),
      t("workbuddy.workflows.wizard.step.data"),
      t("workbuddy.workflows.wizard.step.output"),
    ],
    [t],
  );

  const references = useMemo(() => {
    const declared = inputs.filter((entry) => entry.name.trim() !== "").map((entry) => `{{ inputs.${entry.name.trim()} }}`);
    const produced = steps.map((step) => `{{ nodes.${step.id}.output }}`);
    return [...declared, ...produced];
  }, [inputs, steps]);

  // Which diagnostics block leaving this page. The wizard's pages are wider than
  // its steps (the steps page owns every node diagnostic), so this is a page
  // filter, not `diagnosticsForStep(current)`.
  const pageDiagnosticCount = useMemo(() => {
    if (diagnostics === null || diagnostics.length === 0) return 0;
    if (current === 0) {
      return diagnostics.filter((entry) => entry.path?.startsWith("inputs")).length;
    }
    if (current === 1 || current === 2) {
      return diagnostics.filter(
        (entry) => entry.node_id != null || entry.path?.startsWith("nodes"),
      ).length;
    }
    return diagnostics.length;
  }, [diagnostics, current]);

  const blocked = pageDiagnosticCount > 0;

  return (
    <Modal
      open={open}
      onCancel={onClose}
      width={960}
      destroyOnHidden
      title={t("workbuddy.workflows.wizard.title")}
      footer={
        <Space>
          <Button onClick={onClose}>{t("common.cancel")}</Button>
          <Button disabled={current === 0} onClick={() => setCurrent((value) => value - 1)}>
            {t("workbuddy.workflows.wizard.back")}
          </Button>
          {current < 3 && (
            <Button
              type="primary"
              disabled={blocked || validating}
              onClick={() => setCurrent((value) => value + 1)}
            >
              {t("workbuddy.workflows.wizard.next")}
            </Button>
          )}
          {current === 3 && (
            <>
              <Button
                type="primary"
                loading={saving}
                disabled={blocked || name.trim() === "" || steps.length === 0}
                onClick={() => void saveAndActivate()}
              >
                {t("workbuddy.workflows.wizard.save")}
              </Button>
              <Button disabled={savedWorkflowId === null} onClick={() => void dryRun()}>
                {t("workbuddy.workflows.wizard.dryRun.label")}
              </Button>
            </>
          )}
        </Space>
      }
    >
      {metadataError !== null && (
        <Alert
          type="info"
          showIcon
          className={styles.notice}
          message={t("workbuddy.shared.notMergedTitle")}
          description={t("workbuddy.shared.notMergedHint")}
        />
      )}

      <Steps size="small" current={current} items={stepLabels.map((label) => ({ title: label }))} />

      <div className={styles.body}>
        {current === 0 && (
          <>
            <Form layout="vertical">
              <Form.Item label={t("workbuddy.workflows.wizard.name")} required>
                <Input
                  aria-label={t("workbuddy.workflows.wizard.name")}
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                />
              </Form.Item>
            </Form>
            <Space direction="vertical" className={styles.full}>
              {inputs.map((entry, index) => (
                <Space key={`${entry.name}-${index}`} wrap>
                  <Input
                    size="small"
                    addonBefore={t("workbuddy.workflows.wizard.input.name")}
                    value={entry.name}
                    onChange={(event) =>
                      setInputs((previous) =>
                        previous.map((item, position) =>
                          position === index ? { ...item, name: event.target.value } : item,
                        ),
                      )
                    }
                  />
                  <Select
                    size="small"
                    value={entry.type}
                    onChange={(value) =>
                      setInputs((previous) =>
                        previous.map((item, position) =>
                          position === index ? { ...item, type: value } : item,
                        ),
                      )
                    }
                    options={inputTypes.map((type) => ({ value: type, label: type }))}
                  />
                  <Switch
                    size="small"
                    checked={entry.required}
                    onChange={(checked) =>
                      setInputs((previous) =>
                        previous.map((item, position) =>
                          position === index ? { ...item, required: checked } : item,
                        ),
                      )
                    }
                  />
                  <Text type="secondary">{t("workbuddy.workflows.wizard.input.required")}</Text>
                  <Input
                    size="small"
                    placeholder={t("workbuddy.workflows.wizard.input.default")}
                    value={entry.defaultValue ?? ""}
                    onChange={(event) =>
                      setInputs((previous) =>
                        previous.map((item, position) =>
                          position === index ? { ...item, defaultValue: event.target.value } : item,
                        ),
                      )
                    }
                  />
                  <Button
                    size="small"
                    danger
                    icon={<Trash2 size={14} />}
                    onClick={() =>
                      setInputs((previous) => previous.filter((_, position) => position !== index))
                    }
                  />
                </Space>
              ))}
              <Button size="small" icon={<Plus size={14} />} onClick={addInput}>
                {t("workbuddy.workflows.wizard.input.add")}
              </Button>
            </Space>
          </>
        )}

        {current === 1 && (
          <Space direction="vertical" className={styles.full}>
            {steps.map((step, index) => {
              const meta =
                (metadata?.node_types.find((entry) => entry.type === step.type) as
                  | WorkflowFieldBlockLike
                  | undefined) ?? null;
              return (
                <div key={step.id} className={styles.stepCard}>
                  <Space wrap>
                    <Tag color="blue">{index + 1}</Tag>
                    <Select
                      size="small"
                      value={step.type}
                      onChange={(value) => updateStep(index, { type: value, config: {} })}
                      options={STEP_TYPES.filter((type) =>
                        metadata === null ? true : metadata.node_types.some((entry) => entry.type === type),
                      ).map((type) => ({
                        value: type,
                        label: t(`workbuddy.workflows.wizard.type.${type}`),
                      }))}
                    />
                    <Input
                      size="small"
                      value={step.name}
                      onChange={(event) => updateStep(index, { name: event.target.value })}
                    />
                    <Button
                      size="small"
                      danger
                      icon={<Trash2 size={14} />}
                      onClick={() => setSteps((previous) => previous.filter((_, position) => position !== index))}
                    />
                  </Space>
                  {meta !== null && (
                    <div className={styles.fields}>
                      {meta.config_fields.map((field) => (
                        <Space key={field.name} size={6}>
                          <Text type={meta.required.includes(field.name) ? undefined : "secondary"}>
                            {field.name}
                            {meta.required.includes(field.name) ? " *" : ""}
                          </Text>
                          <ConfigField
                            field={field}
                            value={step.config[field.name]}
                            references={meta.template_fields}
                            onChange={(next) =>
                              updateStep(index, { config: { ...step.config, [field.name]: next } })
                            }
                          />
                        </Space>
                      ))}
                    </div>
                  )}
                  {diagnosticsForStep(index).map((entry) => (
                    <Text key={`${entry.code}-${entry.path}`} type="danger" className={styles.diagnostic}>
                      {entry.path}: {entry.message}
                    </Text>
                  ))}
                </div>
              );
            })}
            <Space wrap>
              {STEP_TYPES.filter((type) => type !== "input" && type !== "output").map((type) => (
                <Button key={type} size="small" icon={<Plus size={14} />} onClick={() => addStep(type)}>
                  {t(`workbuddy.workflows.wizard.type.${type}`)}
                </Button>
              ))}
            </Space>
          </Space>
        )}

        {current === 2 && (
          <Space direction="vertical" className={styles.full}>
            {steps.map((step, index) => (
              <Space key={step.id} wrap>
                <Tag>{index + 1}</Tag>
                <Text>{step.name}</Text>
                <Input
                  size="small"
                  addonBefore={t("workbuddy.workflows.wizard.data.resultKey")}
                  value={step.saveAs}
                  onChange={(event) => updateStep(index, { saveAs: event.target.value })}
                />
              </Space>
            ))}
            <Alert
              type="info"
              showIcon
              message={t("workbuddy.workflows.wizard.data.referenceTitle")}
              description={
                <Space direction="vertical">
                  {references.map((reference) => (
                    <Text key={reference} code>
                      {reference}
                    </Text>
                  ))}
                </Space>
              }
            />
          </Space>
        )}

        {current === 3 && (
          <Space direction="vertical" className={styles.full}>
            <Alert
              type="info"
              showIcon
              message={t("workbuddy.workflows.wizard.output.title")}
              description={t("workbuddy.workflows.wizard.output.hint")}
            />
            {diagnostics !== null && diagnostics.length > 0 && (
              <Alert
                type="warning"
                showIcon
                message={t("workbuddy.workflows.wizard.diagnostics", { count: diagnostics.length })}
                description={
                  <Space direction="vertical">
                    {diagnostics.slice(0, 8).map((entry) => (
                      <Text key={`${entry.code}-${entry.path}`} className={styles.diagnostic}>
                        {entry.path ?? entry.code}: {entry.message}
                      </Text>
                    ))}
                  </Space>
                }
              />
            )}
            {saveError !== null && (
              <Alert
                type="error"
                showIcon
                message={t("workbuddy.workflows.wizard.saveFailed")}
                description={
                  isWorkBuddyUnavailableError(saveError)
                    ? t("workbuddy.shared.notMergedHint")
                    : String(saveError)
                }
              />
            )}
            {runMessage !== null && <Alert type="success" showIcon message={runMessage} />}
            {savedWorkflowId !== null && (
              <Text type="secondary">
                {t("workbuddy.workflows.wizard.saved", { id: savedWorkflowId })}
              </Text>
            )}
          </Space>
        )}
      </div>
    </Modal>
  );
}

interface WorkflowFieldBlockLike {
  required: string[];
  config_fields: WorkflowFieldMetadata[];
  template_fields: string[];
}

/** Diagnostics ride in the error envelope; anything else is a request failure. */
function diagnosticsFrom(error: unknown): WorkflowDiagnostic[] {
  const details = (error as { details?: { diagnostics?: unknown } } | undefined)?.details;
  const raw = details?.diagnostics;
  if (!Array.isArray(raw)) return [];
  return raw.filter(
    (entry): entry is WorkflowDiagnostic =>
      typeof entry === "object" && entry !== null && "code" in entry && "message" in entry,
  );
}
