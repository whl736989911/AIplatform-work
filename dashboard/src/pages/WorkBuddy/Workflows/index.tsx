/**
 * WorkBuddy console → Workflows (/workbuddy/workflows).
 *
 * Tab shell over the workflow slice: definitions, immutable versions,
 * executions, trigger registrations and asynchronous jobs. The workflow list is
 * loaded once here and shared with every tab's selector so "the selected
 * workflow" cannot drift between panels.
 */

import { useCallback, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Alert, Button } from "antd";
import {
  FileCog,
  History,
  Lightbulb,
  ListChecks,
  PlayCircle,
  RefreshCw,
  Workflow,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import PageShell from "../../../layouts/PageShell";
import TabBar, { type TabBarItem } from "../../../components/TabLabel/TabBar";
import {
  isWorkflowRecord,
  workbuddyWorkflowsApi,
  type WorkflowListItem,
} from "../../../api/modules/workbuddyWorkflows";
import { useWorkBuddyResource, type WorkflowOption } from "./consoleState";
import DefinitionsPanel from "./DefinitionsPanel";
import VersionsPanel from "./VersionsPanel";
import ExecutionsPanel from "./ExecutionsPanel";
import TriggersPanel from "./TriggersPanel";
import JobsPanel from "./JobsPanel";
import WorkflowProposalsPanel from "./WorkflowProposalsPanel";
import styles from "./index.module.less";

type WorkflowTabKey =
  | "definitions"
  | "versions"
  | "executions"
  | "proposals"
  | "triggers"
  | "jobs";

const TAB_ITEMS: readonly TabBarItem<WorkflowTabKey>[] = [
  {
    key: "definitions",
    labelKey: "workbuddy.workflows.tab.definitions",
    icon: FileCog,
  },
  {
    key: "versions",
    labelKey: "workbuddy.workflows.tab.versions",
    icon: History,
  },
  {
    key: "executions",
    labelKey: "workbuddy.workflows.tab.executions",
    icon: PlayCircle,
  },
  {
    key: "proposals",
    labelKey: "workbuddy.workflows.tab.proposals",
    icon: Lightbulb,
  },
  {
    key: "triggers",
    labelKey: "workbuddy.workflows.tab.triggers",
    icon: Workflow,
  },
  { key: "jobs", labelKey: "workbuddy.workflows.tab.jobs", icon: ListChecks },
];

const TAB_KEYS: Record<WorkflowTabKey, true> = {
  definitions: true,
  versions: true,
  executions: true,
  proposals: true,
  triggers: true,
  jobs: true,
};

function isWorkflowTab(value: string | null): value is WorkflowTabKey {
  return value !== null && value in TAB_KEYS;
}

export default function WorkflowsPage() {
  const { t } = useTranslation();
  const [searchParams, setSearchParams] = useSearchParams();
  // The selected workflow lives in the URL (`?workflow=`), so the dashboard and
  // the runs page can link straight into the workflow they point at instead of
  // dropping the reader on an unselected list.
  const [selectedWorkflowId, setSelectedWorkflowId] = useState<string | null>(
    () => searchParams.get("workflow"),
  );

  const selectWorkflow = useCallback(
    (id: string | null) => {
      setSelectedWorkflowId(id);
      const next = new URLSearchParams(searchParams);
      if (id === null) next.delete("workflow");
      else next.set("workflow", id);
      setSearchParams(next, { replace: true });
    },
    [searchParams, setSearchParams],
  );
  const workflows = useWorkBuddyResource<WorkflowListItem[]>(
    [],
    () => workbuddyWorkflowsApi.listWorkflows(),
    [],
  );

  const requestedTab = searchParams.get("tab");
  const activeTab: WorkflowTabKey = isWorkflowTab(requestedTab)
    ? requestedTab
    : "definitions";

  const selectTab = useCallback(
    (key: WorkflowTabKey) => {
      const next = new URLSearchParams(searchParams);
      if (key === "definitions") next.delete("tab");
      else next.set("tab", key);
      setSearchParams(next, { replace: true });
    },
    [searchParams, setSearchParams],
  );

  const options = useMemo<WorkflowOption[]>(
    () => workflows.data.map((row) => ({ value: row.id, label: row.name })),
    [workflows.data],
  );

  const reloadWorkflows = workflows.reload;

  // Members only ever receive published summaries; a list that contains at
  // least one manager record means the caller can also edit and publish.
  const memberOnlyView =
    workflows.data.length > 0 && !workflows.data.some(isWorkflowRecord);

  return (
    <PageShell.Tabbed
      title={t("workbuddy.workflows.title")}
      subtitle={t("workbuddy.workflows.subtitle")}
      actions={
        <Button
          size="small"
          icon={<RefreshCw size={14} />}
          onClick={() => void reloadWorkflows()}
        >
          {t("common.refresh")}
        </Button>
      }
      tabBar={
        <TabBar tabs={TAB_ITEMS} activeKey={activeTab} onChange={selectTab} />
      }
    >
      {memberOnlyView && (
        <Alert
          type="info"
          showIcon
          className={styles.notice}
          message={t("workbuddy.workflows.memberNoticeTitle")}
          description={t("workbuddy.workflows.memberNoticeHint")}
        />
      )}

      {activeTab === "definitions" && (
        <DefinitionsPanel
          resource={workflows}
          selectedWorkflowId={selectedWorkflowId}
          onSelectWorkflow={selectWorkflow}
          onOpenVersions={(id) => {
            selectWorkflow(id);
            selectTab("versions");
          }}
        />
      )}
      {activeTab === "versions" && (
        <VersionsPanel
          workflowId={selectedWorkflowId}
          onSelectWorkflow={selectWorkflow}
          options={options}
          optionsLoading={workflows.loading}
          onWorkflowChanged={reloadWorkflows}
        />
      )}
      {activeTab === "executions" && (
        <ExecutionsPanel
          workflowId={selectedWorkflowId}
          onSelectWorkflow={selectWorkflow}
          options={options}
          optionsLoading={workflows.loading}
          onExecutionAccepted={reloadWorkflows}
        />
      )}
      {activeTab === "proposals" && (
        <WorkflowProposalsPanel
          workflowId={selectedWorkflowId}
          onSelectWorkflow={selectWorkflow}
          options={options}
          optionsLoading={workflows.loading}
        />
      )}
      {activeTab === "triggers" && (
        <TriggersPanel
          workflowId={selectedWorkflowId}
          onSelectWorkflow={selectWorkflow}
          options={options}
          optionsLoading={workflows.loading}
        />
      )}
      {activeTab === "jobs" && <JobsPanel />}
    </PageShell.Tabbed>
  );
}
