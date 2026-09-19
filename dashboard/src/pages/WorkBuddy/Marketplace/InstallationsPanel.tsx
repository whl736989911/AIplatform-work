/**
 * 模板市场 → 安装记录.
 *
 * The frozen manifest exposes this tenant's install ledger
 * (``GET /marketplace/installations``), so the panel opens with the list the
 * caller may read — their own installations, or the whole tenant's when they are
 * a tenant admin — and reads one installation by id for its state, consent
 * evidence, credential bindings and upgrade history. An upgrade needs the target
 * template version and renewed consent for it; nothing is guessed locally.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Checkbox,
  Form,
  Input,
  Modal,
  Select,
  Space,
  Spin,
  Table,
  Tag,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { message } from "@/utils/antdMessage";
import { ArrowUpCircle, RefreshCw, Search } from "lucide-react";
import { useTranslation } from "react-i18next";
import { EmptyState } from "../../../components/EmptyState";
import { TabPanelHeader } from "../../Settings/AdvancedSettings/TabPanelHeader";
import { formatServerIsoDateTime } from "../../../utils/formatMessageTime";
import { useServerTimezone } from "../../../hooks/useServerTimezone";
import {
  capabilitiesDigest,
  isMarketplaceUnavailableError,
  MarketplaceDigestUnavailableError,
  workbuddyMarketplaceApi,
  type MarketplaceCapabilityDeclaration,
  type MarketplaceCredentialBinding,
  type MarketplaceInstallConsent,
  type MarketplaceInstallation,
  type MarketplaceInstallationDetail,
  type MarketplaceTemplate,
  type MarketplaceTemplateVersion,
  type MarketplaceUpgrade,
  type MarketplaceUpgradeRequest,
} from "../../../api/modules/workbuddyMarketplace";
import { describeApiError, isUuid } from "./helpers";
import styles from "./index.module.less";

const { Text } = Typography;

interface UpgradeFormValues {
  template_id: string;
  template_version_id: string;
  slots?: Record<string, string | undefined>;
  consent?: boolean;
}

const SLOT_KINDS: Record<string, true> = {
  knowledge_base: true,
  approver: true,
  credential: true,
};

type SlotKind = "knowledge_base" | "approver" | "credential";

/** One ledger page; the server clamps anything above its own page maximum. */
const LEDGER_PAGE_SIZE = 100;

function isSlotKind(
  kind: MarketplaceCapabilityDeclaration["kind"],
): kind is SlotKind {
  return kind in SLOT_KINDS;
}

function statusColor(status: string): string {
  if (status === "installed" || status === "succeeded") return "green";
  if (status === "failed") return "red";
  if (status === "installing" || status === "running") return "blue";
  return "default";
}

