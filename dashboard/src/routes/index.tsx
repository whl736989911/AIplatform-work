import { lazy } from "react";
import { Navigate, useLocation } from "react-router-dom";

// Lazy-loaded pages — Common
const ExpertsPage = lazy(() => import("../pages/Experts"));
const CronJobsPage = lazy(() => import("../pages/Control/CronJobs"));
const ConnectorsPage = lazy(() => import("../pages/Agent/Connectors"));
const SkillPackagesPage = lazy(() => import("../pages/SkillPackages"));
const KnowledgeBasesPage = lazy(() => import("../pages/KnowledgeBases"));
const PersonalizationPage = lazy(
  () => import("../pages/Agent/Personalization"),
);
const ACPPage = lazy(() => import("../pages/Agent/ACP"));
const TokenUsagePage = lazy(() => import("../pages/Control/TokenUsage"));
const EnterprisePage = lazy(() => import("../pages/Enterprise"));

// Lazy-loaded pages — WorkBuddy console
const WorkBuddyKnowledgePage = lazy(
  () => import("../pages/WorkBuddy/Knowledge"),
);
const WorkBuddyHomePage = lazy(() => import("../pages/WorkBuddy/Home"));
const WorkBuddyInboxPage = lazy(() => import("../pages/WorkBuddy/Inbox"));
const WorkBuddyRunsPage = lazy(() => import("../pages/WorkBuddy/Runs"));
const WorkBuddyWorkflowsPage = lazy(
  () => import("../pages/WorkBuddy/Workflows"),
);
const WorkBuddyApprovalsPage = lazy(
  () => import("../pages/WorkBuddy/Approvals"),
);
const WorkBuddyProposalsPage = lazy(
  () => import("../pages/WorkBuddy/Proposals"),
);
const WorkBuddyMarketplacePage = lazy(
  () => import("../pages/WorkBuddy/Marketplace"),
);
const WorkBuddyCompliancePage = lazy(
  () => import("../pages/WorkBuddy/Compliance"),
);
const WorkBuddyLifecyclePage = lazy(
  () => import("../pages/WorkBuddy/Lifecycle"),
);

// Lazy-loaded pages — Control
const RemoteDesktopPage = lazy(() => import("../pages/Control/RemoteDesktop"));

// Lazy-loaded pages — Settings
const ModelsPage = lazy(() => import("../pages/Settings/Models"));

// Lazy-loaded pages — Admin
const OctopAdminUsersPage = lazy(() => import("../pages/Admin/Users"));
const AdminSecurityPage = lazy(() => import("../pages/Settings/Security"));
const AdvancedSettingsPage = lazy(
  () => import("../pages/Settings/AdvancedSettings"),
);
const AdminStoragePage = lazy(() => import("../pages/Admin/Storage"));
const AdminPluginsPage = lazy(() => import("../pages/Admin/Plugins"));
const AgentConfigPage = lazy(() => import("../pages/Agent/Config"));

// Misc
const PwaDebugPage = lazy(() => import("../pages/PwaDebug"));
const NotFoundPage = lazy(() => import("../components/NotFoundPage"));

function RedirectPreserveSearch({ to }: { to: string }) {
  const location = useLocation();
  return <Navigate to={`${to}${location.search}${location.hash}`} replace />;
}

export interface RouteConfig {
  path: string;
  element: React.ReactNode;
  /** When true, the route uses a wrapper component instead of a plain element */
  useWrapper?: boolean;
}

export const pathToKey: Record<string, string> = {
  "/chat": "chat",
  // Common
  "/experts": "experts",
  "/tasks": "tasks",
  "/connectors": "connectors",
  "/skill-packages": "skill-packages",
  "/knowledge-bases": "knowledge-bases",
  "/personalization": "personalization",
  "/personalization/skills": "personalization",
  "/personalization/tools": "personalization",
  "/personalization/plugins": "personalization",
  "/personalization/subagents": "personalization",
  "/personalization/channels": "channels",
  "/personalization/mbti": "personalization",
  "/personalization/memory": "personalization",
  "/skills": "personalization",
  "/token-usage": "token-usage",
  // WorkBuddy enterprise governance (authenticated members).
  "/enterprise": "enterprise-governance",
  // WorkBuddy console (authenticated members; the server owns authorization).
  // The four daily entries come first, governance follows — the sidebar groups
  // mirror this order.
  "/workbuddy": "workbuddy-home",
  "/workbuddy/inbox": "workbuddy-inbox",
  "/workbuddy/runs": "workbuddy-runs",
  "/workbuddy/knowledge": "workbuddy-knowledge",
  "/workbuddy/workflows": "workbuddy-workflows",
  "/workbuddy/approvals": "workbuddy-approvals",
  "/workbuddy/proposals": "workbuddy-proposals",
  "/workbuddy/marketplace": "workbuddy-marketplace",
  "/workbuddy/compliance": "workbuddy-compliance",
  "/workbuddy/lifecycle": "workbuddy-lifecycle",
  "/agent-config": "agent-config",
  // Control
  "/acp": "acp",
  "/channels": "channels",
  "/workbench": "workbench",
  "/workbench/terminal": "workbench",
  "/workbench/browser": "workbench",
  "/terminal": "workbench",
  "/remote-browser": "workbench",
  "/remote-desktop": "remote-desktop",
  "/remote-desktop/desktop": "remote-desktop",
  "/remote-desktop/phone": "remote-desktop",
  "/remote-desktop/phone/screen": "remote-desktop",
  "/remote-desktop/phone/shell": "remote-desktop",
  "/remote-phone": "remote-desktop",
  "/remote-android": "remote-desktop",
  "/subagents": "personalization",
  "/mbti": "personalization",
  "/memory": "personalization",
  // Admin
  "/admin/models": "models",
  // Admin
  "/admin/users": "admin-users",
  "/admin/backend": "admin-storage",
  "/admin/plugins": "admin-plugins",
  "/admin/advanced": "admin-advanced",
  "/admin/security": "admin-security",
};

