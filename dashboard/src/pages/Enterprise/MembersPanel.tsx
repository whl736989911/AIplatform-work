/**
 * 企业治理 → 成员.
 * Tenant admins list tenant users and patch display name / role / department /
 * status. The current member is marked and cannot be disabled from here
 * (server-side rules still apply).
 */

import { useCallback, useMemo, useState } from "react";
import {
  Button,
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
import { Pencil, RefreshCw, Users } from "lucide-react";
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
  type TenantUser,
  type TenantUserStatus,
  type TenantUserUpdate,
} from "../../api/modules/enterprise";
import { TabPanelHeader } from "../Settings/AdvancedSettings/TabPanelHeader";
import { statusLabel } from "./statusLabels";
import styles from "./index.module.less";

const { Text } = Typography;

interface MemberFormValues {
  display_name?: string | null;
  role: "owner" | "admin" | "member";
  department_id?: string | null;
  status: string;
}

export default function MembersPanel({
  currentMemberId,
}: {
  currentMemberId: string | number;
}) {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  const {
    data: members,
    loading,
    refresh,
    setData,
  } = useAsyncResource<TenantUser[]>(
    [],
    async () => enterpriseApi.listUsers(),
    [],
    { errorFallback: t("tenantGovernance.members.loadFailed"), t },
  );
  const { data: departments } = useAsyncResource<Department[]>(
    [],
    async () => enterpriseApi.listDepartments(),
    [],
  );
  const [form] = Form.useForm<MemberFormValues>();
  const [editing, setEditing] = useState<TenantUser | null>(null);
  const [saving, setSaving] = useState(false);

  const departmentNameById = useMemo(() => {
    const map = new Map<string, string>();
    for (const item of departments) map.set(item.id, item.name);
    return map;
  }, [departments]);

  const departmentOptions = useMemo(
    () =>
      departments.map((item) => ({ value: item.id, label: item.name })),
    [departments],
  );

  const openEdit = useCallback(
    (row: TenantUser) => {
      setEditing(row);
      form.setFieldsValue({
        display_name: row.display_name,
        role: row.role,
        department_id: row.department_id,
        status: row.status,
      });
    },
    [form],
  );

  const onSubmit = async (values: MemberFormValues) => {
    if (!editing) return;
    setSaving(true);
    try {
      const body: TenantUserUpdate = {
        display_name: values.display_name?.trim() ? values.display_name.trim() : null,
        role: values.role,
        department_id: values.department_id ?? null,
        status: values.status === "suspended" ? "suspended" : "active",
      };
      const updated = await enterpriseApi.updateUser(editing.id, body);
      setData((prev) => prev.map((row) => (row.id === updated.id ? updated : row)));
      message.success(t("tenantGovernance.members.updateSuccess"));
      setEditing(null);
    } catch (err) {
      message.error(
        apiErrorMessage(err, t("tenantGovernance.members.updateFailed"), t),
      );
    } finally {
      setSaving(false);
    }
  };

  const setStatus = async (
    row: TenantUser,
    status: TenantUserStatus,
  ) => {
    try {
      const updated = await enterpriseApi.updateUser(row.id, { status });
      setData((prev) => prev.map((item) => (item.id === row.id ? updated : item)));
      message.success(t("tenantGovernance.members.statusSaved"));
    } catch (err) {
      message.error(
        apiErrorMessage(err, t("tenantGovernance.members.updateFailed"), t),
      );
    }
  };

  const columns: ColumnsType<TenantUser> = [
    {
      title: t("tenantGovernance.members.email"),
      dataIndex: "email",
      key: "email",
      width: 240,
      render: (email: string, row) => (
        <Space size={6}>
          <span>{email}</span>
          {String(row.id) === String(currentMemberId) && (
            <Tag color="blue">{t("tenantGovernance.members.self")}</Tag>
          )}
        </Space>
      ),
    },
    {
      title: t("tenantGovernance.members.displayName"),
      dataIndex: "display_name",
      key: "display_name",
      width: 180,
      render: (value: string | null) =>
        value?.trim() ? value : <Text type="secondary">—</Text>,
    },
    {
      title: t("tenantGovernance.members.role"),
      dataIndex: "role",
      key: "role",
      width: 120,
      render: (role: string) => (
        <Tag color={role === "admin" ? "gold" : "default"}>
          {statusLabel(t, "role", role)}
        </Tag>
      ),
    },
    {
      title: t("tenantGovernance.members.department"),
      dataIndex: "department_id",
      key: "department_id",
      width: 180,
      render: (departmentId: string | null) =>
        departmentId
          ? departmentNameById.get(departmentId) ?? departmentId
          : t("tenantGovernance.members.noDepartment"),
    },
    {
      title: t("tenantGovernance.members.status"),
      dataIndex: "status",
      key: "status",
      width: 120,
      render: (status: string) => (
        <Tag color={status === "active" ? "green" : "red"}>
          {statusLabel(t, "user", status)}
        </Tag>
      ),
    },
    {
      title: t("tenantGovernance.members.createdAt"),
      dataIndex: "created_at",
      key: "created_at",
      width: 180,
      render: (value: number | null) =>
        value ? formatServerDateTime(value, timeZone) : "—",
    },
    {
      title: t("common.actions"),
      key: "actions",
      width: 200,
      render: (_value, row) => (
        <Space size={4}>
          <Button
            type="link"
            size="small"
            icon={<Pencil size={14} />}
            onClick={() => openEdit(row)}
          >
            {t("common.edit")}
          </Button>
          {row.status === "active" ? (
            <Popconfirm
              title={t("tenantGovernance.members.disableConfirm", {
                name: row.display_name?.trim() || row.email,
              })}
              description={t("tenantGovernance.members.disableConfirmHint")}
              okText={t("common.confirm")}
              cancelText={t("common.cancel")}
              onConfirm={() => void setStatus(row, "suspended")}
            >
              <Button type="link" size="small" danger>
                {t("common.disable")}
              </Button>
            </Popconfirm>
          ) : (
            <Button
              type="link"
              size="small"
              onClick={() => void setStatus(row, "active")}
            >
              {t("common.enable")}
            </Button>
          )}
        </Space>
      ),
    },
  ];

  return (
    <div className={styles.panel}>
      <TabPanelHeader
        icon={<Users size={18} />}
        title={t("tenantGovernance.members.title")}
        description={t("tenantGovernance.members.desc")}
        actions={
          <Button
            size="small"
            icon={<RefreshCw size={14} />}
            onClick={() => void refresh()}
          >
            {t("common.refresh")}
          </Button>
        }
      />

      {loading && members.length === 0 ? (
        <div className={styles.centered}>
          <Spin />
        </div>
      ) : members.length === 0 ? (
        <EmptyState
          variant="mascot"
          title={t("tenantGovernance.members.empty")}
          description={t("tenantGovernance.members.emptyHint")}
        />
      ) : (
        <ResizableTable
          columns={columns}
          dataSource={members}
          rowKey="id"
          size="middle"
          tableLayout="fixed"
          scroll={{ x: 1220 }}
          storageKey="enterprise-members-table-widths"
          minWidth={72}
          pagination={false}
        />
      )}

      <Modal
        title={t("tenantGovernance.members.editTitle")}
        open={editing !== null}
        onCancel={() => setEditing(null)}
        onOk={() => form.submit()}
        confirmLoading={saving}
        okText={t("common.save")}
        cancelText={t("common.cancel")}
        destroyOnHidden
      >
        <Form
          form={form}
          layout="vertical"
          requiredMark={false}
          onFinish={(values) => void onSubmit(values)}
        >
          <Form.Item
            name="display_name"
            label={t("tenantGovernance.members.displayName")}
          >
            <Input
              maxLength={64}
              placeholder={t("tenantGovernance.members.displayNamePlaceholder")}
              allowClear
            />
          </Form.Item>
          <Form.Item
            name="role"
            label={t("tenantGovernance.members.role")}
            rules={[
              {
                required: true,
                message: t("tenantGovernance.members.roleRequired"),
              },
            ]}
          >
            <Select
              options={[
                {
                  value: "admin",
                  label: t("tenantGovernance.status.role.admin"),
                },
                {
                  value: "member",
                  label: t("tenantGovernance.status.role.member"),
                },
              ]}
            />
          </Form.Item>
          <Form.Item
            name="department_id"
            label={t("tenantGovernance.members.department")}
          >
            <Select
              allowClear
              showSearch
              optionFilterProp="label"
              placeholder={t("tenantGovernance.members.departmentPlaceholder")}
              options={departmentOptions}
            />
          </Form.Item>
          <Form.Item name="status" label={t("tenantGovernance.members.status")}>
            <Select
              options={[
                {
                  value: "active",
                  label: t("tenantGovernance.status.user.active"),
                },
                {
                  value: "disabled",
                  label: t("tenantGovernance.status.user.disabled"),
                },
              ]}
            />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
