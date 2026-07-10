import { type ReactElement, useEffect, useState } from "react";

type HealthResponse = {
  status: string;
};

const apiBaseUrl = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

export function App(): ReactElement {
  const [health, setHealth] = useState<string>("checking");

  useEffect(() => {
    const controller = new AbortController();

    async function loadHealth(): Promise<void> {
      try {
        const response = await fetch(`${apiBaseUrl}/api/health/`, {
          signal: controller.signal,
        });
        const payload = (await response.json()) as HealthResponse;
        setHealth(payload.status);
      } catch {
        setHealth("unavailable");
      }
    }

    void loadHealth();

    return () => {
      controller.abort();
    };
  }, []);

  return (
    <main className="app-shell">
      <section className="status-panel" aria-labelledby="page-title">
        <p className="eyebrow">Polymarket Screener</p>
        <h1 id="page-title">Market signal workspace</h1>
        <dl>
          <div>
            <dt>API health</dt>
            <dd>{health}</dd>
          </div>
        </dl>
      </section>
    </main>
  );
}
