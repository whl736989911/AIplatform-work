/**
 * Tenant deletion: open a request inside its 30-day cooling-off window, read
 * its stage, cancel while the window is open.
 *
 * Hard rule: deletion is fail-closed. ``COMPLIANCE_GATE_CLOSED`` renders as an
 * explicit "the entry point is closed" state — never as a queued request, an
 * optimistic row or a retry that pretends the request exists. A request view is
 * only ever the payload the server returned.
 */

import { useState } from "react";
import {
  Alert,
  Button,
  Descriptions,
  Input,
  Modal,
  Popconfirm,
  Space,
  Steps,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import { message } from "@/utils/antdMessage";
import { RefreshCw, ShieldAlert, Trash2 } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useServerTimezone } from "../../../hooks/useServerTimezone";
import { apiErrorMessage, parseApiError } from "../../../utils/apiError";
import { formatServerDateTime } from "../../../utils/formatMessageTime";
import {
  COMPLIANCE_GATE_CLOSED,
  DELETION_CANCEL_WINDOW_CLOSED,
  DELETION_REQUEST_CONFLICT,
  PRECONDITION_REQUIRED,
  isLifecycleUnavailableError,
  workbuddyLifecycleApi,
  type DeletionRequestView,
} from "../../../api/modules/workbuddyLifecycle";
import { TabPanelHeader } from "../../Settings/AdvancedSettings/TabPanelHeader";
import { lifecycleStatusLabel } from "./statusLabels";
import styles from "./index.module.less";

const { Text, Title } = Typography;

/** Step index of a live request; a cancelled request stops at cooling-off. */
function stageStepIndex(stage: string): number {
  if (stage === "archived") return 2;
  if (stage === "purged") return 3;
  return 1;
}

