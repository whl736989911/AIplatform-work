/**
 * 知识库 → 文档 → 抽取文本预览.
 *
 * The preview reads the same route as the export, with ``limit`` set: the
 * drawer shows the first slice of the indexed text and says so when the server
 * cut it short, so a reader knows the saved file holds more.
 */

import { Alert, Button, Drawer, Space, Spin, Tag, Typography } from "antd";
import { Download, RefreshCw } from "lucide-react";
import { useTranslation } from "react-i18next";
import { apiErrorMessage } from "../../../utils/apiError";
import {
  workbuddyKnowledgeApi,
  type KnowledgeBase,
  type KnowledgeDocument,
  type KnowledgeDocumentText,
} from "../../../api/modules/workbuddyKnowledge";
import { useKnowledgeResource } from "./useKnowledgeResource";
import styles from "./index.module.less";

const { Text } = Typography;

/** Characters the preview asks for; the export always sends the whole text. */
export const PREVIEW_LIMIT = 4000;

interface DocumentPreviewDrawerProps {
  base: KnowledgeBase;
  document: KnowledgeDocument | null;
  open: boolean;
  downloading: boolean;
  onDownload: (document: KnowledgeDocument) => void;
  onClose: () => void;
}

export default function DocumentPreviewDrawer({
  base,
  document,
  open,
  downloading,
  onDownload,
  onClose,
}: DocumentPreviewDrawerProps) {
  const { t } = useTranslation();
  const resource = useKnowledgeResource<KnowledgeDocumentText | null>(
    null,
    () =>
      document
        ? workbuddyKnowledgeApi.getDocumentText(
            base.kb_id,
            document.document_id,
            PREVIEW_LIMIT,
          )
        : Promise.resolve(null),
    [base.kb_id, document?.document_id ?? ""],
    { enabled: open && document !== null },
  );
  const payload = resource.data;

  return (
    <Drawer
      open={open}
      onClose={onClose}
      width={720}
      destroyOnHidden
      title={document?.title ?? t("workbuddy.knowledge.preview.title")}
      extra={
        <Button
          size="small"
          icon={<Download size={14} />}
          loading={downloading}
          disabled={document === null}
          onClick={() => document && onDownload(document)}
        >
          {t("workbuddy.knowledge.documents.download")}
        </Button>
      }
    >
      {resource.unavailable ? (
        <Alert
          type="info"
          showIcon
          message={t("workbuddy.knowledge.common.notMergedTitle")}
          description={t("workbuddy.knowledge.common.notMergedHint")}
        />
      ) : resource.error ? (
        <Alert
          type="error"
          showIcon
          message={t("workbuddy.knowledge.preview.loadFailed")}
          description={apiErrorMessage(
            resource.error,
            t("workbuddy.knowledge.preview.loadFailed"),
            t,
          )}
          action={
            <Button
              size="small"
              icon={<RefreshCw size={14} />}
              onClick={() => void resource.refresh()}
            >
              {t("workbuddy.knowledge.common.retry")}
            </Button>
          }
        />
      ) : resource.loading ? (
        <div className={styles.centered}>
          <Spin />
        </div>
      ) : payload === null ? null : (
        <>
          <Space size={12} wrap className={styles.previewMeta}>
            <Tag color="blue">
              {t("workbuddy.knowledge.preview.chunkCount", {
                count: payload.chunk_count,
              })}
            </Tag>
            <Text type="secondary" className={styles.mono}>
              {payload.document_id}
            </Text>
          </Space>
          {payload.truncated && (
            <Alert
              type="warning"
              showIcon
              className={styles.notice}
              message={t("workbuddy.knowledge.preview.truncated")}
              description={t("workbuddy.knowledge.preview.truncatedHint", {
                limit: PREVIEW_LIMIT,
              })}
            />
          )}
          {payload.text.trim() ? (
            <pre className={styles.previewText}>{payload.text}</pre>
          ) : (
            <Alert
              type="info"
              showIcon
              message={t("workbuddy.knowledge.preview.empty")}
            />
          )}
        </>
      )}
    </Drawer>
  );
}
