/**
 * 模板市场 → 提交.
 *
 * Author a public draft (name, summary, industry, licence, public definition
 * and capability declarations), read it back with its review trail, freeze it
 * for review, and — on the platform side — record the independent review
 * decision that publishes one immutable version.
 *
 * The slice reserves the public-content rules for the server: a rejection comes
 * back with the server's own code and JSON path, which is shown verbatim.
 */

import { useCallback, useState } from "react";
import {
  Alert,
  Button,
  Descriptions,
  Form,
  Input,
  Modal,
  Popconfirm,
  Radio,
  Space,
  Spin,
  Table,
  Tag,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { message } from "@/utils/antdMessage";
import {
  FilePlus2,
  RefreshCw,
  Search,
  SendHorizonal,
  ShieldCheck,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import { EmptyState } from "../../../components/EmptyState";
import { TabPanelHeader } from "../../Settings/AdvancedSettings/TabPanelHeader";
import { formatServerIsoDateTime } from "../../../utils/formatMessageTime";
import { useServerTimezone } from "../../../hooks/useServerTimezone";
import {
  isMarketplaceUnavailableError,
  workbuddyMarketplaceApi,
  type MarketplaceCapabilityDeclaration,
  type MarketplaceSubmission,
  type MarketplaceSubmissionDetail,
  type MarketplaceSubmissionReview,
  type MarketplaceSubmissionCreateRequest,
  type MarketplaceDecisionRequest,
  type MarketplaceReviewDecision,
} from "../../../api/modules/workbuddyMarketplace";
import {
  describeApiError,
  parseJsonArrayText,
  parseJsonObjectText,
} from "./helpers";
import styles from "./index.module.less";

const { Text, Paragraph } = Typography;
const { TextArea } = Input;

interface DraftFormValues {
  name: string;
  summary?: string;
  industry?: string;
  license_id: string;
  license_text: string;
  definition: string;
  capabilities?: string;
}

interface DecisionFormValues {
  decision: MarketplaceReviewDecision;
  platform_review_ref: string;
  note?: string;
  version?: string;
  publisher_display?: string;
  slug?: string;
  description?: string;
}

function statusColor(status: string): string {
  if (status === "approved") return "green";
  if (status === "rejected") return "red";
  if (status === "submitted") return "blue";
  return "default";
}

export default function SubmissionsPanel({
  knownSubmissionIds,
  onOpened,
}: {
  knownSubmissionIds: string[];
  onOpened: (submissionId: string) => void;
}) {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  const [draftForm] = Form.useForm<DraftFormValues>();
  const [decisionForm] = Form.useForm<DecisionFormValues>();

  const [idInput, setIdInput] = useState("");
  const [detail, setDetail] = useState<MarketplaceSubmissionDetail | null>(
    null,
  );
  const [loading, setLoading] = useState(false);
  const [unavailable, setUnavailable] = useState(false);
  const [errorText, setErrorText] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const [createOpen, setCreateOpen] = useState(false);
  const [createSaving, setCreateSaving] = useState(false);

  const [decisionOpen, setDecisionOpen] = useState(false);
  const [decisionSaving, setDecisionSaving] = useState(false);
  const [decisionError, setDecisionError] = useState<string | null>(null);

  const open = useCallback(
    async (submissionId: string) => {
      const id = submissionId.trim();
      if (!id) return;
      setLoading(true);
      setErrorText(null);
      try {
        const loaded = await workbuddyMarketplaceApi.getSubmission(id);
        setDetail(loaded);
        setUnavailable(false);
        onOpened(loaded.id);
      } catch (error) {
        setDetail(null);
        if (isMarketplaceUnavailableError(error)) {
          setUnavailable(true);
        } else {
          setErrorText(
            describeApiError(
              error,
              t("workbuddy.marketplace.submissions.loadFailed"),
              t,
            ),
          );
        }
      } finally {
        setLoading(false);
      }
    },
    [onOpened, t],
  );

  const createDraft = useCallback(
    async (values: DraftFormValues) => {
      const definition = parseJsonObjectText(values.definition);
      if (!definition.ok) {
        message.error(
          definition.reason === "notObject"
            ? t("workbuddy.marketplace.submissions.create.definitionNotObject")
            : definition.message,
        );
        return;
      }
      let capabilities: MarketplaceCapabilityDeclaration[] | undefined;
      if (values.capabilities?.trim()) {
        const parsed = parseJsonArrayText(values.capabilities);
        if (!parsed.ok) {
          message.error(
            parsed.reason === "notArray"
              ? t(
                  "workbuddy.marketplace.submissions.create.capabilitiesNotArray",
                )
              : parsed.message,
          );
          return;
        }
        capabilities = parsed.value as MarketplaceCapabilityDeclaration[];
      }
      setCreateSaving(true);
      try {
        const body: MarketplaceSubmissionCreateRequest = {
          name: values.name.trim(),
          summary: values.summary?.trim() ?? "",
          industry: values.industry?.trim() ?? "",
          definition: definition.value,
          license_id: values.license_id.trim(),
          license_text: values.license_text,
          capabilities,
        };
        const created = await workbuddyMarketplaceApi.createSubmission(body);
        message.success(t("workbuddy.marketplace.submissions.create.success"));
        setCreateOpen(false);
        draftForm.resetFields();
        setIdInput(created.id);
        await open(created.id);
      } catch (error) {
        message.error(
          describeApiError(
            error,
            t("workbuddy.marketplace.submissions.create.failed"),
            t,
          ),
        );
      } finally {
        setCreateSaving(false);
      }
    },
    [draftForm, open, t],
  );

  const submitForReview = useCallback(async () => {
    if (!detail) return;
    setSubmitting(true);
    try {
      const updated = await workbuddyMarketplaceApi.submitSubmission(
        detail.id,
        {
          expected_revision: detail.revision,
        },
      );
      setDetail({ ...detail, ...updated });
      message.success(
        t("workbuddy.marketplace.submissions.submitForReview.success"),
      );
    } catch (error) {
      message.error(
        describeApiError(
          error,
          t("workbuddy.marketplace.submissions.submitForReview.failed"),
          t,
        ),
      );
    } finally {
      setSubmitting(false);
    }
  }, [detail, t]);

  const openDecision = useCallback(() => {
    setDecisionError(null);
    decisionForm.resetFields();
    setDecisionOpen(true);
  }, [decisionForm]);

  const decide = useCallback(
    async (values: DecisionFormValues) => {
      if (!detail) return;
      setDecisionSaving(true);
      setDecisionError(null);
      try {
        const body: MarketplaceDecisionRequest = {
          decision: values.decision,
          platform_review_ref: values.platform_review_ref.trim(),
          note: values.note?.trim() || undefined,
          expected_revision: detail.revision,
          publication:
            values.decision === "approved"
              ? {
                  version: values.version?.trim() ?? "",
                  publisher_display: values.publisher_display?.trim() ?? "",
                  slug: values.slug?.trim() || undefined,
                  description: values.description?.trim() || undefined,
                }
              : undefined,
        };
        const result = await workbuddyMarketplaceApi.decidePlatformSubmission(
          detail.tenant_id,
          detail.id,
          body,
        );
        message.success(
          t("workbuddy.marketplace.submissions.review.success", {
            status: result.submission.status,
          }),
        );
        setDecisionOpen(false);
        await open(detail.id);
      } catch (error) {
        setDecisionError(
          describeApiError(
            error,
            t("workbuddy.marketplace.submissions.review.failed"),
            t,
          ),
        );
      } finally {
        setDecisionSaving(false);
      }
    },
    [detail, open, t],
  );

  const reviewColumns: ColumnsType<MarketplaceSubmissionReview> = [
    {
      title: t("workbuddy.marketplace.submissions.reviews.decision"),
      dataIndex: "decision",
      key: "decision",
      width: 130,
      render: (value: string) => (
        <Tag color={statusColor(value)}>
          {t(`workbuddy.marketplace.submissionStatus.${value}`)}
        </Tag>
      ),
    },
    {
      title: t("workbuddy.marketplace.submissions.reviews.ref"),
      dataIndex: "platform_review_ref",
      key: "platform_review_ref",
      width: 220,
      render: (value: string | null) => value?.trim() || "—",
    },
    {
      title: t("workbuddy.marketplace.submissions.reviews.note"),
      dataIndex: "note",
      key: "note",
      render: (value: string | null) => value?.trim() || "—",
    },
    {
      title: t("workbuddy.marketplace.submissions.reviews.createdAt"),
      dataIndex: "created_at",
      key: "created_at",
      width: 180,
      render: (value: string | null) =>
        value ? formatServerIsoDateTime(value, timeZone) : "—",
    },
  ];

  const capabilityColumns: ColumnsType<MarketplaceCapabilityDeclaration> = [
    {
      title: t("workbuddy.marketplace.capability.columns.kind"),
      dataIndex: "kind",
      key: "kind",
      width: 150,
      render: (kind: MarketplaceCapabilityDeclaration["kind"]) => (
        <Tag>{t(`workbuddy.marketplace.capability.kind.${kind}`)}</Tag>
      ),
    },
    {
      title: t("workbuddy.marketplace.capability.columns.key"),
      dataIndex: "key",
      key: "key",
      width: 200,
    },
    {
      title: t("workbuddy.marketplace.capability.columns.effect"),
      dataIndex: "effect",
      key: "effect",
      width: 150,
      render: (value: string | null | undefined) =>
        value ? t(`workbuddy.marketplace.capability.effect.${value}`) : "—",
    },
    {
      title: t("workbuddy.marketplace.capability.columns.revision"),
      dataIndex: "revision_id",
      key: "revision_id",
      width: 300,
      render: (value: string | null | undefined) => value?.trim() || "—",
    },
  ];

  const header = (
    <TabPanelHeader
      icon={<FilePlus2 size={18} />}
      title={t("workbuddy.marketplace.submissions.title")}
      description={t("workbuddy.marketplace.submissions.desc")}
      actions={
        <Space size={8}>
          <Button
            size="small"
            icon={<RefreshCw size={14} />}
            disabled={!detail}
            onClick={() => detail && void open(detail.id)}
          >
            {t("common.refresh")}
          </Button>
          <Button
            type="primary"
            size="small"
            icon={<FilePlus2 size={14} />}
            onClick={() => {
              draftForm.resetFields();
              setCreateOpen(true);
            }}
          >
            {t("workbuddy.marketplace.submissions.create.button")}
          </Button>
        </Space>
      }
    />
  );

  const reviewable = detail?.status === "submitted";
  const draftable: MarketplaceSubmission | null =
    detail !== null && detail.status === "draft" ? detail : null;

  const detailFields = !detail ? null : (
    <Descriptions size="small" column={2} bordered>
      <Descriptions.Item
        label={t("workbuddy.marketplace.submissions.fields.id")}
      >
        <span className={styles.mono}>{detail.id}</span>
      </Descriptions.Item>
      <Descriptions.Item
        label={t("workbuddy.marketplace.submissions.fields.revision")}
      >
        {detail.revision}
      </Descriptions.Item>
      <Descriptions.Item
        label={t("workbuddy.marketplace.submissions.fields.definitionHash")}
      >
        <span className={styles.mono}>{detail.definition_hash}</span>
      </Descriptions.Item>
      <Descriptions.Item
        label={t("workbuddy.marketplace.submissions.fields.license")}
      >
        {detail.license_id} ·{" "}
        <span className={styles.mono}>{detail.license_text_hash}</span>
      </Descriptions.Item>
      <Descriptions.Item
        label={t("workbuddy.marketplace.submissions.fields.submittedAt")}
      >
        {detail.submitted_at
          ? formatServerIsoDateTime(detail.submitted_at, timeZone)
          : "—"}
      </Descriptions.Item>
      <Descriptions.Item
        label={t("workbuddy.marketplace.submissions.fields.reviewedAt")}
      >
        {detail.reviewed_at
          ? formatServerIsoDateTime(detail.reviewed_at, timeZone)
          : "—"}
      </Descriptions.Item>
      <Descriptions.Item
        label={t("workbuddy.marketplace.submissions.fields.reviewNote")}
      >
        {detail.review_note?.trim() || "—"}
      </Descriptions.Item>
      <Descriptions.Item
        label={t("workbuddy.marketplace.submissions.fields.platformRef")}
      >
        {detail.platform_review_ref?.trim() || "—"}
      </Descriptions.Item>
      <Descriptions.Item
        label={t("workbuddy.marketplace.submissions.fields.publishedVersion")}
      >
        {detail.published_template_version_id ? (
          <span className={styles.mono}>
            {detail.published_template_version_id}
          </span>
        ) : (
          "—"
        )}
      </Descriptions.Item>
      <Descriptions.Item
        label={t("workbuddy.marketplace.submissions.fields.frozenHash")}
      >
        <span className={styles.mono}>
          {detail.frozen_definition_hash ?? "—"}
        </span>
      </Descriptions.Item>
    </Descriptions>
  );

  if (unavailable) {
    return (
      <div className={styles.panel}>
        {header}
        <EmptyState
          variant="error"
          title={t("workbuddy.marketplace.unavailable.title")}
          description={t("workbuddy.marketplace.unavailable.hint")}
          actionLabel={t("common.refresh")}
          onAction={() => void open(idInput)}
        />
      </div>
    );
  }

  return (
    <div className={styles.panel}>
      {header}

      <Space.Compact style={{ width: "100%", maxWidth: 640 }}>
        <Input
          value={idInput}
          onChange={(event) => setIdInput(event.target.value)}
          placeholder={t("workbuddy.marketplace.submissions.openPlaceholder")}
          onPressEnter={() => void open(idInput)}
        />
        <Button
          loading={loading}
          icon={<Search size={14} />}
          disabled={!idInput.trim()}
          onClick={() => void open(idInput)}
        >
          {t("workbuddy.marketplace.submissions.open")}
        </Button>
      </Space.Compact>

      {knownSubmissionIds.length > 0 && (
        <Space size={6} wrap style={{ marginTop: 8 }}>
          <Text type="secondary">
            {t("workbuddy.marketplace.submissions.sessionIds")}
          </Text>
          {knownSubmissionIds.map((id) => (
            <Tag
              key={id}
              color={detail?.id === id ? "blue" : "default"}
              style={{ cursor: "pointer" }}
              onClick={() => void open(id)}
            >
              <span className={styles.mono}>{id.slice(0, 8)}</span>
            </Tag>
          ))}
        </Space>
      )}

      {errorText && (
        <Alert
          className={styles.notice}
          type="error"
          showIcon
          message={errorText}
        />
      )}

      {loading && !detail ? (
        <div className={styles.centered}>
          <Spin />
        </div>
      ) : !detail ? (
        <EmptyState
          variant="mascot"
          title={t("workbuddy.marketplace.submissions.empty")}
          description={t("workbuddy.marketplace.submissions.emptyHint")}
        />
      ) : (
        <>
          <div className={styles.sectionTitleRow}>
            <h3 className={styles.sectionTitle}>{detail.name}</h3>
            <Space size={8} wrap>
              <Tag color={statusColor(detail.status)}>
                {t(`workbuddy.marketplace.submissionStatus.${detail.status}`)}
              </Tag>
              <Popconfirm
                title={t(
                  "workbuddy.marketplace.submissions.submitForReview.confirmTitle",
                )}
                description={t(
                  "workbuddy.marketplace.submissions.submitForReview.confirmDesc",
                )}
                okText={t("common.confirm")}
                cancelText={t("common.cancel")}
                disabled={!draftable}
                onConfirm={() => void submitForReview()}
              >
                <Button
                  size="small"
                  type="primary"
                  icon={<SendHorizonal size={14} />}
                  disabled={!draftable}
                  loading={submitting}
                >
                  {t(
                    "workbuddy.marketplace.submissions.submitForReview.button",
                  )}
                </Button>
              </Popconfirm>
              <Button
                size="small"
                icon={<ShieldCheck size={14} />}
                disabled={!reviewable}
                onClick={openDecision}
              >
                {t("workbuddy.marketplace.submissions.review.button")}
              </Button>
            </Space>
          </div>

          {draftable && (
            <Alert
              className={styles.notice}
              type="info"
              showIcon
              message={t(
                "workbuddy.marketplace.submissions.submitForReview.hint",
                {
                  revision: detail.revision,
                },
              )}
            />
          )}

          {detail.summary?.trim() && (
            <Paragraph type="secondary" className={styles.sectionHint}>
              {detail.summary}
            </Paragraph>
          )}

          {detailFields}

          <h4 className={styles.subTitle}>
            {t("workbuddy.marketplace.submissions.capabilitiesTitle")}
          </h4>
          {(detail.requested_capabilities ?? []).length === 0 ? (
            <Text type="secondary">
              {t("workbuddy.marketplace.submissions.capabilitiesEmpty")}
            </Text>
          ) : (
            <Table
              columns={capabilityColumns}
              dataSource={detail.requested_capabilities ?? []}
              rowKey={(row) => `${row.kind}:${row.key}`}
              size="small"
              pagination={false}
              scroll={{ x: 800 }}
            />
          )}

          <h4 className={styles.subTitle}>
            {t("workbuddy.marketplace.submissions.definitionTitle")}
          </h4>
          <pre className={styles.codeBlock}>
            {JSON.stringify(detail.definition, null, 2)}
          </pre>

          <h4 className={styles.subTitle}>
            {t("workbuddy.marketplace.submissions.reviewsTitle")}
          </h4>
          {(detail.reviews ?? []).length === 0 ? (
            <Text type="secondary">
              {t("workbuddy.marketplace.submissions.reviewsEmpty")}
            </Text>
          ) : (
            <Table
              columns={reviewColumns}
              dataSource={detail.reviews ?? []}
              rowKey="id"
              size="small"
              pagination={false}
              scroll={{ x: 800 }}
            />
          )}
        </>
      )}

      <Modal
        title={t("workbuddy.marketplace.submissions.create.title")}
        open={createOpen}
        onCancel={() => setCreateOpen(false)}
        onOk={() => draftForm.submit()}
        confirmLoading={createSaving}
        okText={t("workbuddy.marketplace.submissions.create.submit")}
        cancelText={t("common.cancel")}
        width={720}
        destroyOnHidden
      >
        <Alert
          type="info"
          showIcon
          className={styles.notice}
          message={t("workbuddy.marketplace.submissions.create.notice")}
        />
        <Form
          form={draftForm}
          layout="vertical"
          requiredMark={false}
          onFinish={(values) => void createDraft(values)}
        >
          <Form.Item
            name="name"
            label={t("workbuddy.marketplace.submissions.create.name")}
            rules={[
              {
                required: true,
                message: t(
                  "workbuddy.marketplace.submissions.create.nameRequired",
                ),
              },
            ]}
          >
            <Input maxLength={120} />
          </Form.Item>
          <Form.Item
            name="summary"
            label={t("workbuddy.marketplace.submissions.create.summary")}
          >
            <TextArea rows={2} maxLength={1000} />
          </Form.Item>
          <Form.Item
            name="industry"
            label={t("workbuddy.marketplace.submissions.create.industry")}
          >
            <Input maxLength={120} />
          </Form.Item>
          <Form.Item
            name="license_id"
            label={t("workbuddy.marketplace.submissions.create.licenseId")}
            rules={[
              {
                required: true,
                message: t(
                  "workbuddy.marketplace.submissions.create.licenseIdRequired",
                ),
              },
            ]}
          >
            <Input maxLength={64} />
          </Form.Item>
          <Form.Item
            name="license_text"
            label={t("workbuddy.marketplace.submissions.create.licenseText")}
            extra={t(
              "workbuddy.marketplace.submissions.create.licenseTextHint",
            )}
            rules={[
              {
                required: true,
                message: t(
                  "workbuddy.marketplace.submissions.create.licenseTextRequired",
                ),
              },
            ]}
          >
            <TextArea rows={4} />
          </Form.Item>
          <Form.Item
            name="definition"
            label={t("workbuddy.marketplace.submissions.create.definition")}
            extra={t("workbuddy.marketplace.submissions.create.definitionHint")}
            rules={[
              {
                required: true,
                message: t(
                  "workbuddy.marketplace.submissions.create.definitionRequired",
                ),
              },
            ]}
          >
            <TextArea rows={8} className={styles.codeInput} />
          </Form.Item>
          <Form.Item
            name="capabilities"
            label={t("workbuddy.marketplace.submissions.create.capabilities")}
            extra={t(
              "workbuddy.marketplace.submissions.create.capabilitiesHint",
            )}
          >
            <TextArea rows={4} className={styles.codeInput} />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={t("workbuddy.marketplace.submissions.review.title")}
        open={decisionOpen}
        onCancel={() => setDecisionOpen(false)}
        onOk={() => decisionForm.submit()}
        confirmLoading={decisionSaving}
        okText={t("workbuddy.marketplace.submissions.review.submit")}
        cancelText={t("common.cancel")}
        width={680}
        destroyOnHidden
      >
        {detail && (
          <Alert
            type="warning"
            showIcon
            className={styles.notice}
            message={t("workbuddy.marketplace.submissions.review.scope", {
              tenant: detail.tenant_id,
              submission: detail.id,
            })}
            description={t(
              "workbuddy.marketplace.submissions.review.scopeHint",
            )}
          />
        )}
        <Form
          form={decisionForm}
          layout="vertical"
          requiredMark={false}
          onFinish={(values) => void decide(values)}
        >
          <Form.Item
            name="decision"
            label={t("workbuddy.marketplace.submissions.review.decision")}
            rules={[
              {
                required: true,
                message: t(
                  "workbuddy.marketplace.submissions.review.decisionRequired",
                ),
              },
            ]}
          >
            <Radio.Group
              optionType="button"
              buttonStyle="solid"
              options={[
                {
                  value: "approved",
                  label: t("workbuddy.marketplace.submissions.review.approve"),
                },
                {
                  value: "rejected",
                  label: t("workbuddy.marketplace.submissions.review.reject"),
                },
              ]}
            />
          </Form.Item>
          <Form.Item
            name="platform_review_ref"
            label={t("workbuddy.marketplace.submissions.review.ref")}
            extra={t("workbuddy.marketplace.submissions.review.refHint")}
            rules={[
              {
                required: true,
                message: t(
                  "workbuddy.marketplace.submissions.review.refRequired",
                ),
              },
            ]}
          >
            <Input maxLength={200} />
          </Form.Item>
          <Form.Item
            name="note"
            label={t("workbuddy.marketplace.submissions.review.note")}
          >
            <TextArea rows={3} maxLength={1000} />
          </Form.Item>
          <Form.Item
            noStyle
            shouldUpdate={(
              prev: DecisionFormValues,
              next: DecisionFormValues,
            ) => prev.decision !== next.decision}
          >
            {({ getFieldValue }) =>
              getFieldValue("decision") === "rejected" ? null : (
                <>
                  <Form.Item
                    name="version"
                    label={t(
                      "workbuddy.marketplace.submissions.review.version",
                    )}
                    extra={t(
                      "workbuddy.marketplace.submissions.review.versionHint",
                    )}
                    rules={[
                      {
                        required: true,
                        message: t(
                          "workbuddy.marketplace.submissions.review.versionRequired",
                        ),
                      },
                    ]}
                  >
                    <Input maxLength={32} placeholder="1.0.0" />
                  </Form.Item>
                  <Form.Item
                    name="publisher_display"
                    label={t(
                      "workbuddy.marketplace.submissions.review.publisherDisplay",
                    )}
                    rules={[
                      {
                        required: true,
                        message: t(
                          "workbuddy.marketplace.submissions.review.publisherRequired",
                        ),
                      },
                    ]}
                  >
                    <Input maxLength={120} />
                  </Form.Item>
                  <Form.Item
                    name="slug"
                    label={t("workbuddy.marketplace.submissions.review.slug")}
                    extra={t(
                      "workbuddy.marketplace.submissions.review.slugHint",
                    )}
                  >
                    <Input maxLength={64} />
                  </Form.Item>
                  <Form.Item
                    name="description"
                    label={t(
                      "workbuddy.marketplace.submissions.review.description",
                    )}
                  >
                    <TextArea rows={3} />
                  </Form.Item>
                </>
              )
            }
          </Form.Item>
        </Form>
        {decisionError && (
          <Alert type="error" showIcon message={decisionError} />
        )}
      </Modal>
    </div>
  );
}
