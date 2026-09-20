/**
 * 权限策略页 (/settings/access) — 岗位授权.
 *
 * The page must render what the server returned: the duty vocabulary with a
 * grant count per duty, and one row per grant whose subject reads as
 * 全员 / department name / member name. Both mutations go through the access
 * API and then re-read the list from the server; a failing list call leaves a
 * readable error state instead of a blank page.
 */

import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { UserEvent } from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type * as EnterpriseApi from "../../../api/modules/enterprise";
import type { Department } from "../../../api/modules/enterprise";
import type * as AccessApi from "../../../api/modules/workbuddyAccess";
import type {
  AccessMember,
  Duty,
  DutyGrant,
  DutyGrantList,
  DutySubject,
} from "../../../api/modules/workbuddyAccess";

const listDuties = vi.fn<() => Promise<DutyGrantList>>();
const listMembers = vi.fn<() => Promise<AccessMember[]>>();
const grantDuty =
  vi.fn<(duty: Duty, subject: DutySubject) => Promise<unknown>>();
const revokeDuty =
  vi.fn<(duty: Duty, subject: DutySubject) => Promise<unknown>>();

vi.mock("../../../api/modules/workbuddyAccess", async (importOriginal) => {
  const actual = await importOriginal<typeof AccessApi>();
  return {
    ...actual,
    workbuddyAccessApi: {
      listDuties: () => listDuties(),
      listMembers: () => listMembers(),
      grantDuty: (duty: Duty, subject: DutySubject) => grantDuty(duty, subject),
      revokeDuty: (duty: Duty, subject: DutySubject) =>
        revokeDuty(duty, subject),
    },
  };
});

const listDepartments = vi.fn<() => Promise<Department[]>>();

vi.mock("../../../api/modules/enterprise", async (importOriginal) => {
  const actual = await importOriginal<typeof EnterpriseApi>();
  return {
    ...actual,
    enterpriseApi: {
      ...actual.enterpriseApi,
      listDepartments: () => listDepartments(),
    },
  };
});

import AccessPolicyPage from "./index";

const VOCABULARY: Duty[] = [
  "author",
  "publisher",
  "approver",
  "kb_admin",
  "ops",
];

const TENANT_GRANT: DutyGrant = {
  duty: "ops",
  subject_kind: "tenant",
  subject_id: null,
  granted_at: 1_767_225_600,
};

const DEPARTMENT_GRANT: DutyGrant = {
  duty: "author",
  subject_kind: "department",
  subject_id: "dept-1",
  granted_at: 1_767_225_600,
};

const MEMBER_GRANT: DutyGrant = {
  duty: "author",
  subject_kind: "member",
  subject_id: "42",
  granted_at: 1_767_225_600,
};

const DEPARTMENTS: Department[] = [
  { id: "dept-1", name: "研发部", parent_id: null, status: "active" },
];

const MEMBERS: AccessMember[] = [
  {
    id: "m-1",
    user_id: 42,
    username: "zhangsan",
    display_name: "张三",
    email: "zhangsan@example.com",
  },
];

function grantsOf(items: DutyGrant[]): DutyGrantList {
  return { duties: VOCABULARY, items };
}

/** Text of every body row of the duty grant table, in render order. */
function bodyRows(): string[] {
  const table = screen.getByRole("table");
  return within(table)
    .getAllByRole("row")
    .slice(1)
    .map((row) => row.textContent ?? "");
}

async function openGrantDialog(user: UserEvent) {
  await user.click(screen.getByRole("button", { name: "access.add" }));
  const dialog = await waitFor(() => {
    const node = document.querySelector<HTMLElement>(".ant-modal-content");
    if (!node) throw new Error("the duty grant dialog did not open");
    return node;
  });
  return within(dialog);
}

/** Open one antd Select and click the option carrying this label. */
async function pickOption(
  user: UserEvent,
  combobox: HTMLElement,
  label: string,
) {
  fireEvent.mouseDown(combobox);
  const option = await screen.findByText(label, {
    selector: ".ant-select-item-option-content",
  });
  await user.click(option);
}

