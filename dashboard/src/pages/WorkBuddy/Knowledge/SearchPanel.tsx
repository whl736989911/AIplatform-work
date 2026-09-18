/**
 * 知识库 → 检索.
 *
 * Cosine search inside the selected base, using its pinned embedding revision.
 * Search is explicit (button / Enter) because it embeds the query server-side;
 * a missing backend slice is reported as an informational notice, never as a
 * fabricated empty result.
 */

import { useEffect, useState } from "react";
import {
  Alert,
  Button,
  Form,
  Input,
  InputNumber,
  Space,
  Spin,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import { Search } from "lucide-react";
import { useTranslation } from "react-i18next";
import { EmptyState } from "../../../components/EmptyState";
import { apiErrorMessage } from "../../../utils/apiError";
import {
  workbuddyKnowledgeApi,
  type KnowledgeBase,
  type KnowledgeSearchHit,
  type KnowledgeSearchResult,
} from "../../../api/modules/workbuddyKnowledge";
import { TabPanelHeader } from "../../Settings/AdvancedSettings/TabPanelHeader";
import { isSliceUnavailable } from "./useKnowledgeResource";
import styles from "./index.module.less";

const { Text } = Typography;

interface SearchFormValues {
  query: string;
  match_count: number;
}

export default function SearchPanel({ base }: { base: KnowledgeBase }) {
  const { t } = useTranslation();
  const [form] = Form.useForm<SearchFormValues>();
  const [result, setResult] = useState<KnowledgeSearchResult | null>(null);
  const [searchedQuery, setSearchedQuery] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<unknown | null>(null);
  const [unavailable, setUnavailable] = useState(false);

  useEffect(() => {
    setResult(null);
    setError(null);
    setUnavailable(false);
    setSearchedQuery("");
  }, [base.kb_id]);

  const onSearch = async (values: SearchFormValues) => {
    const query = values.query.trim();
    setLoading(true);
    try {
      const page = await workbuddyKnowledgeApi.search(base.kb_id, {
        query,
        match_count: values.match_count,
      });
      setResult(page);
      setSearchedQuery(query);
      setError(null);
      setUnavailable(false);
    } catch (err) {
      console.error("[workbuddy/knowledge]", err);
      setResult(null);
      setError(err);
      setUnavailable(isSliceUnavailable(err));
    } finally {
      setLoading(false);
    }
  };

  const renderHit = (hit: KnowledgeSearchHit, index: number) => (
    <div className={styles.hitCard} key={hit.chunk_id}>
      <div className={styles.hitHead}>
        <Space size={8} wrap>
          <Tag color="geekblue">#{index + 1}</Tag>
          <Text strong>
            {hit.document_title || t("workbuddy.knowledge.search.untitled")}
          </Text>
          <Tooltip title={t("workbuddy.knowledge.search.scoreHint")}>
            <Tag color="green">{hit.score.toFixed(4)}</Tag>
          </Tooltip>
        </Space>
        <Space size={12} wrap>
          <Text type="secondary" className={styles.mono}>
            {t("workbuddy.knowledge.search.ordinal")} {hit.ordinal}
          </Text>
          <Tooltip title={hit.generation_id}>
            <Text type="secondary" className={styles.mono}>
              {t("workbuddy.knowledge.search.generation")}
            </Text>
          </Tooltip>
          <Tooltip title={hit.document_id}>
            <Text type="secondary" className={styles.mono}>
              {t("workbuddy.knowledge.search.document")}
            </Text>
          </Tooltip>
        </Space>
      </div>
      <div className={styles.hitBody}>{hit.content}</div>
    </div>
  );

  return (
    <div className={styles.panel}>
      <TabPanelHeader
        icon={<Search size={18} />}
        title={t("workbuddy.knowledge.search.title")}
        description={t("workbuddy.knowledge.search.desc", { name: base.name })}
      />

      <Form
        form={form}
        layout="inline"
        className={styles.searchForm}
        initialValues={{ match_count: 5 }}
        onFinish={(values) => void onSearch(values)}
      >
        <Form.Item
          name="query"
          className={styles.searchQueryItem}
          rules={[
            {
              required: true,
              message: t("workbuddy.knowledge.search.queryRequired"),
            },
          ]}
        >
          <Input
            allowClear
            maxLength={2000}
            placeholder={t("workbuddy.knowledge.search.queryPlaceholder")}
          />
        </Form.Item>
        <Form.Item
          name="match_count"
          label={t("workbuddy.knowledge.search.matchCount")}
        >
          <InputNumber min={1} max={50} precision={0} />
        </Form.Item>
        <Form.Item>
          <Button
            type="primary"
            htmlType="submit"
            icon={<Search size={14} />}
            loading={loading}
          >
            {t("workbuddy.knowledge.search.submit")}
          </Button>
        </Form.Item>
      </Form>

      {unavailable ? (
        <Alert
          type="info"
          showIcon
          message={t("workbuddy.knowledge.common.notMergedTitle")}
          description={t("workbuddy.knowledge.common.notMergedHint")}
        />
      ) : error ? (
        <Alert
          type="error"
          showIcon
          message={t("workbuddy.knowledge.search.failed")}
          description={apiErrorMessage(
            error,
            t("workbuddy.knowledge.search.failed"),
            t,
          )}
        />
      ) : loading ? (
        <div className={styles.centered}>
          <Spin />
        </div>
      ) : result === null ? (
        <EmptyState
          title={t("workbuddy.knowledge.search.initial")}
          description={t("workbuddy.knowledge.search.initialHint")}
        />
      ) : result.hits.length === 0 ? (
        <EmptyState
          variant="mascot"
          title={t("workbuddy.knowledge.search.noHits")}
          description={t("workbuddy.knowledge.search.noHitsHint", {
            query: searchedQuery,
          })}
        />
      ) : (
        <>
          <Space size={8} wrap className={styles.resultMeta}>
            <Tag color="blue">
              {t("workbuddy.knowledge.search.matchCountLabel", {
                count: result.match_count,
              })}
            </Tag>
            <Text type="secondary">
              {t("workbuddy.knowledge.search.embedding", {
                model: result.embedding.model_key,
                revision: result.embedding.revision,
                dimensions: result.embedding.dimensions,
              })}
            </Text>
          </Space>
          <div className={styles.hitList}>
            {result.hits.map((hit, index) => renderHit(hit, index))}
          </div>
        </>
      )}
    </div>
  );
}
