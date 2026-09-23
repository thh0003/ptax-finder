/** Public settings served by the backend so the same build works in every environment. */
export type AppConfig = {
  cognito_client_id: string;
  cognito_endpoint_url: string | null;
  aws_region: string;
};

let cached: Promise<AppConfig> | null = null;

export function getConfig(): Promise<AppConfig> {
  cached ??= fetch("/api/config").then(async (response) => {
    if (!response.ok) {
      cached = null;
      throw new Error(`Could not load app config (${response.status})`);
    }
    return (await response.json()) as AppConfig;
  });
  return cached;
}
