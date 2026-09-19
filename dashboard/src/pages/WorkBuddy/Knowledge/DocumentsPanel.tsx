/**
 * 知识库 → 文档.
 *
 * Lists the selected base's documents, runs the bound-upload handshake
 * (request target → complete → bind file reference) and removes documents.
 * Uploads and removals need write permission; read-only bases render the list
 * without mutation controls.
 */

import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  Button,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Space,
  Spin,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { FileText, FileUp, RefreshCw, Trash2 } from "lucide-react";
import { useTranslation } from "react-i18next";
import { message } from "@/utils/antdMessage";
import { ResizableTable } from "../../../components/ResizableTable";
import { EmptyState } from "../../../components/EmptyState";
import { useServerTimezone } from "../../../hooks/useServerTimezone";
import { formatServerDateTime } from "../../../utils/formatMessageTime";
import { apiErrorMessage } from "../../../utils/apiError";
import {
  canonicalKnowledgeMime,
  workbuddyKnowledgeApi,
  KNOWLEDGE_MAX_UPLOAD_BYTES,
  type KnowledgeBase,
  type KnowledgeDocument,
  type KnowledgeFileRef,
  type KnowledgeUpload,
} from "../../../api/modules/workbuddyKnowledge";
import { TabPanelHeader } from "../../Settings/AdvancedSettings/TabPanelHeader";
import { useKnowledgeResource } from "./useKnowledgeResource";
import { knowledgeLabel } from "./labels";
import styles from "./index.module.less";

const { Text } = Typography;

interface UploadRequestValues {
  filename: string;
  mime_type: string;
  size_bytes: number;
}

interface DocumentFormValues {
  title?: string;
}

/** Indexing lifecycle colour: ready = green, failed/deleted = red, in-flight = blue. */
const STATUS_COLORS: Record<string, string> = {
  pending: "blue",
  parsing: "blue",
  indexing: "blue",
  ready: "green",
  failed: "red",
  deleted: "red",
};

