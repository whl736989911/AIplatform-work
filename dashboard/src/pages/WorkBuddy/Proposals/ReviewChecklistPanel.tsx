/**
 * 建议审核清单 — the review checklist a proposal carries.
 *
 * Every row is computed from fields the proposals slice actually returns:
 * recorded independent reviews against ``required_approvals``, the replay-only
 * shadow proof, the last canary gate verdict, and the baseline/risk flags. A
 * field the payload does not carry is shown as absent (未提供) — no checklist is
 * invented in its place, and no gate is marked passed without evidence.
 */

import { Table, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import {
  AlertTriangle,
  CheckCircle2,
  CircleHelp,
  Info,
  XCircle,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import type { ImprovementProposal } from "../../../api/modules/workbuddyProposals";
import styles from "./index.module.less";

const { Text } = Typography;

type ChecklistState = "passed" | "failed" | "absent" | "info";

interface ChecklistRow {
  key: string;
  label: string;
  state: ChecklistState;
  detail: string;
}

function StateTag({ state }: { state: ChecklistState }) {
  const { t } = useTranslation();
  if (state === "passed") {
    return (
      <Tag icon={<CheckCircle2 size={12} />} color="green">
        {t("workbuddy.proposals.checklist.state.passed")}
      </Tag>
    );
  }
  if (state === "failed") {
    return (
      <Tag icon={<XCircle size={12} />} color="red">
        {t("workbuddy.proposals.checklist.state.failed")}
      </Tag>
    );
  }
  if (state === "absent") {
    return (
      <Tag icon={<CircleHelp size={12} />} color="default">
        {t("workbuddy.proposals.checklist.state.absent")}
      </Tag>
    );
  }
  return (
    <Tag icon={<Info size={12} />} color="blue">
      {t("workbuddy.proposals.checklist.state.info")}
    </Tag>
  );
}

export default function ReviewChecklistPanel({
  proposal,
}: {
  proposal: ImprovementProposal;
}) {
  const { t } = useTranslation();

  const reviews = proposal.reviews;
  const approvals = reviews?.filter((review) => review.decision === "approved")
    .length;
  const rejections = reviews?.filter((review) => review.decision === "rejected")
    .length;

  const rows: ChecklistRow[] = [
    {
      key: "reviews",
      label: t("workbuddy.proposals.checklist.reviews"),
      state:
        reviews === undefined
          ? "absent"
          : rejections && rejections > 0
          ? "failed"
          : (approvals ?? 0) >= proposal.required_approvals
          ? "passed"
          : "failed",
      detail:
        reviews === undefined
          ? t("workbuddy.proposals.checklist.reviewsHidden")
          : rejections && rejections > 0
          ? t("workbuddy.proposals.checklist.reviewsRejected", {
              count: rejections,
            })
          : t("workbuddy.proposals.checklist.reviewsCount", {
              approved: approvals ?? 0,
              required: proposal.required_approvals,
            }),
    },
    {
      key: "shadow",
      label: t("workbuddy.proposals.checklist.shadow"),
      state:
        proposal.shadow_proof === undefined
          ? "absent"
          : proposal.shadow_proof.complete
          ? "passed"
          : "failed",
      detail:
        proposal.shadow_proof === undefined
          ? t("workbuddy.proposals.checklist.shadowAbsent")
          : proposal.shadow_proof.complete
          ? t("workbuddy.proposals.checklist.shadowComplete", {
              runs: proposal.shadow_proof.settled_runs,
            })
          : t("workbuddy.proposals.checklist.shadowIncomplete", {
              runs: proposal.shadow_proof.settled_runs,
              failures: proposal.shadow_proof.failures.join(", ") || "—",
            }),
    },
    {
      key: "gate",
      label: t("workbuddy.proposals.checklist.gate"),
      state:
        proposal.last_gate === undefined
          ? "absent"
          : proposal.last_gate.passed
          ? "passed"
          : "failed",
      detail:
        proposal.last_gate === undefined
          ? t("workbuddy.proposals.checklist.gateAbsent")
          : proposal.last_gate.passed
          ? t("workbuddy.proposals.checklist.gatePassed", {
              days: proposal.last_gate.full_days,
            })
          : t("workbuddy.proposals.checklist.gateFailed", {
              failures: proposal.last_gate.failures.join(", ") || "—",
            }),
    },
    {
      key: "baseline",
      label: t("workbuddy.proposals.checklist.baseline"),
      state: proposal.stale ? "failed" : "passed",
      detail: proposal.stale
        ? t("workbuddy.proposals.checklist.baselineStale")
        : t("workbuddy.proposals.checklist.baselineCurrent", {
            revision: proposal.workflow_revision,
          }),
    },
    {
      key: "risk",
      label: t("workbuddy.proposals.checklist.risk"),
      state: "info",
      detail: t("workbuddy.proposals.checklist.riskDetail", {
        risk: t(`workbuddy.proposals.risk.${proposal.risk_level}`),
        approvals: proposal.required_approvals,
        pii: t(
          proposal.pii_involved
            ? "workbuddy.proposals.checklist.piiYes"
            : "workbuddy.proposals.checklist.piiNo",
        ),
        shadow: t(
          proposal.requires_manual_shadow
            ? "workbuddy.proposals.checklist.shadowRequired"
            : "workbuddy.proposals.checklist.shadowNotRequired",
        ),
      }),
    },
    ...(proposal.canary_ratio_bp !== null
      ? [
          {
            key: "canary",
            label: t("workbuddy.proposals.checklist.canary"),
            state: "info" as ChecklistState,
            detail: t("workbuddy.proposals.checklist.canaryDetail", {
              ratio: (proposal.canary_ratio_bp / 100).toFixed(2),
            }),
          },
        ]
      : []),
  ];

  const columns: ColumnsType<ChecklistRow> = [
    {
      title: t("workbuddy.proposals.checklist.columns.item"),
      dataIndex: "label",
      key: "label",
      width: 220,
    },
    {
      title: t("workbuddy.proposals.checklist.columns.state"),
      dataIndex: "state",
      key: "state",
      width: 140,
      render: (state: ChecklistState) => <StateTag state={state} />,
    },
    {
      title: t("workbuddy.proposals.checklist.columns.detail"),
      dataIndex: "detail",
      key: "detail",
      render: (detail: string, row) => (
        <span
          className={row.state === "failed" ? styles.checklistFail : undefined}
        >
          {detail}
        </span>
      ),
    },
  ];

  return (
    <section className={styles.sectionBlock}>
      <div className={styles.sectionTitleRow}>
        <h3 className={styles.sectionTitle}>
          {t("workbuddy.proposals.checklist.title")}
        </h3>
        <Tag icon={<AlertTriangle size={12} />} color="orange">
          {t("workbuddy.proposals.checklist.source")}
        </Tag>
      </div>
      <Text type="secondary" className={styles.sectionHint}>
        {t("workbuddy.proposals.checklist.desc")}
      </Text>
      <Table
        columns={columns}
        dataSource={rows}
        rowKey="key"
        size="small"
        pagination={false}
        scroll={{ x: 760 }}
      />
    </section>
  );
}
