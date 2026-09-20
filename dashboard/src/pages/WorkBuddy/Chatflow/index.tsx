/**
 * WorkBuddy → 对话触发（A-17）。
 *
 * A conversation is not a workflow; it is a place a workflow may be bound to. So
 * this page does two things and no more: it sends a message, and it shows **what
 * that conversation caused** — every delivery the message produced, with the run
 * it started or the reason it was refused.
 *
 * The second half is the point. A chat flow whose effects nobody can see is a
 * chat flow nobody should trust, which is why the page reads the delivery ledger
 * rather than only reporting "sent".
 *
 * The message id is generated here and travels with the message, because it *is*
 * the dedupe key: a resend (flaky network, impatient second click) must not run
 * the workflow twice.
 */

import { useCallback, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Input,
  Space,
  Table,
  Tag,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { MessageSquare, Send } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";

import PageShell from "../../../layouts/PageShell";
import { apiErrorMessage } from "../../../utils/apiError";
import {
  workbuddyKnowledgeApi,
  type ChatMessageResult,
  type ConversationDelivery,
} from "../../../api/modules/workbuddyKnowledge";

const { Text } = Typography;

function newMessageId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `m-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

export default function WorkBuddyChatflowPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [conversationId, setConversationId] = useState("");
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [sent, setSent] = useState<ChatMessageResult | null>(null);
  const [deliveries, setDeliveries] = useState<ConversationDelivery[]>([]);
  const [loaded, setLoaded] = useState(false);

  const conversation = conversationId.trim();

  const loadDeliveries = useCallback(async (id: string) => {
    const page = await workbuddyKnowledgeApi.listConversationDeliveries(id);
    setDeliveries(page.items);
    setLoaded(true);
  }, []);

  const send = useCallback(async () => {
    const id = conversation.trim();
    const body = text.trim();
    if (!id || !body) return;
    setSending(true);
    setError(null);
    try {
      const result = await workbuddyKnowledgeApi.postChatMessage(id, {
        message_id: newMessageId(),
        text: body,
      });
      setSent(result);
      setText("");
      await loadDeliveries(id);
    } catch (caught) {
      setError(caught);
    } finally {
      setSending(false);
    }
  }, [conversation, text, loadDeliveries]);

  const columns: ColumnsType<ConversationDelivery> = [
    {
      title: t("workbuddy.chatflow.columnStatus"),
      dataIndex: "status",
      key: "status",
      width: 120,
      render: (value: string) => (
        <Tag
          color={
            value === "executed"
              ? "green"
              : value === "rejected"
              ? "orange"
              : "default"
          }
        >
          {value}
        </Tag>
      ),
    },
    {
      title: t("workbuddy.chatflow.columnRun"),
      dataIndex: "execution_id",
      key: "execution_id",
      render: (value: string | null) =>
        value === null ? (
          <Text type="secondary">{t("workbuddy.chatflow.noRun")}</Text>
        ) : (
          <Button
            size="small"
            type="link"
            onClick={() =>
              navigate(`/workbuddy/runs?execution=${encodeURIComponent(value)}`)
            }
          >
            {value.slice(0, 8)}
          </Button>
        ),
    },
    {
      title: t("workbuddy.chatflow.columnReason"),
      dataIndex: "rejection_code",
      key: "rejection_code",
      render: (value: string | null) =>
        value ?? <Text type="secondary">—</Text>,
    },
  ];

  return (
    <PageShell
      title={t("workbuddy.chatflow.title")}
      subtitle={t("workbuddy.chatflow.subtitle")}
    >
      <Card
        size="small"
        title={
          <Space size={8}>
            <MessageSquare size={16} />
            {t("workbuddy.chatflow.sendTitle")}
          </Space>
        }
      >
        <Space direction="vertical" style={{ width: "100%" }} size={8}>
          <Input
            value={conversationId}
            onChange={(event) => setConversationId(event.target.value)}
            placeholder={t("workbuddy.chatflow.conversationPlaceholder")}
            maxLength={200}
          />
          <Input.TextArea
            value={text}
            onChange={(event) => setText(event.target.value)}
            rows={3}
            maxLength={8000}
            placeholder={t("workbuddy.chatflow.messagePlaceholder")}
          />
          <Space size={8}>
            <Button
              type="primary"
              icon={<Send size={14} />}
              loading={sending}
              disabled={!conversation || text.trim().length === 0}
              onClick={() => void send()}
            >
              {t("workbuddy.chatflow.send")}
            </Button>
            <Text type="secondary">{t("workbuddy.chatflow.bindingHint")}</Text>
          </Space>
          {error !== null && (
            <Alert
              type="error"
              showIcon
              message={apiErrorMessage(
                error,
                t("workbuddy.chatflow.failed"),
                t,
              )}
            />
          )}
          {sent !== null && (
            <Alert
              type={sent.matched > 0 ? "success" : "info"}
              showIcon
              message={t("workbuddy.chatflow.result", { count: sent.matched })}
            />
          )}
        </Space>
      </Card>

      <Card
        size="small"
        style={{ marginTop: 12 }}
        title={t("workbuddy.chatflow.deliveriesTitle")}
        extra={
          <Button
            size="small"
            disabled={!conversation}
            onClick={() => void loadDeliveries(conversation)}
          >
            {t("common.refresh")}
          </Button>
        }
      >
        <Table<ConversationDelivery>
          rowKey="delivery_id"
          size="small"
          columns={columns}
          dataSource={deliveries}
          pagination={false}
          locale={{
            emptyText: loaded
              ? t("workbuddy.chatflow.empty")
              : t("workbuddy.chatflow.emptyHint"),
          }}
        />
      </Card>
    </PageShell>
  );
}
