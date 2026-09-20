/**
 * Definition ↔ canvas (A-16).
 *
 * The canvas is a second view of the same workflow, not a second source of
 * truth: the JSON definition stays authoritative for structure, and everything
 * this module does is a deterministic translation between the two so a drag and
 * an edit can never disagree about what the workflow is.
 *
 * Two deliberate separations:
 *
 * * **Structure vs layout.** The definition schema has no coordinates, and that
 *   is right — where a box sits on someone's screen is not part of what the
 *   workflow does. So positions live in a per-workflow layout store (a view
 *   concern, kept beside the editor's other view state), while nodes and edges
 *   come from the definition. Moving a box therefore changes nothing in the JSON;
 *   connecting two boxes does.
 * * **Editing edges, not nodes.** A new node needs a type and its required
 *   fields, which is the wizard's job (driven by the server's metadata). The
 *   canvas edits the *graph* — which step feeds which — and hands node creation
 *   back to the panel that already knows how to do it.
 *
 * Positions for a definition that has never been laid out are computed here, so
 * the first render is stable and the same definition always looks the same.
 */

import type { Edge, Node } from "@xyflow/react";

/** A definition as far as this module cares: nodes and the edges between them. */
export interface GraphDefinition {
  nodes?: Array<{ id?: unknown; type?: unknown; name?: unknown }>;
  edges?: Array<{ from?: unknown; to?: unknown }>;
}

export interface CanvasGraph {
  nodes: Node[];
  edges: Edge[];
}

/** Where the boxes were last put, per workflow. A view concern, never stored in the definition. */
export type CanvasLayout = Record<string, { x: number; y: number }>;

export const LAYOUT_STORAGE_PREFIX = "workbuddy-canvas-layout:";

const COLUMN_WIDTH = 240;
const ROW_HEIGHT = 96;

/**
 * Deterministic initial layout: one column per dependency depth.
 *
 * A workflow that has never been laid out must not be laid out randomly — the
 * same definition has to look the same every time it is opened, or the canvas
 * would teach the reader a graph that is not there.
 */
export function derivedLayout(definition: GraphDefinition): CanvasLayout {
  const ids = nodeIds(definition);
  const incoming = new Map<string, string[]>(ids.map((id) => [id, []]));
  for (const edge of definition.edges ?? []) {
    const from = String(edge?.from ?? "");
    const to = String(edge?.to ?? "");
    if (incoming.has(to) && incoming.has(from)) {
      incoming.get(to)?.push(from);
    }
  }
  const depth = new Map<string, number>();
  const visit = (id: string, seen: Set<string>): number => {
    const cached = depth.get(id);
    if (cached !== undefined) return cached;
    if (seen.has(id)) return 0; // A cycle is the compiler's to reject; layout must not hang.
    seen.add(id);
    const parents = incoming.get(id) ?? [];
    const value =
      parents.length === 0
        ? 0
        : 1 + Math.max(...parents.map((p) => visit(p, seen)));
    seen.delete(id);
    depth.set(id, value);
    return value;
  };
  const rows = new Map<number, number>();
  const layout: CanvasLayout = {};
  for (const id of ids) {
    const column = visit(id, new Set());
    const row = rows.get(column) ?? 0;
    rows.set(column, row + 1);
    layout[id] = { x: column * COLUMN_WIDTH, y: row * ROW_HEIGHT };
  }
  return layout;
}

function nodeIds(definition: GraphDefinition): string[] {
  return (definition.nodes ?? [])
    .map((node) => String(node?.id ?? ""))
    .filter((id) => id.length > 0);
}

