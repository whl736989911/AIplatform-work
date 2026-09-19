import type { TFunction } from "i18next";
import { apiErrorMessage } from "../../../utils/apiError";
import { parseMarketplaceServerError } from "../../../api/modules/workbuddyMarketplace";

/**
 * Server error text with the server's own error code kept verbatim: the
 * translated/`apiErrors` text stays authoritative, the code is prefixed when
 * the message does not already carry it, and the JSON path a rejection names
 * (private identifier, credential-shaped value, missing binding) is appended.
 */
export function describeApiError(
  error: unknown,
  fallback: string,
  t: TFunction,
): string {
  const text = apiErrorMessage(error, fallback, t);
  const server = parseMarketplaceServerError(error);
  let labelled = text;
  if (server?.code && !labelled.includes(server.code)) {
    labelled = `${server.code}: ${labelled}`;
  }
  if (server?.path && !labelled.includes(server.path)) {
    labelled = `${labelled} · ${t("workbuddy.marketplace.errorPath")}: ${
      server.path
    }`;
  }
  return labelled;
}

/** RFC 4122 shape check for tenant objects, slot bindings and reviewer ids. */
export function isUuid(value: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(
    value.trim(),
  );
}

export type JsonObjectTextResult =
  | { ok: true; value: Record<string, unknown> }
  | { ok: false; reason: "parse"; message: string }
  | { ok: false; reason: "notObject" };

/**
 * Parse a textarea that must hold a JSON object (a public definition). The
 * caller translates the ``notObject`` case; a parser failure keeps its own
 * technical message.
 */
export function parseJsonObjectText(text: string): JsonObjectTextResult {
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
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    return { ok: false, reason: "notObject" };
  }
  return { ok: true, value: parsed as Record<string, unknown> };
}

export type JsonArrayTextResult =
  | { ok: true; value: unknown[] }
  | { ok: false; reason: "parse"; message: string }
  | { ok: false; reason: "notArray" };

/** Parse a textarea that must hold a JSON array (capability declarations). */
export function parseJsonArrayText(text: string): JsonArrayTextResult {
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
  return { ok: true, value: parsed };
}
