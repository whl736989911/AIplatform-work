/**
 * WorkBuddy workbench → 运行指标 (A-24).
 *
 * The same six numbers the promotion gates judge on, over whatever scope the
 * caller has: a member sees the runs they started, an admin sees the tenant's.
 * The block renders what the server computed and *says* which scope it is, rather
 * than leaving the reader to infer it — a workbench that quietly showed tenant
 * numbers to a member, or member numbers to an admin who thinks they are looking
 * at the tenant, would be worse than showing nothing.
 *
 * Nothing is recomputed here. Reporting built on a second set of definitions is
 * how a dashboard and a gate end up disagreeing about the same week.
 */

import { useCallback } from "react";
import { Statistic, Table, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { Gauge } from "lucide-react";
import { useTranslation } from "react-i18next";

import type {
  ExecutionMetrics,
  WorkflowMetricsPayload,
} from "../../../api/modules/workbuddyRuntime";
import {
  ResourceState,
  type WorkBuddyResource,
} from "../Workflows/consoleState";
import Section from "./Section";

const { Text } = Typography;

function percent(value: number): string {
  return `${(value * 100).toFixed(1)}%`;
}

export default function MetricsBlock({
  resource,
  workflowNames,
}: {
  resource: WorkBuddyResource<ExecutionMetrics | null>;
  /** workflow id → name, from the workbench's workflow list. */
  workflowNames: Record<string, string>;
}) {
  const { t } = useTranslation();
  const metrics = resource.data;
  const label = useCallback(
    (id: string): string => workflowNames[id] ?? id.slice(0, 8),
    [workflowNames],
  );

  const columns: ColumnsType<WorkflowMetricsPayload> = [
    {
      title: t("workbuddy.metrics.columnWorkflow"),
      dataIndex: "workflow_id",
      key: "workflow_id",
      render: (value: string) => <Text>{label(value)}</Text>,
    },
    {
      title: t("workbuddy.metrics.columnSettled"),
      dataIndex: "settled_runs",
      key: "settled_runs",
      width: 100,
    },
    {
      title: t("workbuddy.metrics.columnSuccess"),
      dataIndex: "success_rate",
      key: "success_rate",
      width: 100,
      render: (value: number) => <Text>{percent(value)}</Text>,
    },
    {
      title: t("workbuddy.metrics.columnP95"),
      dataIndex: "p95_latency_ms",
      key: "p95_latency_ms",
      width: 110,
      render: (value: number) => <Text>{Math.round(value)} ms</Text>,
    },
  ];

  const failed = resource.error !== null;
  const loading = resource.loading;
  const empty =
    !failed &&
    !loading &&
    (metrics === null || metrics.settled.settled_runs === 0);

  return (
    <Section
      icon={<Gauge size={16} />}
      title={t("workbuddy.metrics.title")}
      wide
    >
      {failed || loading || empty || metrics === null ? (
        <ResourceState
          resource={resource}
          isEmpty={empty}
          loadingLabel={t("common.loading")}
          errorTitle={t("workbuddy.shared.loadFailed")}
          errorFallback={t("common.unknownError")}
          unavailableTitle={t("workbuddy.shared.notMergedTitle")}
          unavailableHint={t("workbuddy.shared.notMergedHint")}
          emptyTitle={t("workbuddy.metrics.empty")}
          emptyHint={t("workbuddy.metrics.emptyHint")}
        />
      ) : (
        <>
          <Tag color={metrics.scope === "tenant" ? "blue" : "default"}>
            {t(`workbuddy.metrics.scope.${metrics.scope}`)}
          </Tag>
          <div
            style={{
              display: "flex",
              gap: 32,
              flexWrap: "wrap",
              margin: "12px 0 16px",
            }}
          >
            <Statistic
              title={t("workbuddy.metrics.settled")}
              value={metrics.settled.settled_runs}
            />
            <Statistic
              title={t("workbuddy.metrics.successRate")}
              value={percent(metrics.settled.success_rate)}
            />
            <Statistic
              title={t("workbuddy.metrics.p95")}
              value={Math.round(metrics.settled.p95_latency_ms)}
              suffix="ms"
            />
            <Statistic
              title={t("workbuddy.metrics.avgTokens")}
              value={Math.round(metrics.settled.avg_tokens)}
            />
            <Statistic
              title={t("workbuddy.metrics.wait")}
              value={Math.round(metrics.settled.wait_ms)}
              suffix="ms"
            />
          </div>
          <Table<WorkflowMetricsPayload>
            rowKey="workflow_id"
            size="small"
            columns={columns}
            dataSource={metrics.workflows}
            pagination={false}
            scroll={{ x: 520 }}
          />
        </>
      )}
    </Section>
  );
}
