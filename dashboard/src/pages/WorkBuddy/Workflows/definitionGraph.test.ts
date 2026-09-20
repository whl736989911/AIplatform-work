/**
 * Definition ↔ canvas (A-16).
 *
 * The canvas may not become a second source of truth, so the properties worth
 * testing are about agreement: the JSON that goes out equals what the canvas
 * showed, positions never leak into the definition, and a definition nobody has
 * laid out always looks the same.
 */

import { describe, expect, it } from "vitest";

import {
  LAYOUT_STORAGE_PREFIX,
  derivedLayout,
  readLayout,
  sameConnections,
  toCanvas,
  withCanvasEdges,
  writeLayout,
  type GraphDefinition,
} from "./definitionGraph";

function definition(): GraphDefinition {
  return {
    nodes: [
      { id: "start", type: "transform", name: "开始" },
      { id: "price", type: "llm", name: "报价" },
      { id: "summary", type: "transform", name: "总结" },
    ],
    edges: [
      { from: "start", to: "price" },
      { from: "price", to: "summary" },
    ],
  };
}

function fakeStorage(initial: Record<string, string> = {}): Storage {
  const data = new Map(Object.entries(initial));
  return {
    get length() {
      return data.size;
    },
    clear: () => data.clear(),
    getItem: (key: string) => data.get(key) ?? null,
    key: (index: number) => [...data.keys()][index] ?? null,
    removeItem: (key: string) => void data.delete(key),
    setItem: (key: string, value: string) => void data.set(key, value),
  } satisfies Storage;
}

describe("definitionCanvas", () => {
  it("shows every step and connection, and lays out the same way every time", () => {
    const first = toCanvas(definition());
    const second = toCanvas(definition());
    expect(first.nodes.map((node) => node.id)).toEqual([
      "start",
      "price",
      "summary",
    ]);
    expect(first.edges.map((edge) => edge.id)).toEqual([
      "start->price",
      "price->summary",
    ]);
    // Determinism is the point: the same definition must not look different on
    // two openings, or the canvas would teach a graph that is not there.
    expect(second.nodes.map((node) => node.position)).toEqual(
      first.nodes.map((node) => node.position),
    );
    // Depth is the column: start at 0, its dependent one column right, and so on.
    const byId = Object.fromEntries(
      first.nodes.map((node) => [node.id, node.position]),
    );
    expect(byId.start.x).toBeLessThan(byId.price.x);
    expect(byId.price.x).toBeLessThan(byId.summary.x);
  });

  it("keeps saved positions and ignores an unreadable layout", () => {
    const store = fakeStorage({
      [`${LAYOUT_STORAGE_PREFIX}wf-1`]: JSON.stringify({
        start: { x: 5, y: 7 },
      }),
    });
    expect(readLayout("wf-1", store).start).toEqual({ x: 5, y: 7 });
    // Garbage in storage is not worth failing a screen over.
    store.setItem(`${LAYOUT_STORAGE_PREFIX}wf-2`, "{not json");
    expect(readLayout("wf-2", store)).toEqual({});
    writeLayout("wf-3", { start: { x: 1, y: 2 } }, store);
    expect(readLayout("wf-3", store)).toEqual({ start: { x: 1, y: 2 } });
  });

  it("lays out a cyclic definition without hanging", () => {
    // A cycle is the compiler's to reject; the canvas still has to draw something.
    const layout = derivedLayout({
      nodes: [{ id: "a" }, { id: "b" }],
      edges: [
        { from: "a", to: "b" },
        { from: "b", to: "a" },
      ],
    });
    expect(Object.keys(layout).sort()).toEqual(["a", "b"]);
  });

  it("applies connections as the canvas shows them, and nothing else", () => {
    const base = definition();
    const canvas = toCanvas(base);
    // Connect 开始 → 总结 as well: the definition gains exactly that edge.
    const connected = withCanvasEdges(base, [
      ...canvas.edges,
      { id: "start->summary", source: "start", target: "summary" },
    ]);
    expect(
      sameConnections(connected.edges ?? [], [
        { from: "start", to: "price" },
        { from: "price", to: "summary" },
        { from: "start", to: "summary" },
      ]),
    ).toBe(true);

    // Disconnecting one removes it from the workflow: the canvas is an editor.
    const disconnected = withCanvasEdges(base, [canvas.edges[0]]);
    expect(disconnected.edges).toEqual([{ from: "start", to: "price" }]);
    // Everything else about the definition is untouched.
    expect(disconnected.nodes).toEqual(base.nodes);
  });

  it("never lets a layout change the definition", () => {
    const base = definition();
    const moved = withCanvasEdges(base, toCanvas(base).edges);
    // Moving boxes is a view concern: the round trip through the canvas must not
    // add coordinates (or anything else) to the JSON.
    expect(JSON.stringify(moved)).toBe(JSON.stringify(base));
  });

  it("drops self-connections, duplicates and edges to unknown steps", () => {
    const base = definition();
    const cleaned = withCanvasEdges(base, [
      { id: "self", source: "start", target: "start" },
      { id: "dup1", source: "start", target: "price" },
      { id: "dup2", source: "start", target: "price" },
      { id: "ghost", source: "start", target: "missing" },
    ]);
    // The canvas cannot draw an edge to a step that is not there, and the
    // definition's own validation is what reports the dangling reference.
    expect(cleaned.edges).toEqual([{ from: "start", to: "price" }]);
  });

  it("compares connections without caring about order", () => {
    expect(
      sameConnections(
        [
          { from: "a", to: "b" },
          { from: "b", to: "c" },
        ],
        [
          { from: "b", to: "c" },
          { from: "a", to: "b" },
        ],
      ),
    ).toBe(true);
    expect(
      sameConnections([{ from: "a", to: "b" }], [{ from: "a", to: "c" }]),
    ).toBe(false);
  });
});