/** The definition as canvas nodes and edges, using saved positions where they exist. */
export function toCanvas(
  definition: GraphDefinition,
  layout?: CanvasLayout,
): CanvasGraph {
  const fallback = derivedLayout(definition);
  const nodes: Node[] = (definition.nodes ?? []).map((node) => {
    const id = String(node?.id ?? "");
    return {
      id,
      position: layout?.[id] ?? fallback[id] ?? { x: 0, y: 0 },
      data: {
        label: String(node?.name ?? "") || id,
        kind: String(node?.type ?? ""),
      },
      type: "default",
    };
  });
  const known = new Set(nodes.map((node) => node.id));
  const edges: Edge[] = [];
  const seen = new Set<string>();
  for (const edge of definition.edges ?? []) {
    const from = String(edge?.from ?? "");
    const to = String(edge?.to ?? "");
    // An edge to a step that does not exist cannot be drawn and is not added;
    // the definition's own validation is what reports it.
    if (!known.has(from) || !known.has(to) || from === to) continue;
    const key = `${from}->${to}`;
    if (seen.has(key)) continue;
    seen.add(key);
    edges.push({ id: key, source: from, target: to });
  }
  return { nodes, edges };
}

/**
 * The structural edits a canvas may make, as a new edge list.
 *
 * Only edges: node membership is not the canvas's to change, so a node dragged
 * off the canvas or deleted there does not silently disappear from the workflow.
 */
export function edgesFromCanvas(
  edges: Edge[],
): Array<{ from: string; to: string }> {
  const result: Array<{ from: string; to: string }> = [];
  const seen = new Set<string>();
  for (const edge of edges) {
    const from = String(edge.source ?? "");
    const to = String(edge.target ?? "");
    if (!from || !to || from === to) continue;
    const key = `${from}->${to}`;
    if (seen.has(key)) continue;
    seen.add(key);
    result.push({ from, to });
  }
  return result;
}

/**
 * The definition with the canvas's connections applied.
 *
 * Edges are taken **as the canvas shows them**, so removing a connection removes
 * it from the workflow — which is what makes the canvas an editor rather than a
 * picture. Everything else about the definition is returned untouched.
 */
export function withCanvasEdges<T extends GraphDefinition>(
  definition: T,
  edges: Edge[],
): T {
  const known = new Set(nodeIds(definition));
  // The same rule ``toCanvas`` applies, so the two never disagree: a connection
  // the canvas cannot draw is not written back either. A step removed elsewhere
  // therefore takes its connections with it, instead of leaving edges behind
  // that point at nothing.
  const kept = edgesFromCanvas(edges).filter(
    (edge) => known.has(edge.from) && known.has(edge.to),
  );
  return { ...definition, edges: kept } as T;
}

/** Whether two edge lists describe the same connections, order aside. */
export function sameConnections(
  left: ReadonlyArray<{ from?: unknown; to?: unknown }>,
  right: ReadonlyArray<{ from?: unknown; to?: unknown }>,
): boolean {
  const key = (edge: { from?: unknown; to?: unknown }) =>
    `${String(edge?.from ?? "")}->${String(edge?.to ?? "")}`;
  const a = new Set(left.map(key));
  const b = new Set(right.map(key));
  return a.size === b.size && [...a].every((item) => b.has(item));
}

/** Saved positions for one workflow, or an empty layout. */
export function readLayout(
  workflowKey: string,
  storage: Storage | null = null,
): CanvasLayout {
  const store =
    storage ?? (typeof window === "undefined" ? null : window.localStorage);
  if (store === null) return {};
  try {
    const raw = store.getItem(LAYOUT_STORAGE_PREFIX + workflowKey);
    if (!raw) return {};
    const parsed: unknown = JSON.parse(raw);
    if (parsed === null || typeof parsed !== "object") return {};
    const layout: CanvasLayout = {};
    for (const [id, value] of Object.entries(
      parsed as Record<string, unknown>,
    )) {
      const point = value as { x?: unknown; y?: unknown };
      if (typeof point?.x === "number" && typeof point?.y === "number") {
        layout[id] = { x: point.x, y: point.y };
      }
    }
    return layout;
  } catch {
    // A layout that cannot be read is not worth failing a screen over: the
    // definition can always be laid out again, deterministically.
    return {};
  }
}

export function writeLayout(
  workflowKey: string,
  layout: CanvasLayout,
  storage: Storage | null = null,
): void {
  const store =
    storage ?? (typeof window === "undefined" ? null : window.localStorage);
  if (store === null) return;
  try {
    store.setItem(LAYOUT_STORAGE_PREFIX + workflowKey, JSON.stringify(layout));
  } catch {
    // Storage being unavailable (private mode, quota) must not break editing.
  }
}
