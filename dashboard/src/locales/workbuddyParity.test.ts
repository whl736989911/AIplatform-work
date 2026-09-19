/**
 * WorkBuddy console translation parity.
 *
 * `dashboard/src/locales/{en,zh}.json` are edited by hand (and by the console's
 * i18n fragments before they are merged), so a key added to one bundle can
 * silently stay missing from the other: the UI then renders the raw key for that
 * locale only. The rest of the bundles already carry that kind of drift; this
 * test keeps the WorkBuddy console honest by requiring the two `workbuddy`
 * subtrees to expose exactly the same key paths.
 */

import { describe, expect, it } from "vitest";

import en from "./en.json";
import zh from "./zh.json";

/** Leaf key paths of a nested translation tree. */
function keyPaths(node: unknown, prefix = ""): string[] {
  if (node === null || typeof node !== "object") {
    return [prefix];
  }
  return Object.entries(node as Record<string, unknown>).flatMap(
    ([key, value]) => keyPaths(value, prefix ? `${prefix}.${key}` : key),
  );
}

/** Key paths whose value is an empty or whitespace-only string. */
function untranslatedPaths(root: unknown): string[] {
  return keyPaths(root).filter((path) => {
    const value = path
      .split(".")
      .reduce<unknown>(
        (node, segment) => (node as Record<string, unknown>)[segment],
        root,
      );
    return typeof value === "string" && value.trim() === "";
  });
}

const DOMAINS = [
  "knowledge",
  "workflows",
  "approvals",
  "proposals",
  "marketplace",
  "compliance",
  "lifecycle",
] as const;

describe("WorkBuddy console locales", () => {
  const enWorkBuddy = (en as Record<string, unknown>).workbuddy;
  const zhWorkBuddy = (zh as Record<string, unknown>).workbuddy;

  it("both bundles expose a workbuddy subtree", () => {
    expect(enWorkBuddy).toBeTypeOf("object");
    expect(zhWorkBuddy).toBeTypeOf("object");
  });

  it.each(DOMAINS)("%s has translations in both bundles", (domain) => {
    const enDomain = (enWorkBuddy as Record<string, unknown>)[domain];
    const zhDomain = (zhWorkBuddy as Record<string, unknown>)[domain];

    expect(enDomain).toBeTypeOf("object");
    expect(zhDomain).toBeTypeOf("object");
    expect(keyPaths(enDomain).length).toBeGreaterThan(0);
    expect(keyPaths(zhDomain)).toEqual(keyPaths(enDomain));
  });

  it("no WorkBuddy key is left untranslated in either bundle", () => {
    expect(untranslatedPaths(enWorkBuddy)).toEqual([]);
    expect(untranslatedPaths(zhWorkBuddy)).toEqual([]);
  });
});
