/**
 * 知识库 → 知识库列表.
 *
 * Lists every base the caller may read, creates new bases (name / scope /
 * department / pinned bge-m3 revision) and archives them. Archiving requires
 * base-admin permission and is confirmed, because retrieval stops immediately.
 */

import { useCallback, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Spin,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { Archive, Database, Plus, RefreshCw } from "lucide-react";
import { useTranslation } from "react-i18next";
import { message } from "@/utils/antdMessage";
import { ResizableTable } from "../../../components/ResizableTable";
import { EmptyState } from "../../../components/EmptyState";
import { useServerTimezone } from "../../../hooks/useServerTimezone";
import { formatServerDateTime } from "../../../utils/formatMessageTime";
import { apiErrorMessage } from "../../../utils/apiError";
import {
  workbuddyKnowledgeApi,
  type KnowledgeBase,
  type KnowledgeBaseCreate,
  type KnowledgeModelRevision,
  type KnowledgeScope,
} from "../../../api/modules/workbuddyKnowledge";
import { TabPanelHeader } from "../../Settings/AdvancedSettings/TabPanelHeader";
import {
  useKnowledgeResource,
  type KnowledgeResource,
} from "./useKnowledgeResource";
import { knowledgeLabel } from "./labels";
import {
  filterBasesByLayer,
  KNOWLEDGE_LAYERS,
  KNOWLEDGE_LAYER_LABEL_KEY,
  knowledgeAccessSourceLabel,
  toggleKnowledgeLayer,
  type KnowledgeLayer,
} from "./visibility";
import styles from "./index.module.less";

const { Text } = Typography;

const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

const SCOPE_OPTIONS: readonly KnowledgeScope[] = [
  "personal",
  "department",
  "enterprise",
];

interface BaseFormValues {
  name: string;
  description?: string;
  scope: KnowledgeScope;
  department_id?: string;
  model_revision_id: string;
}

interface BasesPanelProps {
  resource: KnowledgeResource<KnowledgeBase[]>;
  selectedId: string | null;
  onSelect: (kbId: string) => void;
  /** Selected visibility layers (empty = every readable base). */
  layers: readonly KnowledgeLayer[];
  onLayersChange: (layers: KnowledgeLayer[]) => void;
}

export default function BasesPanel({
  resource,
  selectedId,
  onSelect,
  layers,
  onLayersChange,
}: BasesPanelProps) {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  const [form] = Form.useForm<BaseFormValues>();
  const [creating, setCreating] = useState(false);
  const [saving, setSaving] = useState(false);
  const [archivingId, setArchivingId] = useState<string | null>(null);
  const scope = Form.useWatch("scope", form);

  /** Only published bge-m3 revisions can back a knowledge base. */
  const modelCatalog = useKnowledgeResource<KnowledgeModelRevision[]>(
    [],
    () =>
      workbuddyKnowledgeApi
        .modelCatalog()
        .then((page) =>
          page.items.filter(
            (item) =>
              item.model_key === "bge-m3" && item.status === "published",
          ),
        ),
    [],
    { enabled: creating },
  );

  const modelOptions = useMemo(
    () =>
      modelCatalog.data.map((item) => ({
        value: item.id,
        label: `${item.display_name || item.model_key} · r${item.revision}`,
      })),
    [modelCatalog.data],
  );

  // The chips only re-present the server's own access sources, so this filter
  // narrows what the caller already reads — it never hides a base the caller
  // may read while another chip matches it.
  const visibleBases = useMemo(
    () => filterBasesByLayer(resource.data, layers),
    [resource.data, layers],
  );
  const filterActive = layers.length > 0;

  const openCreate = useCallback(() => {
    setCreating(true);
    form.resetFields();
    form.setFieldsValue({ scope: "personal" });
  }, [form]);

  const onCreate = async (values: BaseFormValues) => {
    const payload: KnowledgeBaseCreate = {
      scope: values.scope,
      name: values.name.trim(),
      description: values.description?.trim() ?? "",
      department_id:
        values.scope === "department"
          ? values.department_id?.trim() ?? null
          : null,
      model_revision_id: values.model_revision_id,
    };
    setSaving(true);
    try {
      const created = await workbuddyKnowledgeApi.createBase(payload);
      message.success(t("workbuddy.knowledge.bases.createSuccess"));
      setCreating(false);
      form.resetFields();
      await resource.refresh();
      onSelect(created.kb_id);
    } catch (err) {
      message.error(
        apiErrorMessage(err, t("workbuddy.knowledge.bases.createFailed"), t),
      );
    } finally {
      setSaving(false);
    }
  };

  const onArchive = async (base: KnowledgeBase) => {
    setArchivingId(base.kb_id);
    try {
      await workbuddyKnowledgeApi.archiveBase(base.kb_id);
      message.success(t("workbuddy.knowledge.bases.archiveSuccess"));
      await resource.refresh();
    } catch (err) {
      message.error(
        apiErrorMessage(err, t("workbuddy.knowledge.bases.archiveFailed"), t),
      );
    } finally {
      setArchivingId(null);
    }
  };

  const columns: ColumnsType<KnowledgeBase> = [
    {
      title: t("workbuddy.knowledge.bases.columnName"),
      dataIndex: "name",
      key: "name",
      width: 260,
      render: (name: string, row) => (
        <Space size={6} wrap>
          <span>{name}</span>
          {row.kb_id === selectedId && (
            <Tag color="blue">{t("workbuddy.knowledge.bases.selected")}</Tag>
          )}
        </Space>
      ),
    },
    {
      title: t("workbuddy.knowledge.bases.columnScope"),
      dataIndex: "scope",
      key: "scope",
      width: 140,
      render: (value: string) => (
        <Tag color={value === "enterprise" ? "gold" : "default"}>
          {knowledgeLabel(t, "scope", value)}
        </Tag>
      ),
    },
    {
      title: t("workbuddy.knowledge.bases.columnPermission"),
      dataIndex: "permission",
      key: "permission",
      width: 120,
      render: (value: string | null) =>
        value ? (
          <Tag color={value === "admin" ? "green" : "default"}>
            {knowledgeLabel(t, "permission", value)}
          </Tag>
        ) : (
          <Text type="secondary">—</Text>
        ),
    },
    {
      // Why *this* caller may read the row, straight from the server resolver:
      // the same field the layer chips filter on.
      title: t("workbuddy.knowledge.bases.columnAccessSources"),
      dataIndex: "access_sources",
      key: "access_sources",
      width: 240,
      render: (sources: string[] | undefined) =>
        sources && sources.length > 0 ? (
          <Space size={4} wrap>
            {sources.map((source) => (
              <Tag key={source}>{knowledgeAccessSourceLabel(t, source)}</Tag>
            ))}
          </Space>
        ) : (
          <Text type="secondary">—</Text>
        ),
    },
    {
      title: t("workbuddy.knowledge.bases.columnEmbedding"),
      dataIndex: "embedding",
      key: "embedding",
      width: 200,
      render: (_value, row) => (
        <Tooltip title={row.embedding.model_revision_id}>
          <span>
            {row.embedding.model_key} · r{row.embedding.revision}
          </span>
        </Tooltip>
      ),
    },
    {
      title: t("workbuddy.knowledge.bases.columnDepartment"),
      dataIndex: "department_id",
      key: "department_id",
      width: 200,
      render: (value: string | null) =>
        value ? (
          <span className={styles.mono}>{value}</span>
        ) : (
          <Text type="secondary">—</Text>
        ),
    },
    {
      title: t("workbuddy.knowledge.bases.columnOwner"),
      dataIndex: "owner_user_id",
      key: "owner_user_id",
      width: 120,
      render: (value: number | null) =>
        value === null ? (
          <Text type="secondary">—</Text>
        ) : (
          <span>#{value}</span>
        ),
    },
    {
      title: t("workbuddy.knowledge.bases.columnCreatedAt"),
      dataIndex: "created_at",
      key: "created_at",
      width: 180,
      render: (value: number) => formatServerDateTime(value, timeZone),
    },
    {
      title: t("workbuddy.knowledge.common.actions"),
      key: "actions",
      width: 180,
      render: (_value, row) => (
        <Space size={4}>
          <Button type="link" size="small" onClick={() => onSelect(row.kb_id)}>
            {t("workbuddy.knowledge.bases.open")}
          </Button>
          {row.permission === "admin" && (
            <Popconfirm
              title={t("workbuddy.knowledge.bases.archiveConfirm", {
                name: row.name,
              })}
              okText={t("workbuddy.knowledge.common.confirm")}
              cancelText={t("workbuddy.knowledge.common.cancel")}
              onConfirm={() => void onArchive(row)}
            >
              <Button
                type="link"
                size="small"
                danger
                icon={<Archive size={14} />}
                loading={archivingId === row.kb_id}
              >
                {t("workbuddy.knowledge.bases.archive")}
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
        icon={<Database size={18} />}
        title={t("workbuddy.knowledge.bases.title")}
        description={t("workbuddy.knowledge.bases.desc")}
        actions={
          <Space size={8}>
            <Button
              size="small"
              icon={<RefreshCw size={14} />}
              onClick={() => void resource.refresh()}
            >
              {t("workbuddy.knowledge.common.refresh")}
            </Button>
            <Button
              size="small"
              type="primary"
              icon={<Plus size={14} />}
              onClick={openCreate}
            >
              {t("workbuddy.knowledge.bases.create")}
            </Button>
          </Space>
        }
      />

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
          message={t("workbuddy.knowledge.bases.loadFailed")}
          description={apiErrorMessage(
            resource.error,
            t("workbuddy.knowledge.bases.loadFailed"),
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
          title={t("workbuddy.knowledge.bases.empty")}
          description={t("workbuddy.knowledge.bases.emptyHint")}
          actionLabel={t("workbuddy.knowledge.bases.create")}
          onAction={openCreate}
        />
      ) : (
        <>
          <div className={styles.layerBar}>
            <span className={styles.layerBarLabel}>
              {t("workbuddy.knowledge.layers.label")}
            </span>
            <Button
              size="small"
              type={filterActive ? "default" : "primary"}
              onClick={() => onLayersChange([])}
            >
              {t("workbuddy.knowledge.layers.all")}
            </Button>
            {KNOWLEDGE_LAYERS.map((layer) => (
              <Button
                key={layer}
                size="small"
                type={layers.includes(layer) ? "primary" : "default"}
                aria-pressed={layers.includes(layer)}
                onClick={() =>
                  onLayersChange(toggleKnowledgeLayer(layers, layer))
                }
              >
                {t(KNOWLEDGE_LAYER_LABEL_KEY[layer])}
              </Button>
            ))}
            <Text type="secondary" className={styles.layerBarCount}>
              {t("workbuddy.knowledge.layers.count", {
                visible: visibleBases.length,
                total: resource.data.length,
              })}
            </Text>
          </div>
          <Text type="secondary" className={styles.layerBarHint}>
            {t("workbuddy.knowledge.layers.hint")}
          </Text>
          {visibleBases.length === 0 ? (
            <EmptyState
              title={t("workbuddy.knowledge.bases.filterEmpty")}
              description={t("workbuddy.knowledge.bases.filterEmptyHint")}
              actionLabel={t("workbuddy.knowledge.bases.filterClear")}
              onAction={() => onLayersChange([])}
            />
          ) : (
            <ResizableTable
              columns={columns}
              dataSource={visibleBases}
              rowKey="kb_id"
              size="middle"
              tableLayout="fixed"
              scroll={{ x: 1600 }}
              storageKey="workbuddy-knowledge-bases-table-widths"
              minWidth={72}
              pagination={false}
            />
          )}
        </>
      )}

      <Modal
        title={t("workbuddy.knowledge.bases.createTitle")}
        open={creating}
        onCancel={() => setCreating(false)}
        onOk={() => form.submit()}
        confirmLoading={saving}
        okText={t("workbuddy.knowledge.common.create")}
        cancelText={t("workbuddy.knowledge.common.cancel")}
        destroyOnHidden
      >
        {modelCatalog.unavailable && (
          <Alert
            type="info"
            showIcon
            className={styles.notice}
            message={t("workbuddy.knowledge.common.notMergedTitle")}
            description={t("workbuddy.knowledge.common.notMergedHint")}
          />
        )}
        {modelCatalog.error ? (
          <Alert
            type="error"
            showIcon
            className={styles.notice}
            message={t("workbuddy.knowledge.bases.modelCatalogFailed")}
            description={apiErrorMessage(
              modelCatalog.error,
              t("workbuddy.knowledge.bases.modelCatalogFailed"),
              t,
            )}
          />
        ) : null}
        <Form
          form={form}
          layout="vertical"
          requiredMark={false}
          onFinish={(values) => void onCreate(values)}
        >
          <Form.Item
            name="name"
            label={t("workbuddy.knowledge.bases.name")}
            rules={[
              {
                required: true,
                message: t("workbuddy.knowledge.bases.nameRequired"),
              },
            ]}
          >
            <Input
              maxLength={120}
              placeholder={t("workbuddy.knowledge.bases.namePlaceholder")}
            />
          </Form.Item>
          <Form.Item
            name="description"
            label={t("workbuddy.knowledge.bases.description")}
          >
            <Input.TextArea
              rows={3}
              maxLength={2000}
              showCount
              placeholder={t(
                "workbuddy.knowledge.bases.descriptionPlaceholder",
              )}
            />
          </Form.Item>
          <Form.Item
            name="scope"
            label={t("workbuddy.knowledge.bases.scope")}
            rules={[
              {
                required: true,
                message: t("workbuddy.knowledge.bases.scopeRequired"),
              },
            ]}
          >
            <Select
              options={SCOPE_OPTIONS.map((value) => ({
                value,
                label: knowledgeLabel(t, "scope", value),
              }))}
            />
          </Form.Item>
          {scope === "department" && (
            <Form.Item
              name="department_id"
              label={t("workbuddy.knowledge.bases.departmentId")}
              rules={[
                {
                  required: true,
                  message: t("workbuddy.knowledge.bases.departmentIdRequired"),
                },
                {
                  pattern: UUID_PATTERN,
                  message: t("workbuddy.knowledge.common.uuidInvalid"),
                },
              ]}
            >
              <Input
                placeholder={t(
                  "workbuddy.knowledge.bases.departmentIdPlaceholder",
                )}
              />
            </Form.Item>
          )}
          <Form.Item
            name="model_revision_id"
            label={t("workbuddy.knowledge.bases.modelRevision")}
            rules={[
              {
                required: true,
                message: t("workbuddy.knowledge.bases.modelRevisionRequired"),
              },
            ]}
            extra={
              modelOptions.length === 0
                ? t("workbuddy.knowledge.bases.modelRevisionEmpty")
                : t("workbuddy.knowledge.bases.modelRevisionHint")
            }
          >
            <Select
              showSearch
              optionFilterProp="label"
              loading={modelCatalog.loading}
              disabled={modelOptions.length === 0}
              options={modelOptions}
              placeholder={t(
                "workbuddy.knowledge.bases.modelRevisionPlaceholder",
              )}
            />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
