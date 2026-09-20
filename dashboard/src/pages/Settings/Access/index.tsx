/**
 * 权限策略 (/settings/access) — 岗位授权.
 *
 * A tenant admin gives one duty to the whole tenant, to a department (which
 * also covers its sub-departments), or to a single member. Duties are
 * additive: an admin already holds all five, and a grant only adds a path to a
 * job — it never takes one away. The vocabulary comes from the server, so a
 * duty added server-side shows up here without a frontend change, and a name
 * that cannot be resolved falls back to the raw id instead of being invented.
 *
 * Only tenant admins may read or change these grants; the 403 a member gets is
 * rendered as a real error state, never as an empty table.
 */

import { useCallback, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Form,
  Modal,
  Popconfirm,
  Radio,
  Select,
  Space,
  Tag,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { message } from "@/utils/antdMessage";
import { Plus, RefreshCw, ShieldCheck, Trash2 } from "lucide-react";
import { useTranslation } from "react-i18next";
import PageShell from "../../../layouts/PageShell";
import { ResizableTable } from "../../../components/ResizableTable";
import { useServerTimezone } from "../../../hooks/useServerTimezone";
import { formatServerDateTime } from "../../../utils/formatMessageTime";
import { apiErrorMessage } from "../../../utils/apiError";
import {
  enterpriseApi,
  type Department,
} from "../../../api/modules/enterprise";
import {
  EMPTY_DUTY_GRANTS,
  dutyGrantKey,
  workbuddyAccessApi,
  type AccessMember,
  type Duty,
  type DutyGrant,
  type DutyGrantList,
  type DutySubject,
  type DutySubjectKind,
} from "../../../api/modules/workbuddyAccess";
import {
  ResourceState,
  useWorkBuddyResource,
} from "../../WorkBuddy/Workflows/consoleState";
import { dutyDescription, dutyLabel, subjectKindLabel } from "./labels";
import styles from "./index.module.less";

const { Text } = Typography;

/** Subject kinds in the order the picker offers them. */
const SUBJECT_KINDS: readonly DutySubjectKind[] = [
  "tenant",
  "department",
  "member",
];

const SUBJECT_COLORS: Record<DutySubjectKind, string> = {
  tenant: "gold",
  department: "purple",
  member: "blue",
};

/** Widest subject first: a tenant grant covers every member of a department. */
const SUBJECT_RANK: Record<DutySubjectKind, number> = {
  tenant: 0,
  department: 1,
  member: 2,
};

interface GrantFormValues {
  duty: Duty;
  subject_kind: DutySubjectKind;
  subject_id?: string;
}

export default function AccessPolicyPage() {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  const [form] = Form.useForm<GrantFormValues>();
  const [adding, setAdding] = useState(false);
  const [saving, setSaving] = useState(false);
  const [revokingKey, setRevokingKey] = useState<string | null>(null);
  const subjectKind = Form.useWatch("subject_kind", form);

  const grants = useWorkBuddyResource<DutyGrantList>(
    EMPTY_DUTY_GRANTS,
    () => workbuddyAccessApi.listDuties(),
    [],
  );
  // Names for the subjects the grants point at. A failure here costs the
  // display name only — the row falls back to the id and the table still stands.
  const departments = useWorkBuddyResource<Department[]>(
    [],
    () => enterpriseApi.listDepartments(),
    [],
  );
  const members = useWorkBuddyResource<AccessMember[]>(
    [],
    () => workbuddyAccessApi.listMembers(),
    [],
  );

  const duties = grants.data.duties;

  const departmentNames = useMemo(() => {
    const map = new Map<string, string>();
    for (const row of departments.data) map.set(row.id, row.name);
    return map;
  }, [departments.data]);

  /**
   * Member user id → display name, keyed by the identity a grant names. It is
   * the picker's option source too, so a label is derived exactly once.
   */
  const memberNames = useMemo(() => {
    const map = new Map<string, string>();
    for (const row of members.data) {
      if (row.user_id === undefined || row.user_id === null) continue;
      const id = String(row.user_id);
      const name = row.display_name?.trim();
      const email = row.email?.trim();
      map.set(id, name || email || row.username?.trim() || `#${id}`);
    }
    return map;
  }, [members.data]);

  const subjectText = useCallback(
    (grant: DutyGrant): string => {
      const id = grant.subject_id ?? "";
      if (grant.subject_kind === "department") {
        return departmentNames.get(id) ?? id;
      }
      return memberNames.get(id) ?? `#${id}`;
    },
    [departmentNames, memberNames],
  );

  const grantCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const row of grants.data.items) {
      counts.set(row.duty, (counts.get(row.duty) ?? 0) + 1);
    }
    return counts;
  }, [grants.data.items]);

  /** Rows in vocabulary order, so the grants of one duty stay together. */
  const rows = useMemo(() => {
    const order = new Map<string, number>(
      duties.map((duty, index) => [duty, index]),
    );
    return [...grants.data.items].sort((left, right) => {
      const byDuty = (order.get(left.duty) ?? 0) - (order.get(right.duty) ?? 0);
      if (byDuty !== 0) return byDuty;
      const byKind =
        SUBJECT_RANK[left.subject_kind] - SUBJECT_RANK[right.subject_kind];
      if (byKind !== 0) return byKind;
      return subjectText(left).localeCompare(subjectText(right));
    });
  }, [grants.data.items, duties, subjectText]);

  /** Members are addressed by their Octop user id, departments by their id. */
  const subjectOptions = useMemo(() => {
    if (subjectKind === "department") {
      return departments.data.map((row) => ({
        value: row.id,
        label: row.name,
      }));
    }
    return [...memberNames].map(([value, label]) => ({ value, label }));
  }, [subjectKind, departments.data, memberNames]);

  const openAdd = useCallback(() => {
    setAdding(true);
    form.resetFields();
    form.setFieldsValue({ duty: duties[0], subject_kind: "member" });
  }, [form, duties]);

  const closeAdd = useCallback(() => {
    setAdding(false);
    form.resetFields();
  }, [form]);

  const onGrant = async (values: GrantFormValues) => {
    const subject: DutySubject = {
      subject_kind: values.subject_kind,
      subject_id:
        values.subject_kind === "tenant" ? null : values.subject_id ?? null,
    };
    setSaving(true);
    try {
      await workbuddyAccessApi.grantDuty(values.duty, subject);
      message.success(t("access.grantSuccess"));
      closeAdd();
      await grants.reload();
    } catch (err) {
      message.error(apiErrorMessage(err, t("access.grantFailed"), t));
    } finally {
      setSaving(false);
    }
  };

  const onRevoke = async (row: DutyGrant) => {
    setRevokingKey(dutyGrantKey(row));
    try {
      await workbuddyAccessApi.revokeDuty(row.duty, {
        subject_kind: row.subject_kind,
        subject_id: row.subject_id,
      });
      message.success(t("access.revokeSuccess"));
      await grants.reload();
    } catch (err) {
      message.error(apiErrorMessage(err, t("access.revokeFailed"), t));
    } finally {
      setRevokingKey(null);
    }
  };

  const columns: ColumnsType<DutyGrant> = [
    {
      title: t("access.column.duty"),
      dataIndex: "duty",
      key: "duty",
      width: 150,
      render: (duty: Duty) => <Tag color="geekblue">{dutyLabel(t, duty)}</Tag>,
    },
    {
      title: t("access.column.subject"),
      key: "subject",
      width: 320,
      render: (_value, row) => (
        <Space size={6}>
          <Tag color={SUBJECT_COLORS[row.subject_kind]}>
            {subjectKindLabel(t, row.subject_kind)}
          </Tag>
          {row.subject_kind === "tenant" ? null : (
            <span>{subjectText(row)}</span>
          )}
        </Space>
      ),
    },
    {
      title: t("access.column.grantedAt"),
      dataIndex: "granted_at",
      key: "granted_at",
      width: 180,
      render: (value: number) => formatServerDateTime(value, timeZone),
    },
    {
      title: t("common.actions"),
      key: "actions",
      width: 120,
      render: (_value, row) => (
        <Popconfirm
          title={t("access.revokeConfirm")}
          okText={t("access.revokeConfirmOk")}
          cancelText={t("common.cancel")}
          onConfirm={() => void onRevoke(row)}
        >
          <Button
            type="link"
            size="small"
            danger
            icon={<Trash2 size={14} />}
            loading={revokingKey === dutyGrantKey(row)}
          >
            {t("access.revoke")}
          </Button>
        </Popconfirm>
      ),
    },
  ];

  const failed = grants.error !== null && grants.error !== undefined;
  const blocked = failed || grants.loading || grants.data.items.length === 0;

  return (
    <PageShell
      title={t("access.title")}
      subtitle={t("access.subtitle")}
      actions={
        <Space size={8}>
          <Button
            icon={<RefreshCw size={14} />}
            onClick={() => void grants.reload()}
          >
            {t("common.refresh")}
          </Button>
          <Button
            type="primary"
            icon={<Plus size={14} />}
            onClick={openAdd}
            disabled={duties.length === 0}
          >
            {t("access.add")}
          </Button>
        </Space>
      }
    >
      <div className={styles.panel}>
        <Alert
          type="info"
          showIcon
          className={styles.notice}
          message={t("access.policy.title")}
          description={
            <ul className={styles.policyList}>
              <li>{t("access.policy.additive")}</li>
              <li>{t("access.policy.adminImplicit")}</li>
              <li>{t("access.policy.departmentChain")}</li>
              <li>{t("access.policy.tenantWide")}</li>
              <li>{t("access.policy.adminOnly")}</li>
            </ul>
          }
        />

        {duties.length > 0 && (
          <div className={styles.vocabulary}>
            <div className={styles.vocabularyHead}>
              <ShieldCheck size={16} aria-hidden="true" />
              <span className={styles.vocabularyTitle}>
                {t("access.vocabulary.title")}
              </span>
            </div>
            <div className={styles.dutyGrid}>
              {duties.map((duty) => (
                <div key={duty} className={styles.dutyCard}>
                  <div className={styles.dutyHead}>
                    <span className={styles.dutyName}>
                      {dutyLabel(t, duty)}
                    </span>
                    <Tag>
                      {t("access.grantCount", {
                        count: grantCounts.get(duty) ?? 0,
                      })}
                    </Tag>
                  </div>
                  <div className={styles.dutyDesc}>
                    {dutyDescription(t, duty)}
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {blocked ? (
          <ResourceState
            resource={grants}
            isEmpty={!failed && !grants.loading && rows.length === 0}
            loadingLabel={t("common.loading")}
            errorTitle={t("access.loadFailed")}
            errorFallback={t("access.loadFailedHint")}
            unavailableTitle={t("workbuddy.shared.notMergedTitle")}
            unavailableHint={t("workbuddy.shared.notMergedHint")}
            emptyTitle={t("access.emptyTitle")}
            emptyHint={t("access.emptyHint")}
            emptyActionLabel={t("access.add")}
            onEmptyAction={openAdd}
          />
        ) : (
          <ResizableTable<DutyGrant>
            rowKey={dutyGrantKey}
            size="small"
            columns={columns}
            dataSource={rows}
            pagination={false}
            scroll={{ x: 900 }}
            storageKey="settings-access-duty-grants"
          />
        )}
      </div>

      <Modal
        open={adding}
        title={t("access.form.addTitle")}
        okText={t("access.form.submit")}
        cancelText={t("common.cancel")}
        confirmLoading={saving}
        onOk={() => void form.submit()}
        onCancel={closeAdd}
        destroyOnClose
      >
        <Form
          form={form}
          layout="vertical"
          initialValues={{ subject_kind: "member" }}
          onFinish={(values) => void onGrant(values)}
        >
          <Form.Item
            name="duty"
            label={t("access.form.duty")}
            rules={[{ required: true, message: t("access.form.dutyRequired") }]}
          >
            <Select
              placeholder={t("access.form.dutyPlaceholder")}
              options={duties.map((duty) => ({
                value: duty,
                label: dutyLabel(t, duty),
              }))}
            />
          </Form.Item>
          <Form.Item name="subject_kind" label={t("access.form.subjectKind")}>
            <Radio.Group
              options={SUBJECT_KINDS.map((kind) => ({
                value: kind,
                label: subjectKindLabel(t, kind),
              }))}
            />
          </Form.Item>
          {subjectKind === "tenant" ? (
            <Text type="secondary">{t("access.form.tenantHint")}</Text>
          ) : (
            <Form.Item
              name="subject_id"
              label={t("access.form.subject")}
              rules={[
                { required: true, message: t("access.form.subjectRequired") },
              ]}
              extra={
                subjectKind === "department"
                  ? t("access.form.departmentHint")
                  : t("access.form.memberHint")
              }
            >
              <Select
                showSearch
                optionFilterProp="label"
                placeholder={t("access.form.subjectPlaceholder")}
                notFoundContent={t("access.form.subjectEmpty")}
                options={subjectOptions}
              />
            </Form.Item>
          )}
        </Form>
      </Modal>
    </PageShell>
  );
}
