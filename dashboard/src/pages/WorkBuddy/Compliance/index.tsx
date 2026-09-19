/**
 * Compliance (/workbuddy/compliance) — tenant compliance checklists.
 *
 * The checklist surface is the whole page: list, create, immutable revision
 * timeline, pinned-revision read, approve / revoke, archive. Platform review of
 * marketplace submissions lives on the marketplace surface; the decision route
 * is owned by that slice's client.
 */

import { useTranslation } from "react-i18next";
import PageShell from "../../../layouts/PageShell";
import ChecklistsPanel from "./ChecklistsPanel";

export default function CompliancePage() {
  const { t } = useTranslation();

  return (
    <PageShell
      title={t("workbuddy.compliance.title")}
      subtitle={t("workbuddy.compliance.subtitle")}
    >
      <ChecklistsPanel />
    </PageShell>
  );
}
