/**
 * Compliance checklists: list + create, then the selected checklist's
 * immutable revision timeline with read / approve / revoke / archive.
 *
 * The list is loaded through a local state machine instead of
 * ``useAsyncResource`` because this surface needs three distinct outcomes the
 * hook cannot express: loaded, "the slice is not mounted yet" (404/501) and a
 * real error. Nothing here renders a revision the server did not return.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Form,
  Input,
  Modal,
  Popconfirm,
  Space,
  Spin,
  Table,
  Tag,
  Timeline,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { message } from "@/utils/antdMessage";
import {
  Archive,
  CircleCheck,
  CircleSlash,
  ClipboardList,
  Eye,
  FilePlus2,
  Plus,
  RefreshCw,
  Trash2,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import { EmptyState } from "../../../components/EmptyState";
import { useServerTimezone } from "../../../hooks/useServerTimezone";
import { apiErrorMessage } from "../../../utils/apiError";
import { formatServerDateTime } from "../../../utils/formatMessageTime";
import {
  isComplianceUnavailableError,
  workbuddyComplianceApi,
  type ComplianceChecklist,
  type ComplianceChecklistVersion,
  type ComplianceRule,
} from "../../../api/modules/workbuddyCompliance";
import { TabPanelHeader } from "../../Settings/AdvancedSettings/TabPanelHeader";
import { complianceStatusLabel } from "./statusLabels";
import styles from "./index.module.less";

const { Text } = Typography;

interface ChecklistFormValues {
  name: string;
  description?: string | null;
}

interface RevisionFormValues {
  rules: { statement: string; detail?: string | null }[];
}

/** Tag colour per revision status; unknown states stay neutral. */
const VERSION_TAG_COLOR: Record<string, string> = {
  draft: "default",
  approved: "green",
  revoked: "red",
};

const CHECKLIST_TAG_COLOR: Record<string, string> = {
  draft: "gold",
  approved: "green",
  archived: "default",
};

