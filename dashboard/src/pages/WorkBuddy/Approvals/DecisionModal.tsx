/**
 * WorkBuddy console → approval decision.
 *
 * One decision = one pending approval + one fresh two-minute challenge. The
 * challenge token is returned once by the server (only its hash is stored),
 * lives in this component's state for exactly one decision, and is cleared on
 * close, on submit and when it expires — it is never persisted or logged.
 * Nothing in the UI moves before ``POST /executions/{id}/resume`` answered.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Descriptions,
  Input,
  Modal,
  Radio,
  Select,
  Space,
  Tag,
  Typography,
} from "antd";
import { message } from "@/utils/antdMessage";
import { KeyRound } from "lucide-react";
import { useTranslation } from "react-i18next";
import { apiErrorMessage } from "../../../utils/apiError";
import { useServerTimezone } from "../../../hooks/useServerTimezone";
import { formatServerIsoDateTime } from "../../../utils/formatMessageTime";
import {
  workbuddyRuntimeApi,
  type ApprovalChallenge,
  type ApprovalDecision,
  type ApprovalRequest,
  type ApprovalRequestDetail,
} from "../../../api/modules/workbuddyRuntime";
import { isWorkBuddyUnavailableError } from "../../../api/modules/workbuddyWorkflows";
import {
  JsonPreview,
  LoadingBlock,
  LoadError,
  UnavailableNotice,
  useWorkBuddyResource,
} from "../Workflows/consoleState";
import styles from "./index.module.less";

const { Text } = Typography;

export default function DecisionModal({
  open,
  executionId,
  approvalRequestId,
  onClose,
  onDecided,
}: {
  open: boolean;
  executionId: string | null;
  approvalRequestId: string | null;
  onClose: () => void;
  onDecided: () => void;
}) {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  const [chosenId, setChosenId] = useState<string | null>(null);
  const [detail, setDetail] = useState<ApprovalRequestDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<unknown>(null);
  const [decision, setDecision] = useState<ApprovalDecision | null>(null);
  const [challenge, setChallenge] = useState<ApprovalChallenge | null>(null);
  const [secondsLeft, setSecondsLeft] = useState(0);
  const [requesting, setRequesting] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [detailNonce, setDetailNonce] = useState(0);

  const pending = useWorkBuddyResource<ApprovalRequest[]>(
    [],
    () =>
      open
        ? workbuddyRuntimeApi.listApprovalRequests({
            scope: "self",
            status: "pending",
            limit: 200,
          })
        : Promise.resolve([]),
    [open],
  );

  const pendingForExecution = useMemo(
    () =>
      executionId
        ? pending.data.filter((row) => row.execution_id === executionId)
        : pending.data,
    [executionId, pending.data],
  );

  const resolvedId =
    approvalRequestId ?? chosenId ?? pendingForExecution[0]?.id ?? null;
  const targetExecutionId =
    executionId ??
    pendingForExecution.find((row) => row.id === resolvedId)?.execution_id ??
    null;

  useEffect(() => {
    if (!open) {
      setChosenId(null);
      setDecision(null);
      setChallenge(null);
      setSecondsLeft(0);
      setDetail(null);
      setDetailError(null);
    }
  }, [open]);

  useEffect(() => {
    if (!open || !resolvedId) {
      setDetail(null);
      setDetailError(null);
      return;
    }
    let active = true;
    setDetailLoading(true);
    workbuddyRuntimeApi
      .getApprovalRequest(resolvedId)
      .then((row) => {
        if (!active) return;
        setDetail(row);
        setDetailError(null);
      })
      .catch((err: unknown) => {
        if (!active) return;
        setDetail(null);
        setDetailError(err);
      })
      .finally(() => {
        if (active) setDetailLoading(false);
      });
    return () => {
      active = false;
    };
  }, [open, resolvedId, detailNonce]);

  // Countdown for the issued challenge; at zero the token is unusable and the
  // operator has to request a new one.
  useEffect(() => {
    if (!challenge) {
      setSecondsLeft(0);
      return;
    }
    const expiresAt = Date.now() + challenge.expires_in * 1000;
    const tick = () =>
      setSecondsLeft(Math.max(0, Math.ceil((expiresAt - Date.now()) / 1000)));
    tick();
    const timer = window.setInterval(tick, 1000);
    return () => window.clearInterval(timer);
  }, [challenge]);

  const close = useCallback(() => {
    setChallenge(null);
    setSecondsLeft(0);
    setDecision(null);
    onClose();
  }, [onClose]);

  const requestChallenge = useCallback(async () => {
    if (!resolvedId) return;
    setRequesting(true);
    try {
      const issued = await workbuddyRuntimeApi.challengeApprovalRequest(
        resolvedId,
      );
      setChallenge(issued);
      message.success(
        t("workbuddy.approvals.decision.issued", {
          seconds: issued.expires_in,
        }),
      );
    } catch (err) {
      message.error(
        apiErrorMessage(
          err,
          t("workbuddy.approvals.decision.challengeFailed"),
          t,
        ),
      );
    } finally {
      setRequesting(false);
    }
  }, [resolvedId, t]);

  const submit = useCallback(async () => {
    if (!targetExecutionId || !resolvedId) return;
    if (!decision) {
      message.warning(t("workbuddy.approvals.decision.decisionLabel"));
      return;
    }
    if (!challenge || secondsLeft <= 0) {
      message.warning(t("workbuddy.approvals.decision.challengeRequired"));
      return;
    }
    setSubmitting(true);
    try {
      await workbuddyRuntimeApi.resumeExecution(targetExecutionId, {
        approval_request_id: resolvedId,
        decision,
        token: challenge.token,
      });
      message.success(t("workbuddy.approvals.decision.submitted"));
      setChallenge(null);
      setDecision(null);
      onDecided();
      onClose();
    } catch (err) {
      message.error(
        apiErrorMessage(err, t("workbuddy.approvals.decision.submitFailed"), t),
      );
    } finally {
      setSubmitting(false);
    }
  }, [
    targetExecutionId,
    resolvedId,
    decision,
    challenge,
    secondsLeft,
    onDecided,
    onClose,
    t,
  ]);

  const pendingFailed = pending.error !== null && pending.error !== undefined;

  return (
    <Modal
      open={open}
      onCancel={close}
      destroyOnHidden
      maskClosable={false}
      width={720}
      title={t("workbuddy.approvals.decision.title")}
      footer={
        <Space>
          <Button onClick={close}>{t("common.cancel")}</Button>
          <Button
            type="primary"
            loading={submitting}
            disabled={!resolvedId || secondsLeft <= 0}
            onClick={() => void submit()}
          >
            {t("workbuddy.approvals.decision.submit")}
          </Button>
        </Space>
      }
    >
      {!resolvedId && pendingFailed ? (
        isWorkBuddyUnavailableError(pending.error) ? (
          <UnavailableNotice
            error={pending.error}
            errorFallback={t("workbuddy.approvals.state.errorFallback")}
            title={t("workbuddy.approvals.state.unavailableTitle")}
            hint={t("workbuddy.approvals.state.unavailableHint")}
            onRetry={() => void pending.reload()}
          />
        ) : (
          <LoadError
            error={pending.error}
            title={t("workbuddy.approvals.inbox.loadFailed")}
            fallback={t("workbuddy.approvals.state.errorFallback")}
            onRetry={() => void pending.reload()}
          />
        )
      ) : !resolvedId && pending.loading ? (
        <LoadingBlock label={t("common.loading")} />
      ) : !resolvedId ? (
        <Alert
          type="info"
          showIcon
          message={t("workbuddy.approvals.decision.noPendingTitle")}
          description={t("workbuddy.approvals.decision.noPendingHint")}
        />
      ) : (
        <Space direction="vertical" size={14} style={{ width: "100%" }}>
          {pendingForExecution.length > 1 && (
            <Select
              style={{ width: "100%" }}
              value={resolvedId ?? undefined}
              onChange={(value: string) => {
                setChosenId(value);
                setChallenge(null);
              }}
              options={pendingForExecution.map((row) => ({
                value: row.id,
                label: `${row.node_id} · ${row.id.slice(0, 8)}`,
              }))}
              placeholder={t("workbuddy.approvals.decision.chooseApproval")}
            />
          )}

          {detailLoading ? (
            <LoadingBlock label={t("common.loading")} />
          ) : detailError !== null && detailError !== undefined ? (
            isWorkBuddyUnavailableError(detailError) ? (
              <UnavailableNotice
                error={detailError}
                errorFallback={t("workbuddy.approvals.state.errorFallback")}
                title={t("workbuddy.approvals.state.unavailableTitle")}
                hint={t("workbuddy.approvals.state.unavailableHint")}
                onRetry={() => setDetailNonce((n) => n + 1)}
              />
            ) : (
              <LoadError
                error={detailError}
                title={t("workbuddy.approvals.inbox.detailLoadFailed")}
                fallback={t("workbuddy.approvals.state.errorFallback")}
                onRetry={() => setDetailNonce((n) => n + 1)}
              />
            )
          ) : detail ? (
            <>
              <Descriptions size="small" column={2} bordered>
                <Descriptions.Item
                  label={t("workbuddy.approvals.decision.node")}
                >
                  <Text code>{detail.node_id}</Text>
                </Descriptions.Item>
                <Descriptions.Item
                  label={t("workbuddy.approvals.decision.execution")}
                >
                  <Text code>{detail.execution_id.slice(0, 8)}</Text>
                </Descriptions.Item>
                <Descriptions.Item
                  label={t("workbuddy.approvals.inbox.decided")}
                >
                  {t("workbuddy.approvals.decision.summary", {
                    decided: detail.decided_approvals,
                    required: detail.required_approvals,
                  })}
                </Descriptions.Item>
                <Descriptions.Item
                  label={t("workbuddy.approvals.inbox.challengeExpires")}
                >
                  {detail.token_expires_at
                    ? formatServerIsoDateTime(detail.token_expires_at, timeZone)
                    : t("workbuddy.approvals.inbox.none")}
                </Descriptions.Item>
              </Descriptions>

              <div>
                <Text strong>{t("workbuddy.approvals.decision.params")}</Text>
                <JsonPreview
                  value={detail.params}
                  emptyLabel={t("workbuddy.approvals.inbox.noParams")}
                />
              </div>

              <Radio.Group
                value={decision ?? undefined}
                onChange={(event) =>
                  setDecision(
                    event.target.value === "reject" ? "reject" : "approve",
                  )
                }
              >
                <Space size={16}>
                  <Radio value="approve">
                    {t("workbuddy.approvals.decision.option.approve")}
                  </Radio>
                  <Radio value="reject">
                    {t("workbuddy.approvals.decision.option.reject")}
                  </Radio>
                </Space>
              </Radio.Group>

              <div className={styles.challengeBlock}>
                <Space size={8} wrap>
                  <KeyRound size={14} />
                  <Text strong>
                    {t("workbuddy.approvals.decision.challenge")}
                  </Text>
                  <Button
                    size="small"
                    loading={requesting}
                    onClick={() => void requestChallenge()}
                  >
                    {requesting
                      ? t("workbuddy.approvals.decision.requesting")
                      : t("workbuddy.approvals.decision.request")}
                  </Button>
                  {challenge && (
                    <Tag color={secondsLeft > 0 ? "green" : "red"}>
                      {secondsLeft > 0
                        ? t("workbuddy.approvals.decision.expiresIn", {
                            seconds: secondsLeft,
                          })
                        : t("workbuddy.approvals.decision.expired")}
                    </Tag>
                  )}
                </Space>
                <Text type="secondary" className={styles.blockHint}>
                  {t("workbuddy.approvals.decision.challengeHint")}
                </Text>
                {challenge && (
                  <Input.Password
                    value={challenge.token}
                    readOnly
                    visibilityToggle
                    addonBefore={t("workbuddy.approvals.decision.token")}
                    className={styles.tokenInput}
                  />
                )}
              </div>
            </>
          ) : null}
        </Space>
      )}
    </Modal>
  );
}
