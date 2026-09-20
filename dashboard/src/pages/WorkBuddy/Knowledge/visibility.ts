/**
 * The four visibility layers of the knowledge page — 我的 / 部门 / 公司 /
 * 单独授权.
 *
 * The backend already answers *why the calling member may read a base*:
 * ``KnowledgeBase.access_sources`` carries the resolver's own tuple (``owner``,
 * ``department-member``, ``enterprise-member``, ``tenant-admin`` and
 * ``acl:<permission>``). These helpers only re-present that answer, so a layer
 * is never guessed from a base's name, scope or owner, and the caller's own
 * sources stay the only input — a member cannot tell from here what other
 * members were granted.
 *
 * A base can qualify for several layers (an enterprise base that also holds an
 * explicit grant is both 公司 and 单独授权), so the chips union: selecting one
 * never removes a base another selected chip already matched, and no selection
 * shows every readable base. ``tenant-admin`` is deliberately its own reason —
 * an admin reaching another department's base is not a 部门 match, and the
 * access-source column says so.
 */

import type { TFunction } from "i18next";
import type { KnowledgeBase } from "../../../api/modules/workbuddyKnowledge";
import { knowledgeLabel } from "./labels";

/** One chip of the visibility filter. */
export type KnowledgeLayer = "personal" | "department" | "enterprise" | "acl";

/** Declared order of the chips (and of the toggle result). */
export const KNOWLEDGE_LAYERS: readonly KnowledgeLayer[] = [
  "personal",
  "department",
  "enterprise",
  "acl",
];

/** Label namespace of each chip. */
export const KNOWLEDGE_LAYER_LABEL_KEY: Record<KnowledgeLayer, string> = {
  personal: "workbuddy.knowledge.layers.personal",
  department: "workbuddy.knowledge.layers.department",
  enterprise: "workbuddy.knowledge.layers.enterprise",
  acl: "workbuddy.knowledge.layers.acl",
};

/** The resolver source that puts a base into the personal / department / enterprise layer. */
const LAYER_ACCESS_SOURCE: Record<Exclude<KnowledgeLayer, "acl">, string> = {
  personal: "owner",
  department: "department-member",
  enterprise: "enterprise-member",
};

/** Prefix the resolver uses for an explicit grant (``acl:read``, ``acl:write``, …). */
const ACL_SOURCE_PREFIX = "acl:";

/** True when the caller's own access sources include this layer. */
export function baseMatchesLayer(
  base: KnowledgeBase,
  layer: KnowledgeLayer,
): boolean {
  const sources = base.access_sources ?? [];
  if (layer === "acl") {
    return sources.some((source) => source.startsWith(ACL_SOURCE_PREFIX));
  }
  return sources.includes(LAYER_ACCESS_SOURCE[layer]);
}

/**
 * The union of the selected layers in the original order; no selection means
 * every readable base, because visibility itself is decided server-side.
 */
export function filterBasesByLayer(
  bases: readonly KnowledgeBase[],
  layers: readonly KnowledgeLayer[],
): KnowledgeBase[] {
  if (layers.length === 0) return [...bases];
  return bases.filter((base) =>
    layers.some((layer) => baseMatchesLayer(base, layer)),
  );
}

/** Toggle one chip; the result keeps the declared chip order. */
export function toggleKnowledgeLayer(
  layers: readonly KnowledgeLayer[],
  layer: KnowledgeLayer,
): KnowledgeLayer[] {
  if (layers.includes(layer)) {
    return layers.filter((candidate) => candidate !== layer);
  }
  return KNOWLEDGE_LAYERS.filter(
    (candidate) => layers.includes(candidate) || candidate === layer,
  );
}

/** Human-readable name of one resolver source, for the row explanation. */
export function knowledgeAccessSourceLabel(
  t: TFunction,
  source: string,
): string {
  if (source.startsWith(ACL_SOURCE_PREFIX)) {
    return t("workbuddy.knowledge.accessSource.acl", {
      permission: knowledgeLabel(
        t,
        "permission",
        source.slice(ACL_SOURCE_PREFIX.length),
      ),
    });
  }
  const key = ACCESS_SOURCE_KEYS[source];
  // An unknown source stays visible as itself instead of rendering an i18n key.
  return key ? t(key) : source;
}

const ACCESS_SOURCE_KEYS: Record<string, string> = {
  owner: "workbuddy.knowledge.accessSource.owner",
  "department-member": "workbuddy.knowledge.accessSource.departmentMember",
  "enterprise-member": "workbuddy.knowledge.accessSource.enterpriseMember",
  "tenant-admin": "workbuddy.knowledge.accessSource.tenantAdmin",
};
