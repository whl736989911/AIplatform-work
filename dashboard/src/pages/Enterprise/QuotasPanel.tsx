/**
 * 企业治理 → 配额.
 *
 * The API returns one row per quota metric (tenant limit, platform hard cap,
 * current usage and unit) plus a `quotas` map. Limits are edited per metric and
 * submitted as `{ quotas: { metric: limit } }`; every metric is validated
 * against its hard cap server-side.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { Button, Form, InputNumber, Spin, Tag, Typography } from "antd";
import { message } from "@/utils/antdMessage";
import { Gauge, RefreshCw, Save } from "lucide-react";
import { useTranslation } from "react-i18next";
import { EmptyState } from "../../components/EmptyState";
import { useAsyncResource } from "../../hooks/useAsyncResource";
import { apiErrorMessage } from "../../utils/apiError";
import {
  enterpriseApi,
  type QuotaMetric,
  type TenantQuotas,
} from "../../api/modules/enterprise";
import { TabPanelHeader } from "../Settings/AdvancedSettings/TabPanelHeader";
import styles from "./index.module.less";

const { Text } = Typography;

/** Known metric keys; unknown keys fall back to the raw metric name. */
const METRIC_LABEL_KEYS: Record<string, string> = {
  users: "tenantGovernance.quotas.metric.users",
  departments: "tenantGovernance.quotas.metric.departments",
  agents: "tenantGovernance.quotas.metric.agents",
  connectors: "tenantGovernance.quotas.metric.connectors",
  storage_mb: "tenantGovernance.quotas.metric.storageMb",
  monthly_tokens: "tenantGovernance.quotas.metric.monthlyTokens",
};

export default function QuotasPanel() {
  const { t } = useTranslation();
  const [form] = Form.useForm<Record<string, number>>();
  const {
    data: quotas,
    loading,
    refresh,
    setData,
  } = useAsyncResource<TenantQuotas | null>(
    null,
    () => enterpriseApi.getQuotas(),
    [],
    { errorFallback: t("tenantGovernance.quotas.loadFailed"), t },
  );
  const [saving, setSaving] = useState(false);

  const metrics = useMemo(() => quotas?.items ?? [], [quotas]);

  useEffect(() => {
    if (quotas) form.setFieldsValue(quotas.quotas);
  }, [quotas, form]);

  const metricLabel = useCallback(
    (metric: string) => {
      const key = METRIC_LABEL_KEYS[metric];
      return key ? t(key) : metric;
    },
    [t],
  );

  const metricHint = useCallback(
    (row: QuotaMetric) => {
      const parts = [
        t("tenantGovernance.quotas.usedHint", { used: row.used ?? 0 }),
      ];
      if (row.hard_cap !== null && row.hard_cap !== undefined) {
        parts.push(
          t("tenantGovernance.quotas.hardCapHint", { cap: row.hard_cap }),
        );
      }
      if (row.unit) {
        parts.push(t("tenantGovernance.quotas.unitHint", { unit: row.unit }));
      }
      return parts.join(" · ");
    },
    [t],
  );

  const onSave = useCallback(
    async (values: Record<string, number>) => {
      const next: Record<string, number> = {};
      for (const row of metrics) {
        const value = values[row.metric];
        if (typeof value === "number") next[row.metric] = Math.trunc(value);
      }
      setSaving(true);
      try {
        const updated = await enterpriseApi.updateQuotas({ quotas: next });
        setData(updated);
        form.setFieldsValue(updated.quotas);
        message.success(t("tenantGovernance.quotas.saveSuccess"));
      } catch (err) {
        message.error(
          apiErrorMessage(err, t("tenantGovernance.quotas.saveFailed"), t),
        );
      } finally {
        setSaving(false);
      }
    },
    [form, metrics, setData, t],
  );

  return (
    <div className={styles.panel}>
      <TabPanelHeader
        icon={<Gauge size={18} />}
        title={t("tenantGovernance.quotas.title")}
        description={t("tenantGovernance.quotas.desc")}
        actions={
          <Button
            size="small"
            icon={<RefreshCw size={14} />}
            onClick={() => void refresh()}
            loading={loading}
          >
            {t("common.refresh")}
          </Button>
        }
      />

      {loading && !quotas ? (
        <div className={styles.centered}>
          <Spin />
        </div>
      ) : metrics.length === 0 ? (
        <EmptyState
          variant="error"
          title={t("tenantGovernance.quotas.loadFailed")}
          actionLabel={t("common.refresh")}
          onAction={() => void refresh()}
        />
      ) : (
        <Form
          form={form}
          layout="vertical"
          requiredMark={false}
          className={styles.form}
          onFinish={(values) => void onSave(values)}
        >
          <div className={styles.fieldGrid}>
            {metrics.map((row) => (
              <Form.Item
                key={row.metric}
                name={row.metric}
                label={metricLabel(row.metric)}
                extra={metricHint(row)}
                rules={[
                  {
                    required: true,
                    message: t("tenantGovernance.quotas.valueRequired"),
                  },
                ]}
              >
                <InputNumber
                  min={0}
                  max={row.hard_cap ?? undefined}
                  precision={0}
                  style={{ width: "100%" }}
                />
              </Form.Item>
            ))}
          </div>

          <div className={styles.footer}>
            <Button
              type="primary"
              htmlType="submit"
              icon={<Save size={14} />}
              loading={saving}
            >
              {t("common.save")}
            </Button>
            <Text type="secondary" className={styles.footerHint}>
              {t("tenantGovernance.quotas.saveHint")}
            </Text>
            {quotas?.hard_caps && (
              <Tag>
                {t("tenantGovernance.quotas.hardCapCount", {
                  count: Object.keys(quotas.hard_caps).length,
                })}
              </Tag>
            )}
          </div>
        </Form>
      )}
    </div>
  );
}