/**
 * Pages that should fill the entire content area without padding/scroll wrapper.
 */
export const FULLSCREEN_PATHS = new Set([
  "/workbench",
  "/workbench/terminal",
  "/workbench/browser",
  "/chat",
  "/remote-desktop",
  "/remote-desktop/desktop",
  "/remote-desktop/phone",
  "/remote-desktop/phone/screen",
  "/remote-desktop/phone/shell",
  "/remote-phone",
]);

/**
 * Pages that provide their own compact mobile header.
 */
export const SELF_HEADER_PATHS = new Set<string>([]);

/** Mobile-only fullscreen pages (custom header + no content padding). */
export const MOBILE_FULLSCREEN_PATHS = new Set<string>([]);

export function isWorkbenchPath(pathname: string): boolean {
  return pathname === "/workbench" || pathname.startsWith("/workbench/");
}

export function isRemoteDesktopPath(pathname: string): boolean {
  return (
    pathname === "/remote-desktop" || pathname.startsWith("/remote-desktop/")
  );
}

export function isPersonalizationPath(pathname: string): boolean {
  return (
    pathname === "/personalization" || pathname.startsWith("/personalization/")
  );
}

export function resolveSelectedKey(pathname: string): string {
  if (pathToKey[pathname]) return pathToKey[pathname];
  if (pathname.startsWith("/chat/")) return "chat";
  if (pathname.startsWith("/workbench/")) return "workbench";
  if (pathname.startsWith("/remote-desktop/")) return "remote-desktop";
  if (pathname.startsWith("/personalization/")) return "personalization";
  return "";
}

