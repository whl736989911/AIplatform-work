/**
 * Data lifecycle (/workbuddy/lifecycle) — controlled tenant export and
 * deletion.
 *
 * Two surfaces: 导出 (build an export, redeem its one-time token, download the
 * payload) and 删除 (open a deletion request inside its cooling-off window,
 * read its stage, cancel). Both are tenant-admin surfaces; every mutation is
 * authorised server-side.
 */

import { useCallback } from "react";
import { useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import PageShell from "../../../layouts/PageShell";
import TabBar, { type TabBarItem } from "../../../components/TabLabel/TabBar";
import { Download, Trash2 } from "lucide-react";
import ExportsPanel from "./ExportsPanel";
import DeletionPanel from "./DeletionPanel";

type LifecycleTabKey = "exports" | "deletion";

const TAB_ITEMS: readonly TabBarItem<LifecycleTabKey>[] = [
  {
    key: "exports",
    labelKey: "workbuddy.lifecycle.tab.exports",
    icon: Download,
  },
  {
    key: "deletion",
    labelKey: "workbuddy.lifecycle.tab.deletion",
    icon: Trash2,
  },
];

const TAB_KEYS: Record<LifecycleTabKey, true> = {
  exports: true,
  deletion: true,
};

function isLifecycleTab(value: string | null): value is LifecycleTabKey {
  return value !== null && value in TAB_KEYS;
}

export default function LifecyclePage() {
  const { t } = useTranslation();
  const [searchParams, setSearchParams] = useSearchParams();

  const requestedTab = searchParams.get("tab");
  const activeTab: LifecycleTabKey = isLifecycleTab(requestedTab)
    ? requestedTab
    : "exports";

  const selectTab = useCallback(
    (key: LifecycleTabKey) => {
      const next = new URLSearchParams(searchParams);
      if (key === "exports") next.delete("tab");
      else next.set("tab", key);
      setSearchParams(next, { replace: true });
    },
    [searchParams, setSearchParams],
  );

  return (
    <PageShell.Tabbed
      title={t("workbuddy.lifecycle.title")}
      subtitle={t("workbuddy.lifecycle.subtitle")}
      tabBar={
        <TabBar tabs={TAB_ITEMS} activeKey={activeTab} onChange={selectTab} />
      }
    >
      {activeTab === "exports" ? <ExportsPanel /> : <DeletionPanel />}
    </PageShell.Tabbed>
  );
}
