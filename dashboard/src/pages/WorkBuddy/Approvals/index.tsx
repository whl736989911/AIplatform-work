/**
 * WorkBuddy console → Approvals (/workbuddy/approvals).
 *
 * One surface: the caller's notification strip above the approval inbox. Every
 * decision runs through the shared modal (frozen parameters → one-time
 * challenge → resume), so a stale or expired approval can never be "decided"
 * locally.
 */

import { useTranslation } from "react-i18next";
import PageShell from "../../../layouts/PageShell";
import ApprovalInboxPanel from "./ApprovalInboxPanel";
import NotificationsStrip from "./NotificationsStrip";

export default function ApprovalsPage() {
  const { t } = useTranslation();

  return (
    <PageShell
      title={t("workbuddy.approvals.title")}
      subtitle={t("workbuddy.approvals.subtitle")}
    >
      <NotificationsStrip />
      <ApprovalInboxPanel />
    </PageShell>
  );
}