describe("<AccessPolicyPage />", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // mockReset on the list, not resetAllMocks: a `Once` value queued by an
    // earlier test must not be consumed here, where it would silently replace
    // this test's fixture. Resetting every global mock would also wipe the
    // matchMedia stub the setup file installs.
    listDuties.mockReset();
    listDuties.mockResolvedValue(grantsOf([]));
    listDepartments.mockResolvedValue(DEPARTMENTS);
    listMembers.mockResolvedValue(MEMBERS);
    grantDuty.mockResolvedValue({});
    revokeDuty.mockResolvedValue({});
  });

  it("lists every grant under its duty and names each subject", async () => {
    // Deliberately out of vocabulary order: the table must group by duty.
    listDuties.mockResolvedValue(
      grantsOf([TENANT_GRANT, DEPARTMENT_GRANT, MEMBER_GRANT]),
    );

    render(<AccessPolicyPage />);

    await screen.findByRole("table");
    // Every duty of the server vocabulary gets a card, each with its own count
    // (the auto-mocked t() renders the count key, so the tags read alike).
    expect(screen.getByText("access.vocabulary.title")).toBeInTheDocument();
    for (const duty of VOCABULARY) {
      expect(screen.getAllByText(duty).length).toBeGreaterThan(0);
    }
    expect(screen.getAllByText("access.grantCount")).toHaveLength(
      VOCABULARY.length,
    );
    // The page states how the grants are meant to be read.
    expect(screen.getByText("access.policy.title")).toBeInTheDocument();
    expect(screen.getByText("access.policy.adminImplicit")).toBeInTheDocument();
    expect(
      screen.getByText("access.policy.departmentChain"),
    ).toBeInTheDocument();

    const rows = bodyRows();
    expect(rows).toHaveLength(3);
    expect(rows[0]).toContain("author");
    expect(rows[1]).toContain("author");
    expect(rows[2]).toContain("ops");

    const text = rows.join(" | ");
    expect(text).toContain("研发部");
    expect(text).toContain("张三");
    expect(rows[2]).toContain("tenant");
  });

  it("grants a duty to a department and re-reads the list", async () => {
    const user = userEvent.setup();
    listDuties
      .mockResolvedValueOnce(grantsOf([MEMBER_GRANT]))
      .mockResolvedValueOnce(grantsOf([MEMBER_GRANT, DEPARTMENT_GRANT]));

    render(<AccessPolicyPage />);
    await screen.findByRole("table");

    const dialog = await openGrantDialog(user);
    const comboboxes = dialog.getAllByRole("combobox");
    // The duty picker defaults to the first duty; switch it to prove the chosen
    // duty is what gets sent.
    await pickOption(user, comboboxes[0], "approver");
    await user.click(dialog.getByText("department"));
    await pickOption(user, dialog.getAllByRole("combobox")[1], "研发部");
    await user.click(
      dialog.getByRole("button", { name: "access.form.submit" }),
    );

    await waitFor(() =>
      expect(grantDuty).toHaveBeenCalledWith("approver", {
        subject_kind: "department",
        subject_id: "dept-1",
      }),
    );
    // The row comes from the re-read, not from the form's own state.
    await waitFor(() => expect(listDuties).toHaveBeenCalledTimes(2));
    await waitFor(() =>
      expect(bodyRows().some((row) => row.includes("研发部"))).toBe(true),
    );
  });

  it("revokes one grant after the confirmation and re-reads the list", async () => {
    const user = userEvent.setup();
    listDuties
      .mockResolvedValueOnce(grantsOf([DEPARTMENT_GRANT]))
      .mockResolvedValueOnce(grantsOf([]));

    render(<AccessPolicyPage />);
    await screen.findByRole("table");

    const table = screen.getByRole("table");
    await user.click(
      within(within(table).getAllByRole("row")[1]).getByRole("button", {
        name: "access.revoke",
      }),
    );
    await screen.findByText("access.revokeConfirm");
    await user.click(
      screen.getByRole("button", { name: "access.revokeConfirmOk" }),
    );

    await waitFor(() =>
      expect(revokeDuty).toHaveBeenCalledWith("author", {
        subject_kind: "department",
        subject_id: "dept-1",
      }),
    );
    // With the last grant gone the page falls back to its empty state.
    expect(await screen.findByText("access.emptyTitle")).toBeInTheDocument();
  });

  it("shows the empty state when the tenant has no grant", async () => {
    listDuties.mockResolvedValue(grantsOf([]));

    render(<AccessPolicyPage />);

    expect(await screen.findByText("access.emptyTitle")).toBeInTheDocument();
    expect(screen.getByText("access.emptyHint")).toBeInTheDocument();
  });

  it("keeps the page readable when the grants cannot be loaded", async () => {
    listDuties.mockRejectedValue(
      new Error(
        'Request failed: 500 Internal Server Error - {"error":{"code":"INTERNAL","message":"boom"}}',
      ),
    );

    render(<AccessPolicyPage />);

    expect(await screen.findByText("access.loadFailed")).toBeInTheDocument();
    expect(screen.getByText("access.title")).toBeInTheDocument();
  });

  it("reports an unmounted duty slice instead of an empty table", async () => {
    listDuties.mockRejectedValue(
      new Error('Request failed: 404 Not Found - {"detail":"Not Found"}'),
    );

    render(<AccessPolicyPage />);

    expect(
      await screen.findByText("workbuddy.shared.notMergedTitle"),
    ).toBeInTheDocument();
    expect(screen.queryByRole("table")).toBeNull();
  });
});
