/**
 * WorkBuddy console → Approvals → notification strip.
 *
 * The caller's own notifications, with an explicit mark-as-read action. Every
 * mutation awaits the server row it returns before the list changes — there is
 * no optimistic update and no invented "read" state.
 */

import { useCallback, useState } from "react";
import { Alert, Button, Checkbox, Space, Tag, Typography } from "antd";
import { message } from "@/utils/antdMessage";
import { BellRing, CheckCheck, RefreshCw } from "lucide-react";
import { useTranslation } from "react-i18next";
import { apiErrorMessage } from "../../../utils/apiError";
import { useServerTimezone } from "../../../hooks/useServerTimezone";
import { formatServerIsoDateTime } from "../../../utils/formatMessageTime";
import {
  workbuddyRuntimeApi,
  type WorkBuddyNotification,
} from "../../../api/modules/workbuddyRuntime";
import { isWorkBuddyUnavailableError } from "../../../api/modules/workbuddyWorkflows";
import { LoadingBlock, useWorkBuddyResource } from "../Workflows/consoleState";
import styles from "./index.module.less";

const { Text } = Typography;

export default function NotificationsStrip() {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  const [unreadOnly, setUnreadOnly] = useState(true);
  const [markingId, setMarkingId] = useState<string | null>(null);
  const [markingAll, setMarkingAll] = useState(false);

  const notifications = useWorkBuddyResource<WorkBuddyNotification[]>(
    [],
    () =>
      workbuddyRuntimeApi.listNotifications({
        unread_only: unreadOnly,
        limit: 20,
      }),
    [unreadOnly],
  );

  const { data, loading, error } = notifications;
  const failed = error !== null && error !== undefined;
  const unread = data.filter((row) => !row.read).length;

  const markRead = useCallback(
    async (row: WorkBuddyNotification) => {
      setMarkingId(row.id);
      try {
        const updated = await workbuddyRuntimeApi.readNotification(row.id);
        notifications.setData((prev) =>
          unreadOnly
            ? prev.filter((item) => item.id !== updated.id)
            : prev.map((item) => (item.id === updated.id ? updated : item)),
        );
        message.success(t("workbuddy.approvals.notifications.markedRead"));
      } catch (err) {
        message.error(
          apiErrorMessage(
            err,
            t("workbuddy.approvals.notifications.markFailed"),
            t,
          ),
        );
      } finally {
        setMarkingId(null);
      }
    },
    [notifications, unreadOnly, t],
  );

  const markAllRead = useCallback(async () => {
    const unreadRows = notifications.data.filter((row) => !row.read);
    if (unreadRows.length === 0) return;
    setMarkingAll(true);
    const failedIds: string[] = [];
    const readIds: string[] = [];
    for (const row of unreadRows) {
      try {
        await workbuddyRuntimeApi.readNotification(row.id);
        readIds.push(row.id);
      } catch {
        failedIds.push(row.id);
      }
    }
    if (readIds.length > 0) {
      notifications.setData((prev) =>
        unreadOnly
          ? prev.filter((item) => !readIds.includes(item.id))
          : prev.map((item) =>
              readIds.includes(item.id) ? { ...item, read: true } : item,
            ),
      );
      message.success(
        t("workbuddy.approvals.notifications.markedAll", {
          count: readIds.length,
        }),
      );
    }
    if (failedIds.length > 0) {
      message.error(
        t("workbuddy.approvals.notifications.markAllFailed", {
          count: failedIds.length,
        }),
      );
    }
    setMarkingAll(false);
  }, [notifications, unreadOnly, t]);

  return (
    <div className={styles.strip}>
      <div className={styles.stripHeader}>
        <Space size={8} wrap>
          <BellRing size={15} />
          <Text strong>{t("workbuddy.approvals.notifications.title")}</Text>
          {unread > 0 && (
            <Tag color="blue">
              {t("workbuddy.approvals.notifications.unreadCount", {
                count: unread,
              })}
            </Tag>
          )}
          <Checkbox
            checked={unreadOnly}
            onChange={(event) => setUnreadOnly(event.target.checked)}
          >
            {t("workbuddy.approvals.notifications.unreadOnly")}
          </Checkbox>
        </Space>
        <Space size={8}>
          <Button
            size="small"
            icon={<CheckCheck size={14} />}
            loading={markingAll}
            disabled={unread === 0}
            onClick={() => void markAllRead()}
          >
            {t("workbuddy.approvals.notifications.markAll")}
          </Button>
          <Button
            size="small"
            icon={<RefreshCw size={14} />}
            onClick={() => void notifications.reload()}
          >
            {t("common.refresh")}
          </Button>
        </Space>
      </div>

      {failed ? (
        <Alert
          type={isWorkBuddyUnavailableError(error) ? "info" : "error"}
          showIcon
          message={
            isWorkBuddyUnavailableError(error)
              ? t("workbuddy.approvals.state.unavailableTitle")
              : t("workbuddy.approvals.notifications.loadFailed")
          }
          description={apiErrorMessage(
            error,
            t("workbuddy.approvals.state.errorFallback"),
            t,
          )}
        />
      ) : loading && data.length === 0 ? (
        <LoadingBlock label={t("common.loading")} />
      ) : data.length === 0 ? (
        <Text type="secondary" className={styles.stripEmpty}>
          {unreadOnly
            ? t("workbuddy.approvals.notifications.unreadEmpty")
            : t("workbuddy.approvals.notifications.emptyHint")}
        </Text>
      ) : (
        <div className={styles.stripList}>
          {data.map((row) => (
            <div key={row.id} className={styles.stripRow}>
              <Space size={8} wrap>
                <Tag>{row.kind}</Tag>
                <Text strong={!row.read}>{row.title}</Text>
                {row.body && <Text type="secondary">{row.body}</Text>}
              </Space>
              <Space size={8}>
                <Text type="secondary">
                  {row.created_at
                    ? formatServerIsoDateTime(row.created_at, timeZone)
                    : "—"}
                </Text>
                {!row.read && (
                  <Button
                    type="link"
                    size="small"
                    loading={markingId === row.id}
                    onClick={() => void markRead(row)}
                  >
                    {t("workbuddy.approvals.notifications.markRead")}
                  </Button>
                )}
              </Space>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
