/**
 * Knowledge page: the four visibility layers of the base list.
 *
 * The page must re-present the server's own answer — the ``access_sources`` of
 * every base the caller may read — instead of guessing a layer from a name, a
 * scope or an owner id. The chips therefore union: a base the caller reads
 * through two layers stays visible under either, and a filter that matches
 * nothing says the filter matched nothing instead of telling the reader the
 * tenant has no knowledge bases.
 *
 * The whole page is mounted (它的默认页签就是「知识库」列表), so these cases also
 * cover the page-level state the chips live in.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../../api/request", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../api/request")>();
  return { ...actual, request: vi.fn() };
});

import { request } from "../../../api/request";
import type { KnowledgeBase } from "../../../api/modules/workbuddyKnowledge";
import KnowledgePage from "./index";

const KB_PERSONAL = "11111111-1111-4111-8111-111111111111";
const KB_DEPARTMENT = "22222222-2222-4222-8222-222222222222";
const KB_ENTERPRISE = "33333333-3333-4333-8333-333333333333";
const KB_SHARED = "44444444-4444-4444-8444-444444444444";
const KB_ADMIN_VIEWED = "55555555-5555-4555-8555-555555555555";
const DEPARTMENT = "66666666-6666-4666-8666-666666666666";

function knowledgeBase(
  overrides: Pick<KnowledgeBase, "kb_id" | "name" | "scope"> &
    Partial<KnowledgeBase>,
): KnowledgeBase {
  return {
    description: "",
    department_id: null,
    owner_user_id: 7,
    archived_at: null,
    permission: "admin",
    access_sources: [],
    embedding: {
      adapter_key: "bge",
      model_key: "bge-m3",
      revision: 4,
      dimensions: 1024,
      model_revision_id: "rev-4",
    },
    created_at: 1_700_000_000,
    updated_at: 1_700_000_500,
    ...overrides,
  };
}

/** One base per way the resolver can make this member read it. */
function ownedBase(): KnowledgeBase {
  return knowledgeBase({
    kb_id: KB_PERSONAL,
    name: "我的手册",
    scope: "personal",
    access_sources: ["owner"],
  });
}

function departmentBase(): KnowledgeBase {
  return knowledgeBase({
    kb_id: KB_DEPARTMENT,
    name: "部门手册",
    scope: "department",
    department_id: DEPARTMENT,
    owner_user_id: null,
    access_sources: ["department-member"],
  });
}

/** Enterprise scope *and* an explicit grant: the row belongs to two layers. */
function enterpriseGrantedBase(): KnowledgeBase {
  return knowledgeBase({
    kb_id: KB_ENTERPRISE,
    name: "公司手册",
    scope: "enterprise",
    owner_user_id: null,
    access_sources: ["enterprise-member", "acl:read"],
  });
}

function sharedBase(): KnowledgeBase {
  return knowledgeBase({
    kb_id: KB_SHARED,
    name: "共享给我的手册",
    scope: "personal",
    owner_user_id: 99,
    access_sources: ["acl:read"],
  });
}

/** Someone else's department base, reachable only as tenant admin. */
function adminViewedBase(): KnowledgeBase {
  return knowledgeBase({
    kb_id: KB_ADMIN_VIEWED,
    name: "另一部门手册",
    scope: "department",
    department_id: "77777777-7777-4777-8777-777777777777",
    owner_user_id: null,
    access_sources: ["tenant-admin"],
  });
}

function serve(bases: KnowledgeBase[]): void {
  vi.mocked(request).mockImplementation((async (path: string) => {
    if (path === "/settings/timezone") return { timezone: "UTC" };
    if (path === "/v1/knowledge-bases") {
      return {
        data: { items: bases, count: bases.length },
        request_id: "test-request",
      };
    }
    throw new Error('Request failed: 404 Not Found - {"detail":"Not Found"}');
  }) as typeof request);
}

function mount(bases: KnowledgeBase[]) {
  serve(bases);
  return render(
    <MemoryRouter>
      <KnowledgePage />
    </MemoryRouter>,
  );
}

function layerChip(layer: string): HTMLElement {
  return screen.getByRole("button", {
    name: `workbuddy.knowledge.layers.${layer}`,
  });
}

