/**
 * WorkBuddy workbench (/workbuddy) — the console's landing page.
 *
 * Three summaries of what the caller can act on right now: their pending
 * approvals, their workflows, and their recent runs. Each one owns its own
 * request and its own load state, so a backend slice that is not merged (404)
 * or a single failing endpoint degrades exactly one block and never the page.
 */

import { useMemo } from "react";
import { Button, Space } from "antd";
import { Activity, Inbox, Workflow } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import PageShell from "../../../layouts/PageShell";
import {
  workbuddyRuntimeApi,
  type ApprovalRequest,
  type Execution,
} from "../../../api/modules/workbuddyRuntime";
import {
  workbuddyWorkflowsApi,
  type WorkflowListItem,
} from "../../../api/modules/workbuddyWorkflows";
import { useWorkBuddyResource } from "../Workflows/consoleState";
import PendingApprovalsBlock from "./PendingApprovalsBlock";
import MyWorkflowsBlock from "./MyWorkflowsBlock";
import RecentRunsBlock from "./RecentRunsBlock";
import styles from "./index.module.less";

export default function WorkBuddyHomePage() {
  const { t } = useTranslation();
  const navigate = useNavigate();

  const approvals = useWorkBuddyResource<ApprovalRequest[]>(
    [],
    () =>
      workbuddyRuntimeApi.listApprovalRequests({
        scope: "self",
        status: "pending",
        limit: 5,
      }),
    [],
  );

  const workflows = useWorkBuddyResource<WorkflowListItem[]>(
    [],
    () => workbuddyWorkflowsApi.listWorkflows(),
    [],
  );

  const executions = useWorkBuddyResource<Execution[]>(
    [],
    () => workbuddyRuntimeApi.listExecutions({ scope: "self", limit: 5 }),
    [],
  );

  const workflowNames = useMemo<Record<string, string>>(
    () => Object.fromEntries(workflows.data.map((row) => [row.id, row.name])),
    [workflows.data],
  );

  return (
    <PageShell
      title={t("workbuddy.home.title")}
      subtitle={t("workbuddy.home.subtitle")}
      actions={
        <Space size={8} wrap>
          <Button
            size="small"
            icon={<Inbox size={14} />}
            onClick={() => navigate("/workbuddy/inbox")}
          >
            {t("workbuddy.home.openInbox")}
          </Button>
          <Button
            size="small"
            icon={<Workflow size={14} />}
            onClick={() => navigate("/workbuddy/workflows")}
          >
            {t("workbuddy.home.openWorkflows")}
          </Button>
          <Button
            size="small"
            icon={<Activity size={14} />}
            onClick={() => navigate("/workbuddy/runs")}
          >
            {t("workbuddy.home.openRuns")}
          </Button>
        </Space>
      }
    >
      <div className={styles.grid}>
        <PendingApprovalsBlock resource={approvals} />
        <MyWorkflowsBlock resource={workflows} />
        <RecentRunsBlock resource={executions} workflowNames={workflowNames} />
      </div>
    </PageShell>
  );
}
