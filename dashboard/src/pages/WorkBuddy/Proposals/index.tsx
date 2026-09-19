/**
 * 改进建议 (/workbuddy/proposals) — WorkBuddy improvement proposals.
 *
 * Lists the proposals this member may see, opens one with its semantic diff,
 * review trail, canary evidence and review checklist, and drives the three
 * mutations the slice defines: an independent review decision, reviewer
 * assignment, and an `If-Match` promotion against the workflow revision.
 *
 * The backend slice is unmerged, so a 404/501/503 renders an explicit
 * "not available yet" state instead of an empty list that looks like "none".
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
  Spin,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { message } from "@/utils/antdMessage";
import { Plus, RefreshCw, Search } from "lucide-react";
import { useTranslation } from "react-i18next";
import PageShell from "../../../layouts/PageShell";
import { EmptyState } from "../../../components/EmptyState";
import { ResizableTable } from "../../../components/ResizableTable";
import { formatServerDateTime } from "../../../utils/formatMessageTime";
import { useServerTimezone } from "../../../hooks/useServerTimezone";
import {
  isProposalsUnavailableError,
  workbuddyProposalsApi,
  type ImprovementProposal,
  type ProposalCreateResult,
  type ProposalStatus,
} from "../../../api/modules/workbuddyProposals";
import ProposalDetailPanel from "./ProposalDetailPanel";
import { describeApiError, isUuid, parsePatchText } from "./helpers";
import styles from "./index.module.less";

const { Text } = Typography;
const { TextArea } = Input;

const STATUS_OPTIONS: readonly ProposalStatus[] = [
  "under_review",
  "approved",
  "rejected",
  "shadow",
  "canary",
  "applied",
  "aborted",
  "superseded",
  "stale",
];

interface FilterValues {
  workflow_id?: string;
  status?: ProposalStatus;
  limit?: number;
}

interface CreateFormValues {
  workflow_id: string;
  workflow_revision: number;
  patch: string;
  change_summary?: string;
}

function statusColor(status: string): string {
  if (status === "applied" || status === "approved") return "green";
  if (status === "rejected" || status === "aborted" || status === "stale")
    return "red";
  if (status === "canary" || status === "shadow") return "blue";
  return "default";
}

function riskColor(risk: string): string {
  if (risk === "low") return "green";
  if (risk === "high") return "red";
  return "gold";
}

export default function ProposalsPage() {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  const [filterForm] = Form.useForm<FilterValues>();
  const [createForm] = Form.useForm<CreateFormValues>();

  const [items, setItems] = useState<ImprovementProposal[]>([]);
  const [loading, setLoading] = useState(true);
  const [unavailable, setUnavailable] = useState(false);

  const [detail, setDetail] = useState<ImprovementProposal | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);

  const [createOpen, setCreateOpen] = useState(false);
  const [createSaving, setCreateSaving] = useState(false);
  const [created, setCreated] = useState<ProposalCreateResult | null>(null);

  const loadList = useCallback(
    async (filter: FilterValues = filterForm.getFieldsValue()) => {
      setLoading(true);
      try {
        setItems(
          await workbuddyProposalsApi.listProposals({
            workflow_id: filter.workflow_id?.trim() || undefined,
            status: filter.status,
            limit: filter.limit,
          }),
        );
        setUnavailable(false);
      } catch (error) {
        if (isProposalsUnavailableError(error)) {
          setUnavailable(true);
          setItems([]);
        } else {
          message.error(
            describeApiError(
              error,
              t("workbuddy.proposals.list.loadFailed"),
              t,
            ),
          );
        }
      } finally {
        setLoading(false);
      }
    },
    [filterForm, t],
  );

  useEffect(() => {
    void loadList({});
  }, [loadList]);

  const open = useCallback(
    async (proposalId: string) => {
      setDetailLoading(true);
      setDetailError(null);
      try {
        setDetail(await workbuddyProposalsApi.getProposal(proposalId));
      } catch (error) {
        setDetail(null);
        setDetailError(
          isProposalsUnavailableError(error)
            ? t("workbuddy.proposals.unavailable.hint")
            : describeApiError(
                error,
                t("workbuddy.proposals.detail.loadFailed"),
                t,
              ),
        );
      } finally {
        setDetailLoading(false);
      }
    },
    [t],
  );

  const create = useCallback(
    async (values: CreateFormValues) => {
      const patch = parsePatchText(values.patch);
      if (!patch.ok) {
        message.error(
          patch.reason === "parse"
            ? patch.message
            : patch.reason === "notObject"
            ? t("workbuddy.proposals.create.patchError.notObject", {
                index: patch.index + 1,
              })
            : t(`workbuddy.proposals.create.patchError.${patch.reason}`),
        );
        return;
      }
      setCreateSaving(true);
      try {
        const result = await workbuddyProposalsApi.createProposal(
          values.workflow_id.trim(),
          {
            workflow_revision: values.workflow_revision,
            patch: patch.value,
            change_summary: values.change_summary?.trim() ?? "",
          },
        );
        setCreated(result);
        message.success(t("workbuddy.proposals.create.success"));
        createForm.resetFields();
        await loadList();
        await open(result.proposal_id);
      } catch (error) {
        message.error(
          describeApiError(error, t("workbuddy.proposals.create.failed"), t),
        );
      } finally {
        setCreateSaving(false);
      }
    },
    [createForm, loadList, open, t],
  );

  const columns: ColumnsType<ImprovementProposal> = [
    {
      title: t("workbuddy.proposals.list.columns.proposal"),
      dataIndex: "proposal_id",
      key: "proposal_id",
      width: 160,
      render: (value: string) => (
        <Tooltip title={value}>
          <span className={styles.mono}>{value.slice(0, 8)}</span>
        </Tooltip>
      ),
    },
    {
      title: t("workbuddy.proposals.list.columns.workflow"),
      dataIndex: "workflow_id",
      key: "workflow_id",
      width: 160,
      render: (value: string) => (
        <Tooltip title={value}>
          <span className={styles.mono}>{value.slice(0, 8)}</span>
        </Tooltip>
      ),
    },
    {
      title: t("workbuddy.proposals.list.columns.status"),
      dataIndex: "status",
      key: "status",
      width: 150,
      render: (value: string, row) => (
        <Space size={4} wrap>
          <Tag color={statusColor(value)}>
            {t(`workbuddy.proposals.status.${value}`)}
          </Tag>
          {row.stale && (
            <Tag color="red">{t("workbuddy.proposals.detail.staleTag")}</Tag>
          )}
        </Space>
      ),
    },
    {
      title: t("workbuddy.proposals.list.columns.risk"),
      dataIndex: "risk_level",
      key: "risk_level",
      width: 110,
      render: (value: string) => (
        <Tag color={riskColor(value)}>
          {t(`workbuddy.proposals.risk.${value}`)}
        </Tag>
      ),
    },
    {
      title: t("workbuddy.proposals.list.columns.approvals"),
      key: "approvals",
      width: 130,
      render: (_value, row) =>
        t("workbuddy.proposals.list.approvalsValue", {
          approved:
            row.reviews?.filter((review) => review.decision === "approved")
              .length ?? 0,
          required: row.required_approvals,
        }),
    },
    {
      title: t("workbuddy.proposals.list.columns.summary"),
      dataIndex: "change_summary",
      key: "change_summary",
      render: (value: string) => value?.trim() || "—",
    },
    {
      title: t("workbuddy.proposals.list.columns.updatedAt"),
      dataIndex: "updated_at",
      key: "updated_at",
      width: 180,
      render: (value: number) => formatServerDateTime(value, timeZone),
    },
    {
      title: t("common.actions"),
      key: "actions",
      width: 110,
      render: (_value, row) => (
        <Button
          type="link"
          size="small"
          onClick={() => void open(row.proposal_id)}
        >
          {t("common.view")}
        </Button>
      ),
    },
  ];

  const statusOptions = useMemo(
    () =>
      STATUS_OPTIONS.map((status) => ({
        value: status,
        label: t(`workbuddy.proposals.status.${status}`),
      })),
    [t],
  );

  return (
    <PageShell
      title={t("workbuddy.proposals.title")}
      subtitle={t("workbuddy.proposals.subtitle")}
      actions={
        <Space size={8} wrap>
          <Button
            size="small"
            icon={<RefreshCw size={14} />}
            onClick={() => void loadList()}
          >
            {t("common.refresh")}
          </Button>
          <Button
            type="primary"
            size="small"
            icon={<Plus size={14} />}
            onClick={() => {
              setCreated(null);
              createForm.resetFields();
              setCreateOpen(true);
            }}
          >
            {t("workbuddy.proposals.create.button")}
          </Button>
        </Space>
      }
    >
      <Alert
        type="info"
        showIcon
        className={styles.notice}
        message={t("workbuddy.proposals.notice.title")}
        description={t("workbuddy.proposals.notice.desc")}
      />

      <Form
        form={filterForm}
        layout="inline"
        className={styles.filterBar}
        onFinish={(values) => void loadList(values)}
      >
        <Form.Item
          name="workflow_id"
          label={t("workbuddy.proposals.filters.workflow")}
        >
          <Input
            allowClear
            style={{ width: 280 }}
            placeholder={t("workbuddy.proposals.filters.workflowPlaceholder")}
          />
        </Form.Item>
        <Form.Item
          name="status"
          label={t("workbuddy.proposals.filters.status")}
        >
          <Select
            allowClear
            style={{ width: 180 }}
            placeholder={t("workbuddy.proposals.filters.statusPlaceholder")}
            options={statusOptions}
          />
        </Form.Item>
        <Form.Item name="limit" label={t("workbuddy.proposals.filters.limit")}>
          <Select
            style={{ width: 120 }}
            options={[
              { value: 20, label: "20" },
              { value: 50, label: "50" },
              { value: 100, label: "100" },
            ]}
          />
        </Form.Item>
        <Form.Item>
          <Space size={8}>
            <Button
              type="primary"
              htmlType="submit"
              icon={<Search size={14} />}
              loading={loading}
            >
              {t("workbuddy.proposals.filters.apply")}
            </Button>
            <Button
              onClick={() => {
                filterForm.resetFields();
                void loadList({});
              }}
            >
              {t("common.reset")}
            </Button>
          </Space>
        </Form.Item>
      </Form>

      {unavailable ? (
        <EmptyState
          variant="error"
          title={t("workbuddy.proposals.unavailable.title")}
          description={t("workbuddy.proposals.unavailable.hint")}
          actionLabel={t("common.refresh")}
          onAction={() => void loadList()}
        />
      ) : loading && items.length === 0 ? (
        <div className={styles.centered}>
          <Spin />
        </div>
      ) : items.length === 0 ? (
        <EmptyState
          variant="mascot"
          title={t("workbuddy.proposals.list.empty")}
          description={t("workbuddy.proposals.list.emptyHint")}
        />
      ) : (
        <ResizableTable
          columns={columns}
          dataSource={items}
          rowKey="proposal_id"
          size="middle"
          tableLayout="fixed"
          scroll={{ x: 1200 }}
          storageKey="workbuddy-proposals-table-widths"
          minWidth={72}
          pagination={false}
          onRow={(row) => ({
            onClick: () => void open(row.proposal_id),
            style: { cursor: "pointer" },
          })}
          rowClassName={(row) =>
            row.proposal_id === detail?.proposal_id ? styles.selectedRow : ""
          }
        />
      )}

      {detailError && (
        <Alert
          className={styles.notice}
          type="error"
          showIcon
          message={detailError}
        />
      )}

      {detailLoading && (
        <div className={styles.centered}>
          <Spin />
        </div>
      )}

      {detail && !detailLoading && (
        <ProposalDetailPanel
          proposal={detail}
          onUpdated={(next) => {
            setDetail(next);
            setItems((prev) =>
              prev.map((item) =>
                item.proposal_id === next.proposal_id ? next : item,
              ),
            );
          }}
          onReload={() => void open(detail.proposal_id)}
        />
      )}

      <Modal
        title={t("workbuddy.proposals.create.title")}
        open={createOpen}
        onCancel={() => setCreateOpen(false)}
        onOk={() => createForm.submit()}
        confirmLoading={createSaving}
        okText={t("workbuddy.proposals.create.submit")}
        cancelText={t("common.cancel")}
        width={720}
        destroyOnHidden
      >
        <Alert
          type="info"
          showIcon
          className={styles.notice}
          message={t("workbuddy.proposals.create.notice")}
        />
        <Form
          form={createForm}
          layout="vertical"
          requiredMark={false}
          initialValues={{ workflow_revision: 0 }}
          onFinish={(values) => void create(values)}
        >
          <Form.Item
            name="workflow_id"
            label={t("workbuddy.proposals.create.workflow")}
            rules={[
              {
                required: true,
                message: t("workbuddy.proposals.create.workflowRequired"),
              },
              {
                validator: (_rule, value: string | undefined) =>
                  !value?.trim() || isUuid(value)
                    ? Promise.resolve()
                    : Promise.reject(
                        new Error(
                          t("workbuddy.proposals.create.workflowInvalid"),
                        ),
                      ),
              },
            ]}
          >
            <Input
              placeholder={t("workbuddy.proposals.create.workflowPlaceholder")}
            />
          </Form.Item>
          <Form.Item
            name="workflow_revision"
            label={t("workbuddy.proposals.create.revision")}
            extra={t("workbuddy.proposals.create.revisionHint")}
            rules={[
              {
                required: true,
                message: t("workbuddy.proposals.create.revisionRequired"),
              },
            ]}
          >
            <InputNumber min={0} style={{ width: "100%" }} />
          </Form.Item>
          <Form.Item
            name="patch"
            label={t("workbuddy.proposals.create.patch")}
            extra={t("workbuddy.proposals.create.patchHint")}
            rules={[
              {
                required: true,
                message: t("workbuddy.proposals.create.patchRequired"),
              },
            ]}
          >
            <TextArea rows={8} className={styles.codeInput} />
          </Form.Item>
          <Form.Item
            name="change_summary"
            label={t("workbuddy.proposals.create.summary")}
          >
            <Input maxLength={500} />
          </Form.Item>
        </Form>
        {created && (
          <Alert
            type="success"
            showIcon
            message={t("workbuddy.proposals.create.result", {
              status: created.status,
              risk: t(`workbuddy.proposals.risk.${created.risk_level}`),
              approvals: created.required_approvals,
            })}
            description={
              <Space direction="vertical" size={2}>
                <span className={styles.mono}>{created.proposal_id}</span>
                <Text type="secondary">
                  {t("workbuddy.proposals.create.resultJob", {
                    job: created.job_id,
                  })}
                </Text>
              </Space>
            }
          />
        )}
      </Modal>
    </PageShell>
  );
}
