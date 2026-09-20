/**
 * WorkBuddy inbox → review what a settled run produced.
 *
 * The one place a review is decided: the inbox opens it for a review the caller
 * was asked to do, and the runs page opens it for a settled execution, so both
 * entries read the same review and submit through the same route.
 *
 * Nothing in here holds a run. The execution settled, its outputs were copied
 * into the review when it opened, and every decision adds a record *about* that
 * result: accepting leaves it standing, correcting replaces values the run
 * actually produced — the server refuses a key it never produced, so the editor
 * offers exactly the produced keys and no way to add one — and re-running starts
 * a new execution the review links to. The checks here only keep an obviously
 * incomplete decision from being sent; the server stays authoritative and its
 * own message is what the drawer shows.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Descriptions,
  Drawer,
  Form,
  Input,
  Modal,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { message } from "@/utils/antdMessage";
import { BadgeCheck, ExternalLink, PenLine, RotateCcw } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import { apiErrorMessage } from "../../../utils/apiError";
import { useServerTimezone } from "../../../hooks/useServerTimezone";
import { formatServerIsoDateTime } from "../../../utils/formatMessageTime";
import {
  workbuddyRuntimeApi,
  type OutputReview,
  type OutputReviewDecision,
  type OutputReviewDecisionRequest,
  type OutputReviewReviewer,
} from "../../../api/modules/workbuddyRuntime";
import {
  isWorkBuddyUnavailableError,
  type JsonObject,
} from "../../../api/modules/workbuddyWorkflows";
import {
  JsonPreview,
  LoadError,
  LoadingBlock,
  UnavailableNotice,
  statusLabel,
} from "../Workflows/consoleState";
import styles from "./index.module.less";

const { Text } = Typography;

/** Tag colour per review status, mirroring the inbox's other panes. */
const STATUS_TAG_COLORS: Record<string, string> = {
  open: "gold",
  accepted: "green",
  corrected: "blue",
  rerun: "purple",
};

/** Tag colour per reviewer status — what each asked person did about it. */
const REVIEWER_TAG_COLORS: Record<string, string> = {
  pending: "gold",
  accepted: "green",
  corrected: "blue",
  rerun: "purple",
  abstained: "default",
  invalidated: "default",
};

/** One draft per produced key: the JSON text of the value under review. */
function draftsOf(produced: JsonObject): Record<string, string> {
  const drafts: Record<string, string> = {};
  for (const [key, value] of Object.entries(produced)) {
    drafts[key] = JSON.stringify(value, null, 2) ?? "null";
  }
  return drafts;
}

/**
 * What the drafts say now, read against what the run produced: the keys a
 * reviewer actually changed, and the keys whose text is not JSON at all.
 *
 * The comparison is on the value, not on the text — re-indenting a value is not
 * a correction — and a key that was left alone stays out of the payload, because
 * the server reads a missing key as "this one stands as produced".
 */
function correctionOf(
  produced: JsonObject,
  drafts: Record<string, string>,
): { corrected: JsonObject; unparsed: string[] } {
  const corrected: JsonObject = {};
  const unparsed: string[] = [];
  for (const [key, value] of Object.entries(produced)) {
    let parsed: unknown;
    try {
      parsed = JSON.parse(drafts[key] ?? "");
    } catch {
      unparsed.push(key);
      continue;
    }
    if (
      (JSON.stringify(parsed) ?? "null") !== (JSON.stringify(value) ?? "null")
    ) {
      corrected[key] = parsed;
    }
  }
  return { corrected, unparsed };
}

/**
 * The re-run confirmation. Repeating a settled run is a decision about a *new*
 * execution, so the modal shows the inputs the next run starts with — this run's
 * own, the server's default, still editable — and says plainly that the reviewed
 * execution does not change.
 *
 * When the reviewed run's inputs cannot be read, the modal sends no ``inputs``
 * at all, so the server replays what it recorded instead of an empty object this
 * client invented.
 */
