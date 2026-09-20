import { describe, expect, it } from "vitest";
import {
  SIDEBAR_GROUPED_NAV_KEYS,
  buildNavSections,
  isGroupedNavKey,
} from "./sidebarNav";
import { pathToKey, resolveSelectedKey } from "../routes";
import type { OctopUser } from "../api/modules/auth";

const adminUser = {
  id: 1,
  username: "admin",
  role: "admin",
  permissions: ["*"],
} as OctopUser;

describe("sidebarNav", () => {
  it("marks catalog keys as grouped", () => {
    for (const key of SIDEBAR_GROUPED_NAV_KEYS) {
      expect(isGroupedNavKey(key)).toBe(true);
    }
    expect(isGroupedNavKey("chat")).toBe(false);
    expect(isGroupedNavKey("experts")).toBe(false);
  });

  it("places grouped keys only under sections with groupKey", () => {
    const sections = buildNavSections(adminUser, { mobileEnabled: true });
    const flatKeys = new Set(
      sections
        .filter((s) => !s.groupKey)
        .flatMap((s) => s.items.map((i) => i.key)),
    );
    const groupedKeys = new Set(
      sections
        .filter((s) => s.groupKey)
        .flatMap((s) => s.items.map((i) => i.key)),
    );
    for (const key of groupedKeys) {
      expect(isGroupedNavKey(key)).toBe(true);
      expect(flatKeys.has(key)).toBe(false);
    }
    for (const key of flatKeys) {
      expect(isGroupedNavKey(key)).toBe(false);
    }
  });

  // The console is employee-first: the four entries a member touches every day
  // come first and stay together, and no console surface silently disappears
  // into another group.
  it("keeps the four daily console entries first and complete", () => {
    const sections = buildNavSections(adminUser, { mobileEnabled: true });
    const dailyIndex = sections.findIndex(
      (s) => s.groupKey === "nav.workbuddyDaily",
    );
    const governanceIndex = sections.findIndex(
      (s) => s.groupKey === "nav.workbuddyGovernance",
    );
    expect(dailyIndex).toBeGreaterThanOrEqual(0);
    expect(governanceIndex).toBeGreaterThan(dailyIndex);

    expect(sections[dailyIndex].items.map((i) => i.key)).toEqual([
      "workbuddy-home",
      "workbuddy-inbox",
      "workbuddy-workflows",
      "workbuddy-runs",
    ]);

    const governanceKeys: Record<string, true> = Object.fromEntries(
      sections[governanceIndex].items.map((i) => [i.key, true as const]),
    );
    for (const key of [
      "workbuddy-knowledge",
      "workbuddy-proposals",
      "workbuddy-approvals",
      "workbuddy-marketplace",
      "workbuddy-compliance",
      "workbuddy-lifecycle",
    ]) {
      expect(governanceKeys[key]).toBe(true);
    }
  });

  // A nav entry whose path is not in `pathToKey` renders but never highlights,
  // which reads as "the sidebar is broken" rather than "this page is unmapped".
  it("maps every nav entry to the key it highlights", () => {
    for (const section of buildNavSections(adminUser, { mobileEnabled: true })) {
      for (const item of section.items) {
        expect(pathToKey[item.path]).toBe(item.key);
        expect(resolveSelectedKey(item.path)).toBe(item.key);
      }
    }
  });
});
