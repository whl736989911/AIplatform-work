/**
 * WorkBuddy console → Inbox (/workbuddy/inbox).
 *
 * The one surface for "something is waiting on me": approvals addressed to the
 * caller, questions a run asked them (the ``ask`` node's form), outputs they
 * were asked to review, and proposals that still need an independent review.
 * Every pane is an existing panel — the approvals pane *is*
 * Approvals/ApprovalInboxPanel (it already defaults to scope=self,
 * status=pending, i.e. exactly "mine, still pending"), the questions pane lists
 * the caller's own input requests, the review pane lists the output reviews they
 * are a reviewer of, and the proposals pane reuses the proposals detail/review
 * panel through the one list this page adds. No panel logic is duplicated here.
 *
 * Nothing on this page is a gate: an approval is a decision a run is waiting on,
 * while a question feeds a run and an output review is about a run that already
 * settled — a record about the result, never a way to release or block it.
 */

import { useCallback } from "react";
import { useSearchParams } from "react-router-dom";
import { ClipboardCheck, Gavel, PenLine, ScanSearch } from "lucide-react";
import { useTranslation } from "react-i18next";
import PageShell from "../../../layouts/PageShell";
import TabBar, { type TabBarItem } from "../../../components/TabLabel/TabBar";
import ApprovalInboxPanel from "../Approvals/ApprovalInboxPanel";
import InputInboxPanel from "./InputInboxPanel";
import OutputReviewPanel from "./OutputReviewPanel";
import ProposalsReviewPanel from "./ProposalsReviewPanel";

type InboxTabKey = "approvals" | "inputs" | "reviews" | "proposals";

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
  // The third cell of the inbox the plan asks for (A-11): approvals, questions
  // and output reviews are what a run needs a person for.
  {
    key: "reviews",
    labelKey: "workbuddy.inbox.reviewsTitle",
    icon: ScanSearch,
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
  reviews: true,
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
      {activeTab === "reviews" && <OutputReviewPanel />}
      {activeTab === "proposals" && <ProposalsReviewPanel />}
    </PageShell.Tabbed>
  );
}
