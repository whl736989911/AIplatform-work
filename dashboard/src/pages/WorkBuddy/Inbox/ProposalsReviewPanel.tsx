/**
 * WorkBuddy inbox → "proposals awaiting my review".
 *
 * Lists the proposals that are still waiting on a reviewer and opens the
 * existing proposals detail panel for the decision itself, so the review
 * rules (independent reviewer, CAS promotion) live in exactly one place.
 *
 * Scope caveat, deliberately visible in the header: the proposals list route
 * only filters by workflow and status — it has no "assigned to me" parameter —
 * so this pane asks for the reviewable status and shows every such proposal of
 * the caller's tenant rather than claiming a reviewer-scoped result.
 */

import { useCallback, useState } from "react";
import { Alert, Button, Space, Spin, Tag, Tooltip } from "antd";
import type { ColumnsType } from "antd/es/table";
import { ClipboardCheck, RefreshCw } from "lucide-react";
import { useTranslation } from "react-i18next";
import { ResizableTable } from "../../../components/ResizableTable";
import { useServerTimezone } from "../../../hooks/useServerTimezone";
import { formatServerDateTime } from "../../../utils/formatMessageTime";
import {
  isProposalsUnavailableError,
  workbuddyProposalsApi,
  type ImprovementProposal,
  type ProposalStatus,
} from "../../../api/modules/workbuddyProposals";
import { TabPanelHeader } from "../../Settings/AdvancedSettings/TabPanelHeader";
import {
  ResourceState,
  statusLabel,
  useWorkBuddyResource,
} from "../Workflows/consoleState";
import ProposalDetailPanel from "../Proposals/ProposalDetailPanel";
import { describeApiError } from "../Proposals/helpers";
import styles from "./index.module.less";

/** The one status that means "a reviewer still has to look at this". */
const REVIEWABLE_STATUS: ProposalStatus = "under_review";

/** Tag colours for the proposals status/risk codes (mirrors the proposals page). */
const STATUS_TAG_COLORS: Record<string, string> = {
  approved: "green",
  applied: "green",
  rejected: "red",
  aborted: "red",
  stale: "red",
  shadow: "blue",
  canary: "blue",
};

const RISK_TAG_COLORS: Record<string, string> = {
  low: "green",
  high: "red",
};