function DocumentUploadModal({
  base,
  open,
  onClose,
  onCreated,
}: {
  base: KnowledgeBase;
  open: boolean;
  onClose: () => void;
  onCreated: () => Promise<void>;
}) {
  const { t } = useTranslation();
  const [requestForm] = Form.useForm<UploadRequestValues>();
  const [documentForm] = Form.useForm<DocumentFormValues>();
  const [upload, setUpload] = useState<KnowledgeUpload | null>(null);
  const [fileRef, setFileRef] = useState<KnowledgeFileRef | null>(null);
  const [checksum, setChecksum] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!open) return;
    requestForm.resetFields();
    documentForm.resetFields();
    setUpload(null);
    setFileRef(null);
    setChecksum("");
  }, [open, requestForm, documentForm]);

  const onRequestUpload = async (values: UploadRequestValues) => {
    setBusy(true);
    try {
      const created = await workbuddyKnowledgeApi.createUpload(base.kb_id, {
        filename: values.filename.trim(),
        mime_type: values.mime_type.trim(),
        size_bytes: values.size_bytes,
      });
      setUpload(created);
      message.success(t("workbuddy.knowledge.documents.uploadCreated"));
    } catch (err) {
      message.error(
        apiErrorMessage(
          err,
          t("workbuddy.knowledge.documents.uploadCreateFailed"),
          t,
        ),
      );
    } finally {
      setBusy(false);
    }
  };

  const onCompleteUpload = async () => {
    if (!upload) return;
    setBusy(true);
    try {
      const ref = await workbuddyKnowledgeApi.completeUpload(
        base.kb_id,
        upload.upload_id,
        { checksum_sha256: checksum.trim() || null },
      );
      setFileRef(ref);
      message.success(t("workbuddy.knowledge.documents.uploadCompleted"));
    } catch (err) {
      message.error(
        apiErrorMessage(
          err,
          t("workbuddy.knowledge.documents.completeFailed"),
          t,
        ),
      );
    } finally {
      setBusy(false);
    }
  };

  const onCreateDocument = async (values: DocumentFormValues) => {
    if (!fileRef) return;
    setBusy(true);
    try {
      await workbuddyKnowledgeApi.createDocument(base.kb_id, {
        file_ref: fileRef.file_ref_id,
        upload_id: fileRef.upload_id,
        title: values.title?.trim() ?? "",
      });
      message.success(t("workbuddy.knowledge.documents.created"));
      await onCreated();
      onClose();
    } catch (err) {
      message.error(
        apiErrorMessage(
          err,
          t("workbuddy.knowledge.documents.createFailed"),
          t,
        ),
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      title={t("workbuddy.knowledge.documents.uploadTitle")}
      open={open}
      onCancel={onClose}
      footer={null}
      destroyOnHidden
      width={600}
    >
      <Form
        form={requestForm}
        layout="vertical"
        requiredMark={false}
        onFinish={(values) => void onRequestUpload(values)}
      >
        <Form.Item
          name="filename"
          label={t("workbuddy.knowledge.documents.filename")}
          rules={[
            {
              required: true,
              message: t("workbuddy.knowledge.documents.filenameRequired"),
            },
          ]}
        >
          <Input
            maxLength={255}
            disabled={upload !== null}
            placeholder={t("workbuddy.knowledge.documents.filenamePlaceholder")}
            onChange={(event) => {
              const guessed = canonicalKnowledgeMime(event.target.value);
              if (guessed) requestForm.setFieldValue("mime_type", guessed);
            }}
          />
        </Form.Item>
        <Form.Item
          name="mime_type"
          label={t("workbuddy.knowledge.documents.mimeType")}
          rules={[
            {
              required: true,
              message: t("workbuddy.knowledge.documents.mimeTypeRequired"),
            },
          ]}
          extra={t("workbuddy.knowledge.documents.mimeTypeHint")}
        >
          <Input disabled={upload !== null} placeholder="application/pdf" />
        </Form.Item>
        <Form.Item
          name="size_bytes"
          label={t("workbuddy.knowledge.documents.size")}
          rules={[
            {
              required: true,
              message: t("workbuddy.knowledge.documents.sizeRequired"),
            },
          ]}
          extra={t("workbuddy.knowledge.documents.sizeHint", {
            max: KNOWLEDGE_MAX_UPLOAD_BYTES / (1024 * 1024),
          })}
        >
          <InputNumber
            min={1}
            max={KNOWLEDGE_MAX_UPLOAD_BYTES}
            precision={0}
            disabled={upload !== null}
            className={styles.fullWidth}
          />
        </Form.Item>
        {upload === null && (
          <Button type="primary" htmlType="submit" loading={busy}>
            {t("workbuddy.knowledge.documents.requestUpload")}
          </Button>
        )}
      </Form>

      {upload !== null && (
        <div className={styles.stepBlock}>
          <Text strong>{t("workbuddy.knowledge.documents.targetTitle")}</Text>
          <div className={styles.kv}>
            <span className={styles.kvKey}>
              {t("workbuddy.knowledge.documents.objectKey")}
            </span>
            <span className={styles.mono}>
              {upload.upload_target.object_key}
            </span>
          </div>
          <div className={styles.kv}>
            <span className={styles.kvKey}>
              {t("workbuddy.knowledge.documents.uploadUrl")}
            </span>
            <span className={styles.mono}>
              {upload.upload_target.upload_url ?? "—"}
            </span>
          </div>
          {upload.upload_target.upload_url === null && (
            <Text type="secondary" className={styles.hint}>
              {t("workbuddy.knowledge.documents.noUploadUrlHint")}
            </Text>
          )}
          <Form layout="vertical" requiredMark={false}>
            <Form.Item
              label={t("workbuddy.knowledge.documents.checksum")}
              extra={t("workbuddy.knowledge.documents.checksumHint")}
            >
              <Input
                value={checksum}
                onChange={(event) => setChecksum(event.target.value)}
                disabled={fileRef !== null}
                placeholder="e3b0c442…"
              />
            </Form.Item>
          </Form>
          {fileRef === null ? (
            <Button
              type="primary"
              loading={busy}
              onClick={() => void onCompleteUpload()}
            >
              {t("workbuddy.knowledge.documents.completeUpload")}
            </Button>
          ) : (
            <Text type="secondary" className={styles.hint}>
              {t("workbuddy.knowledge.documents.boundRef", {
                ref: fileRef.file_ref_id,
              })}
            </Text>
          )}
        </div>
      )}

      {fileRef !== null && (
        <div className={styles.stepBlock}>
          <Text strong>{t("workbuddy.knowledge.documents.registerTitle")}</Text>
          <Form
            form={documentForm}
            layout="vertical"
            requiredMark={false}
            onFinish={(values) => void onCreateDocument(values)}
          >
            <Form.Item
              name="title"
              label={t("workbuddy.knowledge.documents.documentTitle")}
              extra={t("workbuddy.knowledge.documents.titleHint")}
            >
              <Input
                maxLength={255}
                placeholder={t(
                  "workbuddy.knowledge.documents.titlePlaceholder",
                )}
              />
            </Form.Item>
            <Button type="primary" htmlType="submit" loading={busy}>
              {t("workbuddy.knowledge.documents.createDocument")}
            </Button>
          </Form>
        </div>
      )}
    </Modal>
  );
}

export default function DocumentsPanel({ base }: { base: KnowledgeBase }) {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  const [uploadOpen, setUploadOpen] = useState(false);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const canWrite = base.permission === "write" || base.permission === "admin";

  const resource = useKnowledgeResource<KnowledgeDocument[]>(
    [],
    () =>
      workbuddyKnowledgeApi
        .listDocuments(base.kb_id)
        .then((page) => page.items),
    [base.kb_id],
  );
  const refreshDocuments = resource.refresh;

  const onDelete = useCallback(
    async (document: KnowledgeDocument) => {
      setDeletingId(document.document_id);
      try {
        await workbuddyKnowledgeApi.deleteDocument(
          base.kb_id,
          document.document_id,
        );
        message.success(t("workbuddy.knowledge.documents.deleteSuccess"));
        await refreshDocuments();
      } catch (err) {
        message.error(
          apiErrorMessage(
            err,
            t("workbuddy.knowledge.documents.deleteFailed"),
            t,
          ),
        );
      } finally {
        setDeletingId(null);
      }
    },
    [base.kb_id, refreshDocuments, t],
  );

  const columns: ColumnsType<KnowledgeDocument> = [
    {
      title: t("workbuddy.knowledge.documents.columnTitle"),
      dataIndex: "title",
      key: "title",
      width: 260,
      render: (value: string, row) => (
        <Tooltip title={row.document_id}>
          <span>{value}</span>
        </Tooltip>
      ),
    },
    {
      title: t("workbuddy.knowledge.documents.columnStatus"),
      dataIndex: "status",
      key: "status",
      width: 130,
      render: (value: string) => (
        <Tag color={STATUS_COLORS[value] ?? "default"}>
          {knowledgeLabel(t, "documentStatus", value)}
        </Tag>
      ),
    },
    {
      title: t("workbuddy.knowledge.documents.columnChunks"),
      dataIndex: "chunk_count",
      key: "chunk_count",
      width: 100,
      render: (value: number) => <span>{value}</span>,
    },
    {
      title: t("workbuddy.knowledge.documents.columnGeneration"),
      dataIndex: "active_generation_id",
      key: "active_generation_id",
      width: 200,
      render: (value: string | null) =>
        value ? (
          <span className={styles.mono}>{value}</span>
        ) : (
          <Text type="secondary">—</Text>
        ),
    },
    {
      title: t("workbuddy.knowledge.documents.columnError"),
      dataIndex: "error_code",
      key: "error_code",
      width: 180,
      render: (value: string | null) =>
        value ? (
          <Text type="danger">{value}</Text>
        ) : (
          <Text type="secondary">—</Text>
        ),
    },
    {
      title: t("workbuddy.knowledge.documents.columnUpdatedAt"),
      dataIndex: "updated_at",
      key: "updated_at",
      width: 180,
      render: (value: number) => formatServerDateTime(value, timeZone),
    },
    {
      title: t("workbuddy.knowledge.common.actions"),
      key: "actions",
      width: 140,
      render: (_value, row) =>
        canWrite ? (
          <Popconfirm
            title={t("workbuddy.knowledge.documents.deleteConfirm", {
              title: row.title,
            })}
            okText={t("workbuddy.knowledge.common.confirm")}
            cancelText={t("workbuddy.knowledge.common.cancel")}
            onConfirm={() => void onDelete(row)}
          >
            <Button
              type="link"
              size="small"
              danger
              icon={<Trash2 size={14} />}
              loading={deletingId === row.document_id}
            >
              {t("workbuddy.knowledge.documents.remove")}
            </Button>
          </Popconfirm>
        ) : (
          <Text type="secondary">—</Text>
        ),
    },
  ];

  return (
    <div className={styles.panel}>
      <TabPanelHeader
        icon={<FileText size={18} />}
        title={t("workbuddy.knowledge.documents.title")}
        description={t("workbuddy.knowledge.documents.desc", {
          name: base.name,
        })}
        actions={
          <Space size={8}>
            <Button
              size="small"
              icon={<RefreshCw size={14} />}
              onClick={() => void resource.refresh()}
            >
              {t("workbuddy.knowledge.common.refresh")}
            </Button>
            {canWrite && (
              <Button
                size="small"
                type="primary"
                icon={<FileUp size={14} />}
                onClick={() => setUploadOpen(true)}
              >
                {t("workbuddy.knowledge.documents.upload")}
              </Button>
            )}
          </Space>
        }
      />

      {!canWrite && (
        <Alert
          type="info"
          showIcon
          className={styles.notice}
          message={t("workbuddy.knowledge.documents.readOnlyTitle")}
          description={t("workbuddy.knowledge.documents.readOnlyHint")}
        />
      )}

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
          message={t("workbuddy.knowledge.documents.loadFailed")}
          description={apiErrorMessage(
            resource.error,
            t("workbuddy.knowledge.documents.loadFailed"),
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
      ) : resource.loading && resource.data.length === 0 ? (
        <div className={styles.centered}>
          <Spin />
        </div>
      ) : resource.data.length === 0 ? (
        <EmptyState
          variant="mascot"
          title={t("workbuddy.knowledge.documents.empty")}
          description={t("workbuddy.knowledge.documents.emptyHint")}
          actionLabel={
            canWrite ? t("workbuddy.knowledge.documents.upload") : undefined
          }
          onAction={canWrite ? () => setUploadOpen(true) : undefined}
        />
      ) : (
        <ResizableTable
          columns={columns}
          dataSource={resource.data}
          rowKey="document_id"
          size="middle"
          tableLayout="fixed"
          scroll={{ x: 1240 }}
          storageKey="workbuddy-knowledge-documents-table-widths"
          minWidth={72}
          pagination={false}
        />
      )}

      <DocumentUploadModal
        base={base}
        open={uploadOpen}
        onClose={() => setUploadOpen(false)}
        onCreated={resource.refresh}
      />
    </div>
  );
}
