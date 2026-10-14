import { useCallback, useEffect, useRef, useState } from "react";
import { getKnowledgeIndex, peekKnowledgeIndexCache } from "../services/workspaceApi";

export function useKnowledgeIndex() {
  const mountedRef = useRef(true);
  const [indexPayload, setIndexPayload] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const loadIndex = useCallback(async ({ force = false } = {}) => {
    const hasWarmCache = !force && peekKnowledgeIndexCache();
    if (!hasWarmCache) {
      setLoading(true);
    }
    setError("");

    try {
      const payload = await getKnowledgeIndex({ force });
      if (mountedRef.current) {
        setIndexPayload(payload);
      }
      return payload;
    } catch (loadError) {
      if (mountedRef.current) {
        setError(loadError.message || "知识库索引加载失败。");
      }
      throw loadError;
    } finally {
      if (mountedRef.current) {
        setLoading(false);
      }
    }
  }, []);

  const ensureIndex = useCallback(() => loadIndex(), [loadIndex]);
  const reloadIndex = useCallback(() => loadIndex({ force: true }), [loadIndex]);

  return {
    indexPayload,
    loading,
    error,
    ensureIndex,
    reloadIndex
  };
}
