import type { TFunction } from "i18next";
import { apiErrorMessage, parseApiError } from "../../../utils/apiError";
import type { ProposalPatchOperation } from "../../../api/modules/workbuddyProposals";

/**
 * Server error text with the server's own error code kept verbatim: the
 * translated/`apiErrors` text stays authoritative (it already appends the
 * refusal reason the router puts in ``details.reason``) and the code is
 * prefixed when the message does not already carry it.
 */
export function describeApiError(
  error: unknown,
  fallback: string,
  t: TFunction,
): string {
  const text = apiErrorMessage(error, fallback, t);
  const code = parseApiError(error)?.code;
  return code && !text.includes(code) ? `${code}: ${text}` : text;
}

/** RFC 4122 shape check for workflow, proposal and reviewer member ids. */
export function isUuid(value: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(
    value.trim(),
  );
}

export type PatchTextResult =
  | { ok: true; value: ProposalPatchOperation[] }
  | { ok: false; reason: "parse"; message: string }
  | { ok: false; reason: "notArray" }
  | { ok: false; reason: "empty" }
  | { ok: false; reason: "notObject"; index: number };

/** RFC 6902 operations the router accepts (``_PATCH_OPS`` on the slice). */
const PATCH_OPS: Record<string, true> = {
  add: true,
  remove: true,
  replace: true,
  move: true,
  copy: true,
  test: true,
};

/**
 * Parse the RFC 6902 patch a proposal is opened with. The server re-applies the
 * patch to the canonical base and owns the deep rules; this only checks that
 * every entry is an operation object with a known ``op`` and a ``path``.
 */
export function parsePatchText(text: string): PatchTextResult {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text) as unknown;
  } catch (error) {
    return {
      ok: false,
      reason: "parse",
      message: error instanceof Error ? error.message : String(error),
    };
  }
  if (!Array.isArray(parsed)) return { ok: false, reason: "notArray" };
  if (parsed.length === 0) return { ok: false, reason: "empty" };
  const operations: ProposalPatchOperation[] = [];
  for (const [index, entry] of parsed.entries()) {
    if (entry === null || typeof entry !== "object" || Array.isArray(entry)) {
      return { ok: false, reason: "notObject", index };
    }
    const record = entry as Record<string, unknown>;
    const op = typeof record.op === "string" ? record.op : "";
    const path = typeof record.path === "string" ? record.path : "";
    if (!PATCH_OPS[op] || !path) {
      return { ok: false, reason: "notObject", index };
    }
    operations.push({
      op: op as ProposalPatchOperation["op"],
      path,
      value: record.value,
      from: typeof record.from === "string" ? record.from : undefined,
    });
  }
  return { ok: true, value: operations };
}