export default function ChecklistsPanel() {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();

  const [checklists, setChecklists] = useState<ComplianceChecklist[]>([]);
  const [loading, setLoading] = useState(false);
  const [unavailable, setUnavailable] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const [createOpen, setCreateOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [createForm] = Form.useForm<ChecklistFormValues>();

  const [revisionOpen, setRevisionOpen] = useState(false);
  const [revisionSaving, setRevisionSaving] = useState(false);
  const [revisionForm] = Form.useForm<RevisionFormValues>();

  const [detail, setDetail] = useState<ComplianceChecklistVersion | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [busyVersionId, setBusyVersionId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const items = await workbuddyComplianceApi.listChecklists();
      setChecklists(items);
      setUnavailable(false);
      setLoadError(null);
      setSelectedId((prev) =>
        prev && items.some((item) => item.id === prev) ? prev : null,
      );
    } catch (err) {
      setChecklists([]);
      if (isComplianceUnavailableError(err)) {
        setUnavailable(true);
        setLoadError(null);
      } else {
        setUnavailable(false);
        setLoadError(
          apiErrorMessage(
            err,
            t("workbuddy.compliance.checklists.loadFailed"),
            t,
          ),
        );
      }
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    void load();
  }, [load]);

  const selected = useMemo(
    () => checklists.find((item) => item.id === selectedId) ?? null,
    [checklists, selectedId],
  );

  const versions = useMemo<ComplianceChecklistVersion[]>(
    () => selected?.versions ?? [],
    [selected],
  );

  const onCreateSubmit = async (values: ChecklistFormValues) => {
    setCreating(true);
    try {
      const created = await workbuddyComplianceApi.createChecklist({
        name: values.name.trim(),
        description: values.description?.trim()
          ? values.description.trim()
          : null,
      });
      message.success(t("workbuddy.compliance.checklists.createSuccess"));
      setCreateOpen(false);
      createForm.resetFields();
      await load();
      setSelectedId(created.id);
    } catch (err) {
      message.error(
        apiErrorMessage(
          err,
          t("workbuddy.compliance.checklists.createFailed"),
          t,
        ),
      );
    } finally {
      setCreating(false);
    }
  };

  const onCreateRevision = async (values: RevisionFormValues) => {
    if (!selected) return;
    setRevisionSaving(true);
    try {
      const rules: ComplianceRule[] = (values.rules ?? [])
        .map((rule) => ({
          statement: (rule.statement ?? "").trim(),
          detail: rule.detail?.trim() ? rule.detail.trim() : null,
        }))
        .filter((rule) => rule.statement.length > 0);
      await workbuddyComplianceApi.createChecklistVersion(selected.id, {
        rules,
      });
      message.success(t("workbuddy.compliance.checklists.revisionCreated"));
      setRevisionOpen(false);
      revisionForm.resetFields();
      await load();
    } catch (err) {
      message.error(
        apiErrorMessage(
          err,
          t("workbuddy.compliance.checklists.revisionCreateFailed"),
          t,
        ),
      );
    } finally {
      setRevisionSaving(false);
    }
  };

  const setVersionStatus = async (
    version: ComplianceChecklistVersion,
    action: "approve" | "revoke",
  ) => {
    if (!selected) return;
    setBusyVersionId(version.id);
    try {
      if (action === "approve") {
        await workbuddyComplianceApi.approveChecklistVersion(
          selected.id,
          version.id,
        );
      } else {
        await workbuddyComplianceApi.revokeChecklistVersion(
          selected.id,
          version.id,
        );
      }
      message.success(
        t(
          action === "approve"
            ? "workbuddy.compliance.checklists.approveSuccess"
            : "workbuddy.compliance.checklists.revokeSuccess",
        ),
      );
      await load();
    } catch (err) {
      message.error(
        apiErrorMessage(
          err,
          t(
            action === "approve"
              ? "workbuddy.compliance.checklists.approveFailed"
              : "workbuddy.compliance.checklists.revokeFailed",
          ),
          t,
        ),
      );
    } finally {
      setBusyVersionId(null);
    }
  };

  const archiveChecklist = async (checklist: ComplianceChecklist) => {
    try {
      await workbuddyComplianceApi.archiveChecklist(checklist.id);
      message.success(t("workbuddy.compliance.checklists.archiveSuccess"));
      await load();
    } catch (err) {
      message.error(
        apiErrorMessage(
          err,
          t("workbuddy.compliance.checklists.archiveFailed"),
          t,
        ),
      );
    }
  };

  const openVersion = async (version: ComplianceChecklistVersion) => {
    if (!selected) return;
    setDetail(version);
    setDetailLoading(true);
    try {
      const pinned = await workbuddyComplianceApi.getChecklistVersion(
        selected.id,
        version.id,
      );
      setDetail(pinned);
    } catch (err) {
      message.error(
        apiErrorMessage(
          err,
          t("workbuddy.compliance.checklists.versionLoadFailed"),
          t,
        ),
      );
    } finally {
      setDetailLoading(false);
    }
  };

  const columns: ColumnsType<ComplianceChecklist> = [
    {
      title: t("workbuddy.compliance.checklists.name"),
      dataIndex: "name",
      key: "name",
      width: 260,
      render: (name: string, row) => (
        <Space direction="vertical" size={0}>
          <span>{name}</span>
          {row.description?.trim() ? (
            <Text type="secondary" className={styles.rowHint}>
              {row.description}
            </Text>
          ) : null}
        </Space>
      ),
    },
    {
      title: t("workbuddy.compliance.checklists.colStatus"),
      dataIndex: "status",
      key: "status",
      width: 140,
      render: (status: string) => (
        <Tag color={CHECKLIST_TAG_COLOR[status] ?? "default"}>
          {complianceStatusLabel(t, "checklist", status)}
        </Tag>
      ),
    },
    {
      title: t("workbuddy.compliance.checklists.colRevisions"),
      key: "revisions",
      width: 120,
      render: (_value, row) => row.versions?.length ?? 0,
    },
    {
      title: t("workbuddy.compliance.checklists.colUpdated"),
      dataIndex: "updated_at",
      key: "updated_at",
      width: 180,
      render: (value: number | null) =>
        value ? formatServerDateTime(value, timeZone) : "—",
    },
    {
      title: t("common.actions"),
      key: "actions",
      width: 140,
      render: (_value, row) =>
        row.status === "archived" ? (
          <Text type="secondary">—</Text>
        ) : (
          <Popconfirm
            title={t("workbuddy.compliance.checklists.archiveConfirm", {
              name: row.name,
            })}
            description={t(
              "workbuddy.compliance.checklists.archiveConfirmHint",
            )}
            okText={t("common.confirm")}
            cancelText={t("common.cancel")}
            onConfirm={() => void archiveChecklist(row)}
          >
            <Button
              type="link"
              size="small"
              danger
              icon={<Archive size={14} />}
            >
              {t("workbuddy.compliance.checklists.archive")}
            </Button>
          </Popconfirm>
        ),
    },
  ];

  return (
    <div className={styles.panel}>
      <TabPanelHeader
        icon={<ClipboardList size={18} />}
        title={t("workbuddy.compliance.checklists.title")}
        description={t("workbuddy.compliance.checklists.desc")}
        actions={
          <Space size={8} wrap>
            <Button
              size="small"
              icon={<RefreshCw size={14} />}
              onClick={() => void load()}
            >
              {t("common.refresh")}
            </Button>
            <Button
              type="primary"
              size="small"
              icon={<Plus size={14} />}
              onClick={() => setCreateOpen(true)}
            >
              {t("workbuddy.compliance.checklists.create")}
            </Button>
          </Space>
        }
      />

      {loading && checklists.length === 0 ? (
        <div className={styles.centered}>
          <Spin />
        </div>
      ) : unavailable ? (
        <EmptyState
          title={t("workbuddy.compliance.checklists.unavailableTitle")}
          description={t("workbuddy.compliance.checklists.unavailableHint")}
          actionLabel={t("common.refresh")}
          onAction={() => void load()}
        />
      ) : loadError ? (
        <EmptyState
          variant="error"
          title={t("workbuddy.compliance.checklists.loadFailed")}
          description={loadError}
          actionLabel={t("common.refresh")}
          onAction={() => void load()}
        />
      ) : checklists.length === 0 ? (
        <EmptyState
          variant="mascot"
          title={t("workbuddy.compliance.checklists.empty")}
          description={t("workbuddy.compliance.checklists.emptyHint")}
        />
      ) : (
        <Table
          columns={columns}
          dataSource={checklists}
          rowKey="id"
          size="middle"
          pagination={false}
          scroll={{ x: 840 }}
          rowSelection={{
            type: "radio",
            selectedRowKeys: selectedId ? [selectedId] : [],
            onChange: (keys) =>
              setSelectedId(keys.length > 0 ? String(keys[0]) : null),
          }}
        />
      )}

      {selected && (
        <div className={styles.sectionBlock}>
          <TabPanelHeader
            icon={<FilePlus2 size={18} />}
            title={t("workbuddy.compliance.checklists.revisionsTitle", {
              name: selected.name,
            })}
            description={t("workbuddy.compliance.checklists.revisionsDesc")}
            actions={
              selected.status === "archived" ? (
                <Text type="secondary" className={styles.rowHint}>
                  {t("workbuddy.compliance.checklists.archivedHint")}
                </Text>
              ) : (
                <Button
                  type="primary"
                  size="small"
                  icon={<Plus size={14} />}
                  onClick={() => setRevisionOpen(true)}
                >
                  {t("workbuddy.compliance.checklists.newRevision")}
                </Button>
              )
            }
          />

          {versions.length === 0 ? (
            <Text type="secondary">
              {t("workbuddy.compliance.checklists.revisionsEmpty")}
            </Text>
          ) : (
            <Timeline
              className={styles.timeline}
              items={versions.map((version) => ({
                color:
                  version.status === "approved"
                    ? "green"
                    : version.status === "revoked"
                    ? "red"
                    : "gray",
                children: (
                  <div className={styles.revision}>
                    <Space size={8} wrap>
                      <Tag
                        color={VERSION_TAG_COLOR[version.status] ?? "default"}
                      >
                        {complianceStatusLabel(t, "version", version.status)}
                      </Tag>
                      <Text type="secondary">
                        {t("workbuddy.compliance.checklists.versionCreatedAt", {
                          time: version.created_at
                            ? formatServerDateTime(version.created_at, timeZone)
                            : "—",
                        })}
                      </Text>
                      {version.approved_at ? (
                        <Text type="secondary">
                          {t(
                            "workbuddy.compliance.checklists.versionApprovedAt",
                            {
                              time: formatServerDateTime(
                                version.approved_at,
                                timeZone,
                              ),
                            },
                          )}
                        </Text>
                      ) : null}
                      {version.revoked_at ? (
                        <Text type="secondary">
                          {t(
                            "workbuddy.compliance.checklists.versionRevokedAt",
                            {
                              time: formatServerDateTime(
                                version.revoked_at,
                                timeZone,
                              ),
                            },
                          )}
                        </Text>
                      ) : null}
                    </Space>

                    {version.rules ? (
                      <ul className={styles.rulePreview}>
                        {version.rules.slice(0, 3).map((rule, index) => (
                          <li key={`${version.id}-${index}`}>
                            {rule.statement}
                          </li>
                        ))}
                        {version.rules.length > 3 ? (
                          <li className={styles.ruleMore}>
                            {t("workbuddy.compliance.checklists.moreRules", {
                              n: version.rules.length - 3,
                            })}
                          </li>
                        ) : null}
                      </ul>
                    ) : (
                      <Text type="secondary" className={styles.rowHint}>
                        {t("workbuddy.compliance.checklists.rulesUnloaded")}
                      </Text>
                    )}

                    <Space size={4} wrap>
                      <Button
                        type="link"
                        size="small"
                        icon={<Eye size={14} />}
                        onClick={() => void openVersion(version)}
                      >
                        {t("workbuddy.compliance.checklists.read")}
                      </Button>
                      {version.status !== "approved" && (
                        <Popconfirm
                          title={t(
                            "workbuddy.compliance.checklists.approveConfirm",
                          )}
                          description={t(
                            "workbuddy.compliance.checklists.approveConfirmHint",
                          )}
                          okText={t("common.confirm")}
                          cancelText={t("common.cancel")}
                          onConfirm={() =>
                            void setVersionStatus(version, "approve")
                          }
                        >
                          <Button
                            type="link"
                            size="small"
                            icon={<CircleCheck size={14} />}
                            loading={busyVersionId === version.id}
                          >
                            {t("workbuddy.compliance.checklists.approve")}
                          </Button>
                        </Popconfirm>
                      )}
                      {version.status === "approved" && (
                        <Popconfirm
                          title={t(
                            "workbuddy.compliance.checklists.revokeConfirm",
                          )}
                          description={t(
                            "workbuddy.compliance.checklists.revokeConfirmHint",
                          )}
                          okText={t("common.confirm")}
                          cancelText={t("common.cancel")}
                          onConfirm={() =>
                            void setVersionStatus(version, "revoke")
                          }
                        >
                          <Button
                            type="link"
                            size="small"
                            danger
                            icon={<CircleSlash size={14} />}
                            loading={busyVersionId === version.id}
                          >
                            {t("workbuddy.compliance.checklists.revoke")}
                          </Button>
                        </Popconfirm>
                      )}
                    </Space>
                  </div>
                ),
              }))}
            />
          )}
        </div>
      )}

      <Modal
        title={t("workbuddy.compliance.checklists.createTitle")}
        open={createOpen}
        onCancel={() => setCreateOpen(false)}
        onOk={() => createForm.submit()}
        confirmLoading={creating}
        okText={t("common.create")}
        cancelText={t("common.cancel")}
        destroyOnHidden
      >
        <Form
          form={createForm}
          layout="vertical"
          requiredMark={false}
          onFinish={(values) => void onCreateSubmit(values)}
        >
          <Form.Item
            name="name"
            label={t("workbuddy.compliance.checklists.name")}
            rules={[
              {
                required: true,
                message: t("workbuddy.compliance.checklists.nameRequired"),
              },
            ]}
          >
            <Input
              maxLength={120}
              placeholder={t("workbuddy.compliance.checklists.namePlaceholder")}
            />
          </Form.Item>
          <Form.Item
            name="description"
            label={t("workbuddy.compliance.checklists.description")}
          >
            <Input.TextArea
              maxLength={1000}
              rows={3}
              placeholder={t(
                "workbuddy.compliance.checklists.descriptionPlaceholder",
              )}
            />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={t("workbuddy.compliance.checklists.revisionCreateTitle", {
          name: selected?.name ?? "",
        })}
        open={revisionOpen}
        onCancel={() => setRevisionOpen(false)}
        onOk={() => revisionForm.submit()}
        confirmLoading={revisionSaving}
        okText={t("common.save")}
        cancelText={t("common.cancel")}
        destroyOnHidden
        width={640}
      >
        <Alert
          type="info"
          showIcon
          className={styles.notice}
          message={t("workbuddy.compliance.checklists.revisionImmutableNotice")}
        />
        <Form
          form={revisionForm}
          layout="vertical"
          requiredMark={false}
          onFinish={(values) => void onCreateRevision(values)}
          initialValues={{ rules: [{ statement: "", detail: "" }] }}
        >
          <Form.List name="rules">
            {(fields, { add, remove }) => (
              <div className={styles.ruleList}>
                {fields.map((field, index) => (
                  <div key={field.key} className={styles.ruleRow}>
                    <Form.Item
                      name={[field.name, "statement"]}
                      label={
                        index === 0
                          ? t("workbuddy.compliance.checklists.ruleStatement")
                          : undefined
                      }
                      rules={[
                        {
                          required: true,
                          message: t(
                            "workbuddy.compliance.checklists.ruleStatementRequired",
                          ),
                        },
                      ]}
                    >
                      <Input
                        maxLength={500}
                        placeholder={t(
                          "workbuddy.compliance.checklists.ruleStatementPlaceholder",
                        )}
                      />
                    </Form.Item>
                    <Form.Item
                      name={[field.name, "detail"]}
                      label={
                        index === 0
                          ? t("workbuddy.compliance.checklists.ruleDetail")
                          : undefined
                      }
                      className={styles.ruleDetail}
                    >
                      <Input
                        maxLength={500}
                        placeholder={t(
                          "workbuddy.compliance.checklists.ruleDetailPlaceholder",
                        )}
                      />
                    </Form.Item>
                    <Button
                      type="text"
                      danger
                      aria-label={t(
                        "workbuddy.compliance.checklists.removeRule",
                      )}
                      icon={<Trash2 size={14} />}
                      disabled={fields.length <= 1}
                      onClick={() => remove(field.name)}
                    />
                  </div>
                ))}
                <Button
                  type="dashed"
                  block
                  icon={<Plus size={14} />}
                  onClick={() => add({ statement: "", detail: "" })}
                >
                  {t("workbuddy.compliance.checklists.addRule")}
                </Button>
              </div>
            )}
          </Form.List>
        </Form>
      </Modal>

      <Modal
        title={t("workbuddy.compliance.checklists.versionTitle")}
        open={detail !== null}
        onCancel={() => setDetail(null)}
        footer={
          <Button onClick={() => setDetail(null)}>{t("common.close")}</Button>
        }
        destroyOnHidden
        width={640}
      >
        {detail && (
          <div>
            <Space size={8} wrap>
              <Tag color={VERSION_TAG_COLOR[detail.status] ?? "default"}>
                {complianceStatusLabel(t, "version", detail.status)}
              </Tag>
              <Text type="secondary">
                {t("workbuddy.compliance.checklists.versionCreatedAt", {
                  time: detail.created_at
                    ? formatServerDateTime(detail.created_at, timeZone)
                    : "—",
                })}
              </Text>
              <Text code>{detail.id}</Text>
            </Space>
            {detailLoading ? (
              <div className={styles.centered}>
                <Spin />
              </div>
            ) : detail.rules && detail.rules.length > 0 ? (
              <ol className={styles.ruleDetailList}>
                {detail.rules.map((rule, index) => (
                  <li key={`${detail.id}-${index}`}>
                    <div>{rule.statement}</div>
                    {rule.detail?.trim() ? (
                      <Text type="secondary">{rule.detail}</Text>
                    ) : null}
                  </li>
                ))}
              </ol>
            ) : (
              <Text type="secondary">
                {t("workbuddy.compliance.checklists.noRules")}
              </Text>
            )}
          </div>
        )}
      </Modal>
    </div>
  );
}
