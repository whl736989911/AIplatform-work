/**
 * 企业治理 → 邀请.
 *
 * The list endpoint returns invitation metadata only; the raw token exists
 * solely in the creation response and is shown once in a dialog that is
 * destroyed on close (never cached, never logged).
 */

import { useCallback, useMemo, useState } from "react";
import {
  Button,
  Form,
  Input,
  InputNumber,
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
import { MailPlus, RefreshCw, Trash2 } from "lucide-react";
import { useTranslation } from "react-i18next";
import { ResizableTable } from "../../components/ResizableTable";
import { EmptyState } from "../../components/EmptyState";
import { useAsyncResource } from "../../hooks/useAsyncResource";
import { useServerTimezone } from "../../hooks/useServerTimezone";
import { formatServerDateTime } from "../../utils/formatMessageTime";
import { apiErrorMessage } from "../../utils/apiError";
import {
  enterpriseApi,
  type Department,
  type Invitation,
  type InvitationCreate,
  type InvitationCreated,
} from "../../api/modules/enterprise";
import { TabPanelHeader } from "../Settings/AdvancedSettings/TabPanelHeader";
import { statusLabel } from "./statusLabels";
import OneTimeTokenModal from "./OneTimeTokenModal";
import styles from "./index.module.less";

const { Text } = Typography;

interface InvitationFormValues {
  email: string;
  role: "admin" | "member";
  department_id?: string | null;
  expires_in_hours?: number | null;
}

export default function InvitationsPanel() {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  const {
    data: invitations,
    loading,
    refresh,
    setData,
  } = useAsyncResource<Invitation[]>(
    [],
    () => enterpriseApi.listInvitations(),
    [],
    { errorFallback: t("tenantGovernance.invitations.loadFailed"), t },
  );
  const { data: departments } = useAsyncResource<Department[]>(
    [],
    () => enterpriseApi.listDepartments(),
    [],
  );
  const [form] = Form.useForm<InvitationFormValues>();
  const [createOpen, setCreateOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [created, setCreated] = useState<InvitationCreated | null>(null);
  const [revokingId, setRevokingId] = useState<string | null>(null);

  const departmentOptions = useMemo(
    () => departments.map((item) => ({ value: item.id, label: item.name })),
    [departments],
  );

  const onCreate = async (values: InvitationFormValues) => {
    setCreating(true);
    try {
      const body: InvitationCreate = {
        email: values.email.trim(),
        role: values.role,
      };
      if (values.department_id) body.department_id = values.department_id;
      if (typeof values.expires_in_hours === "number") {
        body.expires_in_hours = values.expires_in_hours;
      }
      const invitation = await enterpriseApi.createInvitation(body);
      setCreated(invitation);
      setCreateOpen(false);
      message.success(t("tenantGovernance.invitations.createSuccess"));
      await refresh();
    } catch (err) {
      message.error(
        apiErrorMessage(err, t("tenantGovernance.invitations.createFailed"), t),
      );
    } finally {
      setCreating(false);
    }
  };

  const revoke = useCallback(
    async (row: Invitation) => {
      setRevokingId(row.id);
      try {
        const revoked = await enterpriseApi.revokeInvitation(row.id);
        setData((prev) =>
          prev.map((item) =>
            item.id === revoked.id
              ? {
                  ...item,
                  status: revoked.status,
                  revoked_at: revoked.revoked_at ?? item.revoked_at ?? null,
                }
              : item,
          ),
        );
        message.success(t("tenantGovernance.invitations.revokeSuccess"));
      } catch (err) {
        message.error(
          apiErrorMessage(
            err,
            t("tenantGovernance.invitations.revokeFailed"),
            t,
          ),
        );
      } finally {
        setRevokingId(null);
      }
    },
    [setData, t],
  );

  const columns: ColumnsType<Invitation> = [
    {
      title: t("tenantGovernance.invitations.email"),
      dataIndex: "email",
      key: "email",
      width: 260,
    },
    {
      title: t("tenantGovernance.invitations.role"),
      dataIndex: "role",
      key: "role",
      width: 120,
      render: (role: string) => statusLabel(t, "role", role),
    },
    {
      title: t("tenantGovernance.invitations.status"),
      dataIndex: "status",
      key: "status",
      width: 120,
      render: (status: string) => (
        <Tag color={status === "pending" ? "processing" : "default"}>
          {statusLabel(t, "invitation", status)}
        </Tag>
      ),
    },
    {
      title: t("tenantGovernance.invitations.createdAt"),
      dataIndex: "created_at",
      key: "created_at",
      width: 200,
      render: (value: number | null) =>
        value ? formatServerDateTime(value, timeZone) : "—",
    },
    {
      title: t("tenantGovernance.invitations.expiresAt"),
      dataIndex: "expires_at",
      key: "expires_at",
      width: 200,
      render: (value: number | null) =>
        value ? formatServerDateTime(value, timeZone) : "—",
    },
    {
      title: t("common.actions"),
      key: "actions",
      width: 140,
      render: (_value, row) =>
        row.status === "pending" ? (
          <Popconfirm
            title={t("tenantGovernance.invitations.revokeConfirm", {
              email: row.email,
            })}
            description={t("tenantGovernance.invitations.revokeConfirmHint")}
            okText={t("common.confirm")}
            cancelText={t("common.cancel")}
            onConfirm={() => void revoke(row)}
          >
            <Button
              type="link"
              size="small"
              danger
              icon={<Trash2 size={14} />}
              loading={revokingId === row.id}
            >
              {t("tenantGovernance.invitations.revoke")}
            </Button>
          </Popconfirm>
        ) : (
          <Text type="secondary">
            {t("tenantGovernance.invitations.terminalHint")}
          </Text>
        ),
    },
  ];

  return (
    <div className={styles.panel}>
      <TabPanelHeader
        icon={<MailPlus size={18} />}
        title={t("tenantGovernance.invitations.title")}
        description={t("tenantGovernance.invitations.desc")}
        actions={
          <Space size={8}>
            <Button
              size="small"
              icon={<RefreshCw size={14} />}
              onClick={() => void refresh()}
              loading={loading}
            >
              {t("common.refresh")}
            </Button>
            <Button
              type="primary"
              size="small"
              icon={<MailPlus size={14} />}
              onClick={() => {
                form.setFieldsValue({
                  email: "",
                  role: "member",
                  department_id: null,
                  expires_in_hours: null,
                });
                setCreateOpen(true);
              }}
            >
              {t("tenantGovernance.invitations.create")}
            </Button>
          </Space>
        }
      />

      {loading && invitations.length === 0 ? (
        <div className={styles.centered}>
          <Spin />
        </div>
      ) : invitations.length === 0 ? (
        <EmptyState
          variant="mascot"
          title={t("tenantGovernance.invitations.empty")}
          description={t("tenantGovernance.invitations.emptyHint")}
        />
      ) : (
        <ResizableTable
          columns={columns}
          dataSource={invitations}
          rowKey="id"
          size="middle"
          tableLayout="fixed"
          pagination={false}
          storageKey="enterprise-invitations-table-widths"
          minWidth={72}
          scroll={{ x: 1040 }}
        />
      )}

      <Modal
        title={t("tenantGovernance.invitations.createTitle")}
        open={createOpen}
        onCancel={() => setCreateOpen(false)}
        onOk={() => form.submit()}
        confirmLoading={creating}
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
            name="email"
            label={t("tenantGovernance.invitations.email")}
            rules={[
              {
                required: true,
                message: t("tenantGovernance.invitations.emailRequired"),
              },
              {
                type: "email",
                message: t("tenantGovernance.invitations.emailInvalid"),
              },
            ]}
          >
            <Input
              maxLength={254}
              placeholder={t("tenantGovernance.invitations.emailPlaceholder")}
            />
          </Form.Item>
          <Form.Item
            name="role"
            label={t("tenantGovernance.invitations.role")}
            rules={[
              {
                required: true,
                message: t("tenantGovernance.invitations.roleRequired"),
              },
            ]}
          >
            <Select
              options={[
                {
                  value: "member",
                  label: t("tenantGovernance.status.role.member"),
                },
                {
                  value: "admin",
                  label: t("tenantGovernance.status.role.admin"),
                },
              ]}
            />
          </Form.Item>
          <Form.Item
            name="department_id"
            label={t("tenantGovernance.invitations.department")}
          >
            <Select
              allowClear
              showSearch
              optionFilterProp="label"
              placeholder={t(
                "tenantGovernance.invitations.departmentPlaceholder",
              )}
              options={departmentOptions}
            />
          </Form.Item>
          <Form.Item
            name="expires_in_hours"
            label={t("tenantGovernance.invitations.expiresInHours")}
            extra={t("tenantGovernance.invitations.expiresInHoursHint")}
          >
            <InputNumber
              min={1}
              max={720}
              style={{ width: "100%" }}
              placeholder={t(
                "tenantGovernance.invitations.expiresInHoursPlaceholder",
              )}
            />
          </Form.Item>
        </Form>
      </Modal>

      <OneTimeTokenModal
        open={created !== null}
        token={created?.invite_token ?? created?.invitation_token ?? ""}
        email={created?.email ?? ""}
        onClose={() => setCreated(null)}
      />
    </div>
  );
}
