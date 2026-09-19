/**
 * Tenant export: request, one-time redeem, download challenge.
 *
 * Hard rule: the redeem token exists only in the creation response. It is held
 * in state for exactly the lifetime of its one-time dialog, dropped the moment
 * that dialog closes, and never written to storage, a URL, a log or an error
 * message. The server keeps only its sha256, so there is nothing to re-read.
 */

import { useState } from "react";
import {
  Alert,
  Button,
  Descriptions,
  Form,
  Input,
  Modal,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { message } from "@/utils/antdMessage";
import {
  Copy,
  Download,
  FileLock2,
  KeyRound,
  RefreshCw,
  ShieldCheck,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import { copyText } from "../../../utils/copyText";
import { useServerTimezone } from "../../../hooks/useServerTimezone";
import { apiErrorMessage } from "../../../utils/apiError";
import { formatServerDateTime } from "../../../utils/formatMessageTime";
import {
  isLifecycleUnavailableError,
  workbuddyLifecycleApi,
  type ExportDownload,
  type ExportDownloadChallenge,
  type ExportManifestTable,
  type ReauthenticateResult,
  type TenantExportIssue,
} from "../../../api/modules/workbuddyLifecycle";
import { TabPanelHeader } from "../../Settings/AdvancedSettings/TabPanelHeader";
import { lifecycleStatusLabel } from "./statusLabels";
import styles from "./index.module.less";

const { Text, Title } = Typography;

/** The creation payload minus the token: the token never lives in this state. */
type ExportJobSummary = Omit<TenantExportIssue, "redeem_token">;

interface RedeemFormValues {
  export_job_id: string;
  token: string;
}

interface ReauthFormValues {
  password: string;
}

export default function ExportsPanel() {
  const { t } = useTranslation();
  const timeZone = useServerTimezone();

  const [tenantId, setTenantId] = useState("");
  const [creating, setCreating] = useState(false);
  const [job, setJob] = useState<ExportJobSummary | null>(null);
  const [createUnavailable, setCreateUnavailable] = useState(false);
  const [tokenForDisplay, setTokenForDisplay] = useState<string | null>(null);

  const [redeemForm] = Form.useForm<RedeemFormValues>();
  const [redeeming, setRedeeming] = useState(false);
  const [download, setDownload] = useState<ExportDownload | null>(null);
  const [redeemUnavailable, setRedeemUnavailable] = useState(false);

  const [reauthForm] = Form.useForm<ReauthFormValues>();
  const [reauth, setReauth] = useState<ReauthenticateResult | null>(null);
  const [reauthBusy, setReauthBusy] = useState(false);
  const [challengeJobId, setChallengeJobId] = useState("");
  const [challengeBusy, setChallengeBusy] = useState(false);
  const [challenge, setChallenge] = useState<ExportDownloadChallenge | null>(
    null,
  );
  const [challengeUnavailable, setChallengeUnavailable] = useState(false);

  const onCreateExport = async () => {
    const id = tenantId.trim();
    if (!id) {
      message.error(t("workbuddy.lifecycle.exports.tenantIdRequired"));
      return;
    }
    setCreating(true);
    setCreateUnavailable(false);
    try {
      const issue = await workbuddyLifecycleApi.requestTenantExport(id);
      const { redeem_token: redeemToken, ...summary } = issue;
      setJob(summary);
      setTokenForDisplay(redeemToken);
      redeemForm.setFieldValue("export_job_id", issue.export_job_id);
      setChallengeJobId(issue.export_job_id);
    } catch (err) {
      if (isLifecycleUnavailableError(err)) {
        setCreateUnavailable(true);
      } else {
        message.error(
          apiErrorMessage(
            err,
            t("workbuddy.lifecycle.exports.createFailed"),
            t,
          ),
        );
      }
    } finally {
      setCreating(false);
    }
  };

  const copyDisplayedToken = async () => {
    if (tokenForDisplay === null) return;
    const ok = await copyText(tokenForDisplay);
    if (ok) message.success(t("common.copied"));
    else message.error(t("common.copyFailed"));
  };

  const onRedeem = async (values: RedeemFormValues) => {
    setRedeeming(true);
    setRedeemUnavailable(false);
    try {
      const result = await workbuddyLifecycleApi.redeemExport(
        values.export_job_id.trim(),
        values.token.trim(),
      );
      setDownload(result);
      // The token is spent; never keep it in the form or in state.
      redeemForm.setFieldValue("token", "");
      message.success(t("workbuddy.lifecycle.exports.redeemSuccess"));
    } catch (err) {
      if (isLifecycleUnavailableError(err)) {
        setRedeemUnavailable(true);
      } else {
        message.error(
          apiErrorMessage(
            err,
            t("workbuddy.lifecycle.exports.redeemFailed"),
            t,
          ),
        );
      }
    } finally {
      setRedeeming(false);
    }
  };

  const onReauthenticate = async (values: ReauthFormValues) => {
    setReauthBusy(true);
    try {
      const result = await workbuddyLifecycleApi.reauthenticate({
        password: values.password,
      });
      setReauth(result);
      reauthForm.resetFields();
      message.success(t("workbuddy.lifecycle.exports.reauthSuccess"));
    } catch (err) {
      if (isLifecycleUnavailableError(err)) {
        setChallengeUnavailable(true);
      } else {
        message.error(
          apiErrorMessage(
            err,
            t("workbuddy.lifecycle.exports.reauthFailed"),
            t,
          ),
        );
      }
    } finally {
      setReauthBusy(false);
    }
  };

  const onRequestChallenge = async () => {
    const id = challengeJobId.trim();
    if (!id) {
      message.error(t("workbuddy.lifecycle.exports.jobIdRequired"));
      return;
    }
    if (reauth === null) {
      message.error(t("workbuddy.lifecycle.exports.reauthFirst"));
      return;
    }
    setChallengeBusy(true);
    setChallengeUnavailable(false);
    try {
      const issued = await workbuddyLifecycleApi.requestDownloadChallenge(id, {
        credential: reauth.credential,
      });
      setChallenge(issued);
    } catch (err) {
      if (isLifecycleUnavailableError(err)) {
        setChallengeUnavailable(true);
      } else {
        message.error(
          apiErrorMessage(
            err,
            t("workbuddy.lifecycle.exports.challengeFailed"),
            t,
          ),
        );
      }
    } finally {
      setChallengeBusy(false);
    }
  };

  const copyDisplayedChallenge = async () => {
    if (challenge === null) return;
    const ok = await copyText(challenge.challenge);
    if (ok) message.success(t("common.copied"));
    else message.error(t("common.copyFailed"));
  };

  const downloadPayload = () => {
    if (download === null) return;
    const payload = JSON.stringify(
      { manifest: download.manifest, tables: download.tables },
      null,
      2,
    );
    const url = URL.createObjectURL(
      new Blob([payload], { type: "application/json" }),
    );
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `workbuddy-export-${download.export_job_id}.json`;
    anchor.click();
    // Revoke on the next tick so the started download keeps its blob URL.
    window.setTimeout(() => URL.revokeObjectURL(url), 0);
  };

  const manifestColumns: ColumnsType<ExportManifestTable> = [
    {
      title: t("workbuddy.lifecycle.exports.tableName"),
      dataIndex: "name",
      key: "name",
      width: 220,
    },
    {
      title: t("workbuddy.lifecycle.exports.tableRows"),
      dataIndex: "row_count",
      key: "row_count",
      width: 100,
    },
    {
      title: t("workbuddy.lifecycle.exports.tableDigest"),
      dataIndex: "content_sha256",
      key: "content_sha256",
      render: (digest: string) => (
        <Tooltip title={digest}>
          <Text code>{digest.slice(0, 16)}…</Text>
        </Tooltip>
      ),
    },
  ];

  return (
    <div className={styles.panel}>
      <TabPanelHeader
        icon={<Download size={18} />}
        title={t("workbuddy.lifecycle.exports.title")}
        description={t("workbuddy.lifecycle.exports.desc")}
      />

      {/* 1. Request an export */}
      <div className={styles.sectionBlock}>
        <Title level={5} className={styles.sectionTitle}>
          {t("workbuddy.lifecycle.exports.createTitle")}
        </Title>
        <Text type="secondary" className={styles.sectionDesc}>
          {t("workbuddy.lifecycle.exports.createHint")}
        </Text>
        <Space size={8} wrap className={styles.actionRow}>
          <Input
            value={tenantId}
            onChange={(event) => setTenantId(event.target.value)}
            placeholder={t("workbuddy.lifecycle.tenantIdPlaceholder")}
            className={styles.idInput}
            allowClear
          />
          <Button
            type="primary"
            icon={<FileLock2 size={14} />}
            loading={creating}
            onClick={() => void onCreateExport()}
          >
            {t("workbuddy.lifecycle.exports.create")}
          </Button>
        </Space>

        {createUnavailable && (
          <Alert
            type="info"
            showIcon
            className={styles.notice}
            message={t("workbuddy.lifecycle.unavailableTitle")}
            description={t("workbuddy.lifecycle.exports.unavailableHint")}
          />
        )}

        {job && (
          <Descriptions
            size="small"
            column={1}
            className={styles.summary}
            items={[
              {
                key: "job",
                label: t("workbuddy.lifecycle.exports.jobId"),
                children: <Text code>{job.export_job_id}</Text>,
              },
              {
                key: "status",
                label: t("workbuddy.lifecycle.exports.status"),
                children: (
                  <Tag color={job.status === "ready" ? "green" : "default"}>
                    {lifecycleStatusLabel(t, "export", job.status)}
                  </Tag>
                ),
              },
              {
                key: "expires",
                label: t("workbuddy.lifecycle.exports.redeemExpiresAt"),
                children: formatServerDateTime(job.redeem_expires_at, timeZone),
              },
              {
                key: "totals",
                label: t("workbuddy.lifecycle.exports.totals"),
                children: t("workbuddy.lifecycle.exports.totalsValue", {
                  tables: job.table_total,
                  rows: job.row_total,
                }),
              },
              {
                key: "manifest",
                label: t("workbuddy.lifecycle.exports.manifestDigest"),
                children: job.manifest_sha256 ? (
                  <Tooltip title={job.manifest_sha256}>
                    <Text code>{job.manifest_sha256.slice(0, 16)}…</Text>
                  </Tooltip>
                ) : (
                  "—"
                ),
              },
            ]}
          />
        )}
      </div>

      {/* 2. Redeem the one-time token */}
      <div className={styles.sectionBlock}>
        <Title level={5} className={styles.sectionTitle}>
          {t("workbuddy.lifecycle.exports.redeemTitle")}
        </Title>
        <Text type="secondary" className={styles.sectionDesc}>
          {t("workbuddy.lifecycle.exports.redeemHint")}
        </Text>
        <Form
          form={redeemForm}
          layout="vertical"
          requiredMark={false}
          className={styles.inlineForm}
          onFinish={(values) => void onRedeem(values)}
        >
          <Form.Item
            name="export_job_id"
            label={t("workbuddy.lifecycle.exports.jobId")}
            rules={[
              {
                required: true,
                message: t("workbuddy.lifecycle.exports.jobIdRequired"),
              },
            ]}
          >
            <Input
              placeholder={t("workbuddy.lifecycle.exports.jobIdPlaceholder")}
            />
          </Form.Item>
          <Form.Item
            name="token"
            label={t("workbuddy.lifecycle.exports.redeemToken")}
            rules={[
              {
                required: true,
                message: t("workbuddy.lifecycle.exports.redeemTokenRequired"),
              },
            ]}
          >
            <Input.Password
              autoComplete="off"
              placeholder={t(
                "workbuddy.lifecycle.exports.redeemTokenPlaceholder",
              )}
            />
          </Form.Item>
          <Button
            type="primary"
            htmlType="submit"
            icon={<KeyRound size={14} />}
            loading={redeeming}
          >
            {t("workbuddy.lifecycle.exports.redeem")}
          </Button>
        </Form>

        {redeemUnavailable && (
          <Alert
            type="info"
            showIcon
            className={styles.notice}
            message={t("workbuddy.lifecycle.unavailableTitle")}
            description={t("workbuddy.lifecycle.exports.unavailableHint")}
          />
        )}

        {download && (
          <div className={styles.summary}>
            <Space size={8} wrap>
              <Text code>{download.export_job_id}</Text>
              <Text type="secondary">
                {t("workbuddy.lifecycle.exports.redeemedAt", {
                  time: formatServerDateTime(download.redeemed_at, timeZone),
                })}
              </Text>
              <Tooltip title={download.manifest_sha256}>
                <Text code>{download.manifest_sha256.slice(0, 16)}…</Text>
              </Tooltip>
              <Button
                size="small"
                icon={<Download size={14} />}
                onClick={downloadPayload}
              >
                {t("workbuddy.lifecycle.exports.downloadPayload")}
              </Button>
            </Space>
            <Table
              className={styles.summaryTable}
              columns={manifestColumns}
              dataSource={download.manifest.tables}
              rowKey="name"
              size="small"
              pagination={false}
            />
            {download.manifest.excluded_tables.length > 0 && (
              <div className={styles.tagRow}>
                <Text type="secondary" className={styles.sectionDesc}>
                  {t("workbuddy.lifecycle.exports.excludedTables")}
                </Text>
                {download.manifest.excluded_tables.map((entry) => (
                  <Tag key={entry.name}>
                    {entry.name} · {entry.category}
                  </Tag>
                ))}
              </div>
            )}
          </div>
        )}
      </div>

      {/* 3. Download challenge behind a fresh re-authentication */}
      <div className={styles.sectionBlock}>
        <Title level={5} className={styles.sectionTitle}>
          {t("workbuddy.lifecycle.exports.challengeTitle")}
        </Title>
        <Text type="secondary" className={styles.sectionDesc}>
          {t("workbuddy.lifecycle.exports.challengeHint")}
        </Text>

        {challengeUnavailable && (
          <Alert
            type="info"
            showIcon
            className={styles.notice}
            message={t("workbuddy.lifecycle.unavailableTitle")}
            description={t(
              "workbuddy.lifecycle.exports.challengeUnavailableHint",
            )}
          />
        )}

        <Form
          form={reauthForm}
          layout="vertical"
          requiredMark={false}
          className={styles.inlineForm}
          onFinish={(values) => void onReauthenticate(values)}
        >
          <Form.Item
            name="password"
            label={t("workbuddy.lifecycle.exports.password")}
            rules={[
              {
                required: true,
                message: t("workbuddy.lifecycle.exports.passwordRequired"),
              },
            ]}
          >
            <Input.Password
              autoComplete="current-password"
              placeholder={t("workbuddy.lifecycle.exports.passwordPlaceholder")}
            />
          </Form.Item>
          <Space size={8} wrap>
            <Button
              htmlType="submit"
              icon={<ShieldCheck size={14} />}
              loading={reauthBusy}
            >
              {t("workbuddy.lifecycle.exports.reauthenticate")}
            </Button>
            {reauth && (
              <Text type="secondary">
                {t("workbuddy.lifecycle.exports.reauthDone", {
                  time: formatServerDateTime(reauth.expires_at, timeZone),
                })}
              </Text>
            )}
          </Space>
        </Form>

        <Space size={8} wrap className={styles.actionRow}>
          <Input
            value={challengeJobId}
            onChange={(event) => setChallengeJobId(event.target.value)}
            placeholder={t("workbuddy.lifecycle.exports.jobIdPlaceholder")}
            className={styles.idInput}
            allowClear
          />
          <Button
            icon={<RefreshCw size={14} />}
            loading={challengeBusy}
            disabled={reauth === null}
            onClick={() => void onRequestChallenge()}
          >
            {t("workbuddy.lifecycle.exports.requestChallenge")}
          </Button>
        </Space>
      </div>

      {/* One-time redeem token: rendered once, dropped on close, never stored. */}
      <Modal
        title={t("workbuddy.lifecycle.exports.tokenTitle")}
        open={tokenForDisplay !== null}
        onCancel={() => setTokenForDisplay(null)}
        destroyOnHidden
        maskClosable={false}
        footer={
          <Space>
            <Button onClick={() => setTokenForDisplay(null)}>
              {t("common.close")}
            </Button>
            <Button
              type="primary"
              icon={<Copy size={14} />}
              onClick={() => void copyDisplayedToken()}
            >
              {t("workbuddy.lifecycle.exports.copyToken")}
            </Button>
          </Space>
        }
      >
        <Alert
          type="warning"
          showIcon
          className={styles.notice}
          message={t("workbuddy.lifecycle.exports.tokenWarning")}
          description={t("workbuddy.lifecycle.exports.tokenWarningHint")}
        />
        <Input.TextArea
          value={tokenForDisplay ?? ""}
          readOnly
          autoSize={{ minRows: 2, maxRows: 4 }}
          className={styles.tokenBox}
        />
        <Text type="secondary" className={styles.tokenHint}>
          {t("workbuddy.lifecycle.exports.tokenHint", {
            time: job
              ? formatServerDateTime(job.redeem_expires_at, timeZone)
              : "—",
          })}
        </Text>
      </Modal>

      {/* One-time download challenge: same discipline as the redeem token. */}
      <Modal
        title={t("workbuddy.lifecycle.exports.challengeIssuedTitle")}
        open={challenge !== null}
        onCancel={() => setChallenge(null)}
        destroyOnHidden
        maskClosable={false}
        footer={
          <Space>
            <Button onClick={() => setChallenge(null)}>
              {t("common.close")}
            </Button>
            <Button
              type="primary"
              icon={<Copy size={14} />}
              onClick={() => void copyDisplayedChallenge()}
            >
              {t("workbuddy.lifecycle.exports.copyChallenge")}
            </Button>
          </Space>
        }
      >
        <Input.TextArea
          value={challenge?.challenge ?? ""}
          readOnly
          autoSize={{ minRows: 2, maxRows: 4 }}
          className={styles.tokenBox}
        />
        <Text type="secondary" className={styles.tokenHint}>
          {t("workbuddy.lifecycle.exports.challengeHintShort", {
            time: challenge
              ? formatServerDateTime(challenge.expires_at, timeZone)
              : "—",
          })}
        </Text>
      </Modal>
    </div>
  );
}
