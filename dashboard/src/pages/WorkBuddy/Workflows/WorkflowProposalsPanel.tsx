/**
 * WorkBuddy console → Workflows → improvements tab.
 *
 * One workflow's improvement proposals, next to its versions and runs, so the
 * workflow page is the hub a member works from instead of a second, drifting
 * copy of the proposals page.  The detail view is the shared
 * {@link ProposalDetailPanel}: reading, deciding and promoting a proposal keeps
 * exactly one implementation in the console.
 */

import { useCallback, useState } from "react";
import { Alert, Button, Drawer, Space, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { Lightbulb, RefreshCw } from "lucide-react";
import { useTranslation } from "react-i18next";
import { ResizableTable } from "../../../components/ResizableTable";
import { formatServerDateTime } from "../../../utils/formatMessageTime";
import { useServerTimezone } from "../../../hooks/useServerTimezone";
import {
  workbuddyProposalsApi,
  type ImprovementProposal,
} from "../../../api/modules/workbuddyProposals";
import { TabPanelHeader } from "../../Settings/AdvancedSettings/TabPanelHeader";
import ProposalDetailPanel from "../Proposals/ProposalDetailPanel";
import {
  ResourceState,
  WorkflowPicker,
  useWorkBuddyResource,
  type WorkflowOption,
} from "./consoleState";
import styles from "./index.module.less";

const { Text } = Typography;

/** Phase colouring mirrors the proposals page's vocabulary. */
function statusColor(status: string): string {
  if (status === "applied" || status === "approved") return "green";
  if (status === "rejected" || status === "aborted" || status === "stale")
    return "red";
  if (status === "canary" || status === "shadow") return "blue";
  return "default";
}

function riskColor(risk: string): string {
  if (risk === "high") return "red";
  if (risk === "medium") return "orange";
  return "green";
}

export default function WorkflowProposalsPanel({
  workflowId,
  onSelectWorkflow,
  options,
  optionsLoading,
}: {
  workflowId: string | null;
  onSelectWorkflow: (id: string | null) => void;
  options: WorkflowOption[];
  optionsLoading: boolean;
}) {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  const [openProposal, setOpenProposal] = useState<ImprovementProposal | null>(
    null,
  );

  const proposals = useWorkBuddyResource<ImprovementProposal[]>(
    [],
    () =>
      workbuddyProposalsApi.listProposals({
        workflow_id: workflowId ?? undefined,
        limit: 100,
      }),
    [workflowId],
  );

  const closeDetail = useCallback(() => setOpenProposal(null), []);

  const columns: ColumnsType<ImprovementProposal> = [
    {
      title: t("workbuddy.proposals.list.columns.status"),
      dataIndex: "status",
      width: 130,
      render: (status: string) => (
        <Tag color={statusColor(status)}>
          {t(`workbuddy.proposals.status.${status}`)}
        </Tag>
      ),
    },
    {
      title: t("workbuddy.proposals.list.columns.risk"),
      dataIndex: "risk_level",
      width: 110,
      render: (risk: string) => (
        <Tag color={riskColor(risk)}>{t(`workbuddy.proposals.risk.${risk}`)}</Tag>
      ),
    },
    {
      title: t("workbuddy.proposals.list.columns.summary"),
      dataIndex: "change_summary",
      render: (summary: string | null) =>
        summary ? <Text>{summary}</Text> : <Text type="secondary">—</Text>,
    },
    {
      title: t("workbuddy.proposals.list.columns.updatedAt"),
      dataIndex: "updated_at",
      key: "updated_at",
      width: 180,
      render: (value: number) => formatServerDateTime(value, timeZone),
    },
    {
      title: "",
      key: "actions",
      width: 90,
      render: (_value, row) => (
        <Button size="small" onClick={() => setOpenProposal(row)}>
          {t("workbuddy.shared.open")}
        </Button>
      ),
    },
  ];

  if (!workflowId) {
    return (
      <div className={styles.panel}>
        <TabPanelHeader
          icon={<Lightbulb size={16} />}
          title={t("workbuddy.workflows.proposals.title")}
          description={t("workbuddy.workflows.proposals.empty")}
          actions={
            <WorkflowPicker
              options={options}
              value={workflowId}
              onChange={onSelectWorkflow}
              loading={optionsLoading}
            />
          }
        />
        <Alert
          type="info"
          showIcon
          message={t("workbuddy.workflows.state.noSelectionTitle")}
          description={t("workbuddy.workflows.state.noSelectionHint")}
        />
      </div>
    );
  }

  const failed = proposals.error !== null && proposals.error !== undefined;
  const blocked = failed || proposals.loading || proposals.data.length === 0;

  return (
    <div className={styles.panel}>
      <TabPanelHeader
        icon={<Lightbulb size={16} />}
        title={t("workbuddy.workflows.proposals.title")}
        description={t("workbuddy.workflows.proposals.empty")}
        actions={
          <Space size={8}>
            <WorkflowPicker
              options={options}
              value={workflowId}
              onChange={onSelectWorkflow}
              loading={optionsLoading}
            />
            <Button
              size="small"
              icon={<RefreshCw size={14} />}
              onClick={() => void proposals.reload()}
            >
              {t("common.refresh")}
            </Button>
          </Space>
        }
      />

      {blocked ? (
        <ResourceState
          resource={proposals}
          isEmpty={!failed && !proposals.loading && proposals.data.length === 0}
          loadingLabel={t("common.loading")}
          errorTitle={t("workbuddy.shared.loadFailed")}
          errorFallback={t("workbuddy.workflows.state.errorFallback")}
          unavailableTitle={t("workbuddy.shared.notMergedTitle")}
          unavailableHint={t("workbuddy.shared.notMergedHint")}
          emptyTitle={t("workbuddy.workflows.proposals.empty")}
          emptyHint={t("workbuddy.proposals.subtitle")}
        />
      ) : (
        <ResizableTable<ImprovementProposal>
          rowKey="proposal_id"
          size="small"
          columns={columns}
          dataSource={proposals.data}
          pagination={{ pageSize: 20, hideOnSinglePage: true }}
        />
      )}

      <Drawer
        open={openProposal !== null}
        onClose={closeDetail}
        width={720}
        destroyOnHidden
        title={t("workbuddy.proposals.detail.title")}
      >
        {openProposal && (
          <ProposalDetailPanel
            proposal={openProposal}
            onUpdated={setOpenProposal}
            onReload={() => void proposals.reload()}
          />
        )}
      </Drawer>
    </div>
  );
}
