/**
 * 建议详情 — one proposal: governance fields, semantic diff, canary evidence,
 * the review checklist, and the three mutations the slice defines: an
 * independent review decision, reviewer assignment, and an `If-Match`
 * promotion.
 */

import { useCallback, useState } from "react";
import {
  Alert,
  Button,
  Descriptions,
  Form,
  Input,
  InputNumber,
  Modal,
  Radio,
  Select,
  Space,
  Table,
  Tag,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { message } from "@/utils/antdMessage";
import { ArrowRightCircle, Gavel, RefreshCw, UserPlus } from "lucide-react";
import { useTranslation } from "react-i18next";
import { formatServerDateTime } from "../../../utils/formatMessageTime";
import { useServerTimezone } from "../../../hooks/useServerTimezone";
import {
  workbuddyProposalsApi,
  type ImprovementProposal,
  type ProposalEvaluation,
  type ProposalPromotionAction,
  type ProposalReview,
  type ProposalSemanticChange,
} from "../../../api/modules/workbuddyProposals";
import ReviewChecklistPanel from "./ReviewChecklistPanel";
import { describeApiError, isUuid } from "./helpers";
import styles from "./index.module.less";

const { Text, Paragraph } = Typography;
const { TextArea } = Input;

/** Actions the frozen state machine allows from each status. */
const PROMOTION_ACTIONS_BY_STATUS: Record<string, ProposalPromotionAction[]> = {
  under_review: ["abort"],
  approved: ["start_shadow", "start_canary", "abort"],
  shadow: ["start_canary", "abort"],
  canary: ["apply", "abort"],
};

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

interface DecisionFormValues {
  decision: "approved" | "rejected";
  comment?: string;
}

interface ReviewersFormValues {
  reviewer_membership_ids?: string[];
}

interface PromoteFormValues {
  action: ProposalPromotionAction;
  ratio_basis_points?: number;
}

export default function ProposalDetailPanel({
  proposal,
  onUpdated,
  onReload,
}: {
  proposal: ImprovementProposal;
  onUpdated: (next: ImprovementProposal) => void;
  onReload: () => void;
}) {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  const [decisionForm] = Form.useForm<DecisionFormValues>();
  const [reviewersForm] = Form.useForm<ReviewersFormValues>();
  const [promoteForm] = Form.useForm<PromoteFormValues>();

  const [decisionOpen, setDecisionOpen] = useState(false);
  const [reviewersOpen, setReviewersOpen] = useState(false);
  const [promoteOpen, setPromoteOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [modalError, setModalError] = useState<string | null>(null);

  const decide = useCallback(
    async (values: DecisionFormValues) => {
      setBusy(true);
      setModalError(null);
      try {
        const updated = await workbuddyProposalsApi.decideProposal(
          proposal.proposal_id,
          { decision: values.decision, comment: values.comment?.trim() ?? "" },
        );
        message.success(t("workbuddy.proposals.decision.success"));
        setDecisionOpen(false);
        onUpdated(updated);
      } catch (error) {
        setModalError(
          describeApiError(error, t("workbuddy.proposals.decision.failed"), t),
        );
      } finally {
        setBusy(false);
      }
    },
    [onUpdated, proposal.proposal_id, t],
  );

  const assignReviewers = useCallback(
    async (values: ReviewersFormValues) => {
      setBusy(true);
      setModalError(null);
      try {
        const updated = await workbuddyProposalsApi.assignReviewers(
          proposal.proposal_id,
          { reviewer_membership_ids: values.reviewer_membership_ids ?? [] },
        );
        message.success(t("workbuddy.proposals.reviewers.success"));
        setReviewersOpen(false);
        onUpdated(updated);
      } catch (error) {
        setModalError(
          describeApiError(error, t("workbuddy.proposals.reviewers.failed"), t),
        );
      } finally {
        setBusy(false);
      }
    },
    [onUpdated, proposal.proposal_id, t],
  );

  const promote = useCallback(
    async (values: PromoteFormValues) => {
      setBusy(true);
      setModalError(null);
      try {
        const updated = await workbuddyProposalsApi.promoteProposal(
          proposal.proposal_id,
          {
            action: values.action,
            ratio_basis_points:
              values.action === "start_canary"
                ? values.ratio_basis_points ?? 0
                : 0,
          },
          proposal.workflow_revision,
        );
        message.success(
          t("workbuddy.proposals.promote.success", { status: updated.status }),
        );
        setPromoteOpen(false);
        onUpdated(updated);
      } catch (error) {
        setModalError(
          describeApiError(error, t("workbuddy.proposals.promote.failed"), t),
        );
      } finally {
        setBusy(false);
      }
    },
    [onUpdated, proposal.proposal_id, proposal.workflow_revision, t],
  );

  const changeColumns: ColumnsType<ProposalSemanticChange> = [
    {
      title: t("workbuddy.proposals.changes.path"),
      dataIndex: "path",
      key: "path",
      width: 260,
      render: (value: string) => <span className={styles.mono}>{value}</span>,
    },
    {
      title: t("workbuddy.proposals.changes.kind"),
      dataIndex: "kind",
      key: "kind",
      width: 120,
      render: (value: string) => (
        <Tag>{t(`workbuddy.proposals.changeKind.${value}`)}</Tag>
      ),
    },
    {
      title: t("workbuddy.proposals.changes.old"),
      dataIndex: "old",
      key: "old",
      render: (value: unknown) => (
        <span className={styles.mono}>{JSON.stringify(value)}</span>
      ),
    },
    {
      title: t("workbuddy.proposals.changes.new"),
      dataIndex: "new",
      key: "new",
      render: (value: unknown) => (
        <span className={styles.mono}>{JSON.stringify(value)}</span>
      ),
    },
  ];

  const reviewColumns: ColumnsType<ProposalReview> = [
    {
      title: t("workbuddy.proposals.reviews.decision"),
      dataIndex: "decision",
      key: "decision",
      width: 130,
      render: (value: string) => (
        <Tag color={value === "approved" ? "green" : "red"}>
          {t(`workbuddy.proposals.reviewDecision.${value}`)}
        </Tag>
      ),
    },
    {
      title: t("workbuddy.proposals.reviews.reviewer"),
      dataIndex: "reviewer_user_id",
      key: "reviewer_user_id",
      width: 140,
      render: (value: number) => String(value),
    },
    {
      title: t("workbuddy.proposals.reviews.comment"),
      dataIndex: "comment",
      key: "comment",
      render: (value: string) => value?.trim() || "—",
    },
    {
      title: t("workbuddy.proposals.reviews.createdAt"),
      dataIndex: "created_at",
      key: "created_at",
      width: 180,
      render: (value: number) => formatServerDateTime(value, timeZone),
    },
  ];

  const evaluationColumns: ColumnsType<ProposalEvaluation> = [
    {
      title: t("workbuddy.proposals.evaluations.phase"),
      dataIndex: "phase",
      key: "phase",
      width: 110,
      render: (value: string) => (
        <Tag>{t(`workbuddy.proposals.phase.${value}`)}</Tag>
      ),
    },
    {
      title: t("workbuddy.proposals.evaluations.window"),
      key: "window",
      width: 300,
      render: (_value, row) =>
        `${formatServerDateTime(
          row.window_start,
          timeZone,
        )} → ${formatServerDateTime(row.window_end, timeZone)}`,
    },
    {
      title: t("workbuddy.proposals.evaluations.verdict"),
      key: "verdict",
      width: 260,
      render: (_value, row) => (
        <Space direction="vertical" size={2}>
          <Tag color={row.verdict.passed ? "green" : "red"}>
            {t(
              row.verdict.passed
                ? "workbuddy.proposals.checklist.state.passed"
                : "workbuddy.proposals.checklist.state.failed",
            )}
          </Tag>
          {row.verdict.failures.length > 0 && (
            <Text type="secondary" className={styles.mono}>
              {row.verdict.failures.join(", ")}
            </Text>
          )}
        </Space>
      ),
    },
    {
      title: t("workbuddy.proposals.evaluations.samples"),
      key: "samples",
      width: 220,
      render: (_value, row) =>
        t("workbuddy.proposals.evaluations.samplesValue", {
          baseline: row.baseline.settled_runs,
          candidate: row.candidate.settled_runs,
        }),
    },
    {
      title: t("workbuddy.proposals.evaluations.successRate"),
      key: "success",
      width: 220,
      render: (_value, row) =>
        t("workbuddy.proposals.evaluations.successValue", {
          baseline: row.baseline.success_rate.toFixed(3),
          candidate: row.candidate.success_rate.toFixed(3),
        }),
    },
  ];

  const availableActions = PROMOTION_ACTIONS_BY_STATUS[proposal.status] ?? [];
  const reviews = proposal.reviews;

  return (
    <section className={styles.sectionBlock}>
      <div className={styles.sectionTitleRow}>
        <h3 className={styles.sectionTitle}>
          {t("workbuddy.proposals.detail.title")}
        </h3>
        <Space size={8} wrap>
          <Tag color={statusColor(proposal.status)}>
            {t(`workbuddy.proposals.status.${proposal.status}`)}
          </Tag>
          <Tag color={riskColor(proposal.risk_level)}>
            {t("workbuddy.proposals.detail.riskTag", {
              risk: t(`workbuddy.proposals.risk.${proposal.risk_level}`),
            })}
          </Tag>
          {proposal.stale && (
            <Tag color="red">{t("workbuddy.proposals.detail.staleTag")}</Tag>
          )}
          <Button
            size="small"
            icon={<RefreshCw size={14} />}
            onClick={onReload}
          >
            {t("common.refresh")}
          </Button>
          <Button
            size="small"
            icon={<Gavel size={14} />}
            disabled={
              proposal.status !== "under_review" &&
              proposal.status !== "approved"
            }
            onClick={() => {
              setModalError(null);
              decisionForm.resetFields();
              setDecisionOpen(true);
            }}
          >
            {t("workbuddy.proposals.decision.button")}
          </Button>
          <Button
            size="small"
            icon={<UserPlus size={14} />}
            onClick={() => {
              setModalError(null);
              reviewersForm.resetFields();
              setReviewersOpen(true);
            }}
          >
            {t("workbuddy.proposals.reviewers.button")}
          </Button>
          <Button
            size="small"
            type="primary"
            icon={<ArrowRightCircle size={14} />}
            disabled={availableActions.length === 0}
            onClick={() => {
              setModalError(null);
              promoteForm.setFieldsValue({
                action: availableActions[0],
                ratio_basis_points: 1000,
              });
              setPromoteOpen(true);
            }}
          >
            {t("workbuddy.proposals.promote.button")}
          </Button>
        </Space>
      </div>

      {proposal.status_reason?.trim() && (
        <Alert
          type={
            proposal.status === "aborted" || proposal.stale ? "warning" : "info"
          }
          showIcon
          message={proposal.status_reason}
        />
      )}

      {proposal.change_summary?.trim() && (
        <Paragraph type="secondary" className={styles.sectionHint}>
          {proposal.change_summary}
        </Paragraph>
      )}

      <Descriptions size="small" column={2} bordered>
        <Descriptions.Item label={t("workbuddy.proposals.fields.id")}>
          <span className={styles.mono}>{proposal.proposal_id}</span>
        </Descriptions.Item>
        <Descriptions.Item label={t("workbuddy.proposals.fields.workflow")}>
          <span className={styles.mono}>{proposal.workflow_id}</span>
        </Descriptions.Item>
        <Descriptions.Item
          label={t("workbuddy.proposals.fields.workflowRevision")}
        >
          {proposal.workflow_revision}
        </Descriptions.Item>
        <Descriptions.Item label={t("workbuddy.proposals.fields.createdBy")}>
          {proposal.created_by_user_id}
        </Descriptions.Item>
        <Descriptions.Item label={t("workbuddy.proposals.fields.baseVersion")}>
          <span className={styles.mono}>{proposal.base_version_id}</span>
        </Descriptions.Item>
        <Descriptions.Item label={t("workbuddy.proposals.fields.baseHash")}>
          <span className={styles.mono}>{proposal.base_content_hash}</span>
        </Descriptions.Item>
        <Descriptions.Item
          label={t("workbuddy.proposals.fields.candidateVersion")}
        >
          <span className={styles.mono}>{proposal.candidate_version_id}</span>
        </Descriptions.Item>
        <Descriptions.Item
          label={t("workbuddy.proposals.fields.candidateHash")}
        >
          <span className={styles.mono}>{proposal.candidate_content_hash}</span>
        </Descriptions.Item>
        <Descriptions.Item label={t("workbuddy.proposals.fields.createdAt")}>
          {formatServerDateTime(proposal.created_at, timeZone)}
        </Descriptions.Item>
        <Descriptions.Item label={t("workbuddy.proposals.fields.updatedAt")}>
          {formatServerDateTime(proposal.updated_at, timeZone)}
        </Descriptions.Item>
        <Descriptions.Item
          label={t("workbuddy.proposals.fields.appliedVersion")}
        >
          {proposal.applied_version_id ? (
            <span className={styles.mono}>{proposal.applied_version_id}</span>
          ) : (
            "—"
          )}
        </Descriptions.Item>
        <Descriptions.Item
          label={t("workbuddy.proposals.fields.canaryStoppedAt")}
        >
          {proposal.canary_stopped_at
            ? formatServerDateTime(proposal.canary_stopped_at, timeZone)
            : "—"}
        </Descriptions.Item>
      </Descriptions>

      <h4 className={styles.subTitle}>
        {t("workbuddy.proposals.changes.title")}
      </h4>
      {proposal.changes.length === 0 ? (
        <Text type="secondary">{t("workbuddy.proposals.changes.empty")}</Text>
      ) : (
        <Table
          columns={changeColumns}
          dataSource={proposal.changes}
          rowKey={(row) => `${row.kind}:${row.path}`}
          size="small"
          pagination={false}
          scroll={{ x: 900 }}
        />
      )}

      <h4 className={styles.subTitle}>
        {t("workbuddy.proposals.reviews.title")}
      </h4>
      {reviews === undefined ? (
        <Text type="secondary">{t("workbuddy.proposals.reviews.hidden")}</Text>
      ) : reviews.length === 0 ? (
        <Text type="secondary">{t("workbuddy.proposals.reviews.empty")}</Text>
      ) : (
        <Table
          columns={reviewColumns}
          dataSource={reviews}
          rowKey="review_id"
          size="small"
          pagination={false}
          scroll={{ x: 760 }}
        />
      )}

      <h4 className={styles.subTitle}>
        {t("workbuddy.proposals.evaluations.title")}
      </h4>
      {(proposal.evaluations ?? []).length === 0 ? (
        <Text type="secondary">
          {t("workbuddy.proposals.evaluations.empty")}
        </Text>
      ) : (
        <Table
          columns={evaluationColumns}
          dataSource={proposal.evaluations ?? []}
          rowKey="evaluation_id"
          size="small"
          pagination={false}
          scroll={{ x: 1060 }}
        />
      )}

      <ReviewChecklistPanel proposal={proposal} />

      <Modal
        title={t("workbuddy.proposals.decision.title")}
        open={decisionOpen}
        onCancel={() => setDecisionOpen(false)}
        onOk={() => decisionForm.submit()}
        confirmLoading={busy}
        okText={t("workbuddy.proposals.decision.submit")}
        cancelText={t("common.cancel")}
        destroyOnHidden
      >
        <Alert
          type="info"
          showIcon
          className={styles.notice}
          message={t("workbuddy.proposals.decision.independence")}
        />
        <Form
          form={decisionForm}
          layout="vertical"
          requiredMark={false}
          onFinish={(values) => void decide(values)}
        >
          <Form.Item
            name="decision"
            label={t("workbuddy.proposals.decision.field")}
            rules={[
              {
                required: true,
                message: t("workbuddy.proposals.decision.required"),
              },
            ]}
          >
            <Radio.Group
              optionType="button"
              buttonStyle="solid"
              options={[
                {
                  value: "approved",
                  label: t("workbuddy.proposals.decision.approve"),
                },
                {
                  value: "rejected",
                  label: t("workbuddy.proposals.decision.reject"),
                },
              ]}
            />
          </Form.Item>
          <Form.Item
            name="comment"
            label={t("workbuddy.proposals.decision.comment")}
          >
            <TextArea rows={3} maxLength={2000} />
          </Form.Item>
        </Form>
        {modalError && <Alert type="error" showIcon message={modalError} />}
      </Modal>

      <Modal
        title={t("workbuddy.proposals.reviewers.title")}
        open={reviewersOpen}
        onCancel={() => setReviewersOpen(false)}
        onOk={() => reviewersForm.submit()}
        confirmLoading={busy}
        okText={t("workbuddy.proposals.reviewers.submit")}
        cancelText={t("common.cancel")}
        destroyOnHidden
      >
        <Alert
          type="warning"
          showIcon
          className={styles.notice}
          message={t("workbuddy.proposals.reviewers.pendingRoute")}
          description={t("workbuddy.proposals.reviewers.pendingRouteHint")}
        />
        <Form
          form={reviewersForm}
          layout="vertical"
          requiredMark={false}
          onFinish={(values) => void assignReviewers(values)}
        >
          <Form.Item
            name="reviewer_membership_ids"
            label={t("workbuddy.proposals.reviewers.field")}
            extra={t("workbuddy.proposals.reviewers.fieldHint")}
            rules={[
              {
                required: true,
                message: t("workbuddy.proposals.reviewers.required"),
              },
              {
                validator: (_rule, value: string[] | undefined) => {
                  if (!value || value.length === 0) {
                    return Promise.reject(
                      new Error(t("workbuddy.proposals.reviewers.required")),
                    );
                  }
                  const invalid = value.find((entry) => !isUuid(entry));
                  return invalid === undefined
                    ? Promise.resolve()
                    : Promise.reject(
                        new Error(t("workbuddy.proposals.reviewers.invalid")),
                      );
                },
              },
            ]}
          >
            <Select
              mode="tags"
              open={false}
              tokenSeparators={[",", " ", "\n"]}
              placeholder={t("workbuddy.proposals.reviewers.placeholder")}
            />
          </Form.Item>
        </Form>
        {modalError && <Alert type="error" showIcon message={modalError} />}
      </Modal>

      <Modal
        title={t("workbuddy.proposals.promote.title")}
        open={promoteOpen}
        onCancel={() => setPromoteOpen(false)}
        onOk={() => promoteForm.submit()}
        confirmLoading={busy}
        okText={t("workbuddy.proposals.promote.submit")}
        cancelText={t("common.cancel")}
        destroyOnHidden
      >
        <Alert
          type="warning"
          showIcon
          className={styles.notice}
          message={t("workbuddy.proposals.promote.ifMatch", {
            revision: proposal.workflow_revision,
          })}
          description={t("workbuddy.proposals.promote.ifMatchHint")}
        />
        <Form
          form={promoteForm}
          layout="vertical"
          requiredMark={false}
          onFinish={(values) => void promote(values)}
        >
          <Form.Item
            name="action"
            label={t("workbuddy.proposals.promote.actionLabel")}
            rules={[
              {
                required: true,
                message: t("workbuddy.proposals.promote.actionRequired"),
              },
            ]}
          >
            <Select
              options={availableActions.map((action) => ({
                value: action,
                label: t(`workbuddy.proposals.promote.action.${action}`),
              }))}
            />
          </Form.Item>
          <Form.Item
            noStyle
            shouldUpdate={(prev: PromoteFormValues, next: PromoteFormValues) =>
              prev.action !== next.action
            }
          >
            {({ getFieldValue }) =>
              getFieldValue("action") === "start_canary" ? (
                <Form.Item
                  name="ratio_basis_points"
                  label={t("workbuddy.proposals.promote.ratio")}
                  extra={t("workbuddy.proposals.promote.ratioHint")}
                  rules={[
                    {
                      required: true,
                      message: t("workbuddy.proposals.promote.ratioRequired"),
                    },
                  ]}
                >
                  <InputNumber
                    min={1}
                    max={10000}
                    step={100}
                    style={{ width: "100%" }}
                  />
                </Form.Item>
              ) : null
            }
          </Form.Item>
        </Form>
        {modalError && <Alert type="error" showIcon message={modalError} />}
      </Modal>
    </section>
  );
}
