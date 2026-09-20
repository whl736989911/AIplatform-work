/**
 * 知识库 → 文档.
 *
 * Lists the selected base's documents one folder at a time (the folder tree on
 * the left is the navigation), runs the bound-upload handshake (request target →
 * complete → bind file reference), removes documents, previews and exports the
 * indexed text and reindexes one document or the whole base.
 *
 * Reads (list, preview, export) need the base's read permission; uploads,
 * removals, moves and reindexing need write permission.
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
import {
  Check,
  Download,
  FileText,
  FileUp,
  MoveRight,
  Pencil,
  RefreshCw,
  RotateCcw,
  Trash2,
  X,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import { message } from "@/utils/antdMessage";
import { ResizableTable } from "../../../components/ResizableTable";
import { EmptyState } from "../../../components/EmptyState";
import { useServerTimezone } from "../../../hooks/useServerTimezone";
import { formatServerDateTime } from "../../../utils/formatMessageTime";
import { knowledgeBreadcrumb } from "../../../utils/knowledgePath";
import { apiErrorMessage } from "../../../utils/apiError";
import {
  canonicalKnowledgeMime,
  workbuddyKnowledgeApi,
  KNOWLEDGE_MAX_UPLOAD_BYTES,
  type KnowledgeBase,
  type KnowledgeDocument,
  type KnowledgeFileRef,
  type KnowledgeFolder,
  type KnowledgeReindexFailure,
  type KnowledgeUpload,
} from "../../../api/modules/workbuddyKnowledge";
import { TabPanelHeader } from "../../Settings/AdvancedSettings/TabPanelHeader";
import { useKnowledgeResource } from "./useKnowledgeResource";
import { knowledgeLabel } from "./labels";
import {
  documentsInFolder,
  documentTextFilename,
  KNOWLEDGE_ROOT_PATH,
} from "./folders";
import { FolderTree, MoveDocumentModal } from "./FolderTree";
import DocumentPreviewDrawer from "./DocumentPreviewDrawer";
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
  const [downloadingId, setDownloadingId] = useState<string | null>(null);
  const [reindexingId, setReindexingId] = useState<string | null>(null);
  const [reindexingBase, setReindexingBase] = useState(false);
  const [reindexFailures, setReindexFailures] = useState<
    KnowledgeReindexFailure[] | null
  >(null);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [previewDocument, setPreviewDocument] =
    useState<KnowledgeDocument | null>(null);
  const [moveTarget, setMoveTarget] = useState<KnowledgeDocument | null>(null);
  const [folder, setFolder] = useState(KNOWLEDGE_ROOT_PATH);
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const [savingRenameId, setSavingRenameId] = useState<string | null>(null);
  const canWrite = base.permission === "write" || base.permission === "admin";

  const documents = useKnowledgeResource<KnowledgeDocument[]>(
    [],
    () =>
      workbuddyKnowledgeApi
        .listDocuments(base.kb_id)
        .then((page) => page.items),
    [base.kb_id],
  );
  const folders = useKnowledgeResource<KnowledgeFolder[]>(
    [],
    () =>
      workbuddyKnowledgeApi
        .listFolders(base.kb_id)
        .then((page) => page.folders),
    [base.kb_id],
  );
  const refreshDocuments = documents.refresh;
  const refreshFolders = folders.refresh;
  const refreshAll = useCallback(async () => {
    await Promise.all([refreshDocuments(), refreshFolders()]);
  }, [refreshDocuments, refreshFolders]);

  // Another base means another folder tree: back to the root, without the
  // failures or preview of the base the reader just left.
  useEffect(() => {
    setFolder(KNOWLEDGE_ROOT_PATH);
    setReindexFailures(null);
    setPreviewDocument(null);
    setPreviewOpen(false);
    setMoveTarget(null);
  }, [base.kb_id]);

  // An open inline rename belongs to a row on screen; leaving the folder (or the
  // base) closes it instead of leaving an editor bound to a row nobody sees.
  useEffect(() => {
    setRenamingId(null);
    setRenameValue("");
  }, [base.kb_id, folder]);

  // A folder exists while a document sits in it; emptying one by moving its last
  // document away must not strand the reader in a folder the tree no longer has.
  useEffect(() => {
    if (folder === KNOWLEDGE_ROOT_PATH) return;
    if (folders.loading || folders.error) return;
    if (!folders.data.some((entry) => entry.path === folder)) {
      setFolder(KNOWLEDGE_ROOT_PATH);
    }
  }, [folder, folders.data, folders.loading, folders.error]);

  const onDelete = useCallback(
    async (document: KnowledgeDocument) => {
      setDeletingId(document.document_id);
      try {
        await workbuddyKnowledgeApi.deleteDocument(
          base.kb_id,
          document.document_id,
        );
        message.success(t("workbuddy.knowledge.documents.deleteSuccess"));
        await refreshAll();
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
    [base.kb_id, refreshAll, t],
  );

  const startRename = useCallback((row: KnowledgeDocument) => {
    setRenamingId(row.document_id);
    setRenameValue(row.title);
  }, []);

  const cancelRename = useCallback(() => {
    setRenamingId(null);
    setRenameValue("");
  }, []);

  const submitRename = useCallback(
    async (row: KnowledgeDocument) => {
      const title = renameValue.trim();
      if (title.length === 0) {
        message.warning(t("workbuddy.knowledge.documents.renameRequired"));
        return;
      }
      setSavingRenameId(row.document_id);
      try {
        const renamed = await workbuddyKnowledgeApi.renameDocument(
          base.kb_id,
          row.document_id,
          { title },
        );
        message.success(
          t("workbuddy.knowledge.documents.renameSuccess", {
            title: renamed.title,
          }),
        );
        cancelRename();
        await refreshDocuments();
      } catch (err) {
        // The editor stays open: the reader keeps the typed title and can
        // correct it instead of retyping it after the server refused.
        message.error(
          apiErrorMessage(
            err,
            t("workbuddy.knowledge.documents.renameFailed"),
            t,
          ),
        );
      } finally {
        setSavingRenameId(null);
      }
    },
    [base.kb_id, cancelRename, refreshDocuments, renameValue, t],
  );

  const onDownload = useCallback(
    async (row: KnowledgeDocument) => {
      setDownloadingId(row.document_id);
      try {
        const blob = await workbuddyKnowledgeApi.downloadDocumentText(
          base.kb_id,
          row.document_id,
        );
        const link = window.document.createElement("a");
        link.href = URL.createObjectURL(blob);
        link.download = documentTextFilename(row.title, row.document_id);
        link.click();
        URL.revokeObjectURL(link.href);
      } catch (err) {
        message.error(
          apiErrorMessage(
            err,
            t("workbuddy.knowledge.documents.downloadFailed"),
            t,
          ),
        );
      } finally {
        setDownloadingId(null);
      }
    },
    [base.kb_id, t],
  );

  const onReindexDocument = useCallback(
    async (row: KnowledgeDocument) => {
      setReindexingId(row.document_id);
      try {
        await workbuddyKnowledgeApi.reindexDocument(
          base.kb_id,
          row.document_id,
        );
        message.success(
          t("workbuddy.knowledge.reindex.documentQueued", { title: row.title }),
        );
        await refreshAll();
      } catch (err) {
        message.error(
          apiErrorMessage(
            err,
            t("workbuddy.knowledge.reindex.documentFailed"),
            t,
          ),
        );
      } finally {
        setReindexingId(null);
      }
    },
    [base.kb_id, refreshAll, t],
  );

  const onReindexBase = useCallback(async () => {
    setReindexingBase(true);
    try {
      const result = await workbuddyKnowledgeApi.reindexBase(base.kb_id);
      // One unreadable document must not be swallowed behind a success toast:
      // the server reports them by code, so they stay on screen until dismissed.
      setReindexFailures(result.failed.length > 0 ? result.failed : null);
      if (result.failed.length > 0) {
        message.warning(
          t("workbuddy.knowledge.reindex.basePartial", {
            queued: result.queued,
            failed: result.failed.length,
          }),
        );
      } else {
        message.success(
          t("workbuddy.knowledge.reindex.baseQueued", {
            count: result.queued,
          }),
        );
      }
      await refreshAll();
    } catch (err) {
      message.error(
        apiErrorMessage(err, t("workbuddy.knowledge.reindex.baseFailed"), t),
      );
    } finally {
      setReindexingBase(false);
    }
  }, [base.kb_id, refreshAll, t]);

  const rootLabel = t("workbuddy.knowledge.folders.root");
  const folderCrumbs = knowledgeBreadcrumb(folder, rootLabel);
  const folderDocuments = documentsInFolder(documents.data, folder);

  const columns: ColumnsType<KnowledgeDocument> = [
    {
      title: t("workbuddy.knowledge.documents.columnTitle"),
      dataIndex: "title",
      key: "title",
      width: 240,
      render: (value: string, row) =>
        renamingId === row.document_id ? (
          <Space size={4}>
            <Input
              size="small"
              autoFocus
              maxLength={255}
              className={styles.renameInput}
              value={renameValue}
              aria-label={t("workbuddy.knowledge.documents.renameField")}
              onChange={(event) => setRenameValue(event.target.value)}
              onPressEnter={() => void submitRename(row)}
            />
            <Button
              type="text"
              size="small"
              icon={<Check size={14} />}
              aria-label={t("workbuddy.knowledge.documents.renameConfirm")}
              disabled={renameValue.trim().length === 0}
              loading={savingRenameId === row.document_id}
              onClick={() => void submitRename(row)}
            />
            <Button
              type="text"
              size="small"
              icon={<X size={14} />}
              aria-label={t("workbuddy.knowledge.documents.renameCancel")}
              onClick={cancelRename}
            />
          </Space>
        ) : (
          <Tooltip title={row.document_id}>
            <Button
              type="link"
              size="small"
              onClick={() => {
                setPreviewDocument(row);
                setPreviewOpen(true);
              }}
            >
              {value}
            </Button>
          </Tooltip>
        ),
    },
    {
      title: t("workbuddy.knowledge.documents.columnFolder"),
      dataIndex: "folder_path",
      key: "folder_path",
      width: 160,
      render: (value: string) =>
        value ? (
          <span className={styles.mono}>{value}</span>
        ) : (
          <Text type="secondary">{rootLabel}</Text>
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
      width: 320,
      render: (_value, row) => (
        <Space size={0} wrap>
          <Button
            type="link"
            size="small"
            icon={<Download size={14} />}
            loading={downloadingId === row.document_id}
            onClick={() => void onDownload(row)}
          >
            {t("workbuddy.knowledge.documents.download")}
          </Button>
          {canWrite && (
            <Button
              type="link"
              size="small"
              icon={<Pencil size={14} />}
              onClick={() => startRename(row)}
            >
              {t("workbuddy.knowledge.documents.rename")}
            </Button>
          )}
          {canWrite && (
            <Button
              type="link"
              size="small"
              icon={<RotateCcw size={14} />}
              loading={reindexingId === row.document_id}
              onClick={() => void onReindexDocument(row)}
            >
              {t("workbuddy.knowledge.reindex.document")}
            </Button>
          )}
          {canWrite && (
            <Button
              type="link"
              size="small"
              icon={<MoveRight size={14} />}
              onClick={() => setMoveTarget(row)}
            >
              {t("workbuddy.knowledge.folders.move")}
            </Button>
          )}
          {canWrite && (
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
          )}
        </Space>
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
          <Space size={8} wrap>
            <Button
              size="small"
              icon={<RefreshCw size={14} />}
              onClick={() => void refreshAll()}
            >
              {t("workbuddy.knowledge.common.refresh")}
            </Button>
            {canWrite && (
              <Popconfirm
                title={t("workbuddy.knowledge.reindex.baseConfirm", {
                  name: base.name,
                })}
                okText={t("workbuddy.knowledge.common.confirm")}
                cancelText={t("workbuddy.knowledge.common.cancel")}
                onConfirm={() => void onReindexBase()}
              >
                <Button
                  size="small"
                  icon={<RotateCcw size={14} />}
                  loading={reindexingBase}
                >
                  {t("workbuddy.knowledge.reindex.base")}
                </Button>
              </Popconfirm>
            )}
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

      {documents.unavailable ? (
        <Alert
          type="info"
          showIcon
          message={t("workbuddy.knowledge.common.notMergedTitle")}
          description={t("workbuddy.knowledge.common.notMergedHint")}
        />
      ) : documents.error ? (
        <Alert
          type="error"
          showIcon
          message={t("workbuddy.knowledge.documents.loadFailed")}
          description={apiErrorMessage(
            documents.error,
            t("workbuddy.knowledge.documents.loadFailed"),
            t,
          )}
          action={
            <Button
              size="small"
              icon={<RefreshCw size={14} />}
              onClick={() => void refreshAll()}
            >
              {t("workbuddy.knowledge.common.retry")}
            </Button>
          }
        />
      ) : (
        <div className={styles.documentsLayout}>
          <FolderTree
            folders={folders.data}
            loading={folders.loading}
            error={folders.error}
            unavailable={folders.unavailable}
            selectedPath={folder}
            onSelect={setFolder}
            onRetry={() => void refreshFolders()}
          />

          <div className={styles.documentsBody}>
            <div className={styles.folderBar}>
              <nav
                className={styles.pathBreadcrumb}
                aria-label={t("workbuddy.knowledge.folders.pathNav")}
              >
                {folderCrumbs.map((crumb, index) => (
                  <span
                    key={`${crumb.path}:${index}`}
                    className={styles.pathBreadcrumbSegment}
                  >
                    {index > 0 && (
                      <span className={styles.pathBreadcrumbSep} aria-hidden>
                        /
                      </span>
                    )}
                    {index === folderCrumbs.length - 1 ? (
                      <span
                        className={styles.pathBreadcrumbCurrent}
                        title={crumb.label}
                      >
                        {crumb.label}
                      </span>
                    ) : (
                      <button
                        type="button"
                        className={styles.pathBreadcrumbLink}
                        onClick={() => setFolder(crumb.path)}
                        title={crumb.label}
                      >
                        {crumb.label}
                      </button>
                    )}
                  </span>
                ))}
              </nav>
              <Text type="secondary" className={styles.hint}>
                {t("workbuddy.knowledge.documents.previewHint")}
              </Text>
            </div>

            {reindexFailures && reindexFailures.length > 0 && (
              <Alert
                type="warning"
                showIcon
                closable
                onClose={() => setReindexFailures(null)}
                message={t("workbuddy.knowledge.reindex.baseFailuresTitle", {
                  count: reindexFailures.length,
                })}
                description={
                  <ul className={styles.failureList}>
                    {reindexFailures.map((failure) => (
                      <li key={failure.document_id}>
                        <span className={styles.mono}>
                          {failure.document_id}
                        </span>
                        <Tag color="red">{failure.code}</Tag>
                      </li>
                    ))}
                  </ul>
                }
              />
            )}

            {documents.loading && documents.data.length === 0 ? (
              <div className={styles.centered}>
                <Spin />
              </div>
            ) : documents.data.length === 0 ? (
              <EmptyState
                variant="mascot"
                title={t("workbuddy.knowledge.documents.empty")}
                description={t("workbuddy.knowledge.documents.emptyHint")}
                actionLabel={
                  canWrite
                    ? t("workbuddy.knowledge.documents.upload")
                    : undefined
                }
                onAction={canWrite ? () => setUploadOpen(true) : undefined}
              />
            ) : folderDocuments.length === 0 ? (
              <EmptyState
                title={t("workbuddy.knowledge.documents.folderEmpty")}
                description={t(
                  "workbuddy.knowledge.documents.folderEmptyHint",
                  { folder: folder || rootLabel },
                )}
              />
            ) : (
              <ResizableTable
                columns={columns}
                dataSource={folderDocuments}
                rowKey="document_id"
                size="middle"
                tableLayout="fixed"
                scroll={{ x: 1520 }}
                storageKey="workbuddy-knowledge-documents-table-widths"
                minWidth={72}
                pagination={false}
              />
            )}
          </div>
        </div>
      )}

      <DocumentUploadModal
        base={base}
        open={uploadOpen}
        onClose={() => setUploadOpen(false)}
        onCreated={refreshAll}
      />

      <DocumentPreviewDrawer
        base={base}
        document={previewDocument}
        open={previewOpen}
        downloading={downloadingId === previewDocument?.document_id}
        onDownload={(row) => void onDownload(row)}
        onClose={() => setPreviewOpen(false)}
      />

      {moveTarget !== null && (
        <MoveDocumentModal
          base={base}
          document={moveTarget}
          folders={folders.data}
          open
          onClose={() => setMoveTarget(null)}
          onMoved={refreshAll}
        />
      )}
    </div>
  );
}
