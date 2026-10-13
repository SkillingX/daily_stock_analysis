/**
 * useStockIndex Hook
 *
 * Manage stock index loading and state
 */

import { useState, useEffect } from 'react';
import type { StockIndexItem } from '../types/stockIndex';
import { preloadStockIndex } from '../utils/stockIndexLoader';
import type { IndexLoadResult } from '../utils/stockIndexLoader';
import { useEnabledMarkets } from './useEnabledMarkets';

export interface UseStockIndexResult {
  /** Stock index data */
  index: StockIndexItem[];
  /** Is loading */
  loading: boolean;
  /** Load error */
  error: Error | null;
  /** Whether fallback mode is used */
  fallback: boolean;
  /** Is loaded */
  loaded: boolean;
}

/**
 * Stock index loading Hook
 *
 * @returns Index state and data
 */
export function useStockIndex(): UseStockIndexResult {
  const { markets, error: marketError } = useEnabledMarkets();
  const [index, setIndex] = useState<StockIndexItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);
  const [fallback, setFallback] = useState(false);

  useEffect(() => {
    let mounted = true;

    async function load() {
      setLoading(true);
      setError(null);

      const result: IndexLoadResult = await preloadStockIndex();

      if (mounted) {
        setIndex(result.data);
        setFallback(result.fallback);
        if (result.error) {
          setError(result.error);
        }
        setLoading(false);
      }
    }

    load();

    return () => {
      mounted = false;
    };
  }, []);

  return {
    index: index.filter((item) => markets.includes(item.market === 'HK' ? 'hk' : item.market === 'US' ? 'us' : 'cn')),
    loading: loading || (markets.length === 0 && !marketError),
    error: error || marketError,
    fallback,  // Whether fallback
    loaded: !loading,
  };
}

/**
 * Get default exported Hook
 */
export default useStockIndex;
