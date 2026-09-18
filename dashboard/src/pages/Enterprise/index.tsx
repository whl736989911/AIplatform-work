/**
 * 企业治理 (/enterprise) — WorkBuddy tenant surface.
 *
 * Access: any authenticated member. The caller's tenant role comes from
 * GET /api/v1/tenant-context (the sole client source); members see 部门 +
 * 工具/模型目录 read-only views, tenant admins additionally get 成员、邀请、
 * 配额、凭据、能力许可. Every mutation stays server-authorised — the role only
 * drives which tabs and buttons render.
 */

import { useCallback, useMemo } from "react";
import { useSearchParams } from "react-router-dom";
import { Alert, Button, Space, Spin, Tag } from "antd";
import {
  Building2,
  Cpu,
  Gauge,
  KeyRound,
  MailPlus,
  RefreshCw,
  SlidersHorizontal,
  Users,
  Wrench,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import PageShell from "../../layouts/PageShell";
import TabBar, { type TabBarItem } from "../../components/TabLabel/TabBar";
import { EmptyState } from "../../components/EmptyState";
import { useAsyncResource } from "../../hooks/useAsyncResource";
import {
  enterpriseApi,
  type TenantContext,
} from "../../api/modules/enterprise";
import { statusLabel } from "./statusLabels";
import DepartmentsPanel from "./DepartmentsPanel";
import MembersPanel from "./MembersPanel";
import InvitationsPanel from "./InvitationsPanel";
import QuotasPanel from "./QuotasPanel";
import CredentialsPanel from "./CredentialsPanel";
import CapabilitiesPanel from "./CapabilitiesPanel";
import CatalogPanel from "./CatalogPanel";
import styles from "./index.module.less";

type EnterpriseTabKey =
  | "departments"
  | "tools"
  | "models"
  | "users"
  | "invitations"
  | "quotas"
  | "credentials"
  | "capabilities";

/** Visible to every tenant member. */
const MEMBER_TABS: readonly EnterpriseTabKey[] = [
  "departments",
  "tools",
  "models",
];

/** Additional tabs unlocked by membership.role === "admin". */
const ADMIN_ONLY_TABS: readonly EnterpriseTabKey[] = [
  "users",
  "invitations",
  "quotas",
  "credentials",
  "capabilities",
];

const TAB_ITEMS: readonly TabBarItem<EnterpriseTabKey>[] = [
  {
    key: "departments",
    labelKey: "tenantGovernance.tab.departments",
    icon: Building2,
  },
  { key: "tools", labelKey: "tenantGovernance.tab.tools", icon: Wrench },
  { key: "models", labelKey: "tenantGovernance.tab.models", icon: Cpu },
  { key: "users", labelKey: "tenantGovernance.tab.users", icon: Users },
  {
    key: "invitations",
    labelKey: "tenantGovernance.tab.invitations",
    icon: MailPlus,
  },
  { key: "quotas", labelKey: "tenantGovernance.tab.quotas", icon: Gauge },
  {
    key: "credentials",
    labelKey: "tenantGovernance.tab.credentials",
    icon: KeyRound,
  },
  {
    key: "capabilities",
    labelKey: "tenantGovernance.tab.capabilities",
    icon: SlidersHorizontal,
  },
];

const TAB_KEYS: Record<EnterpriseTabKey, true> = {
  departments: true,
  tools: true,
  models: true,
  users: true,
  invitations: true,
  quotas: true,
  credentials: true,
  capabilities: true,
};

function isEnterpriseTab(value: string | null): value is EnterpriseTabKey {
  return value !== null && value in TAB_KEYS;
}

export default function EnterprisePage() {
  const { t } = useTranslation();
  const [searchParams, setSearchParams] = useSearchParams();
  const {
    data: context,
    loading,
    refresh,
  } = useAsyncResource<TenantContext | null>(
    null,
    () => enterpriseApi.tenantContext(),
    [],
    {
      // Surface the server's reason (PostgreSQL-required vs. no membership)
      // instead of a silent blank panel.
      errorFallback: t("tenantGovernance.contextFailed"),
      t,
      logLabel: "enterprise/context",
    },
  );

  const isTenantAdmin = context?.membership.role === "admin";
  const allowedTabs = useMemo<readonly EnterpriseTabKey[]>(
    () => (isTenantAdmin ? [...MEMBER_TABS, ...ADMIN_ONLY_TABS] : MEMBER_TABS),
    [isTenantAdmin],
  );

  const requestedTab = searchParams.get("tab");
  const activeTab: EnterpriseTabKey =
    isEnterpriseTab(requestedTab) && allowedTabs.includes(requestedTab)
      ? requestedTab
      : allowedTabs[0];

  const selectTab = useCallback(
    (key: EnterpriseTabKey) => {
      const next = new URLSearchParams(searchParams);
      if (key === "departments") next.delete("tab");
      else next.set("tab", key);
      setSearchParams(next, { replace: true });
    },
    [searchParams, setSearchParams],
  );

  const visibleTabs = useMemo(
    () => TAB_ITEMS.filter((item) => allowedTabs.includes(item.key)),
    [allowedTabs],
  );

  const tenant = context?.tenant ?? null;
  const subtitle = tenant
    ? t("tenantGovernance.pageSubtitle", {
        name: tenant.name,
        slug: tenant.slug,
      })
    : t("tenantGovernance.pageSubtitleFallback");

  if (!context) {
    return (
      <PageShell title={t("tenantGovernance.pageTitle")} subtitle={subtitle}>
        {loading ? (
          <div className={styles.centered}>
            <Spin size="large" />
          </div>
        ) : (
          <EmptyState
            variant="error"
            title={t("tenantGovernance.contextFailed")}
            description={t("tenantGovernance.contextFailedHint")}
            actionLabel={t("common.refresh")}
            onAction={() => void refresh()}
          />
        )}
      </PageShell>
    );
  }

  return (
    <PageShell.Tabbed
      title={t("tenantGovernance.pageTitle")}
      subtitle={subtitle}
      actions={
        <Space size={8} wrap>
          <Tag color={isTenantAdmin ? "gold" : "default"}>
            {t(
              isTenantAdmin
                ? "tenantGovernance.role.admin"
                : "tenantGovernance.role.member",
            )}
          </Tag>
          <Tag color="blue">
            {statusLabel(t, "tenant", context.tenant.status)}
          </Tag>
          <Button
            size="small"
            icon={<RefreshCw size={14} />}
            onClick={() => void refresh()}
          >
            {t("common.refresh")}
          </Button>
        </Space>
      }
      tabBar={
        <TabBar tabs={visibleTabs} activeKey={activeTab} onChange={selectTab} />
      }
    >
      {!isTenantAdmin && (
        <Alert
          type="info"
          showIcon
          className={styles.notice}
          message={t("tenantGovernance.memberNotice")}
          description={t("tenantGovernance.memberNoticeHint")}
        />
      )}

      {activeTab === "departments" && (
        <DepartmentsPanel canManage={isTenantAdmin} />
      )}
      {activeTab === "tools" && <CatalogPanel kind="tool" />}
      {activeTab === "models" && <CatalogPanel kind="model" />}
      {isTenantAdmin && activeTab === "users" && (
        <MembersPanel currentMemberId={context.membership.id} />
      )}
      {isTenantAdmin && activeTab === "invitations" && <InvitationsPanel />}
      {isTenantAdmin && activeTab === "quotas" && <QuotasPanel />}
      {isTenantAdmin && activeTab === "credentials" && (
        <CredentialsPanel currentMemberId={context.membership.id} />
      )}
      {isTenantAdmin && activeTab === "capabilities" && <CapabilitiesPanel />}
    </PageShell.Tabbed>
  );
}
