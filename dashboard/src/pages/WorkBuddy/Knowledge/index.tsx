/**
 * WorkBuddy 知识库 (/workbuddy/knowledge).
 *
 * Access: any authenticated tenant member. The knowledge-base list is loaded
 * once for the whole page; the selected base and the active tab live in the URL
 * (``?base=<kb_id>`` / ``?tab=<key>``) so a panel view can be shared or
 * reloaded. Everything the caller may not read is already filtered server-side,
 * so the panels never re-check permissions beyond the base's own ``permission``.
 *
 * The Python slice for these routes lives on a module branch; when it is not
 * mounted the panels show an explicit "not available yet" notice instead of a
 * red failure (see ``isSliceUnavailable``).
 */

import { useCallback, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Alert, Button, Select, Space, Spin, Tag } from "antd";
import {
  Database,
  FileText,
  RefreshCw,
  Search,
  ShieldCheck,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import PageShell from "../../../layouts/PageShell";
import TabBar, { type TabBarItem } from "../../../components/TabLabel/TabBar";
import { EmptyState } from "../../../components/EmptyState";
import { apiErrorMessage } from "../../../utils/apiError";
import {
  workbuddyKnowledgeApi,
  type KnowledgeBase,
} from "../../../api/modules/workbuddyKnowledge";
import {
  useKnowledgeResource,
  type KnowledgeResource,
} from "./useKnowledgeResource";
import type { KnowledgeLayer } from "./visibility";
import BasesPanel from "./BasesPanel";
import DocumentsPanel from "./DocumentsPanel";
import AclPanel from "./AclPanel";
import SearchPanel from "./SearchPanel";
import styles from "./index.module.less";

type KnowledgeTabKey = "bases" | "documents" | "acl" | "search";

const TAB_ITEMS: readonly TabBarItem<KnowledgeTabKey>[] = [
  {
    key: "bases",
    labelKey: "workbuddy.knowledge.tab.bases",
    icon: Database,
  },
  {
    key: "documents",
    labelKey: "workbuddy.knowledge.tab.documents",
    icon: FileText,
  },
  {
    key: "acl",
    labelKey: "workbuddy.knowledge.tab.acl",
    icon: ShieldCheck,
  },
  {
    key: "search",
    labelKey: "workbuddy.knowledge.tab.search",
    icon: Search,
  },
];

const TAB_KEYS: Record<KnowledgeTabKey, true> = {
  bases: true,
  documents: true,
  acl: true,
  search: true,
};

function isKnowledgeTab(value: string | null): value is KnowledgeTabKey {
  return value !== null && value in TAB_KEYS;
}

/**
 * Base list states for the tabs that need a selected base: a missing slice is
 * informational, an empty tenant points at the Bases tab.
 */
function BaseGate({
  resource,
}: {
  resource: KnowledgeResource<KnowledgeBase[]>;
}) {
  const { t } = useTranslation();

  if (resource.unavailable) {
    return (
      <Alert
        type="info"
        showIcon
        message={t("workbuddy.knowledge.common.notMergedTitle")}
        description={t("workbuddy.knowledge.common.notMergedHint")}
      />
    );
  }
  if (resource.error) {
    return (
      <Alert
        type="error"
        showIcon
        message={t("workbuddy.knowledge.bases.loadFailed")}
        description={apiErrorMessage(
          resource.error,
          t("workbuddy.knowledge.bases.loadFailed"),
          t,
        )}
        action={
          <Button
            size="small"
            icon={<RefreshCw size={14} />}
            onClick={() => void resource.refresh()}
          >
            {t("workbuddy.knowledge.common.retry")}
          </Button>
        }
      />
    );
  }
  if (resource.loading && resource.data.length === 0) {
    return (
      <div className={styles.centered}>
        <Spin />
      </div>
    );
  }
  return (
    <EmptyState
      variant="mascot"
      title={t("workbuddy.knowledge.noBase.title")}
      description={t("workbuddy.knowledge.noBase.hint")}
    />
  );
}

export default function KnowledgePage() {
  const { t } = useTranslation();
  const [searchParams, setSearchParams] = useSearchParams();
  // The visibility layers survive a tab switch because the page owns them; the
  // URL contract (``?base=`` / ``?tab=``) stays exactly as it was.
  const [layers, setLayers] = useState<KnowledgeLayer[]>([]);

  const basesResource = useKnowledgeResource<KnowledgeBase[]>(
    [],
    () => workbuddyKnowledgeApi.listBases().then((page) => page.items),
    [],
  );
  const bases = basesResource.data;

  const requestedTab = searchParams.get("tab");
  const activeTab: KnowledgeTabKey = isKnowledgeTab(requestedTab)
    ? requestedTab
    : "bases";

  const requestedBaseId = searchParams.get("base");
  const selectedBase = useMemo<KnowledgeBase | null>(() => {
    const match = bases.find((base) => base.kb_id === requestedBaseId);
    return match ?? bases[0] ?? null;
  }, [bases, requestedBaseId]);

  const selectTab = useCallback(
    (key: KnowledgeTabKey) => {
      const next = new URLSearchParams(searchParams);
      if (key === "bases") next.delete("tab");
      else next.set("tab", key);
      setSearchParams(next, { replace: true });
    },
    [searchParams, setSearchParams],
  );

  const selectBase = useCallback(
    (kbId: string) => {
      const next = new URLSearchParams(searchParams);
      next.set("base", kbId);
      setSearchParams(next, { replace: true });
    },
    [searchParams, setSearchParams],
  );

  return (
    <PageShell.Tabbed
      title={t("workbuddy.knowledge.title")}
      subtitle={t("workbuddy.knowledge.subtitle")}
      actions={
        activeTab === "bases" ? undefined : (
          <Space size={8} wrap>
            <Select
              className={styles.baseSelect}
              value={selectedBase?.kb_id}
              onChange={selectBase}
              options={bases.map((base) => ({
                value: base.kb_id,
                label: base.name,
              }))}
              placeholder={t("workbuddy.knowledge.baseSelect.placeholder")}
              disabled={bases.length === 0}
              showSearch
              optionFilterProp="label"
            />
            <Tag color="blue">
              {t("workbuddy.knowledge.baseSelect.count", {
                count: bases.length,
              })}
            </Tag>
          </Space>
        )
      }
      tabBar={
        <TabBar tabs={TAB_ITEMS} activeKey={activeTab} onChange={selectTab} />
      }
    >
      {activeTab === "bases" && (
        <BasesPanel
          resource={basesResource}
          selectedId={selectedBase?.kb_id ?? null}
          onSelect={selectBase}
          layers={layers}
          onLayersChange={setLayers}
        />
      )}
      {activeTab === "documents" &&
        (selectedBase ? (
          <DocumentsPanel base={selectedBase} />
        ) : (
          <BaseGate resource={basesResource} />
        ))}
      {activeTab === "acl" &&
        (selectedBase ? (
          <AclPanel base={selectedBase} />
        ) : (
          <BaseGate resource={basesResource} />
        ))}
      {activeTab === "search" &&
        (selectedBase ? (
          <SearchPanel base={selectedBase} />
        ) : (
          <BaseGate resource={basesResource} />
        ))}
    </PageShell.Tabbed>
  );
}
