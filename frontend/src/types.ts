export type User = { username: string; role: string; csrf: string };
export type Config = {
  instance: string;
  application: string;
  environment: string;
  groups: string[];
  services: { id: string; name: string }[];
  publish_seconds: number;
};
export type Point = {
  time: number;
  codes: Record<string, number>;
  total: number;
};
export type Route = {
  service: string;
  group: string;
  route: string;
  method: string;
  count: number;
  errors: number;
  rpm: number;
  p95: number | null;
};
export type Worker = {
  id: string;
  updated: number;
  state: string;
  invalid_lines?: number;
};
export type Metrics = {
  total: number;
  errors: number;
  rpm: number;
  p50: number | null;
  p95: number | null;
  p99: number | null;
  latency_overflow: number;
  codes: Record<string, number>;
  series: Point[];
  routes: Route[];
  step: number;
  published_at: number;
  newest_event: number;
  workers: Worker[];
};
export type Incident = {
  id: string;
  service: string;
  route_group: string;
  route: string;
  method: string;
  status: number;
  payload: {
    count: number;
    first_seen: number;
    last_seen: number;
    samples: { time: number; request_id: string }[];
  };
};
export type ServiceStatus = {
  id: string;
  name: string;
  configured: boolean;
  state: string;
  checked_at: number | null;
  latency_ms: number | null;
  reason: string | null;
  availability: number | null;
  coverage: number;
  history: { time: number; state: string }[];
};
