import type { TFunction } from "i18next";

/**
 * Label for a backend scope / permission / document-status code.
 *
 * Missing translations fall back to the raw code so a newly added server state
 * stays visible instead of rendering an i18n key.
 */
export function knowledgeLabel(
  t: TFunction,
  group: "scope" | "permission" | "documentStatus",
  value: string,
): string {
  const key = `workbuddy.knowledge.${group}.${value}`;
  const label = t(key);
  return label === key ? value : label;
}