export default function DeletionPanel() {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();

  const [tenantId, setTenantId] = useState("");
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [opening, setOpening] = useState(false);
  const [gateClosed, setGateClosed] = useState<string | null>(null);
  const [authRequired, setAuthRequired] = useState(false);
  const [unavailable, setUnavailable] = useState(false);

  const [requestId, setRequestId] = useState("");
  const [loading, setLoading] = useState(false);
  const [view, setView] = useState<DeletionRequestView | null>(null);
  const [cancelling, setCancelling] = useState(false);

  const openDeletionRequest = async () => {
    const id = tenantId.trim();
    if (!id) {
      message.error(t("workbuddy.lifecycle.deletion.tenantIdRequired"));
      return;
    }
    setOpening(true);
    setGateClosed(null);
    setAuthRequired(false);
    setUnavailable(false);
    try {
      const created = await workbuddyLifecycleApi.createDeletionRequest(id);
      // Only the server response may put the panel into a request state.
      setView(created);
      setRequestId(created.deletion_request_id);
      setConfirmOpen(false);
      message.success(t("workbuddy.lifecycle.deletion.openSuccess"));
    } catch (err) {
      setConfirmOpen(false);
      setView(null);
      const code = parseApiError(err)?.code;
      if (code === COMPLIANCE_GATE_CLOSED) {
        setGateClosed(
          apiErrorMessage(
            err,
            t("workbuddy.lifecycle.deletion.gateClosedFallback"),
            t,
          ),
        );
      } else if (code === PRECONDITION_REQUIRED) {
        setAuthRequired(true);
      } else if (isLifecycleUnavailableError(err)) {
        setUnavailable(true);
      } else {
        message.error(
          apiErrorMessage(err, t("workbuddy.lifecycle.deletion.openFailed"), t),
        );
      }
    } finally {
      setOpening(false);
    }
  };

  const loadDeletionRequest = async () => {
    const id = tenantId.trim();
    const request = requestId.trim();
    if (!id || !request) {
      message.error(t("workbuddy.lifecycle.deletion.idsRequired"));
      return;
    }
    setLoading(true);
    setUnavailable(false);
    try {
      setView(await workbuddyLifecycleApi.getDeletionRequest(id, request));
    } catch (err) {
      if (isLifecycleUnavailableError(err)) {
        setUnavailable(true);
      } else {
        message.error(
          apiErrorMessage(err, t("workbuddy.lifecycle.deletion.loadFailed"), t),
        );
      }
    } finally {
      setLoading(false);
    }
  };

  const cancelDeletionRequest = async () => {
    if (!view) return;
    setCancelling(true);
    try {
      const updated = await workbuddyLifecycleApi.cancelDeletionRequest(
        view.tenant_id,
        view.deletion_request_id,
        { expected_version: view.version },
      );
      setView(updated);
      message.success(t("workbuddy.lifecycle.deletion.cancelSuccess"));
    } catch (err) {
      const code = parseApiError(err)?.code;
      if (code === DELETION_CANCEL_WINDOW_CLOSED) {
        message.error(t("workbuddy.lifecycle.deletion.cancelWindowClosed"));
      } else if (code === DELETION_REQUEST_CONFLICT) {
        message.error(t("workbuddy.lifecycle.deletion.cancelConflict"));
      } else {
        message.error(
          apiErrorMessage(
            err,
            t("workbuddy.lifecycle.deletion.cancelFailed"),
            t,
          ),
        );
      }
    } finally {
      setCancelling(false);
    }
  };

  return (
    <div className={styles.panel}>
      <TabPanelHeader
        icon={<Trash2 size={18} />}
        title={t("workbuddy.lifecycle.deletion.title")}
        description={t("workbuddy.lifecycle.deletion.desc")}
      />

      <Space size={8} wrap className={styles.actionRow}>
        <Input
          value={tenantId}
          onChange={(event) => setTenantId(event.target.value)}
          placeholder={t("workbuddy.lifecycle.tenantIdPlaceholder")}
          className={styles.idInput}
          allowClear
        />
      </Space>

      {gateClosed && (
        <Alert
          type="error"
          showIcon
          className={styles.notice}
          message={t("workbuddy.lifecycle.deletion.gateClosedTitle")}
          description={
            <>
              <div>{gateClosed}</div>
              <Text type="secondary">
                {t("workbuddy.lifecycle.deletion.gateClosedHint")}
              </Text>
            </>
          }
        />
      )}

      {authRequired && (
        <Alert
          type="warning"
          showIcon
          className={styles.notice}
          message={t("workbuddy.lifecycle.deletion.authRequiredTitle")}
          description={t("workbuddy.lifecycle.deletion.authRequiredHint")}
        />
      )}

      {unavailable && (
        <Alert
          type="info"
          showIcon
          className={styles.notice}
          message={t("workbuddy.lifecycle.unavailableTitle")}
          description={t("workbuddy.lifecycle.deletion.unavailableHint")}
        />
      )}

      <div className={styles.sectionBlock}>
        <Title level={5} className={styles.sectionTitle}>
          {t("workbuddy.lifecycle.deletion.openTitle")}
        </Title>
        <Text type="secondary" className={styles.sectionDesc}>
          {t("workbuddy.lifecycle.deletion.openHint")}
        </Text>
        <Button
          danger
          icon={<Trash2 size={14} />}
          loading={opening}
          onClick={() => setConfirmOpen(true)}
        >
          {t("workbuddy.lifecycle.deletion.open")}
        </Button>
      </div>

      <div className={styles.sectionBlock}>
        <Title level={5} className={styles.sectionTitle}>
          {t("workbuddy.lifecycle.deletion.lookupTitle")}
        </Title>
        <Space size={8} wrap className={styles.actionRow}>
          <Input
            value={requestId}
            onChange={(event) => setRequestId(event.target.value)}
            placeholder={t("workbuddy.lifecycle.deletion.requestIdPlaceholder")}
            className={styles.idInput}
            allowClear
          />
          <Button
            icon={<RefreshCw size={14} />}
            loading={loading}
            onClick={() => void loadDeletionRequest()}
          >
            {t("workbuddy.lifecycle.deletion.load")}
          </Button>
        </Space>
      </div>

      {view && (
        <div className={styles.sectionBlock}>
          <Space size={8} wrap>
            <Tag
              color={
                view.stage === "cooling_off"
                  ? "gold"
                  : view.stage === "purged"
                  ? "red"
                  : "default"
              }
            >
              {lifecycleStatusLabel(t, "deletion", view.stage)}
            </Tag>
            <Text code>{view.deletion_request_id}</Text>
            <Tooltip title={view.policy_sha256}>
              <Text code>{view.policy_sha256.slice(0, 16)}…</Text>
            </Tooltip>
          </Space>

          {view.stage === "cancelled" && (
            <Alert
              type="success"
              showIcon
              message={t("workbuddy.lifecycle.deletion.cancelledNotice")}
            />
          )}

          {view.legal_hold_active && (
            <Alert
              type="warning"
              showIcon
              icon={<ShieldAlert size={16} />}
              message={t("workbuddy.lifecycle.deletion.legalHoldNotice")}
            />
          )}

          <Steps
            size="small"
            current={stageStepIndex(view.stage)}
            status={view.stage === "cancelled" ? "error" : "process"}
            items={[
              {
                title: t("workbuddy.lifecycle.deletion.stepRequested"),
                description: formatServerDateTime(view.requested_at, timeZone),
              },
              {
                title:
                  view.stage === "cancelled"
                    ? t("workbuddy.lifecycle.deletion.stepCancelled")
                    : t("workbuddy.lifecycle.deletion.stepCoolingOff"),
                description: formatServerDateTime(
                  view.cooling_off_ends_at,
                  timeZone,
                ),
              },
              {
                title: t("workbuddy.lifecycle.deletion.stepArchive"),
                description: view.purge_due_at
                  ? formatServerDateTime(view.purge_due_at, timeZone)
                  : t("workbuddy.lifecycle.deletion.stepPending"),
              },
              {
                title: t("workbuddy.lifecycle.deletion.stepPurge"),
                description: view.purged_at
                  ? formatServerDateTime(view.purged_at, timeZone)
                  : t("workbuddy.lifecycle.deletion.stepPending"),
              },
            ]}
          />

          <Descriptions
            size="small"
            column={1}
            className={styles.summary}
            items={[
              {
                key: "version",
                label: t("workbuddy.lifecycle.deletion.version"),
                children: view.version,
              },
              {
                key: "policyExpires",
                label: t("workbuddy.lifecycle.deletion.policyExpiresAt"),
                children: formatServerDateTime(
                  view.policy_expires_at,
                  timeZone,
                ),
              },
              {
                key: "archive",
                label: t("workbuddy.lifecycle.deletion.archiveDigest"),
                children: view.archive_sha256 ? (
                  <Tooltip title={view.archive_sha256}>
                    <Text code>{view.archive_sha256.slice(0, 16)}…</Text>
                  </Tooltip>
                ) : (
                  "—"
                ),
              },
              {
                key: "tombstone",
                label: t("workbuddy.lifecycle.deletion.tombstoneDigest"),
                children: view.tombstone_sha256 ? (
                  <Tooltip title={view.tombstone_sha256}>
                    <Text code>{view.tombstone_sha256.slice(0, 16)}…</Text>
                  </Tooltip>
                ) : (
                  "—"
                ),
              },
            ]}
          />

          <Popconfirm
            title={t("workbuddy.lifecycle.deletion.cancelConfirm")}
            description={t("workbuddy.lifecycle.deletion.cancelConfirmHint")}
            okText={t("common.confirm")}
            cancelText={t("common.cancel")}
            onConfirm={() => void cancelDeletionRequest()}
          >
            <Button
              danger
              icon={<Trash2 size={14} />}
              loading={cancelling}
              disabled={!view.cancellable}
            >
              {t("workbuddy.lifecycle.deletion.cancel")}
            </Button>
          </Popconfirm>
          {!view.cancellable && (
            <Text type="secondary" className={styles.rowHint}>
              {t("workbuddy.lifecycle.deletion.cancelUnavailable")}
            </Text>
          )}
        </div>
      )}

      <Modal
        title={t("workbuddy.lifecycle.deletion.confirmTitle")}
        open={confirmOpen}
        onCancel={() => setConfirmOpen(false)}
        onOk={() => void openDeletionRequest()}
        confirmLoading={opening}
        okText={t("workbuddy.lifecycle.deletion.open")}
        okButtonProps={{ danger: true }}
        cancelText={t("common.cancel")}
        destroyOnHidden
      >
        <Alert
          type="error"
          showIcon
          className={styles.notice}
          message={t("workbuddy.lifecycle.deletion.confirmWarning")}
          description={t("workbuddy.lifecycle.deletion.confirmWarningHint")}
        />
        <Text type="secondary">
          {t("workbuddy.lifecycle.deletion.confirmBody")}
        </Text>
      </Modal>
    </div>
  );
}
