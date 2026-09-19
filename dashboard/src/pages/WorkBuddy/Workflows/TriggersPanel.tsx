/**
 * WorkBuddy console → Workflows → Triggers.
 *
 * Cron / webhook / event registrations of the selected workflow. The list
 * routes never return a signing secret, so the only place a secret exists is
 * the rotate-secret response, which is shown once and then dropped.
 */

import { useCallback, useState } from "react";
import {
  Alert,
  Button,
  Divider,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Select,
  Space,
  Tag,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { message } from "@/utils/antdMessage";
import { Link2, Plus, RefreshCw, Trash2 } from "lucide-react";
import { useTranslation } from "react-i18next";
import { ResizableTable } from "../../../components/ResizableTable";
import { copyText } from "../../../utils/copyText";
import { apiErrorMessage, parseApiError } from "../../../utils/apiError";
import { useServerTimezone } from "../../../hooks/useServerTimezone";
import { formatServerDateTime } from "../../../utils/formatMessageTime";
import {
  TRIGGER_KINDS,
  workbuddyWorkflowsApi,
  type RotatedTriggerSecret,
  type TriggerKind,
  type TriggerRegistration,
  type TriggerRegistrationList,
} from "../../../api/modules/workbuddyWorkflows";
import { TabPanelHeader } from "../../Settings/AdvancedSettings/TabPanelHeader";
import {
  ResourceState,
  WorkflowPicker,
  statusLabel,
  useWorkBuddyResource,
  type WorkflowOption,
} from "./consoleState";
import TriggerSecretModal from "./TriggerSecretModal";
import styles from "./index.module.less";

const { Text } = Typography;

/** Matches ``DEFAULT_TOLERANCE_SECONDS`` in the trigger service. */
const DEFAULT_TOLERANCE_SECONDS = 300;

interface GrantFormValues {
  kb_id: string;
  permission: "read" | "write";
}

interface RegistrationFormValues {
  kind: TriggerKind;
  name: string;
  cron_expression?: string;
  event_name?: string;
  event_filter?: string;
  tool_grants?: string[];
  kb_grants?: GrantFormValues[];
  tolerance_seconds: number;
  signature_header: string;
  timestamp_header: string;
}

export default function TriggersPanel({
  workflowId,
  onSelectWorkflow,
  options,
  optionsLoading,
}: {
  workflowId: string | null;
  onSelectWorkflow: (id: string | null) => void;
  options: WorkflowOption[];
  optionsLoading: boolean;
}) {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  const registrations = useWorkBuddyResource<TriggerRegistrationList>(
    { items: [], count: 0 },
    () =>
      workflowId
        ? workbuddyWorkflowsApi.listTriggerRegistrations(workflowId)
        : Promise.resolve({ items: [], count: 0 }),
    [workflowId],
  );
  const [form] = Form.useForm<RegistrationFormValues>();
  const [createOpen, setCreateOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [kind, setKind] = useState<TriggerKind>("cron");
  const [secret, setSecret] = useState<RotatedTriggerSecret | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const openCreate = useCallback(() => {
    setKind("cron");
    form.setFieldsValue({
      kind: "cron",
      name: "",
      cron_expression: "",
      event_name: "",
      event_filter: "{}",
      tool_grants: [],
      kb_grants: [],
      tolerance_seconds: DEFAULT_TOLERANCE_SECONDS,
      signature_header: "x-workbuddy-signature",
      timestamp_header: "x-workbuddy-timestamp",
    });
    setCreateOpen(true);
  }, [form]);

  const create = async () => {
    if (!workflowId) return;
    let values: RegistrationFormValues;
    try {
      values = await form.validateFields();
    } catch {
      return;
    }
    let eventFilter: Record<string, unknown> | null = null;
    if (values.kind === "event" && values.event_filter?.trim()) {
      try {
        const parsed: unknown = JSON.parse(values.event_filter);
        if (
          typeof parsed !== "object" ||
          parsed === null ||
          Array.isArray(parsed)
        ) {
          message.error(t("workbuddy.workflows.create.filterInvalid"));
          return;
        }
        eventFilter = parsed as Record<string, unknown>;
      } catch (err) {
        message.error(
          err instanceof Error
            ? err.message
            : t("workbuddy.workflows.create.filterInvalid"),
        );
        return;
      }
    }
    setCreating(true);
    try {
      await workbuddyWorkflowsApi.createTriggerRegistration(workflowId, {
        kind: values.kind,
        name: values.name.trim(),
        cron_expression:
          values.kind === "cron"
            ? values.cron_expression?.trim() || null
            : null,
        event_name:
          values.kind === "event" ? values.event_name?.trim() || null : null,
        event_filter: eventFilter,
        tool_grants: (values.tool_grants ?? [])
          .map((tool) => tool.trim())
          .filter((tool) => tool.length > 0),
        kb_grants: (values.kb_grants ?? []).map((grant) => ({
          kb_id: grant.kb_id.trim(),
          permission: grant.permission,
        })),
        tolerance_seconds: values.tolerance_seconds,
        signature_header: values.signature_header.trim(),
        timestamp_header: values.timestamp_header.trim(),
      });
      message.success(t("workbuddy.workflows.create.created"));
      setCreateOpen(false);
      await registrations.reload();
    } catch (err) {
      message.error(
        apiErrorMessage(err, t("workbuddy.workflows.create.failed"), t),
      );
    } finally {
      setCreating(false);
    }
  };

  const rotate = useCallback(
    async (registrationId: string) => {
      if (!workflowId) return;
      setBusyId(registrationId);
      try {
        // The one-time secret is placed straight into the modal that shows it
        // once; it is never stored on the resource or logged.
        setSecret(
          await workbuddyWorkflowsApi.rotateTriggerSecret(
            workflowId,
            registrationId,
          ),
        );
        await registrations.reload();
      } catch (err) {
        message.error(
          apiErrorMessage(
            err,
            t("workbuddy.workflows.triggers.rotateFailed"),
            t,
          ),
        );
      } finally {
        setBusyId(null);
      }
    },
    [workflowId, registrations, t],
  );

  const testDelivery = useCallback(
    async (registrationId: string) => {
      setBusyId(registrationId);
      try {
        const result = await workbuddyWorkflowsApi.testTriggerDelivery(
          registrationId,
        );
        message.success(
          t("workbuddy.workflows.triggers.testDone", {
            id: result.execution_id.slice(0, 8),
          }),
        );
      } catch (err) {
        message.error(
          apiErrorMessage(err, t("workbuddy.workflows.triggers.testFailed"), t),
        );
      } finally {
        setBusyId(null);
      }
    },
    [t],
  );

  const revoke = useCallback(
    async (registrationId: string) => {
      if (!workflowId) return;
      setBusyId(registrationId);
      try {
        await workbuddyWorkflowsApi.revokeTriggerRegistration(
          workflowId,
          registrationId,
        );
        message.success(t("workbuddy.workflows.triggers.revoked"));
        await registrations.reload();
      } catch (err) {
        message.error(
          apiErrorMessage(
            err,
            t("workbuddy.workflows.triggers.revokeFailed"),
            t,
          ),
        );
      } finally {
        setBusyId(null);
      }
    },
    [workflowId, registrations, t],
  );

  const columns: ColumnsType<TriggerRegistration> = [
    {
      title: t("workbuddy.workflows.triggers.column.name"),
      dataIndex: "name",
      key: "name",
      width: 200,
      render: (name: string) => <Text strong>{name}</Text>,
    },
    {
      title: t("workbuddy.workflows.triggers.column.kind"),
      dataIndex: "kind",
      key: "kind",
      width: 110,
      render: (value: string) => (
        <Tag>{statusLabel(t, "workbuddy.workflows.triggers.kind", value)}</Tag>
      ),
    },
    {
      title: t("workbuddy.workflows.triggers.column.state"),
      key: "state",
      width: 120,
      render: (_value, row) => (
        <Tag color={row.revoked_at ? "red" : row.enabled ? "green" : "default"}>
          {row.revoked_at
            ? t("workbuddy.workflows.triggers.stateRevoked")
            : row.enabled
            ? t("workbuddy.workflows.triggers.stateEnabled")
            : t("workbuddy.workflows.triggers.stateDisabled")}
        </Tag>
      ),
    },
    {
      title: t("workbuddy.workflows.triggers.column.schedule"),
      key: "schedule",
      width: 220,
      render: (_value, row) => {
        if (row.kind === "cron") {
          return row.cron_expression ? (
            <Text code>{row.cron_expression}</Text>
          ) : (
            <Text type="secondary">—</Text>
          );
        }
        if (row.kind === "event") {
          return row.event_name ? (
            <Text code>{row.event_name}</Text>
          ) : (
            <Text type="secondary">—</Text>
          );
        }
        return row.webhook_path ? (
          <Space size={4}>
            <Text code>{row.webhook_path}</Text>
            <Button
              type="text"
              size="small"
              icon={<Link2 size={13} />}
              onClick={() => {
                const url = `${window.location.origin}/api/v1/webhooks/${row.webhook_path}`;
                void copyText(url).then((ok) => {
                  if (ok) message.success(t("common.copied"));
                  else message.error(t("common.copyFailed"));
                });
              }}
            />
          </Space>
        ) : (
          <Text type="secondary">—</Text>
        );
      },
    },
    {
      title: t("workbuddy.workflows.triggers.column.secret"),
      key: "secret",
      width: 120,
      render: (_value, row) =>
        row.has_secret ? (
          <Tag>
            {t("workbuddy.workflows.triggers.secretVersion", {
              version: row.secret_version,
            })}
          </Tag>
        ) : (
          <Text type="secondary">
            {t("workbuddy.workflows.triggers.secretNone")}
          </Text>
        ),
    },
    {
      title: t("workbuddy.workflows.triggers.column.createdAt"),
      dataIndex: "created_at",
      key: "created_at",
      width: 170,
      render: (value: number | null) =>
        value ? formatServerDateTime(value, timeZone) : "—",
    },
    {
      title: t("workbuddy.workflows.triggers.column.actions"),
      key: "actions",
      width: 300,
      fixed: "right",
      render: (_value, row) => (
        <Space size={4} wrap>
          <Popconfirm
            title={t("workbuddy.workflows.triggers.rotateConfirm", {
              name: row.name,
            })}
            onConfirm={() => void rotate(row.registration_id)}
          >
            <Button
              type="link"
              size="small"
              disabled={row.kind !== "webhook" || row.revoked_at !== null}
              loading={busyId === row.registration_id}
            >
              {t("workbuddy.workflows.triggers.rotate")}
            </Button>
          </Popconfirm>
          <Popconfirm
            title={t("workbuddy.workflows.triggers.testConfirm", {
              name: row.name,
            })}
            onConfirm={() => void testDelivery(row.registration_id)}
          >
            <Button type="link" size="small" disabled={row.revoked_at !== null}>
              {t("workbuddy.workflows.triggers.test")}
            </Button>
          </Popconfirm>
          <Popconfirm
            title={t("workbuddy.workflows.triggers.revokeConfirm", {
              name: row.name,
            })}
            onConfirm={() => void revoke(row.registration_id)}
          >
            <Button
              type="link"
              size="small"
              danger
              icon={<Trash2 size={13} />}
              disabled={row.revoked_at !== null}
            >
              {t("workbuddy.workflows.triggers.revoke")}
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  if (!workflowId) {
    return (
      <div className={styles.panel}>
        <TabPanelHeader
          icon={<Link2 size={16} />}
          title={t("workbuddy.workflows.triggers.title")}
          description={t("workbuddy.workflows.triggers.description")}
          actions={
            <WorkflowPicker
              options={options}
              value={workflowId}
              onChange={onSelectWorkflow}
              loading={optionsLoading}
            />
          }
        />
        <Alert
          type="info"
          showIcon
          message={t("workbuddy.workflows.state.noSelectionTitle")}
          description={t("workbuddy.workflows.state.noSelectionHint")}
        />
      </div>
    );
  }

  const failed =
    registrations.error !== null && registrations.error !== undefined;
  const forbidden = parseApiError(registrations.error)?.code === "FORBIDDEN";
  const blocked =
    failed || registrations.loading || registrations.data.items.length === 0;

  return (
    <div className={styles.panel}>
      <TabPanelHeader
        icon={<Link2 size={16} />}
        title={t("workbuddy.workflows.triggers.title")}
        description={t("workbuddy.workflows.triggers.description")}
        actions={
          <Space size={8} wrap>
            <WorkflowPicker
              options={options}
              value={workflowId}
              onChange={onSelectWorkflow}
              loading={optionsLoading}
            />
            <Button
              size="small"
              icon={<RefreshCw size={14} />}
              onClick={() => void registrations.reload()}
            >
              {t("common.refresh")}
            </Button>
            <Button
              size="small"
              type="primary"
              icon={<Plus size={14} />}
              onClick={openCreate}
            >
              {t("workbuddy.workflows.triggers.create")}
            </Button>
          </Space>
        }
      />

      {forbidden ? (
        <Alert
          type="warning"
          showIcon
          message={t("workbuddy.workflows.triggers.onlyManagers")}
          description={apiErrorMessage(
            registrations.error,
            t("workbuddy.workflows.state.errorFallback"),
            t,
          )}
        />
      ) : blocked ? (
        <ResourceState
          resource={registrations}
          isEmpty={
            !failed &&
            !registrations.loading &&
            registrations.data.items.length === 0
          }
          loadingLabel={t("common.loading")}
          errorTitle={t("workbuddy.workflows.triggers.loadFailed")}
          errorFallback={t("workbuddy.workflows.state.errorFallback")}
          unavailableTitle={t("workbuddy.workflows.state.unavailableTitle")}
          unavailableHint={t("workbuddy.workflows.state.unavailableHint")}
          emptyTitle={t("workbuddy.workflows.triggers.emptyTitle")}
          emptyHint={t("workbuddy.workflows.triggers.emptyHint")}
          emptyActionLabel={t("workbuddy.workflows.triggers.create")}
          onEmptyAction={openCreate}
        />
      ) : (
        <ResizableTable<TriggerRegistration>
          rowKey="registration_id"
          size="small"
          columns={columns}
          dataSource={registrations.data.items}
          pagination={false}
          scroll={{ x: 1340 }}
          storageKey="workbuddy-workflows-triggers"
        />
      )}

      <Modal
        open={createOpen}
        onCancel={() => setCreateOpen(false)}
        destroyOnHidden
        maskClosable={false}
        width={720}
        title={t("workbuddy.workflows.create.title")}
        footer={
          <Button
            type="primary"
            loading={creating}
            onClick={() => void create()}
          >
            {t("workbuddy.workflows.create.submit")}
          </Button>
        }
      >
        <Form form={form} layout="vertical" requiredMark={false}>
          <Form.Item
            name="kind"
            label={t("workbuddy.workflows.create.kind")}
            rules={[{ required: true }]}
          >
            <Select
              onChange={(value: TriggerKind) => setKind(value)}
              options={TRIGGER_KINDS.map((value) => ({
                value,
                label: statusLabel(
                  t,
                  "workbuddy.workflows.triggers.kind",
                  value,
                ),
              }))}
            />
          </Form.Item>
          <Form.Item
            name="name"
            label={t("workbuddy.workflows.create.name")}
            rules={[
              {
                required: true,
                whitespace: true,
                message: t("workbuddy.workflows.create.nameRequired"),
              },
            ]}
          >
            <Input
              placeholder={t("workbuddy.workflows.create.namePlaceholder")}
              maxLength={120}
            />
          </Form.Item>

          {kind === "cron" && (
            <Form.Item
              name="cron_expression"
              label={t("workbuddy.workflows.create.cron")}
              rules={[
                {
                  required: true,
                  whitespace: true,
                  message: t("workbuddy.workflows.create.cronRequired"),
                },
              ]}
            >
              <Input
                placeholder={t("workbuddy.workflows.create.cronPlaceholder")}
              />
            </Form.Item>
          )}
          {kind === "event" && (
            <>
              <Form.Item
                name="event_name"
                label={t("workbuddy.workflows.create.eventName")}
                rules={[
                  {
                    required: true,
                    whitespace: true,
                    message: t("workbuddy.workflows.create.eventRequired"),
                  },
                ]}
              >
                <Input
                  placeholder={t(
                    "workbuddy.workflows.create.eventNamePlaceholder",
                  )}
                />
              </Form.Item>
              <Form.Item
                name="event_filter"
                label={t("workbuddy.workflows.create.eventFilter")}
                extra={t("workbuddy.workflows.create.eventFilterHint")}
              >
                <Input.TextArea
                  autoSize={{ minRows: 3, maxRows: 8 }}
                  spellCheck={false}
                  className={styles.jsonEditor}
                />
              </Form.Item>
            </>
          )}

          <Form.Item
            name="tool_grants"
            label={t("workbuddy.workflows.create.tools")}
            extra={t("workbuddy.workflows.create.toolsHint")}
          >
            <Select mode="tags" tokenSeparators={[",", " "]} open={false} />
          </Form.Item>

          <Divider orientation="left" plain>
            {t("workbuddy.workflows.create.kbGrants")}
          </Divider>
          <Form.List name="kb_grants">
            {(fields, { add, remove }) => (
              <Space direction="vertical" size={8} style={{ width: "100%" }}>
                {fields.map((field) => (
                  <Space key={field.key} size={8} align="start">
                    <Form.Item
                      name={[field.name, "kb_id"]}
                      rules={[
                        {
                          required: true,
                          whitespace: true,
                          message: t("workbuddy.workflows.create.kbIdRequired"),
                        },
                      ]}
                      style={{ marginBottom: 0 }}
                    >
                      <Input
                        style={{ width: 320 }}
                        placeholder={t("workbuddy.workflows.create.kbId")}
                      />
                    </Form.Item>
                    <Form.Item
                      name={[field.name, "permission"]}
                      initialValue="read"
                      style={{ marginBottom: 0 }}
                    >
                      <Select
                        style={{ width: 120 }}
                        options={[
                          {
                            value: "read",
                            label: t(
                              "workbuddy.workflows.create.permissionRead",
                            ),
                          },
                          {
                            value: "write",
                            label: t(
                              "workbuddy.workflows.create.permissionWrite",
                            ),
                          },
                        ]}
                      />
                    </Form.Item>
                    <Button
                      type="text"
                      size="small"
                      danger
                      icon={<Trash2 size={13} />}
                      onClick={() => remove(field.name)}
                    />
                  </Space>
                ))}
                <Button
                  type="dashed"
                  size="small"
                  icon={<Plus size={13} />}
                  onClick={() => add({ kb_id: "", permission: "read" })}
                >
                  {t("workbuddy.workflows.create.addGrant")}
                </Button>
              </Space>
            )}
          </Form.List>

          <Divider orientation="left" plain>
            {t("workbuddy.workflows.create.advanced")}
          </Divider>
          <Form.Item
            name="tolerance_seconds"
            label={t("workbuddy.workflows.create.tolerance")}
            extra={t("workbuddy.workflows.create.toleranceHint")}
          >
            <InputNumber min={30} max={3600} style={{ width: 140 }} />
          </Form.Item>
          <Form.Item
            name="signature_header"
            label={t("workbuddy.workflows.create.signatureHeader")}
          >
            <Input style={{ width: 260 }} maxLength={64} />
          </Form.Item>
          <Form.Item
            name="timestamp_header"
            label={t("workbuddy.workflows.create.timestampHeader")}
          >
            <Input style={{ width: 260 }} maxLength={64} />
          </Form.Item>
        </Form>
      </Modal>

      <TriggerSecretModal secret={secret} onClose={() => setSecret(null)} />
    </div>
  );
}
