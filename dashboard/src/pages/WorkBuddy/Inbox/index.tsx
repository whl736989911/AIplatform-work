/**
 * WorkBuddy console → Inbox (/workbuddy/inbox).
 *
 * The one surface for "something is waiting on me": approvals addressed to the
 * caller, questions a run asked them (the ``ask`` node's form), and proposals
 * that still need an independent review. Every pane is an existing panel — the
 * approvals pane *is* Approvals/ApprovalInboxPanel (it already defaults to
 * scope=self, status=pending, i.e. exactly "mine, still pending"), the questions
 * pane lists the caller's own input requests, and the proposals pane reuses the
 * proposals detail/review panel through the one list this page adds. No panel
 * logic is duplicated here.
 *
 * The inbox is meant to have one more pane — output review (A-09) — which does
 * not exist yet: this page shows the three panes it has and invents nothing.
 */

import { useCallback } from "react";
import { useSearchParams } from "react-router-dom";
import { ClipboardCheck, Gavel, PenLine } from "lucide-react";
import { useTranslation } from "react-i18next";
import PageShell from "../../../layouts/PageShell";
import TabBar, { type TabBarItem } from "../../../components/TabLabel/TabBar";
import ApprovalInboxPanel from "../Approvals/ApprovalInboxPanel";
import InputInboxPanel from "./InputInboxPanel";
import ProposalsReviewPanel from "./ProposalsReviewPanel";

type InboxTabKey = "approvals" | "inputs" | "proposals";

const TAB_ITEMS: readonly TabBarItem<InboxTabKey>[] = [
  {
    key: "approvals",
    labelKey: "workbuddy.inbox.approvalsTitle",
    icon: Gavel,
  },
  {
    key: "inputs",
    labelKey: "workbuddy.inbox.inputsTitle",
    icon: PenLine,
  },
  {
    key: "proposals",
    labelKey: "workbuddy.inbox.proposalsTitle",
    icon: ClipboardCheck,
  },
];

const TAB_KEYS: Record<InboxTabKey, true> = {
  approvals: true,
  inputs: true,
  proposals: true,
};

function isInboxTab(value: string | null): value is InboxTabKey {
  return value !== null && value in TAB_KEYS;
}

export default function InboxPage() {
  const { t } = useTranslation();
  const [searchParams, setSearchParams] = useSearchParams();

  const requestedTab = searchParams.get("tab");
  const activeTab: InboxTabKey = isInboxTab(requestedTab)
    ? requestedTab
    : "approvals";

  const selectTab = useCallback(
    (key: InboxTabKey) => {
      const next = new URLSearchParams(searchParams);
      if (key === "approvals") next.delete("tab");
      else next.set("tab", key);
      setSearchParams(next, { replace: true });
    },
    [searchParams, setSearchParams],
  );

  return (
    <PageShell.Tabbed
      title={t("workbuddy.inbox.title")}
      subtitle={t("workbuddy.inbox.subtitle")}
      tabBar={
        <TabBar tabs={TAB_ITEMS} activeKey={activeTab} onChange={selectTab} />
      }
    >
      {activeTab === "approvals" && <ApprovalInboxPanel />}
      {activeTab === "inputs" && <InputInboxPanel />}
      {activeTab === "proposals" && <ProposalsReviewPanel />}
    </PageShell.Tabbed>
  );
}
