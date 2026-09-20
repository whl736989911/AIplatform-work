/**
 * 知识库 → 文档 → 文件夹.
 *
 * The tree mirrors the server's folder list (a folder exists while a live
 * document sits in it) and drives which folder the document table shows; the
 * dialog next to it moves one document to another folder — or back to the root,
 * which is the empty path.
 */

import { useEffect, useMemo, useState } from "react";
import {
  AutoComplete,
  Alert,
  Button,
  Form,
  Modal,
  Spin,
  Tree,
  Typography,
} from "antd";
import type { TreeDataNode } from "antd";
import { FolderTree as FolderTreeIcon } from "lucide-react";
import { useTranslation } from "react-i18next";
import { message } from "@/utils/antdMessage";
import { apiErrorMessage } from "../../../utils/apiError";
import {
  workbuddyKnowledgeApi,
  type KnowledgeBase,
  type KnowledgeDocument,
  type KnowledgeFolder,
} from "../../../api/modules/workbuddyKnowledge";
import {
  buildFolderTree,
  checkFolderPath,
  KNOWLEDGE_ROOT_PATH,
  type KnowledgeFolderNode,
} from "./folders";
import styles from "./index.module.less";

const { Text } = Typography;

/** Tree keys carry their kind so a folder path can never collide with one. */
const FOLDER_KEY_PREFIX = "f:";

interface FolderTreeProps {
  folders: readonly KnowledgeFolder[];
  loading: boolean;
  error: unknown | null;
  unavailable: boolean;
  /** Folder the document table currently shows. */
  selectedPath: string;
  onSelect: (path: string) => void;
  onRetry: () => void;
}

/** The base's folder tree; selecting a node shows that folder's documents. */
export function FolderTree({
  folders,
  loading,
  error,
  unavailable,
  selectedPath,
  onSelect,
  onRetry,
}: FolderTreeProps) {
  const { t } = useTranslation();
  const rootLabel = t("workbuddy.knowledge.folders.root");
  const [expandedKeys, setExpandedKeys] = useState<string[]>([]);

  const tree = useMemo(() => buildFolderTree(folders), [folders]);
  const treeData = useMemo<TreeDataNode[]>(() => {
    const toNode = (node: KnowledgeFolderNode): TreeDataNode => ({
      key: `${FOLDER_KEY_PREFIX}${node.path}`,
      title: (
        <span className={styles.folderNode}>
          <span className={styles.folderNodeName}>
            {node.path === KNOWLEDGE_ROOT_PATH ? rootLabel : node.name}
          </span>
          <span className={styles.folderNodeCount}>{node.document_count}</span>
        </span>
      ),
      children: node.children.map(toNode),
    });
    return [toNode(tree)];
  }, [tree, rootLabel]);

  // A fresh folder list opens fully expanded — including the ancestors that are
  // only implied by a nested path, which would otherwise hide their children.
  const allKeys = useMemo(() => {
    const keys: string[] = [];
    const collect = (node: KnowledgeFolderNode) => {
      keys.push(`${FOLDER_KEY_PREFIX}${node.path}`);
      node.children.forEach(collect);
    };
    collect(tree);
    return keys;
  }, [tree]);
  useEffect(() => setExpandedKeys(allKeys), [allKeys]);

  if (unavailable) {
    return (
      <div className={styles.folderPane}>
        <Alert
          type="info"
          showIcon
          message={t("workbuddy.knowledge.folders.notMergedTitle")}
        />
      </div>
    );
  }
  if (error) {
    return (
      <div className={styles.folderPane}>
        <Alert
          type="error"
          showIcon
          message={t("workbuddy.knowledge.folders.loadFailed")}
          description={apiErrorMessage(
            error,
            t("workbuddy.knowledge.folders.loadFailed"),
            t,
          )}
          action={
            <Button size="small" onClick={onRetry}>
              {t("workbuddy.knowledge.common.retry")}
            </Button>
          }
        />
      </div>
    );
  }
  if (loading && folders.length === 0) {
    return (
      <div className={styles.folderPane}>
        <div className={styles.centered}>
          <Spin size="small" />
        </div>
      </div>
    );
  }

  return (
    <nav
      className={styles.folderPane}
      aria-label={t("workbuddy.knowledge.folders.title")}
    >
      <div className={styles.folderPaneHeader}>
        <FolderTreeIcon size={14} aria-hidden />
        <span>{t("workbuddy.knowledge.folders.title")}</span>
      </div>
      <Tree
        showLine
        blockNode
        virtual={false}
        treeData={treeData}
        expandedKeys={expandedKeys}
        onExpand={(keys) => setExpandedKeys(keys as string[])}
        selectedKeys={[`${FOLDER_KEY_PREFIX}${selectedPath}`]}
        onSelect={(keys) => {
          const key = keys.length > 0 ? String(keys[0]) : null;
          if (key === null) return;
          onSelect(key.slice(FOLDER_KEY_PREFIX.length));
        }}
      />
    </nav>
  );
}