export default function InstallationsPanel({
  knownInstallationIds,
  onOpened,
}: {
  knownInstallationIds: string[];
  onOpened: (installationId: string) => void;
}) {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();
  const [upgradeForm] = Form.useForm<UpgradeFormValues>();

  const [idInput, setIdInput] = useState("");
  const [detail, setDetail] = useState<MarketplaceInstallationDetail | null>(
    null,
  );
  const [loading, setLoading] = useState(false);
  const [unavailable, setUnavailable] = useState(false);
  const [errorText, setErrorText] = useState<string | null>(null);

  const [ledger, setLedger] = useState<MarketplaceInstallation[]>([]);
  const [ledgerLoading, setLedgerLoading] = useState(false);
  const [ledgerError, setLedgerError] = useState<string | null>(null);

  const [catalogue, setCatalogue] = useState<MarketplaceTemplate[]>([]);
  const [upgradeOpen, setUpgradeOpen] = useState(false);
  const [upgradeSaving, setUpgradeSaving] = useState(false);
  const [targetVersion, setTargetVersion] =
    useState<MarketplaceTemplateVersion | null>(null);
  const [targetLoading, setTargetLoading] = useState(false);
  const [targetError, setTargetError] = useState<string | null>(null);
  const [upgradeError, setUpgradeError] = useState<string | null>(null);

  const loadLedger = useCallback(async () => {
    setLedgerLoading(true);
    setLedgerError(null);
    try {
      setLedger(
        await workbuddyMarketplaceApi.listInstallations({
          limit: LEDGER_PAGE_SIZE,
        }),
      );
      setUnavailable(false);
    } catch (error) {
      if (isMarketplaceUnavailableError(error)) {
        setUnavailable(true);
      } else {
        setLedgerError(
          describeApiError(
            error,
            t("workbuddy.marketplace.installations.ledgerLoadFailed"),
            t,
          ),
        );
      }
    } finally {
      setLedgerLoading(false);
    }
  }, [t]);

  useEffect(() => {
    // The tab opens on this tenant's ledger, not on a blank id box.
    void loadLedger();
  }, [loadLedger]);

  const open = useCallback(
    async (installationId: string) => {
      const id = installationId.trim();
      if (!isUuid(id)) return;
      setLoading(true);
      setErrorText(null);
      try {
        const loaded = await workbuddyMarketplaceApi.getInstallation(id);
        setDetail(loaded);
        setUnavailable(false);
        onOpened(loaded.id);
      } catch (error) {
        setDetail(null);
        if (isMarketplaceUnavailableError(error)) {
          setUnavailable(true);
        } else {
          setErrorText(
            describeApiError(
              error,
              t("workbuddy.marketplace.installations.loadFailed"),
              t,
            ),
          );
        }
      } finally {
        setLoading(false);
      }
    },
    [onOpened, t],
  );

  const firstKnownInstallation = knownInstallationIds[0];
  useEffect(() => {
    // Opening the tab preloads the installation an install just produced.
    if (firstKnownInstallation) void open(firstKnownInstallation);
  }, [firstKnownInstallation, open]);

  const refresh = useCallback(async () => {
    await loadLedger();
    if (detail) await open(detail.id);
  }, [detail, loadLedger, open]);

  const loadTargetVersion = useCallback(
    async (templateId: string, versionId: string) => {
      setTargetLoading(true);
      setTargetError(null);
      try {
        setTargetVersion(
          await workbuddyMarketplaceApi.getTemplateVersion(
            templateId,
            versionId,
          ),
        );
      } catch (error) {
        setTargetVersion(null);
        setTargetError(
          describeApiError(
            error,
            t("workbuddy.marketplace.templates.versionLoadFailed"),
            t,
          ),
        );
      } finally {
        setTargetLoading(false);
      }
    },
    [t],
  );

  const openUpgrade = useCallback(async () => {
    setTargetVersion(null);
    setTargetError(null);
    setUpgradeError(null);
    upgradeForm.resetFields();
    setUpgradeOpen(true);
    if (catalogue.length === 0) {
      try {
        setCatalogue(await workbuddyMarketplaceApi.listTemplates());
      } catch (error) {
        setTargetError(
          describeApiError(
            error,
            t("workbuddy.marketplace.templates.loadFailed"),
            t,
          ),
        );
      }
    }
  }, [catalogue.length, t, upgradeForm]);

  const upgrade = useCallback(
    async (values: UpgradeFormValues) => {
      if (!detail) return;
      if (!targetVersion) {
        // Consent is pinned to a loaded version: without it the licence hash
        // and capability digest would be empty and the server would refuse.
        setUpgradeError(t("workbuddy.marketplace.upgrade.targetRequired"));
        return;
      }
      setUpgradeSaving(true);
      setUpgradeError(null);
      try {
        const digest = await capabilitiesDigest(
          targetVersion.required_capabilities ?? [],
        );
        const knowledgeBases: Record<string, string> = {};
        const approvers: Record<string, string> = {};
        const credentials: Record<string, string> = {};
        for (const declaration of targetVersion?.required_capabilities ?? []) {
          if (!isSlotKind(declaration.kind)) continue;
          const trimmed =
            values.slots?.[`${declaration.kind}.${declaration.key}`]?.trim();
          if (!trimmed) continue;
          if (declaration.kind === "knowledge_base")
            knowledgeBases[declaration.key] = trimmed;
          else if (declaration.kind === "approver")
            approvers[declaration.key] = trimmed;
          else credentials[declaration.key] = trimmed;
        }
        const body: MarketplaceUpgradeRequest = {
          template_id: values.template_id,
          template_version_id: values.template_version_id.trim(),
          consent: {
            accepted: true,
            template_version_id: values.template_version_id.trim(),
            license_text_hash: targetVersion.license_text_hash,
            capabilities_hash: digest,
          },
          bindings: { knowledge_bases: knowledgeBases, approvers },
          credential_bindings: credentials,
        };
        const result = await workbuddyMarketplaceApi.upgradeInstallation(
          detail.id,
          body,
        );
        message.success(
          t("workbuddy.marketplace.upgrade.accepted", {
            status: result.upgrade.status,
          }),
        );
        setUpgradeOpen(false);
        await refresh();
      } catch (error) {
        if (error instanceof MarketplaceDigestUnavailableError) {
          setUpgradeError(t("workbuddy.marketplace.consent.digestFailed"));
        } else {
          setUpgradeError(
            describeApiError(
              error,
              t("workbuddy.marketplace.upgrade.failed"),
              t,
            ),
          );
        }
      } finally {
        setUpgradeSaving(false);
      }
    },
    [detail, refresh, t, targetVersion],
  );

  const ledgerColumns: ColumnsType<MarketplaceInstallation> = [
    {
      title: t("workbuddy.marketplace.installations.installationId"),
      dataIndex: "id",
      key: "id",
      width: 320,
      render: (value: string) => (
        <Button
          type="link"
          size="small"
          className={styles.mono}
          onClick={() => void open(value)}
        >
          {value}
        </Button>
      ),
    },
    {
      title: t("workbuddy.marketplace.installations.templateId"),
      dataIndex: "template_id",
      key: "template_id",
      width: 300,
      render: (value: string) => <span className={styles.mono}>{value}</span>,
    },
    {
      title: t("workbuddy.marketplace.installations.status"),
      dataIndex: "status",
      key: "status",
      width: 130,
      render: (value: string) => (
        <Tag color={statusColor(value)}>
          {t(`workbuddy.marketplace.installationStatus.${value}`)}
        </Tag>
      ),
    },
    {
      title: t("workbuddy.marketplace.installations.workflowId"),
      dataIndex: "workflow_id",
      key: "workflow_id",
      width: 300,
      render: (value: string | null) =>
        value ? <span className={styles.mono}>{value}</span> : "—",
    },
    {
      title: t("workbuddy.marketplace.installations.revision"),
      dataIndex: "revision",
      key: "revision",
      width: 90,
    },
    {
      title: t("workbuddy.marketplace.installations.updatedAt"),
      dataIndex: "updated_at",
      key: "updated_at",
      width: 180,
      render: (value: string | null) =>
        value ? formatServerIsoDateTime(value, timeZone) : "—",
    },
  ];

  const consentColumns: ColumnsType<MarketplaceInstallConsent> = [
    {
      title: t("workbuddy.marketplace.installations.consent.subject"),
      dataIndex: "consented_at",
      key: "consented_at",
      width: 180,
      render: (value: string | null) =>
        value ? formatServerIsoDateTime(value, timeZone) : "—",
    },
    {
      title: t("workbuddy.marketplace.installations.consent.license"),
      dataIndex: "license_id",
      key: "license_id",
      width: 160,
    },
    {
      title: t("workbuddy.marketplace.installations.consent.hash"),
      dataIndex: "capabilities_hash",
      key: "capabilities_hash",
      width: 320,
      render: (value: string) => <span className={styles.mono}>{value}</span>,
    },
  ];

  const bindingColumns: ColumnsType<MarketplaceCredentialBinding> = [
    {
      title: t("workbuddy.marketplace.installations.bindings.slot"),
      dataIndex: "binding_key",
      key: "binding_key",
      width: 200,
    },
    {
      title: t("workbuddy.marketplace.installations.bindings.credential"),
      dataIndex: "credential_id",
      key: "credential_id",
      width: 320,
      render: (value: string) => <span className={styles.mono}>{value}</span>,
    },
  ];

  const upgradeColumns: ColumnsType<MarketplaceUpgrade> = [
    {
      title: t("workbuddy.marketplace.installations.upgrades.from"),
      dataIndex: "from_template_version_id",
      key: "from_template_version_id",
      width: 300,
      render: (value: string) => <span className={styles.mono}>{value}</span>,
    },
    {
      title: t("workbuddy.marketplace.installations.upgrades.to"),
      dataIndex: "to_template_version_id",
      key: "to_template_version_id",
      width: 300,
      render: (value: string) => <span className={styles.mono}>{value}</span>,
    },
    {
      title: t("workbuddy.marketplace.installations.upgrades.status"),
      dataIndex: "status",
      key: "status",
      width: 130,
      render: (value: string) => (
        <Tag color={statusColor(value)}>
          {t(`workbuddy.marketplace.installationStatus.${value}`)}
        </Tag>
      ),
    },
    {
      title: t("workbuddy.marketplace.installations.upgrades.error"),
      dataIndex: "error_code",
      key: "error_code",
      width: 220,
      render: (value: string | null, row) =>
        value ? (
          <Space direction="vertical" size={0}>
            <span className={styles.mono}>{value}</span>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {row.error_detail}
            </Text>
          </Space>
        ) : (
          "—"
        ),
    },
  ];

  const templateOptions = useMemo(
    () =>
      catalogue.map((item) => ({
        value: item.id,
        label: item.current_version
          ? `${item.name} · v${item.current_version}`
          : item.name,
      })),
    [catalogue],
  );

  const slotDeclarations = useMemo(
    () =>
      (targetVersion?.required_capabilities ?? []).filter((entry) =>
        isSlotKind(entry.kind),
      ),
    [targetVersion],
  );

  const header = (
    <TabPanelHeader
      icon={<ArrowUpCircle size={18} />}
      title={t("workbuddy.marketplace.installations.title")}
      description={t("workbuddy.marketplace.installations.desc")}
      actions={
        <Button
          size="small"
          icon={<RefreshCw size={14} />}
          loading={ledgerLoading}
          onClick={() => void refresh()}
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
          onAction={() => void refresh()}
        />
      </div>
    );
  }

  return (
    <div className={styles.panel}>
      {header}

      <div className={styles.sectionTitleRow}>
        <h3 className={styles.sectionTitle}>
          {t("workbuddy.marketplace.installations.ledgerTitle")}
        </h3>
      </div>

      {ledgerError && (
        <Alert
          className={styles.notice}
          type="error"
          showIcon
          message={ledgerError}
        />
      )}

      {ledgerLoading && ledger.length === 0 ? (
        <div className={styles.centered}>
          <Spin />
        </div>
      ) : ledger.length === 0 ? (
        <EmptyState
          variant="mascot"
          title={t("workbuddy.marketplace.installations.ledgerEmpty")}
          description={t("workbuddy.marketplace.installations.ledgerEmptyHint")}
        />
      ) : (
        <>
          <Table
            columns={ledgerColumns}
            dataSource={ledger}
            rowKey="id"
            size="small"
            pagination={false}
            scroll={{ x: 1300 }}
          />
          {ledger.length >= LEDGER_PAGE_SIZE && (
            <Text type="secondary">
              {t("workbuddy.marketplace.installations.ledgerCapped")}
            </Text>
          )}
        </>
      )}

      <Space direction="vertical" size={8} style={{ width: "100%" }}>
        <Space.Compact style={{ width: "100%", maxWidth: 640 }}>
          <Input
            value={idInput}
            onChange={(event) => setIdInput(event.target.value)}
            placeholder={t(
              "workbuddy.marketplace.installations.openPlaceholder",
            )}
            onPressEnter={() => void open(idInput)}
          />
          <Button
            type="primary"
            loading={loading}
            icon={<Search size={14} />}
            disabled={!isUuid(idInput)}
            onClick={() => void open(idInput)}
          >
            {t("workbuddy.marketplace.installations.open")}
          </Button>
        </Space.Compact>

        {knownInstallationIds.length > 0 && (
          <Space size={6} wrap>
            <Text type="secondary">
              {t("workbuddy.marketplace.installations.sessionIds")}
            </Text>
            {knownInstallationIds.map((id) => (
              <Tag
                key={id}
                color={detail?.id === id ? "blue" : "default"}
                style={{ cursor: "pointer" }}
                onClick={() => void open(id)}
              >
                <span className={styles.mono}>{id.slice(0, 8)}</span>
              </Tag>
            ))}
          </Space>
        )}
      </Space>

      {errorText && (
        <Alert
          className={styles.notice}
          type="error"
          showIcon
          message={errorText}
        />
      )}

      {loading && !detail ? (
        <div className={styles.centered}>
          <Spin />
        </div>
      ) : !detail ? (
        <EmptyState
          variant="mascot"
          title={t("workbuddy.marketplace.installations.empty")}
          description={t("workbuddy.marketplace.installations.emptyHint")}
        />
      ) : (
        <>
          <div className={styles.sectionTitleRow}>
            <h3 className={styles.sectionTitle}>
              {t("workbuddy.marketplace.installations.detailTitle")}
            </h3>
            <Space size={8} wrap>
              <Tag color={statusColor(detail.status)}>
                {t(`workbuddy.marketplace.installationStatus.${detail.status}`)}
              </Tag>
              <Button
                size="small"
                icon={<ArrowUpCircle size={14} />}
                disabled={detail.status !== "installed"}
                onClick={() => void openUpgrade()}
              >
                {t("workbuddy.marketplace.upgrade.button")}
              </Button>
            </Space>
          </div>

          <div className={styles.metaGrid}>
            <div>
              <Text type="secondary">
                {t("workbuddy.marketplace.installations.installationId")}
              </Text>
              <div className={styles.mono}>{detail.id}</div>
            </div>
            <div>
              <Text type="secondary">
                {t("workbuddy.marketplace.installations.templateId")}
              </Text>
              <div className={styles.mono}>{detail.template_id}</div>
            </div>
            <div>
              <Text type="secondary">
                {t("workbuddy.marketplace.installations.templateVersionId")}
              </Text>
              <div className={styles.mono}>{detail.template_version_id}</div>
            </div>
            <div>
              <Text type="secondary">
                {t("workbuddy.marketplace.installations.workflowId")}
              </Text>
              <div className={styles.mono}>{detail.workflow_id ?? "—"}</div>
            </div>
            <div>
              <Text type="secondary">
                {t("workbuddy.marketplace.installations.revision")}
              </Text>
              <div>{detail.revision}</div>
            </div>
            <div>
              <Text type="secondary">
                {t("workbuddy.marketplace.installations.createdAt")}
              </Text>
              <div>
                {detail.created_at
                  ? formatServerIsoDateTime(detail.created_at, timeZone)
                  : "—"}
              </div>
            </div>
          </div>

          {(detail.error_code || detail.job?.error_code) && (
            <Alert
              className={styles.notice}
              type="error"
              showIcon
              message={t("workbuddy.marketplace.installations.failureTitle")}
              description={
                <Space direction="vertical" size={2}>
                  <span className={styles.mono}>
                    {detail.job?.error_code ?? detail.error_code}
                  </span>
                  <span>
                    {detail.job?.error_message ?? detail.error_detail ?? ""}
                  </span>
                </Space>
              }
            />
          )}

          {detail.job && (
            <>
              <h4 className={styles.subTitle}>
                {t("workbuddy.marketplace.installations.jobTitle")}
              </h4>
              <Space size={12} wrap>
                <Tag color={statusColor(detail.job.status)}>
                  {t(`workbuddy.marketplace.jobStatus.${detail.job.status}`)}
                </Tag>
                <Text type="secondary">
                  {t("workbuddy.marketplace.installations.jobProgress", {
                    progress: detail.job.progress,
                  })}
                </Text>
                <Text type="secondary" className={styles.mono}>
                  {detail.job.id}
                </Text>
              </Space>
            </>
          )}

          <h4 className={styles.subTitle}>
            {t("workbuddy.marketplace.installations.consentsTitle")}
          </h4>
          {(detail.consents ?? []).length === 0 ? (
            <Text type="secondary">
              {t("workbuddy.marketplace.installations.consentsEmpty")}
            </Text>
          ) : (
            <Table
              columns={consentColumns}
              dataSource={detail.consents ?? []}
              rowKey="id"
              size="small"
              pagination={false}
              scroll={{ x: 660 }}
            />
          )}

          <h4 className={styles.subTitle}>
            {t("workbuddy.marketplace.installations.bindingsTitle")}
          </h4>
          {(detail.credential_bindings ?? []).length === 0 ? (
            <Text type="secondary">
              {t("workbuddy.marketplace.installations.bindingsEmpty")}
            </Text>
          ) : (
            <Table
              columns={bindingColumns}
              dataSource={detail.credential_bindings ?? []}
              rowKey="binding_key"
              size="small"
              pagination={false}
              scroll={{ x: 520 }}
            />
          )}

          <h4 className={styles.subTitle}>
            {t("workbuddy.marketplace.installations.upgradesTitle")}
          </h4>
          {(detail.upgrades ?? []).length === 0 ? (
            <Text type="secondary">
              {t("workbuddy.marketplace.installations.upgradesEmpty")}
            </Text>
          ) : (
            <Table
              columns={upgradeColumns}
              dataSource={detail.upgrades ?? []}
              rowKey="id"
              size="small"
              pagination={false}
              scroll={{ x: 950 }}
            />
          )}
        </>
      )}

      <Modal
        title={t("workbuddy.marketplace.upgrade.title")}
        open={upgradeOpen}
        onCancel={() => setUpgradeOpen(false)}
        onOk={() => upgradeForm.submit()}
        confirmLoading={upgradeSaving}
        okText={t("workbuddy.marketplace.upgrade.submit")}
        cancelText={t("common.cancel")}
        width={680}
        destroyOnHidden
      >
        <Alert
          type="info"
          showIcon
          className={styles.notice}
          message={t("workbuddy.marketplace.upgrade.installedVersion", {
            versionId: detail?.template_version_id ?? "",
          })}
        />
        <Form
          form={upgradeForm}
          layout="vertical"
          requiredMark={false}
          onFinish={(values) => void upgrade(values)}
        >
          <Form.Item
            name="template_id"
            label={t("workbuddy.marketplace.upgrade.templateLabel")}
            rules={[
              {
                required: true,
                message: t("workbuddy.marketplace.upgrade.templateRequired"),
              },
            ]}
          >
            <Select
              showSearch
              optionFilterProp="label"
              placeholder={t(
                "workbuddy.marketplace.upgrade.templatePlaceholder",
              )}
              options={templateOptions}
              onChange={(value: string) => {
                const picked = catalogue.find((item) => item.id === value);
                const versionId = picked?.current_version_id ?? "";
                upgradeForm.setFieldValue("template_version_id", versionId);
                setTargetVersion(null);
                setTargetError(null);
                if (picked && versionId)
                  void loadTargetVersion(picked.id, versionId);
              }}
            />
          </Form.Item>

          <Form.Item
            name="template_version_id"
            label={t("workbuddy.marketplace.upgrade.versionLabel")}
            extra={t("workbuddy.marketplace.upgrade.versionHint")}
            rules={[
              {
                required: true,
                message: t("workbuddy.marketplace.upgrade.versionRequired"),
              },
              {
                validator: (_rule, value: string | undefined) =>
                  !value?.trim() || isUuid(value)
                    ? Promise.resolve()
                    : Promise.reject(
                        new Error(t("workbuddy.marketplace.bindings.invalid")),
                      ),
              },
            ]}
          >
            <Input
              placeholder={t(
                "workbuddy.marketplace.upgrade.versionPlaceholder",
              )}
              onBlur={(event) => {
                const templateId = upgradeForm.getFieldValue("template_id") as
                  | string
                  | undefined;
                const versionId = event.target.value.trim();
                if (templateId && isUuid(versionId)) {
                  void loadTargetVersion(templateId, versionId);
                }
              }}
            />
          </Form.Item>

          {targetLoading && (
            <div className={styles.centered}>
              <Spin />
            </div>
          )}
          {targetError && <Alert type="error" showIcon message={targetError} />}

          {targetVersion && (
            <>
              <Alert
                className={styles.notice}
                type="warning"
                showIcon
                message={t("workbuddy.marketplace.upgrade.targetSummary", {
                  version: targetVersion.version,
                  license: targetVersion.license_id,
                })}
                description={t("workbuddy.marketplace.consent.hint", {
                  license: targetVersion.license_id,
                  hash: targetVersion.license_text_hash,
                })}
              />

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

              <Form.Item
                name="consent"
                valuePropName="checked"
                rules={[
                  {
                    validator: (_rule, value: boolean | undefined) =>
                      value === true
                        ? Promise.resolve()
                        : Promise.reject(
                            new Error(
                              t("workbuddy.marketplace.consent.required"),
                            ),
                          ),
                  },
                ]}
              >
                <Checkbox>
                  {t("workbuddy.marketplace.consent.label", {
                    version: targetVersion.version,
                  })}
                </Checkbox>
              </Form.Item>
            </>
          )}
        </Form>

        {upgradeError && <Alert type="error" showIcon message={upgradeError} />}
      </Modal>
    </div>
  );
}