function RerunDecisionModal({
  executionId,
  open,
  submitting,
  onClose,
  onSubmit,
}: {
  executionId: string;
  open: boolean;
  submitting: boolean;
  onClose: () => void;
  onSubmit: (inputs: JsonObject | undefined) => void;
}) {
  const { t } = useTranslation();
  const [inputsText, setInputsText] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<unknown>(null);
  const [invalid, setInvalid] = useState(false);

  useEffect(() => {
    if (!open) return;
    let active = true;
    setLoading(true);
    setInvalid(false);
    setLoadError(null);
    setInputsText(null);
    workbuddyRuntimeApi
      .getExecution(executionId)
      .then((execution) => {
        if (!active) return;
        setInputsText(JSON.stringify(execution.inputs ?? {}, null, 2));
      })
      .catch((err: unknown) => {
        if (!active) return;
        setLoadError(err);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [executionId, open]);

  const confirm = useCallback(() => {
    // An unread original is the server's own input set; say so by omitting it.
    if (inputsText === null) {
      onSubmit(undefined);
      return;
    }
    let parsed: unknown;
    try {
      parsed = JSON.parse(inputsText);
    } catch {
      setInvalid(true);
      return;
    }
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
      setInvalid(true);
      return;
    }
    setInvalid(false);
    onSubmit(parsed as JsonObject);
  }, [inputsText, onSubmit]);

  return (
    <Modal
      open={open}
      onCancel={onClose}
      destroyOnHidden
      maskClosable={false}
      width={720}
      title={t("workbuddy.inbox.reviews.rerunTitle")}
      okText={t("workbuddy.inbox.reviews.rerunConfirm")}
      cancelText={t("common.cancel")}
      confirmLoading={submitting}
      onOk={() => confirm()}
    >
      <Space direction="vertical" size={12} style={{ width: "100%" }}>
        <Alert
          type="info"
          showIcon
          message={t("workbuddy.inbox.reviews.settledTitle")}
          description={t("workbuddy.inbox.reviews.rerunHint")}
        />
        {loading ? (
          <LoadingBlock label={t("common.loading")} />
        ) : inputsText === null ? (
          <Alert
            type="warning"
            showIcon
            message={t("workbuddy.inbox.reviews.rerunInputsUnread")}
            description={
              <Text>
                {apiErrorMessage(
                  loadError,
                  t("workbuddy.inbox.reviews.rerunInputsUnread"),
                  t,
                )}
              </Text>
            }
          />
        ) : (
          <div>
            <label htmlFor="workbuddy-rerun-inputs">
              <Text strong>{t("workbuddy.inbox.reviews.rerunInputs")}</Text>
            </label>
            <Text type="secondary" className={styles.blockHint}>
              {t("workbuddy.inbox.reviews.rerunInputsHint")}
            </Text>
            <Input.TextArea
              id="workbuddy-rerun-inputs"
              autoSize={{ minRows: 6, maxRows: 16 }}
              spellCheck={false}
              className={styles.jsonEditor}
              value={inputsText}
              onChange={(event) => setInputsText(event.target.value)}
            />
            {invalid && (
              <Alert
                type="error"
                showIcon
                message={t("workbuddy.inbox.reviews.rerunInvalid")}
              />
            )}
          </div>
        )}
      </Space>
    </Modal>
  );
}

export default function OutputReviewDrawer({
  executionId,
  onClose,
  onDecided,
}: {
  /** The settled run whose review is shown; ``null`` keeps the drawer closed. */
  executionId: string | null;
  onClose: () => void;
  /** A decision landed: the caller's own queue/status is now out of date. */
  onDecided?: (review: OutputReview) => void;
}) {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  const navigate = useNavigate();
  const [form] = Form.useForm<Record<string, string>>();
  const [review, setReview] = useState<OutputReview | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [nonce, setNonce] = useState(0);
  const [working, setWorking] = useState<OutputReviewDecision | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [correction, setCorrection] = useState<{
    corrected: JsonObject;
    unparsed: string[];
  }>({ corrected: {}, unparsed: [] });
  const [rerunOpen, setRerunOpen] = useState(false);

  useEffect(() => {
    if (!executionId) {
      setReview(null);
      setError(null);
      return;
    }
    let active = true;
    setLoading(true);
    workbuddyRuntimeApi
      .getOutputReview(executionId)
      .then((row) => {
        if (!active) return;
        setReview(row);
        setError(null);
      })
      .catch((err: unknown) => {
        if (!active) return;
        setReview(null);
        setError(err);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [executionId, nonce]);

  const produced = useMemo(() => review?.produced ?? {}, [review]);
  const producedKeys = useMemo(() => Object.keys(produced), [produced]);
  // Only an open review accepts a decision; a settled one is read-only.
  const decidable = review !== null && review.status === "open";
  const changedKeys = Object.keys(correction.corrected);

  useEffect(() => {
    // A review as the server has it starts clean: every draft is the produced
    // value, so nothing counts as a change until the reviewer types one.
    setSubmitError(null);
    setCorrection({ corrected: {}, unparsed: [] });
    if (!review) return;
    form.resetFields();
    form.setFieldsValue(draftsOf(review.produced));
  }, [form, review]);

  const retry = useCallback(() => setNonce((value) => value + 1), []);

  const decide = useCallback(
    async (
      decision: OutputReviewDecision,
      body: OutputReviewDecisionRequest,
      done: (next: OutputReview) => string,
    ) => {
      if (!executionId) return;
      setWorking(decision);
      setSubmitError(null);
      try {
        const next = await workbuddyRuntimeApi.decideOutputReview(
          executionId,
          body,
        );
        setReview(next);
        message.success(done(next));
        onDecided?.(next);
      } catch (err) {
        setSubmitError(
          apiErrorMessage(err, t("workbuddy.inbox.reviews.submitFailed"), t),
        );
      } finally {
        setWorking(null);
      }
    },
    [executionId, onDecided, t],
  );

  const openRerunExecution = useCallback(() => {
    if (!review?.rerun_execution_id) return;
    navigate(
      `/workbuddy/runs?execution=${encodeURIComponent(review.rerun_execution_id)}`,
    );
    onClose();
  }, [navigate, onClose, review]);

  const reviewerColumns: ColumnsType<OutputReviewReviewer> = [
    {
      title: t("workbuddy.inbox.reviews.reviewerColumns.reviewer"),
      dataIndex: "user_id",
      key: "user_id",
      width: 140,
      render: (value: number) => <Text code>#{value}</Text>,
    },
    {
      title: t("workbuddy.inbox.reviews.reviewerColumns.department"),
      dataIndex: "department_id",
      key: "department_id",
      width: 200,
      render: (value: string | null) =>
        value === null ? (
          <Text type="secondary">{t("workbuddy.inbox.reviews.none")}</Text>
        ) : (
          <Text code>{value.slice(0, 8)}</Text>
        ),
    },
    {
      title: t("workbuddy.inbox.reviews.reviewerColumns.status"),
      dataIndex: "status",
      key: "status",
      width: 150,
      render: (value: string) => (
        <Tag color={REVIEWER_TAG_COLORS[value]}>
          {statusLabel(t, "workbuddy.inbox.reviews.reviewerStatus", value)}
        </Tag>
      ),
    },
    {
      title: t("workbuddy.inbox.reviews.reviewerColumns.decidedAt"),
      dataIndex: "decided_at",
      key: "decided_at",
      width: 200,
      render: (value: string | null) =>
        value ? (
          formatServerIsoDateTime(value, timeZone)
        ) : (
          <Text type="secondary">{t("workbuddy.inbox.reviews.none")}</Text>
        ),
    },
  ];

  return (
    <Drawer
      open={executionId !== null}
      onClose={onClose}
      destroyOnHidden
      width={820}
      title={
        review
          ? t("workbuddy.inbox.reviews.detailTitle", {
              id: review.id.slice(0, 8),
            })
          : t("workbuddy.inbox.reviews.title")
      }
      extra={
        decidable ? (
          <Space size={8}>
            <Button
              type="primary"
              size="small"
              icon={<BadgeCheck size={14} />}
              loading={working === "accept"}
              disabled={working !== null}
              onClick={() =>
                void decide("accept", { decision: "accept" }, () =>
                  t("workbuddy.inbox.reviews.acceptedToast"),
                )
              }
            >
              {t("workbuddy.inbox.reviews.accept")}
            </Button>
            <Button
              size="small"
              icon={<PenLine size={14} />}
              loading={working === "correct"}
              disabled={
                working !== null ||
                changedKeys.length === 0 ||
                correction.unparsed.length > 0
              }
              onClick={() =>
                void decide(
                  "correct",
                  { decision: "correct", corrected: correction.corrected },
                  () => t("workbuddy.inbox.reviews.correctedToast"),
                )
              }
            >
              {t("workbuddy.inbox.reviews.correct")}
            </Button>
            <Button
              size="small"
              icon={<RotateCcw size={14} />}
              loading={working === "rerun"}
              disabled={working !== null}
              onClick={() => setRerunOpen(true)}
            >
              {t("workbuddy.inbox.reviews.rerun")}
            </Button>
          </Space>
        ) : null
      }
    >
      {executionId === null ? null : loading ? (
        <LoadingBlock label={t("common.loading")} />
      ) : error !== null && error !== undefined ? (
        isWorkBuddyUnavailableError(error) ? (
          <UnavailableNotice
            error={error}
            errorFallback={t("workbuddy.inbox.state.errorFallback")}
            title={t("workbuddy.shared.notMergedTitle")}
            hint={t("workbuddy.shared.notMergedHint")}
            onRetry={retry}
          />
        ) : (
          <LoadError
            error={error}
            title={t("workbuddy.inbox.reviews.detailLoadFailed")}
            fallback={t("workbuddy.inbox.state.errorFallback")}
            onRetry={retry}
          />
        )
      ) : review === null ? (
        <Alert
          type="info"
          showIcon
          message={t("workbuddy.inbox.reviews.missingTitle")}
          description={t("workbuddy.inbox.reviews.missingHint")}
        />
      ) : (
        <Space direction="vertical" size={14} style={{ width: "100%" }}>
          {/* The run is over; a review is a record about its result. No action
              in this drawer can release, block or re-open the execution. */}
          <Alert
            type="info"
            showIcon
            message={t("workbuddy.inbox.reviews.settledTitle")}
            description={t("workbuddy.inbox.reviews.settledHint")}
          />

          <Descriptions
            size="small"
            column={2}
            bordered
            title={t("workbuddy.inbox.reviews.overview")}
          >
            <Descriptions.Item
              label={t("workbuddy.inbox.reviews.column.status")}
            >
              <Tag color={STATUS_TAG_COLORS[review.status]}>
                {statusLabel(
                  t,
                  "workbuddy.inbox.reviews.status",
                  review.status,
                )}
              </Tag>
            </Descriptions.Item>
            <Descriptions.Item label={t("workbuddy.inbox.reviews.execution")}>
              <Text code>{review.execution_id.slice(0, 8)}</Text>
            </Descriptions.Item>
            <Descriptions.Item label={t("workbuddy.inbox.reviews.requestedBy")}>
              {review.requested_by_user_id === null
                ? t("workbuddy.inbox.reviews.none")
                : `#${review.requested_by_user_id}`}
            </Descriptions.Item>
            <Descriptions.Item label={t("workbuddy.inbox.reviews.requestedAt")}>
              {formatServerIsoDateTime(review.created_at, timeZone)}
            </Descriptions.Item>
            <Descriptions.Item label={t("workbuddy.inbox.reviews.decidedBy")}>
              {review.decided_by_user_id === null
                ? t("workbuddy.inbox.reviews.none")
                : `#${review.decided_by_user_id}`}
            </Descriptions.Item>
            <Descriptions.Item label={t("workbuddy.inbox.reviews.decidedAt")}>
              {review.decided_at
                ? formatServerIsoDateTime(review.decided_at, timeZone)
                : t("workbuddy.inbox.reviews.none")}
            </Descriptions.Item>
            <Descriptions.Item
              label={t("workbuddy.inbox.reviews.digest")}
              span={2}
            >
              {/* The digest of exactly the bytes under review: it is what ties
                  a decision to the snapshot it was made about. */}
              <Tooltip title={review.produced_sha256}>
                <Text code>{review.produced_sha256.slice(0, 16)}</Text>
              </Tooltip>
            </Descriptions.Item>
            {review.rerun_execution_id !== null && (
              <Descriptions.Item
                label={t("workbuddy.inbox.reviews.rerunExecution")}
                span={2}
              >
                <Space size={6}>
                  <Text code>{review.rerun_execution_id.slice(0, 8)}</Text>
                  <Button
                    type="link"
                    size="small"
                    icon={<ExternalLink size={13} />}
                    onClick={openRerunExecution}
                  >
                    {t("workbuddy.inbox.reviews.rerunOpen")}
                  </Button>
                </Space>
              </Descriptions.Item>
            )}
          </Descriptions>

          <div>
            <Text strong>{t("workbuddy.inbox.reviews.reviewers")}</Text>
            <Table<OutputReviewReviewer>
              rowKey="user_id"
              size="small"
              columns={reviewerColumns}
              dataSource={review.reviewers}
              pagination={false}
              locale={{ emptyText: t("workbuddy.inbox.reviews.noReviewers") }}
            />
          </div>

          <div>
            <Text strong>{t("workbuddy.inbox.reviews.produced")}</Text>
            <JsonPreview
              value={review.produced}
              emptyLabel={t("workbuddy.inbox.reviews.producedEmpty")}
            />
          </div>

          {decidable ? (
            producedKeys.length === 0 ? (
              <Alert
                type="warning"
                showIcon
                message={t("workbuddy.inbox.reviews.noProducedKeys")}
                description={t("workbuddy.inbox.reviews.noProducedKeysHint")}
              />
            ) : (
              <div>
                <Text strong>{t("workbuddy.inbox.reviews.correction")}</Text>
                <Text type="secondary" className={styles.blockHint}>
                  {t("workbuddy.inbox.reviews.correctionHint")}
                </Text>
                {/* One control per produced key and nothing else: the server
                    refuses a key the run did not produce, so there is no way to
                    name a new one here. */}
                <Form
                  form={form}
                  layout="vertical"
                  onValuesChange={(_changed, all) =>
                    setCorrection(correctionOf(produced, all))
                  }
                >
                  {producedKeys.map((key) => (
                    <Form.Item
                      key={key}
                      name={key}
                      label={<Text code>{key}</Text>}
                    >
                      <Input.TextArea
                        autoSize={{ minRows: 2, maxRows: 10 }}
                        spellCheck={false}
                        className={styles.jsonEditor}
                      />
                    </Form.Item>
                  ))}
                </Form>
                {correction.unparsed.length > 0 && (
                  <Alert
                    type="error"
                    showIcon
                    message={t("workbuddy.inbox.reviews.invalidJson", {
                      keys: correction.unparsed.join(", "),
                    })}
                  />
                )}
              </div>
            )
          ) : (
            <div>
              <Text strong>{t("workbuddy.inbox.reviews.corrected")}</Text>
              <JsonPreview
                value={review.corrected}
                emptyLabel={t("workbuddy.inbox.reviews.noCorrected")}
              />
            </div>
          )}

          {submitError !== null && (
            <Alert
              type="error"
              showIcon
              message={t("workbuddy.inbox.reviews.submitFailed")}
              description={<Text>{submitError}</Text>}
            />
          )}
        </Space>
      )}

      {executionId !== null && (
        <RerunDecisionModal
          executionId={executionId}
          open={rerunOpen}
          submitting={working === "rerun"}
          onClose={() => setRerunOpen(false)}
          onSubmit={(inputs) => {
            setRerunOpen(false);
            void decide(
              "rerun",
              inputs === undefined
                ? { decision: "rerun" }
                : { decision: "rerun", inputs },
              (next) =>
                t("workbuddy.inbox.reviews.rerunToast", {
                  id: next.rerun_execution_id?.slice(0, 8) ?? "",
                }),
            );
          }}
        />
      )}
    </Drawer>
  );
}
