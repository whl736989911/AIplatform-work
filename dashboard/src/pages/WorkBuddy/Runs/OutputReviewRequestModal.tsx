/**
 * WorkBuddy runs → ask reviewers to look at what a settled run produced.
 *
 * The one place a review is *requested*: the runs list opens it for a settled
 * execution that has no review yet, and the request itself is a plain body of
 * tenant membership ids — the same shape and the same tag-style picker the
 * proposals reviewer assignment uses, because both resolve identities to the
 * members of this tenant.
 *
 * A review is not a release step: the execution settled, and the modal says so.
 * The server refuses the request (409) when the run is still moving or when a
 * review already exists, and its own message is what is shown.
 */

import { useCallback, useState } from "react";
import { Alert, Form, Modal, Select } from "antd";
import { message } from "@/utils/antdMessage";
import { useTranslation } from "react-i18next";
import { apiErrorMessage } from "../../../utils/apiError";
import {
  workbuddyRuntimeApi,
  type OutputReview,
} from "../../../api/modules/workbuddyRuntime";
import { isUuid } from "../Proposals/helpers";

interface ReviewersFormValues {
  reviewer_user_ids?: string[];
}

/** At most ten, which is the cap both the router and the server enforce. */
const MAX_REVIEWERS = 10;

export default function OutputReviewRequestModal({
  executionId,
  open,
  onClose,
  onRequested,
}: {
  /** The settled run the review is asked for; ``null`` keeps it closed. */
  executionId: string | null;
  open: boolean;
  onClose: () => void;
  onRequested: (review: OutputReview) => void;
}) {
  const { t } = useTranslation();
  const [form] = Form.useForm<ReviewersFormValues>();
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = useCallback(
    async (values: ReviewersFormValues) => {
      if (!executionId) return;
      setSubmitting(true);
      setError(null);
      try {
        const review = await workbuddyRuntimeApi.requestOutputReview(
          executionId,
          { reviewer_user_ids: values.reviewer_user_ids ?? [] },
        );
        message.success(t("workbuddy.runs.reviewRequestSuccess"));
        form.resetFields();
        onRequested(review);
        onClose();
      } catch (err) {
        setError(
          apiErrorMessage(
            err,
            t("workbuddy.runs.reviewRequestFailed"),
            t,
          ),
        );
      } finally {
        setSubmitting(false);
      }
    },
    [executionId, form, onClose, onRequested, t],
  );

  return (
    <Modal
      title={t("workbuddy.runs.reviewRequestTitle")}
      open={open}
      onCancel={onClose}
      onOk={() => form.submit()}
      confirmLoading={submitting}
      okText={t("workbuddy.runs.reviewRequestSubmit")}
      cancelText={t("common.cancel")}
      destroyOnHidden
      maskClosable={false}
    >
      <Alert
        type="info"
        showIcon
        message={t("workbuddy.runs.reviewRequestSettled")}
        description={t("workbuddy.runs.reviewRequestSettledHint")}
      />
      <Form
        form={form}
        layout="vertical"
        requiredMark={false}
        onFinish={(values) => void submit(values)}
      >
        <Form.Item
          name="reviewer_user_ids"
          label={t("workbuddy.runs.reviewRequestField")}
          extra={t("workbuddy.runs.reviewRequestFieldHint")}
          rules={[
            {
              validator: (_rule, value: string[] | undefined) => {
                const named = (value ?? []).map((entry) => entry.trim());
                if (named.length === 0) {
                  return Promise.reject(
                    new Error(t("workbuddy.runs.reviewRequestRequired")),
                  );
                }
                if (named.length > MAX_REVIEWERS) {
                  return Promise.reject(
                    new Error(
                      t("workbuddy.runs.reviewRequestTooMany", {
                        max: MAX_REVIEWERS,
                      }),
                    ),
                  );
                }
                return named.every(isUuid)
                  ? Promise.resolve()
                  : Promise.reject(
                      new Error(t("workbuddy.runs.reviewRequestInvalid")),
                    );
              },
            },
          ]}
        >
          <Select
            mode="tags"
            open={false}
            tokenSeparators={[",", " ", "\n"]}
            placeholder={t("workbuddy.runs.reviewRequestPlaceholder")}
          />
        </Form.Item>
      </Form>
      {error !== null && (
        <Alert type="error" showIcon message={error} />
      )}
    </Modal>
  );
}
