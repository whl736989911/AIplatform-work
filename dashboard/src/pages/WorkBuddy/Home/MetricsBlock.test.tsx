/**
 * The workbench's run-metrics block (A-24).
 *
 * The numbers are the server's — this block must not invent, relabel or
 * recompute them. Two things are therefore worth pinning: whatever scope the
 * server reported is the scope the reader is told, and the figures shown are the
 * figures received. A workbench that showed a member tenant-wide numbers (or the
 * reverse) would be worse than showing none at all.
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import MetricsBlock from "./MetricsBlock";
import type { ExecutionMetrics } from "../../../api/modules/workbuddyRuntime";
import type { WorkBuddyResource } from "../Workflows/consoleState";

function resource(
  data: ExecutionMetrics | null,
): WorkBuddyResource<ExecutionMetrics | null> {
  return {
    data,
    loading: false,
    error: null,
    reload: async () => undefined,
  } as unknown as WorkBuddyResource<ExecutionMetrics | null>;
}

function metrics(
  scope: "member" | "tenant",
  settled: number,
): ExecutionMetrics {
  return {
    scope,
    window: { since: null, until: null },
    settled: {
      settled_runs: settled,
      success_rate: 0.75,
      p95_latency_ms: 1200,
      avg_tokens: 128,
      safety_violations: 0,
      wait_ms: 4000,
    },
    workflows: [
      {
        workflow_id: "wf-1",
        settled_runs: settled,
        success_rate: 0.75,
        p95_latency_ms: 1200,
        avg_tokens: 128,
        safety_violations: 0,
        wait_ms: 4000,
      },
    ],
  };
}

const NAMES = { "wf-1": "报销问答" };

describe("MetricsBlock", () => {
  it("says which scope the numbers belong to", () => {
    const { unmount } = render(
      <MetricsBlock
        resource={resource(metrics("member", 3))}
        workflowNames={NAMES}
      />,
    );
    expect(screen.getByText("workbuddy.metrics.scope.member")).toBeTruthy();

    // The same block, tenant scope: the label must follow the server, not a guess.
    unmount();
    render(
      <MetricsBlock
        resource={resource(metrics("tenant", 9))}
        workflowNames={NAMES}
      />,
    );
    expect(screen.getByText("workbuddy.metrics.scope.tenant")).toBeTruthy();
    expect(screen.queryByText("workbuddy.metrics.scope.member")).toBeNull();
  });

  it("shows the numbers it was given, and the workflow's own name", () => {
    render(
      <MetricsBlock
        resource={resource(metrics("tenant", 9))}
        workflowNames={NAMES}
      />,
    );
    // Settled count, success rate (as a percentage), and p95 in ms. The count
    // appears twice on purpose — as the scope total and in the breakdown row.
    expect(screen.getAllByText("9").length).toBeGreaterThanOrEqual(2);
    // The rate is the block's own formatting, and both the total and the
    // per-workflow row carry it. (The p95 figure is rendered by antd's Statistic
    // as value + suffix, so asserting its text would test antd, not this block.)
    expect(screen.getAllByText("75.0%").length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText("workbuddy.metrics.p95")).toBeTruthy();
    // The breakdown names the workflow when the workbench knows the name.
    expect(screen.getByText("报销问答")).toBeTruthy();
  });

  it("treats 'no settled runs' as an empty state rather than zeroes", () => {
    render(
      <MetricsBlock
        resource={resource(metrics("member", 0))}
        workflowNames={NAMES}
      />,
    );
    // Zeroes would read as "everything failed"; nothing settled is a different
    // thing, and the empty copy says which.
    expect(screen.getByText("workbuddy.metrics.empty")).toBeTruthy();
    expect(screen.queryByText("workbuddy.metrics.scope.member")).toBeNull();
  });
});
