import type { TFunction } from "i18next";

/**
 * Simplified-Chinese label for a checklist / revision status code.
 *
 * Missing translations fall back to the raw code so a newly added server state
 * stays visible instead of rendering an i18n key.
 */
export function complianceStatusLabel(
  t: TFunction,
  group: "checklist" | "version",
  code: string,
): string {
  const key = `workbuddy.compliance.status.${group}.${code}`;
  const label = t(key);
  return label === key ? code : label;
}
