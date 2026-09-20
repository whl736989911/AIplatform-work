/**
 * WorkBuddy console → Workflows → Definitions.
 *
 * Lists the workflows visible to the caller. Tenant admins and workflow
 * creators can create a draft and edit a definition; the edit path always
 * re-reads the workflow so the compare-and-swap ETag handed to the save is the
 * revision the editor actually opened.
 */

import { useCallback, useState } from "react";
import { Button, Space, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { message } from "@/utils/antdMessage";
import { FileCog, Pencil, RefreshCw } from "lucide-react";
import { useTranslation } from "react-i18next";
import { ResizableTable } from "../../../components/ResizableTable";
import { apiErrorMessage } from "../../../utils/apiError";
import { useServerTimezone } from "../../../hooks/useServerTimezone";
import { formatServerDateTime } from "../../../utils/formatMessageTime";
import {
  isWorkflowManagerDetail,
  isWorkflowRecord,
  workbuddyWorkflowsApi,
  type WorkflowListItem,
  type WorkflowVersion,
  type WorkflowWithVersion,
} from "../../../api/modules/workbuddyWorkflows";
import { TabPanelHeader } from "../../Settings/AdvancedSettings/TabPanelHeader";
import {
  ResourceState,
  statusLabel,
  type WorkBuddyResource,
} from "./consoleState";
import DefinitionEditorModal, {
  type DefinitionEditorTarget,
} from "./DefinitionEditorModal";
import CreateWizard from "./CreateWizard";
import styles from "./index.module.less";

const { Text } = Typography;

export default function DefinitionsPanel({
  resource,
  selectedWorkflowId,
  onSelectWorkflow,
  onOpenVersions,
}: {
  resource: WorkBuddyResource<WorkflowListItem[]>;
  selectedWorkflowId: string | null;
  onSelectWorkflow: (id: string) => void;
  onOpenVersions: (id: string) => void;
}) {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  const [target, setTarget] = useState<DefinitionEditorTarget | null>(null);
  // The guided path (A-06) runs beside the JSON editor, not instead of it.
  const [wizardOpen, setWizardOpen] = useState(false);
  const [loadingDefinition, setLoadingDefinition] = useState<string | null>(
    null,
  );

  const openCreate = useCallback(() => setTarget({ mode: "create" }), []);

  /** Re-read the workflow, then open the editor with the revision it read. */
  const openEdit = useCallback(
    async (row: WorkflowListItem) => {
      if (!isWorkflowRecord(row)) return;
      setLoadingDefinition(row.id);
      try {
        const detail = await workbuddyWorkflowsApi.getWorkflow(row.id);
        if (!isWorkflowManagerDetail(detail)) {
          message.error(t("workbuddy.workflows.definitions.editOnlyManagers"));
          return;
        }
        let version: WorkflowVersion | null = detail.active_version;
        if (version && !version.definition) {
          version = await workbuddyWorkflowsApi.getWorkflowVersion(
            detail.id,
            version.id,
          );
        }
        if (!version) {
          const versions = await workbuddyWorkflowsApi.listWorkflowVersions(
            detail.id,
          );
          const newest = [...versions].sort(
            (a, b) => b.version_number - a.version_number,
          )[0];
          if (!newest) {
            message.info(
              t("workbuddy.workflows.definitions.editMissingVersion"),
            );
            return;
          }
          version = await workbuddyWorkflowsApi.getWorkflowVersion(
            detail.id,
            newest.id,
          );
        }
        setTarget({ mode: "edit", workflow: detail, baseVersion: version });
      } catch (err) {
        message.error(
          apiErrorMessage(
            err,
            t("workbuddy.workflows.definitions.editLoadFailed"),
            t,
          ),
        );
      } finally {
        setLoadingDefinition(null);
      }
    },
    [t],
  );

  const onSaved = useCallback(
    (saved: WorkflowWithVersion) => {
      resource.setData((prev) =>
        prev.map((row) => (row.id === saved.id ? saved : row)),
      );
    },
    [resource],
  );

  const columns: ColumnsType<WorkflowListItem> = [
    {
      title: t("workbuddy.workflows.definitions.column.name"),
      dataIndex: "name",
      key: "name",
      width: 260,
      render: (name: string, row) => (
        <Space size={6}>
          <Text strong>{name}</Text>
          {row.id === selectedWorkflowId && (
            <Tag color="blue">
              {t("workbuddy.workflows.definitions.selected")}
            </Tag>
          )}
        </Space>
      ),
    },
    {
      title: t("workbuddy.workflows.definitions.column.description"),
      dataIndex: "description",
      key: "description",
      width: 280,
      render: (value: string | null) =>
        value?.trim() ? value : <Text type="secondary">—</Text>,
    },
    {
      title: t("workbuddy.workflows.definitions.column.status"),
      dataIndex: "status",
      key: "status",
      width: 120,
      render: (status: string) => (
        <Tag color={status === "active" ? "green" : "default"}>
          {statusLabel(t, "workbuddy.workflows.definitions.status", status)}
        </Tag>
      ),
    },
    {
      title: t("workbuddy.workflows.definitions.column.activeVersion"),
      key: "active_version",
      width: 160,
      render: (_value, row) =>
        isWorkflowRecord(row) ? (
          row.active_version_id ? (
            <Text code>{row.active_version_id.slice(0, 8)}</Text>
          ) : (
            <Text type="secondary">
              {t("workbuddy.workflows.definitions.notPublished")}
            </Text>
          )
        ) : (
          <Text type="secondary">—</Text>
        ),
    },
    {
      title: t("workbuddy.workflows.definitions.column.revision"),
      key: "revision",
      width: 100,
      render: (_value, row) =>
        isWorkflowRecord(row) ? row.revision : <Text type="secondary">—</Text>,
    },
    {
      title: t("workbuddy.workflows.definitions.column.updatedAt"),
      dataIndex: "updated_at",
      key: "updated_at",
      width: 170,
      render: (value: number | null) =>
        value
          ? formatServerDateTime(value, timeZone)
          : t("workbuddy.workflows.definitions.noVersion"),
    },
    {
      title: t("workbuddy.workflows.definitions.column.actions"),
      key: "actions",
      width: 220,
      fixed: "right",
      render: (_value, row) => (
        <Space size={4}>
          <Button
            type="link"
            size="small"
            onClick={() => {
              onSelectWorkflow(row.id);
              onOpenVersions(row.id);
            }}
          >
            {t("workbuddy.workflows.definitions.viewVersions")}
          </Button>
          {isWorkflowRecord(row) && (
            <Button
              type="link"
              size="small"
              icon={<Pencil size={13} />}
              loading={loadingDefinition === row.id}
              onClick={() => {
                onSelectWorkflow(row.id);
                void openEdit(row);
              }}
            >
              {t("workbuddy.workflows.definitions.edit")}
            </Button>
          )}
        </Space>
      ),
    },
  ];

  const { data, loading, error } = resource;
  const failed = error !== null && error !== undefined;
  const blocked = failed || loading || data.length === 0;

  return (
    <div className={styles.panel}>
      <TabPanelHeader
        icon={<FileCog size={16} />}
        title={t("workbuddy.workflows.definitions.title")}
        description={t("workbuddy.workflows.definitions.description")}
        actions={
          <Space size={8}>
            <Button
              size="small"
              icon={<RefreshCw size={14} />}
              onClick={() => void resource.reload()}
            >
              {t("common.refresh")}
            </Button>
            <Button size="small" onClick={() => setWizardOpen(true)}>
              {t("workbuddy.workflows.wizard.title")}
            </Button>
            <Button size="small" type="primary" onClick={openCreate}>
              {t("workbuddy.workflows.definitions.create")}
            </Button>
          </Space>
        }
      />

      {blocked ? (
        <ResourceState
          resource={resource}
          isEmpty={!failed && !loading && data.length === 0}
          loadingLabel={t("common.loading")}
          errorTitle={t("workbuddy.workflows.definitions.loadFailed")}
          errorFallback={t("workbuddy.workflows.state.errorFallback")}
          unavailableTitle={t("workbuddy.workflows.state.unavailableTitle")}
          unavailableHint={t("workbuddy.workflows.state.unavailableHint")}
          emptyTitle={t("workbuddy.workflows.definitions.emptyTitle")}
          emptyHint={t("workbuddy.workflows.definitions.emptyHint")}
          emptyActionLabel={t("workbuddy.workflows.definitions.create")}
          onEmptyAction={openCreate}
        />
      ) : (
        <ResizableTable<WorkflowListItem>
          rowKey="id"
          size="small"
          columns={columns}
          dataSource={data}
          pagination={false}
          scroll={{ x: 1180 }}
          storageKey="workbuddy-workflows-definitions"
          onRow={(row) => ({
            onClick: () => onSelectWorkflow(row.id),
            style: { cursor: "pointer" },
          })}
        />
      )}

      <CreateWizard
        open={wizardOpen}
        onClose={() => setWizardOpen(false)}
        onCreated={() => {
          setWizardOpen(false);
          void resource.reload();
        }}
      />

      <DefinitionEditorModal
        open={target !== null}
        target={target}
        onClose={() => setTarget(null)}
        onSaved={onSaved}
        onStale={() => void resource.reload()}
      />
    </div>
  );
}
