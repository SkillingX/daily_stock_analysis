import { useEffect, useState } from 'react';
import apiClient from '../api';

export type EnabledMarket = 'cn' | 'hk' | 'us';

/** New-operation choices come from the server; historical rows remain visible. */
export function useEnabledMarkets() {
  const [markets, setMarkets] = useState<EnabledMarket[]>([]);
  const [error, setError] = useState<Error | null>(null);
  useEffect(() => {
    let mounted = true;
    apiClient.get<{ enabled_markets: EnabledMarket[] }>('/api/v1/health')
      .then(({ data }) => { if (mounted) setMarkets(data.enabled_markets); })
      .catch((reason: Error) => { if (mounted) setError(reason); });
    return () => { mounted = false; };
  }, []);
  return { markets, error };
}
