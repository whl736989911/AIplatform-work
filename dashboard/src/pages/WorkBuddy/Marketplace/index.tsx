/**
 * 模板市场 (/workbuddy/marketplace) — WorkBuddy template marketplace.
 *
 * Three surfaces over the frozen `/api/v1/marketplace*` contract:
 *  - 模板目录: published templates, one fixed version, install with explicit
 *    consent and per-slot rebinding;
 *  - 安装记录: installation state, consent evidence and explicit upgrades;
 *  - 提交: author a public draft, freeze it for review, and (platform side)
 *    record an independent review decision.
 *
 * The backend slice is not merged yet, so every panel renders an explicit
 * "not available yet" state on 404/501/503 instead of inventing data.
 */

import { useCallback, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Alert } from "antd";
import { DownloadCloud, Store, UploadCloud } from "lucide-react";
import { useTranslation } from "react-i18next";
import PageShell from "../../../layouts/PageShell";
import TabBar, { type TabBarItem } from "../../../components/TabLabel/TabBar";
import TemplatesPanel from "./TemplatesPanel";
import InstallationsPanel from "./InstallationsPanel";
import SubmissionsPanel from "./SubmissionsPanel";
import styles from "./index.module.less";

type MarketplaceTabKey = "templates" | "installations" | "submissions";

const TAB_ITEMS: readonly TabBarItem<MarketplaceTabKey>[] = [
  {
    key: "templates",
    labelKey: "workbuddy.marketplace.tab.templates",
    icon: Store,
  },
  {
    key: "installations",
    labelKey: "workbuddy.marketplace.tab.installations",
    icon: DownloadCloud,
  },
  {
    key: "submissions",
    labelKey: "workbuddy.marketplace.tab.submissions",
    icon: UploadCloud,
  },
];

const TAB_KEYS: Record<MarketplaceTabKey, true> = {
  templates: true,
  installations: true,
  submissions: true,
};

function isMarketplaceTab(value: string | null): value is MarketplaceTabKey {
  return value !== null && value in TAB_KEYS;
}

export default function MarketplacePage() {
  const { t } = useTranslation();
  const [searchParams, setSearchParams] = useSearchParams();
  /**
   * Ids observed in this session (an install or an explicit open). The
   * installations panel reads this tenant's ledger from the list route; these
   * ids stay as the shortcut back to what this session just produced.
   */
  const [knownInstallations, setKnownInstallations] = useState<string[]>([]);
  const [knownSubmissions, setKnownSubmissions] = useState<string[]>([]);

  const requestedTab = searchParams.get("tab");
  const activeTab: MarketplaceTabKey = isMarketplaceTab(requestedTab)
    ? requestedTab
    : "templates";

  const selectTab = useCallback(
    (key: MarketplaceTabKey) => {
      const next = new URLSearchParams(searchParams);
      if (key === "templates") next.delete("tab");
      else next.set("tab", key);
      setSearchParams(next, { replace: true });
    },
    [searchParams, setSearchParams],
  );

  const rememberInstallation = useCallback((installationId: string) => {
    setKnownInstallations((prev) =>
      prev.includes(installationId) ? prev : [installationId, ...prev],
    );
  }, []);

  const rememberSubmission = useCallback((submissionId: string) => {
    setKnownSubmissions((prev) =>
      prev.includes(submissionId) ? prev : [submissionId, ...prev],
    );
  }, []);

  return (
    <PageShell.Tabbed
      title={t("workbuddy.marketplace.title")}
      subtitle={t("workbuddy.marketplace.subtitle")}
      tabBar={
        <TabBar tabs={TAB_ITEMS} activeKey={activeTab} onChange={selectTab} />
      }
    >
      <Alert
        type="info"
        showIcon
        className={styles.notice}
        message={t("workbuddy.marketplace.notice.title")}
        description={t("workbuddy.marketplace.notice.desc")}
      />

      {activeTab === "templates" && (
        <TemplatesPanel onInstalled={rememberInstallation} />
      )}
      {activeTab === "installations" && (
        <InstallationsPanel
          knownInstallationIds={knownInstallations}
          onOpened={rememberInstallation}
        />
      )}
      {activeTab === "submissions" && (
        <SubmissionsPanel
          knownSubmissionIds={knownSubmissions}
          onOpened={rememberSubmission}
        />
      )}
    </PageShell.Tabbed>
  );
}
