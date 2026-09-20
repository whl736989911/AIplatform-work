/**
 * The canvas draws the definition (A-16).
 *
 * The editing rules live in `definitionCanvas.ts` and are tested there as pure
 * functions; what this file pins is the part only the component can get wrong:
 * that it renders the definition it was given (every step, by the name the JSON
 * carries) rather than an empty stage or a graph of its own invention.
 *
 * React Flow measures its container, which jsdom does not implement, so the
 * observer is stubbed here — the canvas is not being tested for its layout
 * engine, only for what it draws from the definition.
 */

import { render, screen } from "@testing-library/react";
import { beforeAll, describe, expect, it } from "vitest";

import DefinitionCanvas from "./DefinitionCanvas";
import type { GraphDefinition } from "./definitionGraph";

beforeAll(() => {
  class ResizeObserverStub {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  // A library boundary: jsdom has no ResizeObserver, and React Flow asks for the
  // global one. Cast through `unknown` because the stub only needs the three
  // methods React Flow calls, not the full observer contract.
  (globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver =
    ResizeObserverStub;
  // React Flow also reads these off the element when fitting the view.
  Object.defineProperty(HTMLElement.prototype, "offsetHeight", {
    configurable: true,
    value: 420,
  });
  Object.defineProperty(HTMLElement.prototype, "offsetWidth", {
    configurable: true,
    value: 800,
  });
});

function definition(): GraphDefinition {
  return {
    nodes: [
      { id: "start", type: "transform", name: "开始" },
      { id: "price", type: "llm", name: "报价" },
    ],
    edges: [{ from: "start", to: "price" }],
  };
}

describe("DefinitionCanvas", () => {
  it("draws every step the definition declares", async () => {
    render(
      <DefinitionCanvas
        definition={definition()}
        layoutKey="wf-1"
        onChange={() => undefined}
      />,
    );
    // The names come from the JSON: the canvas is a view of it, not a copy.
    expect(await screen.findByText("开始")).toBeTruthy();
    expect(await screen.findByText("报价")).toBeTruthy();
    expect(screen.getByTestId("workbuddy-definition-canvas")).toBeTruthy();
  });
});