beforeEach(() => {
  vi.mocked(request).mockReset();
});

describe("knowledge page visibility layers", () => {
  it("filters the list by the layer the server reported", async () => {
    mount([
      ownedBase(),
      departmentBase(),
      enterpriseGrantedBase(),
      adminViewedBase(),
    ]);
    await screen.findByText("我的手册");

    // No chip selected: every readable base, including the one this member only
    // reaches as tenant admin.
    expect(screen.getByText("另一部门手册")).toBeTruthy();

    fireEvent.click(layerChip("department"));
    await waitFor(() => expect(screen.queryByText("我的手册")).toBeNull());
    expect(screen.getByText("部门手册")).toBeTruthy();
    expect(screen.queryByText("公司手册")).toBeNull();
    // The admin-only base is not a 部门 match: the resolver never said
    // "department-member" for it.
    expect(screen.queryByText("另一部门手册")).toBeNull();

    // Back to 全部, then one layer alone: the enterprise base and only it.
    fireEvent.click(layerChip("all"));
    await screen.findByText("我的手册");
    fireEvent.click(layerChip("enterprise"));
    await waitFor(() => expect(screen.queryByText("部门手册")).toBeNull());
    expect(screen.getByText("公司手册")).toBeTruthy();
    expect(screen.queryByText("我的手册")).toBeNull();

    fireEvent.click(layerChip("all"));
    await screen.findByText("我的手册");
    expect(screen.getByText("部门手册")).toBeTruthy();
    expect(screen.getByText("另一部门手册")).toBeTruthy();
  });

  it("unions the selected layers instead of partitioning the list", async () => {
    mount([
      ownedBase(),
      departmentBase(),
      enterpriseGrantedBase(),
      sharedBase(),
    ]);
    await screen.findByText("我的手册");

    // 单独授权 alone: the personal base shared with this member, and the
    // enterprise base that also carries a grant.
    fireEvent.click(layerChip("acl"));
    await screen.findByText("共享给我的手册");
    expect(screen.getByText("公司手册")).toBeTruthy();
    expect(screen.queryByText("我的手册")).toBeNull();

    // Adding 部门 keeps what the grant chip already matched — the chips are a
    // union, so switching one on never removes another's rows.
    fireEvent.click(layerChip("department"));
    await screen.findByText("部门手册");
    expect(screen.getByText("共享给我的手册")).toBeTruthy();
    expect(screen.getByText("公司手册")).toBeTruthy();
    expect(screen.queryByText("我的手册")).toBeNull();

    // Dropping 部门 again leaves both grant-matched rows in place.
    fireEvent.click(layerChip("department"));
    await waitFor(() => expect(screen.queryByText("部门手册")).toBeNull());
    expect(screen.getByText("共享给我的手册")).toBeTruthy();
    expect(screen.getByText("公司手册")).toBeTruthy();
  });

  it("names an empty filter result instead of an empty tenant", async () => {
    mount([enterpriseGrantedBase(), sharedBase()]);
    await screen.findByText("公司手册");

    fireEvent.click(layerChip("personal"));
    await screen.findByText("workbuddy.knowledge.bases.filterEmpty");
    // The tenant-empty copy must not be what the reader sees here.
    expect(screen.queryByText("workbuddy.knowledge.bases.empty")).toBeNull();
    expect(screen.queryByText("公司手册")).toBeNull();

    fireEvent.click(
      screen.getByRole("button", {
        name: "workbuddy.knowledge.bases.filterClear",
      }),
    );
    await screen.findByText("公司手册");
    expect(
      screen.queryByText("workbuddy.knowledge.bases.filterEmpty"),
    ).toBeNull();
  });

  it("explains each row with the source the server sent", async () => {
    mount([ownedBase(), sharedBase(), adminViewedBase()]);
    await screen.findByText("我的手册");

    expect(
      screen.getByText("workbuddy.knowledge.accessSource.owner"),
    ).toBeTruthy();
    expect(
      screen.getByText("workbuddy.knowledge.accessSource.acl"),
    ).toBeTruthy();
    expect(
      screen.getByText("workbuddy.knowledge.accessSource.tenantAdmin"),
    ).toBeTruthy();
  });
});
