/**
 * 模板市场 → 模板目录.
 *
 * Published catalogue, one fixed version at a time, and install with the
 * explicit consent the service re-validates: the acknowledgement checkbox
 * starts unchecked, and the request carries the exact version id, license hash
 * and capability digest plus the per-slot rebinding values.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Checkbox,
  Form,
  Input,
  Modal,
  Space,
  Spin,
  Table,
  Tag,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { message } from "@/utils/antdMessage";
import { DownloadCloud, FileJson, RefreshCw, Store } from "lucide-react";
import { useTranslation } from "react-i18next";
import { EmptyState } from "../../../components/EmptyState";
import { ResizableTable } from "../../../components/ResizableTable";
import { TabPanelHeader } from "../../Settings/AdvancedSettings/TabPanelHeader";
import { formatServerIsoDateTime } from "../../../utils/formatMessageTime";
import { useServerTimezone } from "../../../hooks/useServerTimezone";
import {
  capabilitiesDigest,
  isMarketplaceUnavailableError,
  MarketplaceDigestUnavailableError,
  workbuddyMarketplaceApi,
  type MarketplaceCapabilityDeclaration,
  type MarketplaceInstallRequest,
  type MarketplaceInstallResult,
  type MarketplaceTemplate,
  type MarketplaceTemplateVersion,
} from "../../../api/modules/workbuddyMarketplace";
import { describeApiError, isUuid } from "./helpers";
import styles from "./index.module.less";

const { Text, Paragraph } = Typography;

interface InstallFormValues {
  workflow_name?: string;
  /** ``<kind>.<slot>`` → same-tenant object id. */
  slots?: Record<string, string | undefined>;
  consent?: boolean;
}

/** Kinds whose declaration names a rebinding slot the installer must fill. */
const SLOT_KINDS: Record<string, true> = {
  knowledge_base: true,
  approver: true,
  credential: true,
};

type SlotKind = "knowledge_base" | "approver" | "credential";

function isSlotKind(
  kind: MarketplaceCapabilityDeclaration["kind"],
): kind is SlotKind {
  return kind in SLOT_KINDS;
}

