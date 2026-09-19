/**
 * 知识库 → 授权.
 *
 * Explicit, additive grants (member or department, exactly one subject) managed
 * by base admins. Implicit access already granted by the base's scope is shown
 * as a notice — the ACL table only ever holds *extra* permissions, and a revoke
 * takes effect on the next retrieval transaction.
 */

import { useCallback, useMemo, useState } from "react";
import {
  Alert,
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
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { Plus, RefreshCw, ShieldCheck, Trash2 } from "lucide-react";
import { useTranslation } from "react-i18next";
import { message } from "@/utils/antdMessage";
import { ResizableTable } from "../../../components/ResizableTable";
import { EmptyState } from "../../../components/EmptyState";
import { useServerTimezone } from "../../../hooks/useServerTimezone";
import { formatServerDateTime } from "../../../utils/formatMessageTime";
import { apiErrorMessage } from "../../../utils/apiError";
import {
  workbuddyKnowledgeApi,
  type KnowledgeAclCreate,
  type KnowledgeAclRow,
  type KnowledgeBase,
  type KnowledgePermission,
} from "../../../api/modules/workbuddyKnowledge";
import { TabPanelHeader } from "../../Settings/AdvancedSettings/TabPanelHeader";
import { useKnowledgeResource } from "./useKnowledgeResource";
import { knowledgeLabel } from "./labels";
import styles from "./index.module.less";

const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

const PERMISSIONS: readonly KnowledgePermission[] = ["read", "write", "admin"];

interface AclFormValues {
  subject_type: "user" | "department";
  user_id?: number;
  department_id?: string;
  permission: KnowledgePermission;
}

export default function AclPanel({ base }: { base: KnowledgeBase }) {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  const isAdmin = base.permission === "admin";
  const [form] = Form.useForm<AclFormValues>();
  const [adding, setAdding] = useState(false);
  const [saving, setSaving] = useState(false);
  const [updatingId, setUpdatingId] = useState<string | null>(null);
  const [revokingId, setRevokingId] = useState<string | null>(null);
  const subjectType = Form.useWatch("subject_type", form);

  const resource = useKnowledgeResource<KnowledgeAclRow[]>(
    [],
    () => workbuddyKnowledgeApi.listAcl(base.kb_id).then((page) => page.items),
    [base.kb_id],
    { enabled: isAdmin },
  );

  const permissionOptions = useMemo(
    () =>
      PERMISSIONS.map((value) => ({
        value,
        label: knowledgeLabel(t, "permission", value),
      })),
    [t],
  );

  const openAdd = useCallback(() => {
    setAdding(true);
    form.resetFields();
    form.setFieldsValue({ subject_type: "user", permission: "read" });
  }, [form]);

  const onAdd = async (values: AclFormValues) => {
    const payload: KnowledgeAclCreate = {
      permission: values.permission,
      user_id: values.subject_type === "user" ? values.user_id ?? null : null,
      department_id:
        values.subject_type === "department"
          ? values.department_id?.trim() ?? null
          : null,
    };
    setSaving(true);
    try {
      await workbuddyKnowledgeApi.addAcl(base.kb_id, payload);
      message.success(t("workbuddy.knowledge.acl.addSuccess"));
      setAdding(false);
      form.resetFields();
      await resource.refresh();
    } catch (err) {
      message.error(
        apiErrorMessage(err, t("workbuddy.knowledge.acl.addFailed"), t),
      );
    } finally {
      setSaving(false);
    }
  };

  const onUpdate = async (
    row: KnowledgeAclRow,
    permission: KnowledgePermission,
  ) => {
    setUpdatingId(row.acl_id);
    try {
      await workbuddyKnowledgeApi.updateAcl(base.kb_id, row.acl_id, {
        permission,
      });
      message.success(t("workbuddy.knowledge.acl.updateSuccess"));
      await resource.refresh();
    } catch (err) {
      message.error(
        apiErrorMessage(err, t("workbuddy.knowledge.acl.updateFailed"), t),
      );
    } finally {
      setUpdatingId(null);
    }
  };

  const onRevoke = async (row: KnowledgeAclRow) => {
    setRevokingId(row.acl_id);
    try {
      await workbuddyKnowledgeApi.deleteAcl(base.kb_id, row.acl_id);
      message.success(t("workbuddy.knowledge.acl.revokeSuccess"));
      await resource.refresh();
    } catch (err) {
      message.error(
        apiErrorMessage(err, t("workbuddy.knowledge.acl.revokeFailed"), t),
      );
    } finally {
      setRevokingId(null);
    }
  };

  const columns: ColumnsType<KnowledgeAclRow> = [
    {
      title: t("workbuddy.knowledge.acl.columnSubject"),
      key: "subject",
      width: 260,
      render: (_value, row) => {
        const isUser = row.user_id !== null;
        return (
          <Space size={6}>
            <Tag color={isUser ? "blue" : "purple"}>
              {t(
                isUser
                  ? "workbuddy.knowledge.acl.subjectType.user"
                  : "workbuddy.knowledge.acl.subjectType.department",
              )}
            </Tag>
            <span className={styles.mono}>
              {isUser ? `#${row.user_id}` : row.department_id}
            </span>
          </Space>
        );
      },
    },
    {
      title: t("workbuddy.knowledge.acl.columnPermission"),
      dataIndex: "permission",
      key: "permission",
      width: 180,
      render: (value: KnowledgePermission, row) => (
        <Select
          size="small"
          value={value}
          className={styles.permissionSelect}
          loading={updatingId === row.acl_id}
          options={permissionOptions}
          onChange={(next: KnowledgePermission) => void onUpdate(row, next)}
        />
      ),
    },
    {
      title: t("workbuddy.knowledge.acl.columnGrantedAt"),
      dataIndex: "created_at",
      key: "created_at",
      width: 180,
      render: (value: number) => formatServerDateTime(value, timeZone),
    },
    {
      title: t("workbuddy.knowledge.common.actions"),
      key: "actions",
      width: 140,
      render: (_value, row) => (
        <Popconfirm
          title={t("workbuddy.knowledge.acl.revokeConfirm")}
          okText={t("workbuddy.knowledge.common.confirm")}
          cancelText={t("workbuddy.knowledge.common.cancel")}
          onConfirm={() => void onRevoke(row)}
        >
          <Button
            type="link"
            size="small"
            danger
            icon={<Trash2 size={14} />}
            loading={revokingId === row.acl_id}
          >
            {t("workbuddy.knowledge.acl.revoke")}
          </Button>
        </Popconfirm>
      ),
    },
  ];

  return (
    <div className={styles.panel}>
      <TabPanelHeader
        icon={<ShieldCheck size={18} />}
        title={t("workbuddy.knowledge.acl.title")}
        description={t("workbuddy.knowledge.acl.desc", { name: base.name })}
        actions={
          isAdmin && (
            <Space size={8}>
              <Button
                size="small"
                icon={<RefreshCw size={14} />}
                onClick={() => void resource.refresh()}
              >
                {t("workbuddy.knowledge.common.refresh")}
              </Button>
              <Button
                size="small"
                type="primary"
                icon={<Plus size={14} />}
                onClick={openAdd}
              >
                {t("workbuddy.knowledge.acl.add")}
              </Button>
            </Space>
          )
        }
      />

      {!isAdmin ? (
        <Alert
          type="info"
          showIcon
          message={t("workbuddy.knowledge.acl.adminOnlyTitle")}
          description={t("workbuddy.knowledge.acl.adminOnlyHint")}
        />
      ) : (
        <>
          <Alert
            type="info"
            showIcon
            className={styles.notice}
            message={t("workbuddy.knowledge.acl.scopeNotice.title")}
            description={
              base.scope === "enterprise"
                ? t("workbuddy.knowledge.acl.scopeNotice.enterprise")
                : base.scope === "department"
                ? t("workbuddy.knowledge.acl.scopeNotice.department", {
                    departmentId: base.department_id ?? "—",
                  })
                : t("workbuddy.knowledge.acl.scopeNotice.personal")
            }
          />

          {resource.unavailable ? (
            <Alert
              type="info"
              showIcon
              message={t("workbuddy.knowledge.common.notMergedTitle")}
              description={t("workbuddy.knowledge.common.notMergedHint")}
            />
          ) : resource.error ? (
            <Alert
              type="error"
              showIcon
              message={t("workbuddy.knowledge.acl.loadFailed")}
              description={apiErrorMessage(
                resource.error,
                t("workbuddy.knowledge.acl.loadFailed"),
                t,
              )}
              action={
                <Button
                  size="small"
                  icon={<RefreshCw size={14} />}
                  onClick={() => void resource.refresh()}
                >
                  {t("workbuddy.knowledge.common.retry")}
                </Button>
              }
            />
          ) : resource.loading && resource.data.length === 0 ? (
            <div className={styles.centered}>
              <Spin />
            </div>
          ) : resource.data.length === 0 ? (
            <EmptyState
              variant="mascot"
              title={t("workbuddy.knowledge.acl.empty")}
              description={t("workbuddy.knowledge.acl.emptyHint")}
              actionLabel={t("workbuddy.knowledge.acl.add")}
              onAction={openAdd}
            />
          ) : (
            <ResizableTable
              columns={columns}
              dataSource={resource.data}
              rowKey="acl_id"
              size="middle"
              tableLayout="fixed"
              scroll={{ x: 860 }}
              storageKey="workbuddy-knowledge-acl-table-widths"
              minWidth={72}
              pagination={false}
            />
          )}
        </>
      )}

      <Modal
        title={t("workbuddy.knowledge.acl.addTitle")}
        open={adding}
        onCancel={() => setAdding(false)}
        onOk={() => form.submit()}
        confirmLoading={saving}
        okText={t("workbuddy.knowledge.common.create")}
        cancelText={t("workbuddy.knowledge.common.cancel")}
        destroyOnHidden
      >
        <Form
          form={form}
          layout="vertical"
          requiredMark={false}
          onFinish={(values) => void onAdd(values)}
        >
          <Form.Item
            name="subject_type"
            label={t("workbuddy.knowledge.acl.subjectTypeLabel")}
            rules={[
              {
                required: true,
                message: t("workbuddy.knowledge.acl.subjectTypeRequired"),
              },
            ]}
          >
            <Select
              options={[
                {
                  value: "user",
                  label: t("workbuddy.knowledge.acl.subjectType.user"),
                },
                {
                  value: "department",
                  label: t("workbuddy.knowledge.acl.subjectType.department"),
                },
              ]}
            />
          </Form.Item>
          {subjectType === "department" ? (
            <Form.Item
              name="department_id"
              label={t("workbuddy.knowledge.acl.departmentId")}
              rules={[
                {
                  required: true,
                  message: t("workbuddy.knowledge.acl.departmentIdRequired"),
                },
                {
                  pattern: UUID_PATTERN,
                  message: t("workbuddy.knowledge.common.uuidInvalid"),
                },
              ]}
            >
              <Input
                placeholder={t(
                  "workbuddy.knowledge.acl.departmentIdPlaceholder",
                )}
              />
            </Form.Item>
          ) : (
            <Form.Item
              name="user_id"
              label={t("workbuddy.knowledge.acl.userId")}
              rules={[
                {
                  required: true,
                  message: t("workbuddy.knowledge.acl.userIdRequired"),
                },
              ]}
              extra={t("workbuddy.knowledge.acl.userIdHint")}
            >
              <InputNumber
                min={1}
                precision={0}
                className={styles.fullWidth}
                placeholder={t("workbuddy.knowledge.acl.userIdPlaceholder")}
              />
            </Form.Item>
          )}
          <Form.Item
            name="permission"
            label={t("workbuddy.knowledge.acl.permission")}
            rules={[
              {
                required: true,
                message: t("workbuddy.knowledge.acl.permissionRequired"),
              },
            ]}
          >
            <Select options={permissionOptions} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
