/**
 * Describe a workflow, get a draft (A-15).
 *
 * The dialog is deliberately thin: it sends a description and shows what came
 * back. It never builds a definition, because the point of the authoring route is
 * that a model describes and the compiler decides — and the second half of that
 * promise is visible here, since a description the compiler refused comes back
 * with the compiler's own diagnostics rather than a vague failure.
 *
 * What succeeds is a *draft*: the caller receives its id and the panel keeps it
 * un-published, so publishing stays a separate, deliberate act.
 */
import { useCallback, useState } from "react";
import {
  Alert,
  Button,
  Input,
  List,
  Modal,
  Space,
  Tag,
  Typography,
} from "antd";
import { Sparkles } from "lucide-react";
import { useTranslation } from "react-i18next";

import { apiErrorMessage } from "../../../utils/apiError";
import {
  parseWorkflowDiagnostics,
  workbuddyWorkflowsApi,
  type AuthoredWorkflow,
  type WorkflowDiagnostic,
} from "../../../api/modules/workbuddyWorkflows";

const { Text, Paragraph } = Typography;

interface Props {
  open: boolean;
  onClose: () => void;
  /** Handed the created draft's id; the panel decides what to open. */
  onCreated: (workflowId: string) => void;
}

export default function AuthorWorkflowModal({
  open,
  onClose,
  onCreated,
}: Props) {
  const { t } = useTranslation();
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [diagnostics, setDiagnostics] = useState<WorkflowDiagnostic[] | null>(
    null,
  );
  const [authored, setAuthored] = useState<AuthoredWorkflow | null>(null);

  const reset = useCallback(() => {
    setDescription("");
    setError(null);
    setDiagnostics(null);
    setAuthored(null);
    setBusy(false);
  }, []);

  const close = useCallback(() => {
    reset();
    onClose();
  }, [reset, onClose]);

  const generate = useCallback(async () => {
    const request = description.trim();
    if (!request) return;
    setBusy(true);
    setError(null);
    setDiagnostics(null);
    try {
      const draft = await workbuddyWorkflowsApi.authorWorkflowDraft({
        request,
      });
      setAuthored(draft);
    } catch (caught) {
      setError(caught);
      // The refusal carries the compiler's diagnostics: showing them is the
      // difference between "it did not work" and something a person can act on.
      setDiagnostics(parseWorkflowDiagnostics(caught));
    } finally {
      setBusy(false);
    }
  }, [description]);

  const openDraft = useCallback(() => {
    if (authored === null) return;
    const id = authored.id;
    reset();
    onCreated(id);
  }, [authored, reset, onCreated]);

  return (
    <Modal
      open={open}
      onCancel={close}
      width={680}
      title={t("workbuddy.workflows.author.title")}
      footer={
        authored === null
          ? [
              <Button key="cancel" onClick={close}>
                {t("workbuddy.workflows.author.cancel")}
              </Button>,
              <Button
                key="generate"
                type="primary"
                icon={<Sparkles size={14} />}
                loading={busy}
                disabled={description.trim().length === 0}
                onClick={() => void generate()}
              >
                {t("workbuddy.workflows.author.generate")}
              </Button>,
            ]
          : [
              <Button key="again" onClick={reset}>
                {t("workbuddy.workflows.author.again")}
              </Button>,
              <Button key="open" type="primary" onClick={openDraft}>
                {t("workbuddy.workflows.author.openDraft")}
              </Button>,
            ]
      }
    >
      <Paragraph type="secondary">
        {t("workbuddy.workflows.author.hint")}
      </Paragraph>
      <Input.TextArea
        value={description}
        onChange={(event) => setDescription(event.target.value)}
        rows={4}
        maxLength={4000}
        showCount
        disabled={authored !== null}
        placeholder={t("workbuddy.workflows.author.placeholder")}
      />

      {error !== null && authored === null && (
        <Alert
          style={{ marginTop: 12 }}
          type="error"
          showIcon
          message={apiErrorMessage(
            error,
            t("workbuddy.workflows.author.failed"),
            t,
          )}
          description={
            diagnostics !== null && diagnostics.length > 0 ? (
              <List
                size="small"
                dataSource={diagnostics}
                renderItem={(item) => (
                  <List.Item>
                    <Text code>{item.code}</Text>
                    <Text style={{ marginLeft: 8 }}>{item.message}</Text>
                    {item.path ? (
                      <Text type="secondary" style={{ marginLeft: 8 }}>
                        {item.path}
                      </Text>
                    ) : null}
                  </List.Item>
                )}
              />
            ) : null
          }
        />
      )}

      {authored !== null && (
        <div style={{ marginTop: 12 }}>
          <Space size={8} wrap>
            <Text strong>{authored.name}</Text>
            <Tag color="blue">
              {t("workbuddy.workflows.author.rounds", {
                count: authored.authoring.rounds,
              })}
            </Tag>
          </Space>
          <List
            size="small"
            style={{ marginTop: 8 }}
            dataSource={authored.authoring.steps}
            renderItem={(step) => (
              <List.Item>
                <Text code>{step.id}</Text>
                <Text style={{ marginLeft: 8 }}>{step.purpose}</Text>
                {step.uses.length > 0 ? (
                  <Text type="secondary" style={{ marginLeft: 8 }}>
                    ← {step.uses.join(", ")}
                  </Text>
                ) : null}
              </List.Item>
            )}
          />
        </div>
      )}
    </Modal>
  );
}
