import type { TFunction } from "i18next";

/**
 * Simplified-Chinese label for an export-job status / deletion-stage code.
 *
 * Missing translations fall back to the raw code so a newly added server state
 * stays visible instead of rendering an i18n key.
 */
export function lifecycleStatusLabel(
  t: TFunction,
  group: "export" | "deletion",
  code: string,
): string {
  const key = `workbuddy.lifecycle.status.${group}.${code}`;
  const label = t(key);
  return label === key ? code : label;
}