export const routeConfigs: RouteConfig[] = [
  // Chat (handled via ChatWithKey wrapper in MainLayout)
  { path: "/chat", element: null, useWrapper: true },
  { path: "/chat/:agentId", element: null, useWrapper: true },
  { path: "/chat/:agentId/:threadId", element: null, useWrapper: true },

  // Common
  { path: "/experts", element: <ExpertsPage /> },
  { path: "/tasks", element: <CronJobsPage /> },
  { path: "/connectors", element: <ConnectorsPage /> },
  { path: "/skill-packages", element: <SkillPackagesPage /> },
  { path: "/knowledge-bases", element: <KnowledgeBasesPage /> },
  {
    path: "/personalization/acp",
    element: <RedirectPreserveSearch to="/acp" />,
  },
  { path: "/personalization/*", element: <PersonalizationPage /> },
  {
    path: "/skills",
    element: <RedirectPreserveSearch to="/personalization/skills" />,
  },
  { path: "/token-usage", element: <TokenUsagePage /> },
  // Enterprise governance (/enterprise) is authenticated-only: the tenant
  // role from GET /api/v1/tenant-context decides which tabs render.
  { path: "/enterprise", element: <EnterprisePage /> },

  // WorkBuddy console (/workbuddy/*) is authenticated-only, like /enterprise.
  // Each page renders the server's own authorization answer and shows an
  // explicit "not available yet" state while a backend slice is unmerged.
  { path: "/workbuddy", element: <WorkBuddyHomePage /> },
  { path: "/workbuddy/inbox", element: <WorkBuddyInboxPage /> },
  { path: "/workbuddy/runs", element: <WorkBuddyRunsPage /> },
  { path: "/workbuddy/knowledge", element: <WorkBuddyKnowledgePage /> },
  { path: "/workbuddy/workflows", element: <WorkBuddyWorkflowsPage /> },
  { path: "/workbuddy/approvals", element: <WorkBuddyApprovalsPage /> },
  { path: "/workbuddy/proposals", element: <WorkBuddyProposalsPage /> },
  { path: "/workbuddy/marketplace", element: <WorkBuddyMarketplacePage /> },
  { path: "/workbuddy/compliance", element: <WorkBuddyCompliancePage /> },
  { path: "/workbuddy/lifecycle", element: <WorkBuddyLifecyclePage /> },

  // Control (RequirePermission via pathPermissionKeys in MainLayout)
  { path: "/acp", element: <ACPPage /> },
  {
    path: "/channels",
    element: <RedirectPreserveSearch to="/personalization/channels" />,
  },
  // Workbench (terminal + browser) is keep-alive mounted in MainLayout.
  { path: "/workbench", element: null },
  { path: "/workbench/terminal", element: null },
  { path: "/workbench/browser", element: null },
  {
    path: "/terminal",
    element: <RedirectPreserveSearch to="/workbench/terminal" />,
  },
  {
    path: "/remote-browser",
    element: <RedirectPreserveSearch to="/workbench/browser" />,
  },
  { path: "/remote-desktop", element: <RemoteDesktopPage /> },
  { path: "/remote-desktop/desktop", element: <RemoteDesktopPage /> },
  { path: "/remote-desktop/phone", element: <RemoteDesktopPage /> },
  { path: "/remote-desktop/phone/screen", element: <RemoteDesktopPage /> },
  { path: "/remote-desktop/phone/shell", element: <RemoteDesktopPage /> },
  {
    path: "/remote-phone",
    element: <RedirectPreserveSearch to="/remote-desktop/phone" />,
  },
  {
    path: "/remote-android",
    element: <Navigate to="/remote-desktop/phone" replace />,
  },
  {
    path: "/subagents",
    element: <RedirectPreserveSearch to="/personalization/subagents" />,
  },
  {
    path: "/mbti",
    element: <RedirectPreserveSearch to="/personalization/mbti" />,
  },
  {
    path: "/memory",
    element: <RedirectPreserveSearch to="/personalization/memory" />,
  },
  { path: "/workspace", element: <Navigate to="/experts" replace /> },

  // Settings
  { path: "/admin/models", element: <ModelsPage /> },

  // Admin (RequirePermission wrapper applied in MainLayout)
  { path: "/admin/users", element: <OctopAdminUsersPage /> },
  {
    path: "/admin/sso",
    element: <Navigate to="/admin/users?tab=oidc" replace />,
  },
  {
    path: "/admin/shared-models",
    element: <Navigate to="/admin/models" replace />,
  },
  { path: "/models", element: <Navigate to="/admin/models" replace /> },
  { path: "/admin/backend", element: <AdminStoragePage /> },
  {
    path: "/admin/audit",
    element: <Navigate to="/admin/security?tab=audit" replace />,
  },
  { path: "/admin/agents", element: <Navigate to="/admin/users" replace /> },
  { path: "/admin/plugins", element: <AdminPluginsPage /> },
  { path: "/admin/advanced", element: <AdvancedSettingsPage /> },
  { path: "/admin/security", element: <AdminSecurityPage /> },
  {
    path: "/admin/voice",
    element: <Navigate to="/admin/models?tab=voice" replace />,
  },
  {
    path: "/admin/updates",
    element: <Navigate to="/admin/advanced?tab=updates" replace />,
  },

  // Legacy redirects — keeps old bookmarks working
  { path: "/admin/storage", element: <Navigate to="/admin/backend" replace /> },
  { path: "/orca/cron", element: <Navigate to="/tasks" replace /> },
  { path: "/orca/channels", element: <Navigate to="/channels" replace /> },
  {
    path: "/orca/admin/users",
    element: <Navigate to="/admin/users" replace />,
  },
  {
    path: "/orca/admin/audit",
    element: <Navigate to="/admin/security?tab=audit" replace />,
  },
  { path: "/octop/cron", element: <Navigate to="/tasks" replace /> },
  { path: "/octop/channels", element: <Navigate to="/channels" replace /> },
  {
    path: "/octop/admin/users",
    element: <Navigate to="/admin/users" replace />,
  },
  {
    path: "/octop/admin/audit",
    element: <Navigate to="/admin/security?tab=audit" replace />,
  },
  {
    path: "/advanced-settings",
    element: <Navigate to="/admin/advanced" replace />,
  },
  { path: "/environments", element: <Navigate to="/admin/advanced" replace /> },
  { path: "/agent-config", element: <AgentConfigPage /> },
  {
    path: "/updates",
    element: <Navigate to="/admin/advanced?tab=updates" replace />,
  },
  {
    path: "/plugins",
    element: <Navigate to="/admin/plugins" replace />,
  },
  { path: "/sessions", element: <Navigate to="/chat" replace /> },
  { path: "/cron-jobs", element: <Navigate to="/tasks" replace /> },

  // Misc
  { path: "/pwa-debug", element: <PwaDebugPage /> },
  { path: "/", element: <Navigate to="/chat" replace /> },
  { path: "*", element: <NotFoundPage /> },
];
