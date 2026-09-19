/**
 * One-time secret panel for invitation tokens.
 *
 * The raw token exists only in the creation response. It is rendered once,
 * never written to localStorage / sessionStorage, never logged, and dropped
 * from the tree as soon as the dialog closes.
 */

import { Alert, Button, Input, Modal, Space, Typography } from "antd";
import { Copy, ShieldAlert } from "lucide-react";
import { useTranslation } from "react-i18next";
import { message } from "@/utils/antdMessage";
import { copyText } from "../../utils/copyText";
import styles from "./index.module.less";

const { Text } = Typography;

export default function OneTimeTokenModal({
  open,
  token,
  email,
  onClose,
}: {
  open: boolean;
  token: string;
  email: string;
  onClose: () => void;
}) {
  const { t } = useTranslation();

  const onCopy = async () => {
    const ok = await copyText(token);
    if (ok) message.success(t("common.copied"));
    else message.error(t("common.copyFailed"));
  };

  return (
    <Modal
      title={t("tenantGovernance.invitations.tokenTitle")}
      open={open}
      onCancel={onClose}
      destroyOnHidden
      maskClosable={false}
      footer={
        <Space>
          <Button onClick={onClose}>{t("common.close")}</Button>
          <Button
            type="primary"
            icon={<Copy size={14} />}
            onClick={() => void onCopy()}
          >
            {t("tenantGovernance.invitations.copyToken")}
          </Button>
        </Space>
      }
    >
      <Alert
        type="warning"
        showIcon
        icon={<ShieldAlert size={16} />}
        message={t("tenantGovernance.invitations.tokenWarning")}
        description={t("tenantGovernance.invitations.tokenWarningHint", {
          email,
        })}
        className={styles.notice}
      />
      <Input.TextArea
        value={token}
        readOnly
        autoSize={{ minRows: 2, maxRows: 4 }}
        className={styles.tokenBox}
      />
      <Text type="secondary" className={styles.tokenHint}>
        {t("tenantGovernance.invitations.tokenHint")}
      </Text>
    </Modal>
  );
}
