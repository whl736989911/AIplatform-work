/**
 * 企业治理 → 部门.
 * Members read the tree; tenant admins create / rename / re-parent / disable.
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
import { Building2, Pencil, Plus, RefreshCw } from "lucide-react";
import { useTranslation } from "react-i18next";
import { ResizableTable } from "../../components/ResizableTable";
import { EmptyState } from "../../components/EmptyState";
import { useAsyncResource } from "../../hooks/useAsyncResource";
import { apiErrorMessage } from "../../utils/apiError";
import {
  enterpriseApi,
  type Department,
  type DepartmentUpdate,
} from "../../api/modules/enterprise";
import { TabPanelHeader } from "../Settings/AdvancedSettings/TabPanelHeader";
import { statusLabel } from "./statusLabels";
import styles from "./index.module.less";

const { Text } = Typography;

interface DepartmentFormValues {
  name: string;
  parent_id?: string | null;
  status?: string;
}

/** A department may not become its own ancestor. */
function collectSubtree(departments: Department[], rootId: string): Set<string> {
  const childrenByParent = new Map<string | null, Department[]>();
  for (const item of departments) {
    const siblings = childrenByParent.get(item.parent_id);
    if (siblings) siblings.push(item);
    else childrenByParent.set(item.parent_id, [item]);
  }
  const blocked = new Set<string>([rootId]);
  const queue = [rootId];
  while (queue.length > 0) {
    const current = queue.shift();
    if (current === undefined) break;
    for (const child of childrenByParent.get(current) ?? []) {
      if (blocked.has(child.id)) continue;
      blocked.add(child.id);
      queue.push(child.id);
    }
  }
  return blocked;
}

