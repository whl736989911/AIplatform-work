/**
 * 企业治理 → 凭据.
 *
 * Credential metadata only: the API never returns the stored secret, so this
 * panel renders name / type / owner / status / revision / scopes and the
 * grant list. Secret material typed into the create + rotate dialogs lives in
 * form state only, is sent once, and is never echoed, cached or logged.
 */

import { useCallback, useMemo, useState } from "react";
import {
  Button,
  Drawer,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Spin,
  Tag,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { message } from "@/utils/antdMessage";
import {
  KeyRound,
  Plus,
  RefreshCw,
  RotateCcw,
  ShieldOff,
  Trash2,
  UserPlus,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import { ResizableTable } from "../../components/ResizableTable";
import { EmptyState } from "../../components/EmptyState";
import { useAsyncResource } from "../../hooks/useAsyncResource";
import { useServerTimezone } from "../../hooks/useServerTimezone";
import { formatServerDateTime } from "../../utils/formatMessageTime";
import { apiErrorMessage } from "../../utils/apiError";
import {
  enterpriseApi,
  type ConnectorCredential,
  type ConnectorCredentialCreate,
  type CredentialGrant,
  type TenantUser,
  type UserRef,
} from "../../api/modules/enterprise";
import { TabPanelHeader } from "../Settings/AdvancedSettings/TabPanelHeader";
import { statusLabel } from "./statusLabels";
import styles from "./index.module.less";

const { Text } = Typography;
const { TextArea } = Input;

interface CredentialFormValues {
  connector_type: string;
  display_name: string;
  secret: string;
  allowed_scopes?: string[];
}

interface RotateFormValues {
  secret: string;
}

/** Display form for opaque identifiers — never a full secret-bearing value. */
function maskId(value: UserRef | null | undefined): string {
  if (value === null || value === undefined) return "—";
  const text = String(value);
  if (text.length <= 8) return text;
  return `${text.slice(0, 8)}…`;
}

/** The API accepts a plain string or a JSON object; try JSON first. */
function parseSecretInput(raw: string): string | Record<string, unknown> {
  const trimmed = raw.trim();
  if (trimmed.startsWith("{")) {
    try {
      const parsed: unknown = JSON.parse(trimmed);
      if (isPlainObject(parsed)) return parsed;
    } catch {
      // fall through — sent as an opaque string, the server validates it
    }
  }
  return raw;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export default function CredentialsPanel({
  currentMemberId,
}: {
  currentMemberId: UserRef;
}) {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  const {
    data: credentials,
    loading,
    refresh,
    setData,
  } = useAsyncResource<ConnectorCredential[]>(
    [],
    async () => (await enterpriseApi.listCredentials()).items,
    [],
    { errorFallback: t("tenantGovernance.credentials.loadFailed"), t },
  );
  const { data: users } = useAsyncResource<TenantUser[]>(
    [],
    async () => enterpriseApi.listUsers(),
    [],
  );
  const [form] = Form.useForm<CredentialFormValues>();
  const [rotateForm] = Form.useForm<RotateFormValues>();
  const [createOpen, setCreateOpen] = useState(false);
  const [rotating, setRotating] = useState<ConnectorCredential | null>(null);
  const [saving, setSaving] = useState(false);
  const [grantsFor, setGrantsFor] = useState<ConnectorCredential | null>(null);
  const [grants, setGrants] = useState<CredentialGrant[]>([]);
  const [grantsLoading, setGrantsLoading] = useState(false);
  const [grantUserId, setGrantUserId] = useState<string | null>(null);

  const userOptions = useMemo(
    () =>
      users.map((item) => ({
        value: String(item.id),
        label: item.display_name?.trim()
          ? `${item.display_name} · ${item.email}`
          : item.email,
      })),
    [users],
  );

  const reloadGrants = useCallback(
    async (credential: ConnectorCredential) => {
      setGrantsLoading(true);
      try {
        setGrants((await enterpriseApi.listGrants(credential.id)).items);
      } catch (err) {
        message.error(
          apiErrorMessage(err, t("tenantGovernance.credentials.grantsFailed"), t),
        );
      } finally {
        setGrantsLoading(false);
      }
    },
    [t],
  );

  const openGrants = useCallback(
    (credential: ConnectorCredential) => {
      setGrantsFor(credential);
      setGrantUserId(null);
      setGrants([]);
      void reloadGrants(credential);
    },
    [reloadGrants],
  );

  const onCreate = async (values: CredentialFormValues) => {
    setSaving(true);
    try {
      const body: ConnectorCredentialCreate = {
        connector_type: values.connector_type.trim(),
        display_name: values.display_name.trim(),
        secret: parseSecretInput(values.secret),
        allowed_scopes: (values.allowed_scopes ?? []).map((scope) => scope.trim()),
      };
      const created = await enterpriseApi.createCredential(body);
      setData((prev) => [created, ...prev]);
      setCreateOpen(false);
      form.resetFields();
      message.success(t("tenantGovernance.credentials.createSuccess"));
    } catch (err) {
      message.error(
        apiErrorMessage(err, t("tenantGovernance.credentials.createFailed"), t),
      );
    } finally {
      setSaving(false);
    }
  };

  const onRotate = async (values: RotateFormValues) => {
    if (!rotating) return;
    setSaving(true);
    try {
      const updated = await enterpriseApi.rotateCredential(
        rotating.id,
        parseSecretInput(values.secret),
      );
      setData((prev) =>
        prev.map((row) => (row.id === updated.id ? updated : row)),
      );
      setRotating(null);
      rotateForm.resetFields();
      message.success(t("tenantGovernance.credentials.rotateSuccess"));
    } catch (err) {
      message.error(
        apiErrorMessage(err, t("tenantGovernance.credentials.rotateFailed"), t),
      );
    } finally {
      setSaving(false);
    }
  };

  const revoke = async (row: ConnectorCredential) => {
    try {
      const updated = await enterpriseApi.revokeCredential(row.id);
      setData((prev) => prev.map((item) => (item.id === updated.id ? updated : item)));
      message.success(t("tenantGovernance.credentials.revokeSuccess"));
    } catch (err) {
      message.error(
        apiErrorMessage(err, t("tenantGovernance.credentials.revokeFailed"), t),
      );
    }
  };

  const addGrant = async () => {
    if (!grantsFor || !grantUserId) return;
    try {
      const grant = await enterpriseApi.createGrant(grantsFor.id, grantUserId);
      setGrants((prev) => [...prev, grant]);
      setGrantUserId(null);
      message.success(t("tenantGovernance.credentials.grantSuccess"));
    } catch (err) {
      message.error(
        apiErrorMessage(err, t("tenantGovernance.credentials.grantFailed"), t),
      );
    }
  };

  const removeGrant = async (userId: UserRef) => {
    if (!grantsFor) return;
    try {
      await enterpriseApi.deleteGrant(grantsFor.id, userId);
      setGrants((prev) =>
        prev.filter((grant) => String(grant.user_id) !== String(userId)),
      );
      message.success(t("tenantGovernance.credentials.grantRevokeSuccess"));
    } catch (err) {
      message.error(
        apiErrorMessage(
          err,
          t("tenantGovernance.credentials.grantRevokeFailed"),
          t,
        ),
      );
    }
  };

  const columns: ColumnsType<ConnectorCredential> = [
    {
      title: t("tenantGovernance.credentials.displayName"),
      dataIndex: "display_name",
      key: "display_name",
      width: 200,
    },
    {
      title: t("tenantGovernance.credentials.connectorType"),
      dataIndex: "connector_type",
      key: "connector_type",
      width: 160,
    },
    {
      title: t("tenantGovernance.credentials.owner"),
      dataIndex: "owner_id",
      key: "owner_id",
      width: 140,
      render: (ownerId: UserRef) => (
        <Space size={6}>
          <Text code>{maskId(ownerId)}</Text>
          {String(ownerId) === String(currentMemberId) && (
            <Tag color="blue">{t("tenantGovernance.credentials.mine")}</Tag>
          )}
        </Space>
      ),
    },
    {
      title: t("tenantGovernance.credentials.status"),
      dataIndex: "status",
      key: "status",
      width: 120,
      render: (status: string) => (
        <Tag color={status === "active" ? "green" : "red"}>
          {statusLabel(t, "credential", status)}
        </Tag>
      ),
    },
    {
      title: t("tenantGovernance.credentials.revision"),
      dataIndex: "revision",
      key: "revision",
      width: 90,
    },
    {
      title: t("tenantGovernance.credentials.scopes"),
      dataIndex: "allowed_scopes",
      key: "allowed_scopes",
      width: 220,
      render: (scopes: string[]) =>
        scopes.length === 0 ? (
          <Text type="secondary">{t("tenantGovernance.credentials.noScopes")}</Text>
        ) : (
          <Space size={4} wrap>
            {scopes.map((scope) => (
              <Tag key={scope}>{scope}</Tag>
            ))}
          </Space>
        ),
    },
    {
      title: t("tenantGovernance.credentials.rotatedAt"),
      dataIndex: "rotated_at",
      key: "rotated_at",
      width: 180,
      render: (value: number | null) =>
        value ? formatServerDateTime(value, timeZone) : "—",
    },
    {
      title: t("tenantGovernance.credentials.revokedAt"),
      dataIndex: "revoked_at",
      key: "revoked_at",
      width: 180,
      render: (value: number | null) =>
        value ? formatServerDateTime(value, timeZone) : "—",
    },
    {
      title: t("common.actions"),
      key: "actions",
      width: 260,
      render: (_value, row) =>
        row.status === "revoked" ? (
          <Text type="secondary">{t("tenantGovernance.credentials.revokedHint")}</Text>
        ) : (
          <Space size={2} wrap>
            <Button
              type="link"
              size="small"
              icon={<RotateCcw size={14} />}
              onClick={() => {
                rotateForm.resetFields();
                setRotating(row);
              }}
            >
              {t("tenantGovernance.credentials.rotate")}
            </Button>
            <Button
              type="link"
              size="small"
              icon={<UserPlus size={14} />}
              onClick={() => openGrants(row)}
            >
              {t("tenantGovernance.credentials.grants")}
            </Button>
            <Popconfirm
              title={t("tenantGovernance.credentials.revokeConfirm", {
                name: row.display_name,
              })}
              description={t("tenantGovernance.credentials.revokeConfirmHint")}
              okText={t("common.confirm")}
              cancelText={t("common.cancel")}
              onConfirm={() => void revoke(row)}
            >
              <Button
                type="link"
                size="small"
                danger
                icon={<ShieldOff size={14} />}
              >
                {t("tenantGovernance.credentials.revoke")}
              </Button>
            </Popconfirm>
          </Space>
        ),
    },
  ];

  return (
    <div className={styles.panel}>
      <TabPanelHeader
        icon={<KeyRound size={18} />}
        title={t("tenantGovernance.credentials.title")}
        description={t("tenantGovernance.credentials.desc")}
        actions={
          <Space size={8}>
            <Button
              size="small"
              icon={<RefreshCw size={14} />}
              onClick={() => void refresh()}
            >
              {t("common.refresh")}
            </Button>
            <Button
              type="primary"
              size="small"
              icon={<Plus size={14} />}
              onClick={() => {
                form.resetFields();
                setCreateOpen(true);
              }}
            >
              {t("tenantGovernance.credentials.create")}
            </Button>
          </Space>
        }
      />

      {loading && credentials.length === 0 ? (
        <div className={styles.centered}>
          <Spin />
        </div>
      ) : credentials.length === 0 ? (
        <EmptyState
          variant="mascot"
          title={t("tenantGovernance.credentials.empty")}
          description={t("tenantGovernance.credentials.emptyHint")}
        />
      ) : (
        <ResizableTable
          columns={columns}
          dataSource={credentials}
          rowKey="id"
          size="middle"
          tableLayout="fixed"
          scroll={{ x: 1560 }}
          storageKey="enterprise-credentials-table-widths"
          minWidth={72}
          pagination={false}
        />
      )}

      <Modal
        title={t("tenantGovernance.credentials.createTitle")}
        open={createOpen}
        onCancel={() => setCreateOpen(false)}
        onOk={() => form.submit()}
        confirmLoading={saving}
        okText={t("common.create")}
        cancelText={t("common.cancel")}
        destroyOnHidden
      >
        <Form
          form={form}
          layout="vertical"
          requiredMark={false}
          onFinish={(values) => void onCreate(values)}
        >
          <Form.Item
            name="display_name"
            label={t("tenantGovernance.credentials.displayName")}
            rules={[
              {
                required: true,
                message: t("tenantGovernance.credentials.displayNameRequired"),
              },
            ]}
          >
            <Input
              maxLength={80}
              placeholder={t(
                "tenantGovernance.credentials.displayNamePlaceholder",
              )}
            />
          </Form.Item>
          <Form.Item
            name="connector_type"
            label={t("tenantGovernance.credentials.connectorType")}
            extra={t("tenantGovernance.credentials.connectorTypeHint")}
            rules={[
              {
                required: true,
                message: t("tenantGovernance.credentials.connectorTypeRequired"),
              },
            ]}
          >
            <Input
              maxLength={64}
              placeholder={t(
                "tenantGovernance.credentials.connectorTypePlaceholder",
              )}
            />
          </Form.Item>
          <Form.Item
            name="allowed_scopes"
            label={t("tenantGovernance.credentials.scopes")}
            extra={t("tenantGovernance.credentials.scopesHint")}
          >
            <Select
              mode="tags"
              tokenSeparators={[",", " "]}
              notFoundContent={null}
              placeholder={t("tenantGovernance.credentials.scopesPlaceholder")}
            />
          </Form.Item>
          <Form.Item
            name="secret"
            label={t("tenantGovernance.credentials.secret")}
            extra={t("tenantGovernance.credentials.secretHint")}
            rules={[
              {
                required: true,
                message: t("tenantGovernance.credentials.secretRequired"),
              },
            ]}
          >
            <TextArea
              rows={4}
              autoComplete="off"
              spellCheck={false}
              placeholder={t("tenantGovernance.credentials.secretPlaceholder")}
            />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={t("tenantGovernance.credentials.rotateTitle", {
          name: rotating?.display_name ?? "",
        })}
        open={rotating !== null}
        onCancel={() => setRotating(null)}
        onOk={() => rotateForm.submit()}
        confirmLoading={saving}
        okText={t("tenantGovernance.credentials.rotate")}
        cancelText={t("common.cancel")}
        destroyOnHidden
      >
        <Form
          form={rotateForm}
          layout="vertical"
          requiredMark={false}
          onFinish={(values) => void onRotate(values)}
        >
          <Form.Item
            name="secret"
            label={t("tenantGovernance.credentials.secret")}
            extra={t("tenantGovernance.credentials.rotateHint")}
            rules={[
              {
                required: true,
                message: t("tenantGovernance.credentials.secretRequired"),
              },
            ]}
          >
            <TextArea
              rows={4}
              autoComplete="off"
              spellCheck={false}
              placeholder={t("tenantGovernance.credentials.secretPlaceholder")}
            />
          </Form.Item>
        </Form>
      </Modal>

      <Drawer
        title={t("tenantGovernance.credentials.grantsTitle", {
          name: grantsFor?.display_name ?? "",
        })}
        open={grantsFor !== null}
        onClose={() => setGrantsFor(null)}
        width={Math.min(
          520,
          typeof window !== "undefined" ? window.innerWidth - 24 : 520,
        )}
        destroyOnHidden
      >
        <Space direction="vertical" size={12} style={{ width: "100%" }}>
          <Space.Compact style={{ width: "100%" }}>
            <Select
              showSearch
              allowClear
              optionFilterProp="label"
              value={grantUserId}
              onChange={setGrantUserId}
              placeholder={t("tenantGovernance.credentials.grantUserPlaceholder")}
              options={userOptions}
              style={{ flex: 1, minWidth: 0 }}
            />
            <Button
              type="primary"
              icon={<UserPlus size={14} />}
              disabled={!grantUserId}
              onClick={() => void addGrant()}
            >
              {t("tenantGovernance.credentials.grant")}
            </Button>
          </Space.Compact>

          {grantsLoading ? (
            <div className={styles.centered}>
              <Spin />
            </div>
          ) : grants.length === 0 ? (
            <EmptyState
              title={t("tenantGovernance.credentials.grantsEmpty")}
              description={t("tenantGovernance.credentials.grantsEmptyHint")}
            />
          ) : (
            <div className={styles.grantList}>
              {grants.map((grant) => (
                <div key={grant.id} className={styles.grantRow}>
                  <div className={styles.grantMeta}>
                    <Text code>{maskId(grant.user_id)}</Text>
                    <Text type="secondary" className={styles.sectionHint}>
                      {grant.created_at
                        ? formatServerDateTime(grant.created_at, timeZone)
                        : "—"}
                    </Text>
                  </div>
                  <Popconfirm
                    title={t("tenantGovernance.credentials.grantRevokeConfirm")}
                    okText={t("common.confirm")}
                    cancelText={t("common.cancel")}
                    onConfirm={() => void removeGrant(grant.user_id)}
                  >
                    <Button
                      type="text"
                      size="small"
                      danger
                      icon={<Trash2 size={14} />}
                    >
                      {t("tenantGovernance.credentials.grantRevoke")}
                    </Button>
                  </Popconfirm>
                </div>
              ))}
            </div>
          )}
        </Space>
      </Drawer>
    </div>
  );
}
