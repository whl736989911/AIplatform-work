import type { TFunction } from "i18next";

import type { DutySubjectKind } from "../../../api/modules/workbuddyAccess";

/**
 * Label for a duty code of the backend vocabulary.
 *
 * Missing translations fall back to the raw code so a duty added server-side
 * stays visible instead of rendering an i18n key.
 */
export function dutyLabel(t: TFunction, duty: string): string {
  const key = `access.duty.${duty}.label`;
  const label = t(key);
  return label === key ? duty : label;
}

/** One-line explanation of what holding a duty allows. */
export function dutyDescription(t: TFunction, duty: string): string {
  const key = `access.duty.${duty}.description`;
  const text = t(key);
  return text === key ? "" : text;
}

/** Label of a grant subject kind: the tenant, a department, or a member. */
export function subjectKindLabel(t: TFunction, kind: DutySubjectKind): string {
  const key = `access.subject.${kind}`;
  const label = t(key);
  return label === key ? kind : label;
}
