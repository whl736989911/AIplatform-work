/**
 * 企业治理 → 能力许可.
 * Tenant admins atomically replace the tenant's published tool/model allow-list
 * and pick the default model. Ids are catalog revision ids; omitted ids are
 * cleared by the API, so the form always sends all three fields.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { Button, Form, Select, Space, Spin, Tag, Typography } from "antd";
import { message } from "@/utils/antdMessage";
import { Save, SlidersHorizontal } from "lucide-react";
import { useTranslation } from "react-i18next";
import { EmptyState } from "../../components/EmptyState";
import { useAsyncResource } from "../../hooks/useAsyncResource";
import { useServerTimezone } from "../../hooks/useServerTimezone";
import { formatServerDateTime } from "../../utils/formatMessageTime";
import { apiErrorMessage } from "../../utils/apiError";
import {
  catalogItemLabel,
  enterpriseApi,
  type CatalogItem,
  type TenantCapabilities,
} from "../../api/modules/enterprise";
import { TabPanelHeader } from "../Settings/AdvancedSettings/TabPanelHeader";
import styles from "./index.module.less";

const { Text } = Typography;

interface CapabilitiesFormValues {
  tool_ids: string[];
  model_ids: string[];
  default_model_id?: string | null;
}

export default function CapabilitiesPanel() {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  const [form] = Form.useForm<CapabilitiesFormValues>();
  const {
    data: capabilities,
    loading,
    refresh,
    setData,
  } = useAsyncResource<TenantCapabilities | null>(
    null,
    () => enterpriseApi.getCapabilities(),
    [],
    { errorFallback: t("tenantGovernance.capabilities.loadFailed"), t },
  );
  const { data: tools } = useAsyncResource<CatalogItem[]>(
    [],
    async () => (await enterpriseApi.toolCatalog()).items,
    [],
  );
  const { data: models } = useAsyncResource<CatalogItem[]>(
    [],
    async () => (await enterpriseApi.modelCatalog()).items,
    [],
  );
  const [saving, setSaving] = useState(false);
  const [selectedModelIds, setSelectedModelIds] = useState<string[]>([]);

  useEffect(() => {
    if (!capabilities) return;
    form.setFieldsValue({
      tool_ids: capabilities.tool_ids,
      model_ids: capabilities.model_ids,
      default_model_id: capabilities.default_model_id,
    });
    setSelectedModelIds(capabilities.model_ids);
  }, [capabilities, form]);

  const toolOptions = useMemo(
    () =>
      tools.map((item) => ({ value: item.id, label: catalogItemLabel(item) })),
    [tools],
  );

  const modelOptions = useMemo(
    () =>
      models.map((item) => ({ value: item.id, label: catalogItemLabel(item) })),
    [models],
  );

  const defaultModelOptions = useMemo(
    () =>
      modelOptions.filter((option) => selectedModelIds.includes(option.value)),
    [modelOptions, selectedModelIds],
  );

  const onSave = useCallback(
    async (values: CapabilitiesFormValues) => {
      setSaving(true);
      try {
        const updated = await enterpriseApi.updateCapabilities({
          tool_ids: values.tool_ids ?? [],
          model_ids: values.model_ids ?? [],
          default_model_id: values.default_model_id ?? null,
        });
        setData(updated);
        form.setFieldsValue({
          tool_ids: updated.tool_ids,
          model_ids: updated.model_ids,
          default_model_id: updated.default_model_id,
        });
        setSelectedModelIds(updated.model_ids);
        message.success(t("tenantGovernance.capabilities.saveSuccess"));
      } catch (err) {
        message.error(
          apiErrorMessage(err, t("tenantGovernance.capabilities.saveFailed"), t),
        );
      } finally {
        setSaving(false);
      }
    },
    [form, setData, t],
  );

  return (
    <div className={styles.panel}>
      <TabPanelHeader
        icon={<SlidersHorizontal size={18} />}
        title={t("tenantGovernance.capabilities.title")}
        description={t("tenantGovernance.capabilities.desc")}
        actions={
          <Space size={8} wrap>
            {capabilities && (
              <Tag color="blue">
                {t("tenantGovernance.capabilities.revision", {
                  revision: capabilities.revision,
                })}
              </Tag>
            )}
            {capabilities?.updated_at && (
              <Tag>
                {t("tenantGovernance.capabilities.updatedAt", {
                  time: formatServerDateTime(capabilities.updated_at, timeZone),
                })}
              </Tag>
            )}
            <Button
              size="small"
              onClick={() => void refresh()}
              loading={loading}
            >
              {t("common.refresh")}
            </Button>
          </Space>
        }
      />

      {loading && !capabilities ? (
        <div className={styles.centered}>
          <Spin />
        </div>
      ) : !capabilities ? (
        <EmptyState
          variant="error"
          title={t("tenantGovernance.capabilities.loadFailed")}
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
          <Form.Item
            name="tool_ids"
            label={t("tenantGovernance.capabilities.tools")}
            extra={t("tenantGovernance.capabilities.toolsHint")}
          >
            <Select
              mode="multiple"
              allowClear
              showSearch
              optionFilterProp="label"
              placeholder={t("tenantGovernance.capabilities.toolsPlaceholder")}
              options={toolOptions}
            />
          </Form.Item>

          <Form.Item
            name="model_ids"
            label={t("tenantGovernance.capabilities.models")}
            extra={t("tenantGovernance.capabilities.modelsHint")}
          >
            <Select
              mode="multiple"
              allowClear
              showSearch
              optionFilterProp="label"
              placeholder={t("tenantGovernance.capabilities.modelsPlaceholder")}
              options={modelOptions}
              onChange={(value) => {
                const next = value ?? [];
                setSelectedModelIds(next);
                const current = form.getFieldValue("default_model_id");
                if (typeof current === "string" && !next.includes(current)) {
                  form.setFieldValue("default_model_id", null);
                }
              }}
            />
          </Form.Item>

          <Form.Item
            name="default_model_id"
            label={t("tenantGovernance.capabilities.defaultModel")}
            extra={t("tenantGovernance.capabilities.defaultModelHint")}
          >
            <Select
              allowClear
              showSearch
              optionFilterProp="label"
              placeholder={t(
                "tenantGovernance.capabilities.defaultModelPlaceholder",
              )}
              options={defaultModelOptions}
            />
          </Form.Item>

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
              {t("tenantGovernance.capabilities.saveHint")}
            </Text>
          </div>
        </Form>
      )}
    </div>
  );
}
