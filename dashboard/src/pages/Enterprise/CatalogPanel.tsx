/**
 * 企业治理 → 工具目录 / 模型目录.
 * Read-only published capability metadata for every tenant member.
 */

import { useMemo, useState } from "react";
import { Button, Input, Space, Spin, Tag, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { Cpu, RefreshCw, Search, Wrench } from "lucide-react";
import { useTranslation } from "react-i18next";
import { ResizableTable } from "../../components/ResizableTable";
import { EmptyState } from "../../components/EmptyState";
import { useAsyncResource } from "../../hooks/useAsyncResource";
import { useServerTimezone } from "../../hooks/useServerTimezone";
import { formatServerDateTime } from "../../utils/formatMessageTime";
import {
  catalogItemKey,
  catalogItemLabel,
  enterpriseApi,
  type CatalogItem,
} from "../../api/modules/enterprise";
import { TabPanelHeader } from "../Settings/AdvancedSettings/TabPanelHeader";
import { statusLabel } from "./statusLabels";
import styles from "./index.module.less";

const { Text } = Typography;

export default function CatalogPanel({ kind }: { kind: "tool" | "model" }) {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  const [query, setQuery] = useState("");
  const isTool = kind === "tool";
  const { data: items, loading, refresh } = useAsyncResource<CatalogItem[]>(
    [],
    async () =>
      (isTool
        ? await enterpriseApi.toolCatalog()
        : await enterpriseApi.modelCatalog()
      ).items,
    [isTool],
    {
      errorFallback: t("tenantGovernance.catalog.loadFailed"),
      t,
    },
  );

  const rows = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return items;
    return items.filter((item) => {
      const haystack = `${catalogItemLabel(item)} ${catalogItemKey(item)} ${
        item.description ?? ""
      }`.toLowerCase();
      return haystack.includes(needle);
    });
  }, [items, query]);

  const columns: ColumnsType<CatalogItem> = [
    {
      title: t("tenantGovernance.catalog.displayName"),
      key: "display_name",
      width: 220,
      render: (_value, row) => (
        <Space direction="vertical" size={0}>
          <Text strong>{catalogItemLabel(row)}</Text>
          <Text type="secondary" className={styles.sectionHint}>
            {catalogItemKey(row)}
          </Text>
        </Space>
      ),
    },
    {
      title: t("tenantGovernance.catalog.revision"),
      dataIndex: "revision",
      key: "revision",
      width: 100,
      render: (value: number | null | undefined) => value ?? "—",
    },
    {
      title: t("tenantGovernance.catalog.description"),
      dataIndex: "description",
      key: "description",
      width: 360,
      render: (value: string | null | undefined) =>
        value?.trim() ? value : <Text type="secondary">—</Text>,
    },
    {
      title: t("tenantGovernance.catalog.status"),
      dataIndex: "status",
      key: "status",
      width: 120,
      render: (status: string | null | undefined) =>
        status ? (
          <Tag color={status === "revoked" ? "red" : "green"}>
            {statusLabel(t, "catalog", status)}
          </Tag>
        ) : (
          "—"
        ),
    },
    {
      title: t("tenantGovernance.catalog.publishedAt"),
      dataIndex: "published_at",
      key: "published_at",
      width: 180,
      render: (value: number | null | undefined) =>
        value ? formatServerDateTime(value, timeZone) : "—",
    },
  ];

  return (
    <div className={styles.panel}>
      <TabPanelHeader
        icon={isTool ? <Wrench size={18} /> : <Cpu size={18} />}
        title={t(
          isTool
            ? "tenantGovernance.catalog.toolsTitle"
            : "tenantGovernance.catalog.modelsTitle",
        )}
        description={t("tenantGovernance.catalog.desc")}
        actions={
          <Space size={8} wrap>
            <Input
              allowClear
              size="small"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              prefix={<Search size={14} />}
              placeholder={t("tenantGovernance.catalog.searchPlaceholder")}
              className={styles.searchInput}
            />
            <Button
              size="small"
              icon={<RefreshCw size={14} />}
              onClick={() => void refresh()}
            >
              {t("common.refresh")}
            </Button>
          </Space>
        }
      />

      {loading && items.length === 0 ? (
        <div className={styles.centered}>
          <Spin />
        </div>
      ) : rows.length === 0 ? (
        <EmptyState
          variant="mascot"
          title={
            items.length === 0
              ? t("tenantGovernance.catalog.empty")
              : t("tenantGovernance.catalog.noMatch")
          }
          description={
            items.length === 0
              ? t("tenantGovernance.catalog.emptyHint")
              : t("tenantGovernance.catalog.noMatchHint")
          }
        />
      ) : (
        <ResizableTable
          columns={columns}
          dataSource={rows}
          rowKey="id"
          size="middle"
          tableLayout="fixed"
          scroll={{ x: 980 }}
          storageKey={`enterprise-catalog-${kind}-table-widths`}
          minWidth={72}
          pagination={false}
        />
      )}

      {items.length > 0 && rows.length > 0 && (
        <Text type="secondary" className={styles.sectionHint}>
          {t("tenantGovernance.catalog.countHint", { count: rows.length })}
        </Text>
      )}
    </div>
  );
}