interface MoveDocumentModalProps {
  base: KnowledgeBase;
  document: KnowledgeDocument;
  folders: readonly KnowledgeFolder[];
  open: boolean;
  onClose: () => void;
  onMoved: () => Promise<void>;
}

/** Moves one document into a folder, creating it by naming it. */
export function MoveDocumentModal({
  base,
  document,
  folders,
  open,
  onClose,
  onMoved,
}: MoveDocumentModalProps) {
  const { t } = useTranslation();
  const [form] = Form.useForm<{ folder_path: string }>();
  const [saving, setSaving] = useState(false);
  const rootLabel = t("workbuddy.knowledge.folders.root");
  const currentFolder = document.folder_path || rootLabel;

  useEffect(() => {
    if (!open) return;
    form.setFieldsValue({ folder_path: document.folder_path });
  }, [open, document.document_id, document.folder_path, form]);

  const options = useMemo(
    () => [
      { value: KNOWLEDGE_ROOT_PATH, label: rootLabel },
      ...folders
        .filter(
          (folder) =>
            folder.path !== KNOWLEDGE_ROOT_PATH &&
            folder.path !== document.folder_path,
        )
        .map((folder) => ({ value: folder.path, label: folder.path })),
    ],
    [folders, document.folder_path, rootLabel],
  );

  const onSubmit = async (values: { folder_path?: string }) => {
    const target = checkFolderPath(values.folder_path ?? KNOWLEDGE_ROOT_PATH);
    if (target.path === null) {
      form.setFields([
        {
          name: "folder_path",
          errors: [t(`workbuddy.knowledge.folders.issue.${target.issue}`)],
        },
      ]);
      return;
    }
    setSaving(true);
    try {
      await workbuddyKnowledgeApi.moveDocumentToFolder(
        base.kb_id,
        document.document_id,
        { folder_path: target.path },
      );
      message.success(t("workbuddy.knowledge.folders.moveSuccess"));
      await onMoved();
      onClose();
    } catch (err) {
      message.error(
        apiErrorMessage(err, t("workbuddy.knowledge.folders.moveFailed"), t),
      );
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      title={t("workbuddy.knowledge.folders.moveTitle", {
        title: document.title,
      })}
      open={open}
      onCancel={onClose}
      onOk={() => form.submit()}
      okText={t("workbuddy.knowledge.common.confirm")}
      cancelText={t("workbuddy.knowledge.common.cancel")}
      confirmLoading={saving}
      destroyOnHidden
      width={520}
    >
      <Form
        form={form}
        layout="vertical"
        requiredMark={false}
        onFinish={(values) => void onSubmit(values)}
      >
        <div className={styles.kv}>
          <span className={styles.kvKey}>
            {t("workbuddy.knowledge.folders.moveCurrent")}
          </span>
          <Text>{currentFolder}</Text>
        </div>
        <Form.Item
          name="folder_path"
          label={t("workbuddy.knowledge.folders.moveTarget")}
          extra={t("workbuddy.knowledge.folders.moveHint")}
        >
          <AutoComplete
            options={options}
            placeholder={t("workbuddy.knowledge.folders.movePlaceholder")}
            allowClear
          />
        </Form.Item>
      </Form>
    </Modal>
  );
}