export default function DepartmentsPanel({
  canManage,
}: {
  canManage: boolean;
}) {
  const { t } = useTranslation();
  const {
    data: departments,
    loading,
    refresh,
    setData,
  } = useAsyncResource<Department[]>(
    [],
    async () => enterpriseApi.listDepartments(),
    [],
    { errorFallback: t("tenantGovernance.departments.loadFailed"), t },
  );
  const [form] = Form.useForm<DepartmentFormValues>();
  const [editing, setEditing] = useState<Department | null>(null);
  const [formOpen, setFormOpen] = useState(false);
  const [saving, setSaving] = useState(false);

  const nameById = useMemo(() => {
    const map = new Map<string, string>();
    for (const item of departments) map.set(item.id, item.name);
    return map;
  }, [departments]);

  const blockedIds = useMemo(
    () => (editing ? collectSubtree(departments, editing.id) : new Set<string>()),
    [departments, editing],
  );

  const parentOptions = useMemo(
    () =>
      departments
        .filter((item) => !blockedIds.has(item.id))
        .map((item) => ({ value: item.id, label: item.name })),
    [departments, blockedIds],
  );

  const openCreate = useCallback(() => {
    setEditing(null);
    form.setFieldsValue({ name: "", parent_id: null, status: "active" });
    setFormOpen(true);
  }, [form]);

  const openEdit = useCallback(
    (row: Department) => {
      setEditing(row);
      form.setFieldsValue({
        name: row.name,
        parent_id: row.parent_id,
        status: row.status,
      });
      setFormOpen(true);
    },
    [form],
  );

  const onSubmit = async (values: DepartmentFormValues) => {
    setSaving(true);
    try {
      if (editing) {
        const body: DepartmentUpdate = {
          name: values.name.trim(),
          parent_id: values.parent_id ?? null,
          status: values.status,
        };
        const updated = await enterpriseApi.updateDepartment(editing.id, body);
        setData((prev) =>
          prev.map((row) => (row.id === updated.id ? updated : row)),
        );
        message.success(t("tenantGovernance.departments.updateSuccess"));
      } else {
        const created = await enterpriseApi.createDepartment({
          name: values.name.trim(),
          parent_id: values.parent_id ?? null,
        });
        setData((prev) => [...prev, created]);
        message.success(t("tenantGovernance.departments.createSuccess"));
      }
      setFormOpen(false);
    } catch (err) {
      message.error(
        apiErrorMessage(err, t("tenantGovernance.departments.saveFailed"), t),
      );
    } finally {
      setSaving(false);
    }
  };

  const setStatus = async (row: Department, status: string) => {
    try {
      const updated = await enterpriseApi.updateDepartment(row.id, { status });
      setData((prev) => prev.map((item) => (item.id === row.id ? updated : item)));
      message.success(t("tenantGovernance.departments.statusSaved"));
    } catch (err) {
      message.error(
        apiErrorMessage(err, t("tenantGovernance.departments.saveFailed"), t),
      );
    }
  };

  const columns: ColumnsType<Department> = [
    {
      title: t("tenantGovernance.departments.name"),
      dataIndex: "name",
      key: "name",
      width: 240,
    },
    {
      title: t("tenantGovernance.departments.parent"),
      dataIndex: "parent_id",
      key: "parent",
      width: 200,
      render: (parentId: string | null) =>
        parentId ? nameById.get(parentId) ?? parentId : (
          <Text type="secondary">{t("tenantGovernance.departments.root")}</Text>
        ),
    },
    {
      title: t("tenantGovernance.departments.status"),
      dataIndex: "status",
      key: "status",
      width: 120,
      render: (status: string) => (
        <Tag color={status === "active" ? "green" : "default"}>
          {statusLabel(t, "department", status)}
        </Tag>
      ),
    },
  ];

  if (canManage) {
    columns.push({
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
              title={t("tenantGovernance.departments.disableConfirm", {
                name: row.name,
              })}
              description={t("tenantGovernance.departments.disableConfirmHint")}
              okText={t("common.confirm")}
              cancelText={t("common.cancel")}
              onConfirm={() => void setStatus(row, "disabled")}
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
    });
  }

  return (
    <div className={styles.panel}>
      <TabPanelHeader
        icon={<Building2 size={18} />}
        title={t("tenantGovernance.departments.title")}
        description={
          canManage
            ? t("tenantGovernance.departments.descAdmin")
            : t("tenantGovernance.departments.descMember")
        }
        actions={
          <Space size={8}>
            <Button
              size="small"
              icon={<RefreshCw size={14} />}
              onClick={() => void refresh()}
            >
              {t("common.refresh")}
            </Button>
            {canManage && (
              <Button
                type="primary"
                size="small"
                icon={<Plus size={14} />}
                onClick={openCreate}
              >
                {t("tenantGovernance.departments.create")}
              </Button>
            )}
          </Space>
        }
      />

      {loading && departments.length === 0 ? (
        <div className={styles.centered}>
          <Spin />
        </div>
      ) : departments.length === 0 ? (
        <EmptyState
          variant="mascot"
          title={t("tenantGovernance.departments.empty")}
          description={t("tenantGovernance.departments.emptyHint")}
        />
      ) : (
        <ResizableTable
          columns={columns}
          dataSource={departments}
          rowKey="id"
          size="middle"
          tableLayout="fixed"
          scroll={{ x: canManage ? 800 : 620 }}
          storageKey="enterprise-departments-table-widths"
          minWidth={72}
          pagination={false}
        />
      )}

      <Modal
        title={t(
          editing
            ? "tenantGovernance.departments.editTitle"
            : "tenantGovernance.departments.createTitle",
        )}
        open={formOpen}
        onCancel={() => setFormOpen(false)}
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
            name="name"
            label={t("tenantGovernance.departments.name")}
            rules={[
              {
                required: true,
                message: t("tenantGovernance.departments.nameRequired"),
              },
            ]}
          >
            <Input
              maxLength={64}
              placeholder={t("tenantGovernance.departments.namePlaceholder")}
            />
          </Form.Item>
          <Form.Item
            name="parent_id"
            label={t("tenantGovernance.departments.parent")}
            extra={t("tenantGovernance.departments.parentHint")}
          >
            <Select
              allowClear
              showSearch
              optionFilterProp="label"
              placeholder={t("tenantGovernance.departments.parentPlaceholder")}
              options={parentOptions}
            />
          </Form.Item>
          {editing && (
            <Form.Item
              name="status"
              label={t("tenantGovernance.departments.status")}
            >
              <Select
                options={[
                  {
                    value: "active",
                    label: t("tenantGovernance.status.department.active"),
                  },
                  {
                    value: "disabled",
                    label: t("tenantGovernance.status.department.disabled"),
                  },
                ]}
              />
            </Form.Item>
          )}
        </Form>
      </Modal>
    </div>
  );
}