export default function ProposalsReviewPanel() {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  const [detail, setDetail] = useState<ImprovementProposal | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);

  const proposals = useWorkBuddyResource<ImprovementProposal[]>(
    [],
    () =>
      workbuddyProposalsApi.listProposals({ status: REVIEWABLE_STATUS }),
    [],
  );
  const setProposalRows = proposals.setData;

  const openDetail = useCallback(
    async (proposalId: string) => {
      setDetailLoading(true);
      setDetailError(null);
      try {
        setDetail(await workbuddyProposalsApi.getProposal(proposalId));
      } catch (error) {
        setDetail(null);
        setDetailError(
          isProposalsUnavailableError(error)
            ? t("workbuddy.shared.notMergedHint")
            : describeApiError(error, t("workbuddy.shared.loadFailed"), t),
        );
      } finally {
        setDetailLoading(false);
      }
    },
    [t],
  );

  // A decided proposal is no longer waiting on anyone: keep the detail open so
  // the outcome is visible, but drop the row this pane defines as "reviewable".
  const applyUpdate = useCallback(
    (next: ImprovementProposal) => {
      setDetail(next);
      setProposalRows((prev) =>
        next.status === REVIEWABLE_STATUS
          ? prev.map((item) =>
              item.proposal_id === next.proposal_id ? next : item,
            )
          : prev.filter((item) => item.proposal_id !== next.proposal_id),
      );
    },
    [setProposalRows],
  );

  const columns: ColumnsType<ImprovementProposal> = [
    {
      title: t("workbuddy.proposals.list.columns.proposal"),
      dataIndex: "proposal_id",
      key: "proposal_id",
      width: 140,
      render: (value: string) => (
        <Tooltip title={value}>
          <span className={styles.mono}>{value.slice(0, 8)}</span>
        </Tooltip>
      ),
    },
    {
      title: t("workbuddy.proposals.list.columns.workflow"),
      dataIndex: "workflow_id",
      key: "workflow_id",
      width: 140,
      render: (value: string) => (
        <Tooltip title={value}>
          <span className={styles.mono}>{value.slice(0, 8)}</span>
        </Tooltip>
      ),
    },
    {
      title: t("workbuddy.proposals.list.columns.status"),
      dataIndex: "status",
      key: "status",
      width: 160,
      render: (value: string, row) => (
        <Space size={4} wrap>
          <Tag color={STATUS_TAG_COLORS[value]}>
            {statusLabel(t, "workbuddy.proposals.status", value)}
          </Tag>
          {row.stale && (
            <Tag color="red">{t("workbuddy.proposals.detail.staleTag")}</Tag>
          )}
        </Space>
      ),
    },
    {
      title: t("workbuddy.proposals.list.columns.risk"),
      dataIndex: "risk_level",
      key: "risk_level",
      width: 110,
      render: (value: string) => (
        <Tag color={RISK_TAG_COLORS[value]}>
          {statusLabel(t, "workbuddy.proposals.risk", value)}
        </Tag>
      ),
    },
    {
      title: t("workbuddy.proposals.list.columns.approvals"),
      key: "approvals",
      width: 120,
      // The router strips ``reviews`` for non-admin readers; an absent array is
      // "not disclosed to this caller", not "zero approvals".
      render: (_value, row) =>
        row.reviews === undefined
          ? "—"
          : t("workbuddy.proposals.list.approvalsValue", {
              approved: row.reviews.filter(
                (review) => review.decision === "approved",
              ).length,
              required: row.required_approvals,
            }),
    },
    {
      title: t("workbuddy.proposals.list.columns.summary"),
      dataIndex: "change_summary",
      key: "change_summary",
      render: (value: string) => value?.trim() || "—",
    },
    {
      title: t("workbuddy.proposals.list.columns.updatedAt"),
      dataIndex: "updated_at",
      key: "updated_at",
      width: 180,
      render: (value: number) => formatServerDateTime(value, timeZone),
    },
    {
      title: t("common.actions"),
      key: "actions",
      width: 100,
      fixed: "right",
      render: (_value, row) => (
        <Button
          type="link"
          size="small"
          onClick={() => void openDetail(row.proposal_id)}
        >
          {t("common.view")}
        </Button>
      ),
    },
  ];

  const failed = proposals.error !== null && proposals.error !== undefined;
  const blocked = failed || proposals.loading || proposals.data.length === 0;

  return (
    <div className={styles.panel}>
      <TabPanelHeader
        icon={<ClipboardCheck size={16} />}
        title={t("workbuddy.inbox.proposalsTitle")}
        description={t("workbuddy.inbox.proposalsScope")}
        actions={
          <Button
            size="small"
            icon={<RefreshCw size={14} />}
            onClick={() => void proposals.reload()}
          >
            {t("common.refresh")}
          </Button>
        }
      />

      {blocked ? (
        <ResourceState
          resource={proposals}
          isEmpty={!failed && !proposals.loading && proposals.data.length === 0}
          loadingLabel={t("common.loading")}
          errorTitle={t("workbuddy.shared.loadFailed")}
          errorFallback={t("workbuddy.shared.loadFailed")}
          unavailableTitle={t("workbuddy.shared.notMergedTitle")}
          unavailableHint={t("workbuddy.shared.notMergedHint")}
          emptyTitle={t("workbuddy.inbox.proposalsEmpty")}
          emptyHint={t("workbuddy.inbox.proposalsEmptyHint")}
        />
      ) : (
        <ResizableTable<ImprovementProposal>
          rowKey="proposal_id"
          size="middle"
          columns={columns}
          dataSource={proposals.data}
          pagination={false}
          scroll={{ x: 1080 }}
          storageKey="workbuddy-inbox-proposals-table-widths"
          onRow={(row) => ({
            onClick: () => void openDetail(row.proposal_id),
            style: { cursor: "pointer" },
          })}
          rowClassName={(row) =>
            row.proposal_id === detail?.proposal_id ? styles.selectedRow : ""
          }
        />
      )}

      {detailError && (
        <Alert
          className={styles.notice}
          type="error"
          showIcon
          message={detailError}
        />
      )}

      {detailLoading && (
        <div className={styles.centered}>
          <Spin />
        </div>
      )}

      {detail && !detailLoading && (
        <ProposalDetailPanel
          proposal={detail}
          onUpdated={applyUpdate}
          onReload={() => void openDetail(detail.proposal_id)}
        />
      )}
    </div>
  );
}
