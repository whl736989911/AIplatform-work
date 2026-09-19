/**
 * WorkBuddy console → Workflows → Triggers → one-time secret panel.
 *
 * The webhook signing secret exists only in the rotate-secret response. It is
 * rendered here and dropped as soon as the dialog closes: it is never written
 * to any storage, never sent anywhere else and never logged.
 */

import { Alert, Button, Input, Modal, Space, Typography } from "antd";
import { Copy, ShieldAlert } from "lucide-react";
import { useTranslation } from "react-i18next";
import { message } from "@/utils/antdMessage";
import { copyText } from "../../../utils/copyText";
import { useServerTimezone } from "../../../hooks/useServerTimezone";
import { formatServerDateTime } from "../../../utils/formatMessageTime";
import type { RotatedTriggerSecret } from "../../../api/modules/workbuddyWorkflows";
import styles from "./index.module.less";

const { Text } = Typography;

export default function TriggerSecretModal({
  secret,
  onClose,
}: {
  secret: RotatedTriggerSecret | null;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();

  return (
    <Modal
      open={secret !== null}
      onCancel={onClose}
      destroyOnHidden
      maskClosable={false}
      title={t("workbuddy.workflows.secret.title")}
      footer={
        <Space>
          <Button onClick={onClose}>{t("common.close")}</Button>
          <Button
            type="primary"
            icon={<Copy size={14} />}
            onClick={() => {
              if (!secret) return;
              void copyText(secret.secret).then((ok) => {
                if (ok) message.success(t("common.copied"));
                else message.error(t("common.copyFailed"));
              });
            }}
          >
            {t("workbuddy.workflows.secret.copy")}
          </Button>
        </Space>
      }
    >
      {secret && (
        <Space direction="vertical" size={12} style={{ width: "100%" }}>
          <Alert
            type="warning"
            showIcon
            icon={<ShieldAlert size={16} />}
            message={t("workbuddy.workflows.secret.warning")}
            description={t("workbuddy.workflows.secret.warningHint")}
          />
          <Input.Password
            value={secret.secret}
            readOnly
            visibilityToggle
            className={styles.jsonEditor}
          />
          <Text type="secondary">
            {t("workbuddy.workflows.secret.version", {
              version: secret.secret_version,
            })}
          </Text>
          <Text type="secondary">
            {t("workbuddy.workflows.secret.algorithm", {
              algorithm: secret.algorithm,
            })}
          </Text>
          <Text type="secondary">
            {secret.overlap_expires_at
              ? t("workbuddy.workflows.secret.overlap", {
                  time: formatServerDateTime(
                    secret.overlap_expires_at,
                    timeZone,
                  ),
                })
              : t("workbuddy.workflows.secret.noOverlap")}
          </Text>
        </Space>
      )}
    </Modal>
  );
}