export default function TemplatesPanel({
  onInstalled,
}: {
  onInstalled: (installationId: string) => void;
}) {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  const [installForm] = Form.useForm<InstallFormValues>();

  const [templates, setTemplates] = useState<MarketplaceTemplate[]>([]);
  const [loading, setLoading] = useState(true);
  const [unavailable, setUnavailable] = useState(false);

  const [selected, setSelected] = useState<MarketplaceTemplate | null>(null);
  const [version, setVersion] = useState<MarketplaceTemplateVersion | null>(
    null,
  );
  const [versionIdInput, setVersionIdInput] = useState("");
  const [versionLoading, setVersionLoading] = useState(false);
  const [versionError, setVersionError] = useState<string | null>(null);

  const [installOpen, setInstallOpen] = useState(false);
  const [installSaving, setInstallSaving] = useState(false);
  const [installResult, setInstallResult] =
    useState<MarketplaceInstallResult | null>(null);

  const loadTemplates = useCallback(async () => {
    setLoading(true);
    try {
      setTemplates(await workbuddyMarketplaceApi.listTemplates());
      setUnavailable(false);
    } catch (error) {
      if (isMarketplaceUnavailableError(error)) {
        setUnavailable(true);
        setTemplates([]);
      } else {
        message.error(
          describeApiError(
            error,
            t("workbuddy.marketplace.templates.loadFailed"),
            t,
          ),
        );
      }
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    void loadTemplates();
  }, [loadTemplates]);

  const openVersion = useCallback(
    async (templateId: string, versionId: string) => {
      setVersionLoading(true);
      setVersionError(null);
      try {
        const detail = await workbuddyMarketplaceApi.getTemplateVersion(
          templateId,
          versionId,
        );
        setVersion(detail);
        setVersionIdInput(detail.template_version_id);
      } catch (error) {
        setVersion(null);
        setVersionError(
          describeApiError(
            error,
            t("workbuddy.marketplace.templates.versionLoadFailed"),
            t,
          ),
        );
      } finally {
        setVersionLoading(false);
      }
    },
    [t],
  );

  const selectTemplate = useCallback(
    (row: MarketplaceTemplate) => {
      setSelected(row);
      setInstallResult(null);
      if (row.current_version_id) {
        void openVersion(row.id, row.current_version_id);
      } else {
        setVersion(null);
        setVersionIdInput("");
        setVersionError(null);
      }
    },
    [openVersion],
  );

  const slotDeclarations = useMemo(
    () =>
      (version?.required_capabilities ?? []).filter((entry) =>
        isSlotKind(entry.kind),
      ),
    [version],
  );

  const openInstall = useCallback(() => {
    setInstallResult(null);
    installForm.resetFields();
    setInstallOpen(true);
  }, [installForm]);

  const install = useCallback(
    async (values: InstallFormValues) => {
      if (!selected || !version) return;
      setInstallSaving(true);
      try {
        const digest = await capabilitiesDigest(
          version.required_capabilities ?? [],
        );
        const knowledgeBases: Record<string, string> = {};
        const approvers: Record<string, string> = {};
        const credentials: Record<string, string> = {};
        for (const declaration of slotDeclarations) {
          const value =
            values.slots?.[`${declaration.kind}.${declaration.key}`];
          const trimmed = value?.trim();
          if (!trimmed) continue;
          if (declaration.kind === "knowledge_base")
            knowledgeBases[declaration.key] = trimmed;
          else if (declaration.kind === "approver")
            approvers[declaration.key] = trimmed;
          else credentials[declaration.key] = trimmed;
        }
        const body: MarketplaceInstallRequest = {
          template_version_id: version.template_version_id,
          consent: {
            accepted: true,
            template_version_id: version.template_version_id,
            license_text_hash: version.license_text_hash,
            capabilities_hash: digest,
          },
          bindings: { knowledge_bases: knowledgeBases, approvers },
          credential_bindings: credentials,
          workflow_name: values.workflow_name?.trim() || undefined,
        };
        const result = await workbuddyMarketplaceApi.installTemplate(
          selected.id,
          body,
        );
        setInstallResult(result);
        onInstalled(result.installation.id);
        message.success(t("workbuddy.marketplace.install.accepted"));
      } catch (error) {
        if (error instanceof MarketplaceDigestUnavailableError) {
          message.error(t("workbuddy.marketplace.consent.digestFailed"));
        } else {
          message.error(
            describeApiError(
              error,
              t("workbuddy.marketplace.install.failed"),
              t,
            ),
          );
        }
      } finally {
        setInstallSaving(false);
      }
    },
    [onInstalled, selected, slotDeclarations, t, version],
  );

  const columns: ColumnsType<MarketplaceTemplate> = [
    {
      title: t("workbuddy.marketplace.templates.columns.name"),
      dataIndex: "name",
      key: "name",
      width: 240,
      render: (value: string, row) => (
        <Space direction="vertical" size={0}>
          <span>{value}</span>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {row.slug}
          </Text>
        </Space>
      ),
    },
    {
      title: t("workbuddy.marketplace.templates.columns.industry"),
      dataIndex: "industry",
      key: "industry",
      width: 140,
      render: (value: string | null) => value?.trim() || "—",
    },
    {
      title: t("workbuddy.marketplace.templates.columns.publisher"),
      dataIndex: "publisher_display",
      key: "publisher_display",
      width: 160,
      render: (value: string | null) => value?.trim() || "—",
    },
    {
      title: t("workbuddy.marketplace.templates.columns.version"),
      dataIndex: "current_version",
      key: "current_version",
      width: 110,
      render: (value: string | null | undefined) => value?.trim() || "—",
    },
    {
      title: t("workbuddy.marketplace.templates.columns.license"),
      dataIndex: "license_id",
      key: "license_id",
      width: 140,
      render: (value: string | null | undefined) => value?.trim() || "—",
    },
    {
      title: t("workbuddy.marketplace.templates.columns.publishedAt"),
      dataIndex: "published_at",
      key: "published_at",
      width: 180,
      render: (value: string | null | undefined) =>
        value ? formatServerIsoDateTime(value, timeZone) : "—",
    },
    {
      title: t("common.actions"),
      key: "actions",
      width: 140,
      render: (_value, row) => (
        <Button
          type="link"
          size="small"
          icon={<FileJson size={14} />}
          onClick={() => selectTemplate(row)}
        >
          {t("workbuddy.marketplace.templates.viewVersion")}
        </Button>
      ),
    },
  ];

  const capabilityColumns: ColumnsType<MarketplaceCapabilityDeclaration> = [
    {
      title: t("workbuddy.marketplace.capability.columns.kind"),
      dataIndex: "kind",
      key: "kind",
      width: 160,
      render: (kind: MarketplaceCapabilityDeclaration["kind"]) => (
        <Tag>{t(`workbuddy.marketplace.capability.kind.${kind}`)}</Tag>
      ),
    },
    {
      title: t("workbuddy.marketplace.capability.columns.key"),
      dataIndex: "key",
      key: "key",
      width: 200,
    },
    {
      title: t("workbuddy.marketplace.capability.columns.label"),
      dataIndex: "label",
      key: "label",
      width: 180,
      render: (value: string | undefined) => value?.trim() || "—",
    },
    {
      title: t("workbuddy.marketplace.capability.columns.effect"),
      dataIndex: "effect",
      key: "effect",
      width: 150,
      render: (value: string | null | undefined) =>
        value ? t(`workbuddy.marketplace.capability.effect.${value}`) : "—",
    },
    {
      title: t("workbuddy.marketplace.capability.columns.revision"),
      dataIndex: "revision_id",
      key: "revision_id",
      width: 280,
      render: (value: string | null | undefined) => value?.trim() || "—",
    },
    {
      title: t("workbuddy.marketplace.capability.columns.required"),
      dataIndex: "required",
      key: "required",
      width: 110,
      render: (value: boolean | undefined) => (
        <Tag color={value === false ? "default" : "blue"}>
          {t(
            value === false
              ? "workbuddy.marketplace.capability.optional"
              : "workbuddy.marketplace.capability.required",
          )}
        </Tag>
      ),
    },
  ];

  const header = (
    <TabPanelHeader
      icon={<Store size={18} />}
      title={t("workbuddy.marketplace.templates.title")}
      description={t("workbuddy.marketplace.templates.desc")}
      actions={
        <Button
          size="small"
          icon={<RefreshCw size={14} />}
          onClick={() => void loadTemplates()}
        >
          {t("common.refresh")}
        </Button>
      }
    />
  );

  if (unavailable) {
    return (
      <div className={styles.panel}>
        {header}
        <EmptyState
          variant="error"
          title={t("workbuddy.marketplace.unavailable.title")}
          description={t("workbuddy.marketplace.unavailable.hint")}
          actionLabel={t("common.refresh")}
          onAction={() => void loadTemplates()}
        />
      </div>
    );
  }

  return (
    <div className={styles.panel}>
      {header}

      {loading && templates.length === 0 ? (
        <div className={styles.centered}>
          <Spin />
        </div>
      ) : templates.length === 0 ? (
        <EmptyState
          variant="mascot"
          title={t("workbuddy.marketplace.templates.empty")}
          description={t("workbuddy.marketplace.templates.emptyHint")}
        />
      ) : (
        <ResizableTable
          columns={columns}
          dataSource={templates}
          rowKey="id"
          size="middle"
          tableLayout="fixed"
          scroll={{ x: 1080 }}
          storageKey="workbuddy-marketplace-templates-table-widths"
          minWidth={72}
          pagination={false}
          onRow={(row) => ({
            onClick: () => selectTemplate(row),
            style: { cursor: "pointer" },
          })}
          rowClassName={(row) =>
            row.id === selected?.id ? styles.selectedRow : ""
          }
        />
      )}

      {selected && (
        <section className={styles.sectionBlock}>
          <div className={styles.sectionTitleRow}>
            <h3 className={styles.sectionTitle}>
              {t("workbuddy.marketplace.templates.versionTitle", {
                name: selected.name,
              })}
            </h3>
            <Space size={8} wrap>
              {version && (
                <Tag color="green">
                  {t("workbuddy.marketplace.templates.versionTag", {
                    version: version.version,
                  })}
                </Tag>
              )}
              <Button
                type="primary"
                size="small"
                icon={<DownloadCloud size={14} />}
                disabled={!version}
                onClick={openInstall}
              >
                {t("workbuddy.marketplace.install.button")}
              </Button>
            </Space>
          </div>

          {!version && (
            <Space.Compact style={{ width: "100%", maxWidth: 560 }}>
              <Input
                value={versionIdInput}
                onChange={(event) => setVersionIdInput(event.target.value)}
                placeholder={t(
                  "workbuddy.marketplace.templates.versionIdPlaceholder",
                )}
              />
              <Button
                loading={versionLoading}
                disabled={!isUuid(versionIdInput)}
                onClick={() =>
                  void openVersion(selected.id, versionIdInput.trim())
                }
              >
                {t("workbuddy.marketplace.templates.loadVersion")}
              </Button>
            </Space.Compact>
          )}

          {versionLoading && (
            <div className={styles.centered}>
              <Spin />
            </div>
          )}

          {versionError && (
            <Alert type="error" showIcon message={versionError} />
          )}

          {version && (
            <>
              <div className={styles.metaGrid}>
                <div>
                  <Text type="secondary">
                    {t("workbuddy.marketplace.templates.versionId")}
                  </Text>
                  <div>{version.template_version_id}</div>
                </div>
                <div>
                  <Text type="secondary">
                    {t("workbuddy.marketplace.templates.licenseId")}
                  </Text>
                  <div>{version.license_id}</div>
                </div>
                <div>
                  <Text type="secondary">
                    {t("workbuddy.marketplace.templates.licenseHash")}
                  </Text>
                  <div className={styles.mono}>{version.license_text_hash}</div>
                </div>
                <div>
                  <Text type="secondary">
                    {t("workbuddy.marketplace.templates.definitionHash")}
                  </Text>
                  <div className={styles.mono}>{version.definition_hash}</div>
                </div>
                <div>
                  <Text type="secondary">
                    {t("workbuddy.marketplace.templates.publishedAt")}
                  </Text>
                  <div>
                    {version.published_at
                      ? formatServerIsoDateTime(version.published_at, timeZone)
                      : "—"}
                  </div>
                </div>
              </div>

              {version.content_summary?.trim() && (
                <Paragraph type="secondary" className={styles.sectionHint}>
                  {version.content_summary}
                </Paragraph>
              )}

              <h4 className={styles.subTitle}>
                {t("workbuddy.marketplace.templates.capabilitiesTitle")}
              </h4>
              <Table
                columns={capabilityColumns}
                dataSource={version.required_capabilities ?? []}
                rowKey={(row) => `${row.kind}:${row.key}`}
                size="small"
                pagination={false}
                scroll={{ x: 1080 }}
              />

              <h4 className={styles.subTitle}>
                {t("workbuddy.marketplace.templates.definitionTitle")}
              </h4>
              <pre className={styles.codeBlock}>
                {JSON.stringify(version.definition, null, 2)}
              </pre>
            </>
          )}
        </section>
      )}

      <Modal
        title={t("workbuddy.marketplace.install.title", {
          version: version?.version ?? "",
        })}
        open={installOpen}
        onCancel={() => setInstallOpen(false)}
        onOk={() => installForm.submit()}
        confirmLoading={installSaving}
        okText={t("workbuddy.marketplace.install.submit")}
        cancelText={t("common.cancel")}
        width={640}
        destroyOnHidden
      >
        <Form
          form={installForm}
          layout="vertical"
          requiredMark={false}
          onFinish={(values) => void install(values)}
        >
          <Form.Item
            name="workflow_name"
            label={t("workbuddy.marketplace.install.workflowName")}
            extra={t("workbuddy.marketplace.install.workflowNameHint")}
          >
            <Input
              maxLength={120}
              placeholder={t(
                "workbuddy.marketplace.install.workflowNamePlaceholder",
              )}
            />
          </Form.Item>

          {slotDeclarations.length > 0 && (
            <>
              <div className={styles.sectionHint}>
                {t("workbuddy.marketplace.bindings.hint")}
              </div>
              {slotDeclarations.map((declaration) => (
                <Form.Item
                  key={`${declaration.kind}.${declaration.key}`}
                  name={["slots", `${declaration.kind}.${declaration.key}`]}
                  label={`${declaration.label?.trim() || declaration.key} · ${t(
                    `workbuddy.marketplace.capability.kind.${declaration.kind}`,
                  )}`}
                  rules={[
                    {
                      validator: (_rule, value: string | undefined) =>
                        !value?.trim() || isUuid(value)
                          ? Promise.resolve()
                          : Promise.reject(
                              new Error(
                                t("workbuddy.marketplace.bindings.invalid"),
                              ),
                            ),
                    },
                  ]}
                >
                  <Input
                    allowClear
                    placeholder={t(
                      "workbuddy.marketplace.bindings.placeholder",
                    )}
                  />
                </Form.Item>
              ))}
            </>
          )}

          <Alert
            type="warning"
            showIcon
            className={styles.notice}
            message={t("workbuddy.marketplace.consent.title")}
            description={t("workbuddy.marketplace.consent.hint", {
              license: version?.license_id ?? "",
              hash: version?.license_text_hash ?? "",
            })}
          />

          <Form.Item
            name="consent"
            valuePropName="checked"
            rules={[
              {
                validator: (_rule, value: boolean | undefined) =>
                  value === true
                    ? Promise.resolve()
                    : Promise.reject(
                        new Error(t("workbuddy.marketplace.consent.required")),
                      ),
              },
            ]}
          >
            <Checkbox>
              {t("workbuddy.marketplace.consent.label", {
                version: version?.version ?? "",
              })}
            </Checkbox>
          </Form.Item>
        </Form>

        {installResult && (
          <Alert
            className={styles.notice}
            type={
              installResult.job?.status === "failed"
                ? "error"
                : installResult.job?.status === "succeeded"
                ? "success"
                : "info"
            }
            showIcon
            message={t("workbuddy.marketplace.install.resultTitle", {
              status: installResult.installation.status,
            })}
            description={
              <Space direction="vertical" size={2}>
                <span>
                  {t("workbuddy.marketplace.install.resultInstallation")}:{" "}
                  <span className={styles.mono}>
                    {installResult.installation.id}
                  </span>
                </span>
                {installResult.job && (
                  <span>
                    {t("workbuddy.marketplace.install.resultJob")}:{" "}
                    {installResult.job.status} ({installResult.job.progress}%)
                  </span>
                )}
                {(installResult.job?.error_code ||
                  installResult.installation.error_code) && (
                  <span>
                    {t("workbuddy.marketplace.install.resultError")}:{" "}
                    <span className={styles.mono}>
                      {installResult.job?.error_code ??
                        installResult.installation.error_code}
                    </span>{" "}
                    {installResult.job?.error_message ??
                      installResult.installation.error_detail}
                  </span>
                )}
              </Space>
            }
          />
        )}
      </Modal>
    </div>
  );
}
