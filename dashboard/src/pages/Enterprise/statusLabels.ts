import type { TFunction } from "i18next";

/**
 * Simplified-Chinese label for a backend status / role code.
 *
 * Missing translations fall back to the raw code so a newly added server
 * state stays visible instead of rendering an i18n key.
 */
export function statusLabel(
  t: TFunction,
  group: string,
  status: string,
): string {
  const key = `tenantGovernance.status.${group}.${status}`;
  const label = t(key);
  return label === key ? status : label;
}
