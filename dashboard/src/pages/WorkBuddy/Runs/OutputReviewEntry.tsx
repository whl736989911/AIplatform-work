/**
 * WorkBuddy runs → the review of one settled execution.
 *
 * The runs list opens this for a run that has settled, and what it shows depends
 * on what the server already has: a review (its status, who was asked, the
 * decision it settled with) or nothing at all — in which case the entry offers
 * to ask for one. Reading the review and asking for it are the same route, so
 * "there is no review" is the server's 404 and never a guess made here.
 *
 * The decision itself is not in this component: the review drawer renders it, so
 * a review is decided in exactly one place whether it was opened from the runs
 * list or from the inbox.
 *
 * Nothing here suggests a review holds the run: the execution has settled, and
 * the copy says so on both branches. A run that is still moving is refused by the
 * server with its own message (409), which is what gets shown.
 */

import { useCallback, useEffect, useState } from "react";
import { Alert, Button, Descriptions, Drawer, Space, Tag, Typography } from "antd";
import { BadgeCheck, RefreshCw, UserPlus } from "lucide-react";
import { useTranslation } from "react-i18next";
import { isNotFoundApiError } from "../../../utils/apiError";
import { useServerTimezone } from "../../../hooks/useServerTimezone";
import { formatServerIsoDateTime } from "../../../utils/formatMessageTime";
import {
  workbuddyRuntimeApi,
  type OutputReview,
} from "../../../api/modules/workbuddyRuntime";
import { isWorkBuddyUnavailableError } from "../../../api/modules/workbuddyWorkflows";
import {
  LoadError,
  LoadingBlock,
  UnavailableNotice,
  statusLabel,
} from "../Workflows/consoleState";
import OutputReviewDrawer from "../Inbox/OutputReviewDrawer";
import OutputReviewRequestModal from "./OutputReviewRequestModal";

const { Text } = Typography;

/** Tag colour per review status, mirroring the inbox's review pane. */
const STATUS_TAG_COLORS: Record<string, string> = {
  open: "gold",
  accepted: "green",
  corrected: "blue",
  rerun: "purple",
};

export default function OutputReviewEntry({
  executionId,
  onClose,
  onChanged,
}: {
  /** The settled run whose review is shown; ``null`` keeps the drawer closed. */
  executionId: string | null;
  onClose: () => void;
  /** A review was asked for or decided: the runs list is now out of date. */
  onChanged?: () => void;
}) {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  const [review, setReview] = useState<OutputReview | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [nonce, setNonce] = useState(0);
  const [requestOpen, setRequestOpen] = useState(false);
  const [reviewOpen, setReviewOpen] = useState(false);

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

  const reload = useCallback(() => setNonce((value) => value + 1), []);

  // Not found is the answer this entry exists to distinguish: the route answers
  // 404 both when this run has no review and when the caller may not read it, so
  // the offer to ask for one is what an operator gets either way.
  const missing =
    error !== null && error !== undefined && isNotFoundApiError(error);
  const failed =
    error !== null && error !== undefined && !missing;

  return (
    <Drawer
      open={executionId !== null}
      onClose={onClose}
      destroyOnHidden
      width={720}
      title={t("workbuddy.runs.reviewTitle")}
    >
      {executionId === null ? null : loading ? (
        <LoadingBlock label={t("common.loading")} />
      ) : failed ? (
        isWorkBuddyUnavailableError(error) ? (
          <UnavailableNotice
            error={error}
            errorFallback={t("workbuddy.workflows.state.errorFallback")}
            title={t("workbuddy.shared.notMergedTitle")}
            hint={t("workbuddy.shared.notMergedHint")}
            onRetry={reload}
          />
        ) : (
          <LoadError
            error={error}
            title={t("workbuddy.runs.reviewLoadFailed")}
            fallback={t("workbuddy.workflows.state.errorFallback")}
            onRetry={reload}
          />
        )
      ) : review !== null ? (
        <Space direction="vertical" size={14} style={{ width: "100%" }}>
          <Descriptions
            size="small"
            column={2}
            bordered
            title={t("workbuddy.runs.reviewOverview")}
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
            <Descriptions.Item
              label={t("workbuddy.inbox.reviews.requestedBy")}
            >
              {review.requested_by_user_id === null
                ? t("workbuddy.inbox.reviews.none")
                : `#${review.requested_by_user_id}`}
            </Descriptions.Item>
            <Descriptions.Item
              label={t("workbuddy.inbox.reviews.requestedAt")}
            >
              {formatServerIsoDateTime(review.created_at, timeZone)}
            </Descriptions.Item>
            <Descriptions.Item label={t("workbuddy.inbox.reviews.decidedAt")}>
              {review.decided_at
                ? formatServerIsoDateTime(review.decided_at, timeZone)
                : t("workbuddy.inbox.reviews.none")}
            </Descriptions.Item>
            {review.rerun_execution_id !== null && (
              <Descriptions.Item
                label={t("workbuddy.inbox.reviews.rerunExecution")}
                span={2}
              >
                <Text code>{review.rerun_execution_id.slice(0, 8)}</Text>
              </Descriptions.Item>
            )}
          </Descriptions>

          <Space size={8} wrap>
            <Button
              type="primary"
              size="small"
              icon={<BadgeCheck size={14} />}
              onClick={() => setReviewOpen(true)}
            >
              {t("workbuddy.runs.reviewOpen")}
            </Button>
            <Button size="small" icon={<RefreshCw size={14} />} onClick={reload}>
              {t("common.refresh")}
            </Button>
          </Space>
        </Space>
      ) : (
        // The route answered 404: this run has no review yet (or this caller
        // may not read the one it has). Asking for one is the only action here.
        <Space direction="vertical" size={14} style={{ width: "100%" }}>
          <Alert
            type="info"
            showIcon
            message={t("workbuddy.runs.reviewNotRequestedTitle")}
            description={t("workbuddy.runs.reviewNotRequestedHint")}
          />
          <Button
            type="primary"
            size="small"
            icon={<UserPlus size={14} />}
            onClick={() => setRequestOpen(true)}
          >
            {t("workbuddy.runs.reviewRequest")}
          </Button>
        </Space>
      )}

      <OutputReviewRequestModal
        executionId={executionId}
        open={requestOpen}
        onClose={() => setRequestOpen(false)}
        onRequested={(created) => {
          // Show the review that was just asked for, and let the list behind
          // this drawer pick up its new status.
          setReview(created);
          onChanged?.();
        }}
      />

      <OutputReviewDrawer
        executionId={reviewOpen ? executionId : null}
        onClose={() => setReviewOpen(false)}
        onDecided={() => {
          reload();
          onChanged?.();
        }}
      />
    </Drawer>
  );
}
