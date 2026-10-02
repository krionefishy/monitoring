import { useEffect, useRef, useState } from "react";
import {
  Activity,
  BarChart3,
  ChevronRight,
  Database,
  FileWarning,
  HeartPulse,
  LogOut,
  RefreshCw,
  Server,
  ShieldCheck,
  ArrowLeft,
  Clock3,
} from "lucide-react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type {
  Config,
  Incident,
  Metrics,
  ServiceStatus,
  User,
  Worker,
} from "./types";
import { chartPoints, codeColor } from "./chart";
const fmt = (n: number) =>
  new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 1 }).format(n);
const date = (n: number | null | undefined) =>
  n
    ? new Date(n * 1000).toLocaleString("ru-RU", {
        day: "2-digit",
        month: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      })
    : "—";
const ms = (n: number | null | undefined, overflow = 0) =>
  n == null ? (overflow ? ">60 000 мс" : "—") : `${fmt(n)} мс`;
const labels: Record<string, string> = {
  healthy: "Healthy",
  degraded: "Degraded",
  down: "Down",
  unknown: "Unknown",
};
const tones: Record<string, string> = {
  healthy: "green",
  degraded: "amber",
  down: "red",
};
const pages = [
  { id: "dashboards", name: "Dashboards", icon: BarChart3 },
  { id: "incidents", name: "Incidents", icon: FileWarning },
  { id: "services_status", name: "Services status", icon: HeartPulse },
  { id: "db_monitoring", name: "DB monitoring", icon: Database },
];
class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}
async function api<T>(url: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(url, {
    ...options,
    headers: { "Content-Type": "application/json", ...options.headers },
  });
  if (!response.ok) {
    let message = "Не удалось загрузить данные";
    try {
      const data = await response.json();
      message = typeof data.detail === "string" ? data.detail : message;
    } catch {
      /* non-JSON error */
    }
    throw new ApiError(response.status, message);
  }
  return response.json();
}
function Empty({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="empty">
      <BarChart3 size={34} />
      <h2>{title}</h2>
      <p>{children}</p>
    </div>
  );
}
function Badge({ state }: { state: string }) {
  return (
    <span className={`badge ${tones[state] || ""}`}>
      {labels[state] || state}
    </span>
  );
}
function Code({ code }: { code: number }) {
  return (
    <span className="badge" style={{ color: codeColor(String(code)) }}>
      {code}
    </span>
  );
}
function Login({ onLogin }: { onLogin: (u: User) => void }) {
  const [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  return (
    <div className="login">
      <form
        className="panel"
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          setError("");
          const data = new FormData(e.currentTarget);
          try {
            onLogin(
              await api("/api/auth/login", {
                method: "POST",
                body: JSON.stringify({
                  username: data.get("username"),
                  password: data.get("password"),
                }),
              }),
            );
          } catch (e) {
            setError((e as Error).message);
          } finally {
            setBusy(false);
          }
        }}
      >
        <div className="brand">
          <Activity size={23} /> Monitoring
        </div>
        <h1>Вход в мониторинг</h1>
        <p className="muted">Доступ к приложению на этом сервере.</p>
        <label>
          Логин
          <input
            name="username"
            autoComplete="username"
            required
            maxLength={100}
            autoFocus
          />
        </label>
        <label>
          Пароль
          <input
            name="password"
            type="password"
            autoComplete="current-password"
            required
            maxLength={256}
          />
        </label>
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        <button className="primary" disabled={busy}>
          {busy ? "Входим…" : "Войти"}
        </button>
        <p className="muted">
          Учётную запись создаёт администратор этой установки.
        </p>
      </form>
    </div>
  );
}
function StatusChart({ data }: { data: Metrics }) {
  const [mode, setMode] = useState<"rpm" | "share">("rpm"),
    [hidden, setHidden] = useState<string[]>([]);
  const codes = Object.keys(data.codes);
  return (
    <>
      <div className="panel-heading">
        <div>
          <h2>Ответы на запросы</h2>
          <p>
            {mode === "rpm" ? "Запросов в минуту" : "Доля от всех ответов, %"} ·
            интервал {data.step / 60} мин
          </p>
        </div>
        <div className="segmented">
          <button
            className={mode === "rpm" ? "selected" : ""}
            onClick={() => setMode("rpm")}
          >
            Запросы / мин
          </button>
          <button
            className={mode === "share" ? "selected" : ""}
            onClick={() => setMode("share")}
          >
            Доля, %
          </button>
        </div>
      </div>
      {!data.total ? (
        <Empty title="Пока нет запросов">
          В выбранном периоде нет опубликованных данных. Новые запросы появятся
          после обработки nginx-лога.
        </Empty>
      ) : (
        <>
          <div className="chart">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart
                data={chartPoints(data.series, data.step, mode)}
                margin={{ top: 12, right: 12, bottom: 4, left: -17 }}
              >
                <CartesianGrid
                  vertical={false}
                  stroke="#2a2f38"
                  strokeDasharray="3 5"
                />
                <XAxis
                  dataKey="time"
                  minTickGap={65}
                  tickFormatter={(v) =>
                    new Date(v * 1000).toLocaleTimeString("ru-RU", {
                      hour: "2-digit",
                      minute: "2-digit",
                    })
                  }
                  tick={{ fill: "#8b93a0", fontSize: 11 }}
                  axisLine={false}
                  tickLine={false}
                />
                <YAxis
                  domain={mode === "share" ? [0, 100] : [0, "auto"]}
                  tick={{ fill: "#8b93a0", fontSize: 11 }}
                  axisLine={false}
                  tickLine={false}
                  tickFormatter={(v) => fmt(v)}
                />
                <Tooltip
                  content={({ active, payload, label }) =>
                    active && payload?.length ? (
                      <div className="tooltip">
                        <p className="muted">{date(Number(label))}</p>
                        {payload.map((p) => (
                          <p key={String(p.dataKey)} style={{ color: p.color }}>
                            {String(p.dataKey)}{" "}
                            <strong>
                              {fmt(Number(p.value))}
                              {mode === "share" ? "%" : " / мин"}
                            </strong>
                          </p>
                        ))}
                      </div>
                    ) : null
                  }
                />
                {codes
                  .filter((c) => !hidden.includes(c))
                  .map((code) => (
                    <Area
                      key={code}
                      type="linear"
                      dataKey={code}
                      stackId="responses"
                      stroke={codeColor(code)}
                      fill={codeColor(code)}
                      fillOpacity={0.48}
                      strokeWidth={1.5}
                      isAnimationActive={false}
                    />
                  ))}
              </AreaChart>
            </ResponsiveContainer>
          </div>
          <div className="legend">
            {codes.map((code) => (
              <button
                key={code}
                className={hidden.includes(code) ? "hidden" : ""}
                aria-pressed={!hidden.includes(code)}
                onClick={() =>
                  setHidden((old) =>
                    old.includes(code)
                      ? old.filter((c) => c !== code)
                      : [...old, code],
                  )
                }
              >
                <span
                  className="legend-dot"
                  style={{ background: codeColor(code) }}
                />
                {code}
                <span className="muted"> · {fmt(data.codes[code])}</span>
              </button>
            ))}
          </div>
        </>
      )}
    </>
  );
}
function Diagnostics({
  workers,
  published,
  interval = 300,
}: {
  workers: Worker[];
  published?: number;
  interval?: number;
}) {
  const warnings: string[] = [];
  const collector = workers.find((w) => w.id === "collector");
  if (!collector) warnings.push("Сборщик ещё не подключился.");
  if (collector && collector.state !== "running")
    warnings.push(
      collector.state === "missing_log"
        ? "Файл nginx-лога не найден."
        : "Буфер сборщика заполнен; чтение приостановлено.",
    );
  if (collector?.invalid_lines)
    warnings.push(`Пропущено некорректных строк: ${collector.invalid_lines}.`);
  if (
    collector?.backlog_bytes &&
    (collector.catching_up || collector.state === "backpressure")
  )
    warnings.push(
      `В nginx-логе ещё не обработано ${fmt(collector.backlog_bytes / 1048576)} МиБ. Статистика пока неполная.`,
    );
  const stale = workers.filter(
    (w) =>
      Date.now() / 1000 - w.updated >
      (w.id === "publisher" ? interval + 90 : 90),
  );
  if (stale.length)
    warnings.push(`Нет обновлений: ${stale.map((w) => w.id).join(", ")}.`);
  if (published && Date.now() / 1000 - published > interval + 90)
    warnings.push("Публикация аналитики задерживается.");
  return warnings.length ? (
    <div className="notice" role="status">
      {warnings.join(" ")}
    </div>
  ) : null;
}
export default function App() {
  const [location, setLocation] = useState(
    window.location.pathname + window.location.search,
  );
  useEffect(() => {
    const f = () =>
      setLocation(window.location.pathname + window.location.search);
    window.addEventListener("popstate", f);
    return () => window.removeEventListener("popstate", f);
  }, []);
  const navigate = (path: string) => {
    window.history.pushState({}, "", path);
    setLocation(path);
  };
  const url = new URL(location, window.location.origin),
    params = url.searchParams,
    page =
      pages.find((p) => url.pathname.split("/")[1] === p.id)?.id ||
      "dashboards",
    detail = page === "incidents" ? url.pathname.split("/")[2] : undefined;
  const [user, setUser] = useState<User | null | undefined>(undefined),
    [config, setConfig] = useState<Config | null>(null),
    [data, setData] = useState<Metrics | null>(null),
    [incidents, setIncidents] = useState<Incident[]>([]),
    [incidentTotal, setIncidentTotal] = useState(0),
    [incident, setIncident] = useState<Incident | null>(null),
    [services, setServices] = useState<ServiceStatus[]>([]),
    [workers, setWorkers] = useState<Worker[]>([]),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false),
    [revision, setRevision] = useState(0),
    [loading, setLoading] = useState(true);
  const hours = params.get("hours") || "24",
    service = params.get("service") || "",
    group = params.get("group") || "",
    route = params.get("route") || "",
    method = params.get("method") || "",
    offset = Number(params.get("offset") || 0),
    query = new URLSearchParams({
      hours,
      service,
      group,
      route,
      method,
    }).toString();
  const filter = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    next.delete("state");
    if (key !== "offset") next.delete("offset");
    if (value) next.set(key, value);
    else next.delete(key);
    if (["group", "service"].includes(key)) {
      next.delete("route");
      next.delete("method");
    }
    navigate(`/${page}${detail ? "/" + detail : ""}?${next}`);
  };
  useEffect(() => {
    api<User>("/api/auth/me")
      .then(setUser)
      .catch(() => setUser(null));
  }, []);
  useEffect(() => {
    if (user)
      api<Config>("/api/config")
        .then(setConfig)
        .catch((e) => setError(e.message));
  }, [user, revision]);
  useEffect(() => {
    if (!user) return;
    const timer = setInterval(() => setRevision((v) => v + 1), 30000);
    return () => clearInterval(timer);
  }, [user]);
  const lastQuery = useRef("");
  useEffect(() => {
    if (!user) return;
    let cancelled = false;
    setBusy(true);
    setError("");
    const currentQuery = `${page}:${detail || ""}:${query}:${offset}`;
    if (lastQuery.current !== currentQuery) {
      setLoading(true);
      setData(null);
      setIncident(null);
      lastQuery.current = currentQuery;
    }
    const run = async () => {
      if (page === "dashboards") {
        const v = await api<Metrics>(`/api/metrics?${query}`);
        if (!cancelled) setData(v);
      }
      if (page === "incidents") {
        if (detail) {
          const v = await api<Incident>(`/api/incidents/${detail}`);
          const chart = await api<Metrics>(
            `/api/metrics?${new URLSearchParams({ hours, service: v.service, route: v.route, method: v.method })}`,
          );
          if (!cancelled) {
            setIncident(v);
            setData(chart);
          }
        } else {
          const v = await api<{ items: Incident[]; total: number }>(
            `/api/incidents?${query}&offset=${offset}`,
          );
          if (!cancelled) {
            setIncidents(v.items);
            setIncidentTotal(v.total);
          }
        }
      }
      if (page === "services_status") {
        const v = await api<{ services: ServiceStatus[]; workers: Worker[] }>(
          "/api/services-status",
        );
        if (!cancelled) {
          setServices(v.services);
          setWorkers(v.workers);
        }
      }
    };
    run()
      .catch((e) => {
        if (!cancelled) {
          setError(e.message);
          if (e.status === 401) setUser(null);
        }
      })
      .finally(() => {
        if (!cancelled) {
          setBusy(false);
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [user, page, detail, query, offset, revision, hours]);
  const openRoute = (r: { service: string; route: string; method: string }) =>
    navigate(
      `/dashboards?${new URLSearchParams({ hours, service: r.service, route: r.route, method: r.method })}`,
    );
  if (user === undefined)
    return <div className="loading">Подключение к мониторингу…</div>;
  if (!user)
    return (
      <Login
        onLogin={(u) => {
          setUser(u);
          setError("");
        }}
      />
    );
  const active = pages.find((p) => p.id === page)!;
  return (
    <div className="shell">
      <aside>
        <div className="brand">
          <Activity size={24} /> Monitoring
        </div>
        <div className="nav-caption">Рабочее пространство</div>
        <nav>
          {pages.map((p) => (
            <a
              key={p.id}
              href={`/${p.id}`}
              className={page === p.id ? "active" : ""}
              onClick={(e) => {
                e.preventDefault();
                navigate(`/${p.id}`);
              }}
            >
              <p.icon size={18} />
              {p.name}
            </a>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="small">
            <span className="instance-dot" />
            {config?.application || "Локальная установка"}
          </div>
          <div className="small muted">
            Self-hosted · {config?.environment || "—"}
          </div>
          <div className="small muted">
            <ShieldCheck size={13} /> Доступ только к этой установке
          </div>
        </div>
      </aside>
      <div className="workspace">
        <header>
          <span>
            Рабочее пространство <ChevronRight size={11} /> {active.name}
          </span>
          <div className="header-user">
            <span>{user.username}</span>
            <button
              aria-label="Выйти"
              onClick={async () => {
                try {
                  await api("/api/auth/logout", {
                    method: "POST",
                    headers: { "X-CSRF-Token": user.csrf },
                  });
                  setUser(null);
                  setConfig(null);
                } catch (e) {
                  setError((e as Error).message);
                }
              }}
            >
              <LogOut size={14} />
            </button>
          </div>
        </header>
        <main>
          <div className="page-title">
            <div>
              <div className="eyebrow">
                {config?.application || "Monitoring"} /{" "}
                {config?.environment || "—"}
              </div>
              <h1>{detail ? "Инцидент" : active.name}</h1>
              <p>
                {page === "dashboards"
                  ? "Трафик, ответы и время обработки запросов"
                  : page === "incidents"
                    ? "Накопление ошибок по ручкам и кодам ответа"
                    : page === "services_status"
                      ? "Доступность сервисов по их health-эндпоинтам"
                      : "Показатели баз данных вашего приложения"}
              </p>
            </div>
            <button
              aria-label="Обновить данные"
              disabled={busy}
              onClick={() => setRevision((v) => v + 1)}
            >
              <RefreshCw size={16} />
            </button>
          </div>
          {error && (
            <div className="notice error" role="alert">
              {error}{" "}
              <button
                className="text-button"
                onClick={() => setRevision((v) => v + 1)}
              >
                Повторить
              </button>
            </div>
          )}
          {(page === "dashboards" || page === "incidents") && (
            <div className="toolbar">
              <label>
                Сервис
                <select
                  value={detail && incident ? incident.service : service}
                  disabled={!!detail}
                  onChange={(e) => filter("service", e.target.value)}
                >
                  <option value="">Все сервисы</option>
                  {config?.services.map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.name}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                Группа ручек
                <select
                  value={detail && incident ? incident.route_group : group}
                  disabled={!!detail}
                  onChange={(e) => filter("group", e.target.value)}
                >
                  <option value="">all_groups</option>
                  {config?.groups.map((g) => (
                    <option key={g} value={g}>
                      per_route_group · {g}
                    </option>
                  ))}
                </select>
              </label>
              {(page === "dashboards" || detail) && (
                <label>
                  Период
                  <select
                    value={hours}
                    onChange={(e) => filter("hours", e.target.value)}
                  >
                    {[
                      [1, "Последний час"],
                      [6, "6 часов"],
                      [24, "24 часа"],
                      [168, "7 дней"],
                      [720, "30 дней"],
                    ].map(([v, t]) => (
                      <option key={v} value={v}>
                        {t}
                      </option>
                    ))}
                  </select>
                </label>
              )}
            </div>
          )}
          {loading && !data && page !== "db_monitoring" && (
            <div className="loading" role="status">
              Загрузка данных…
            </div>
          )}
          {page === "dashboards" && data && (
            <>
              <Diagnostics
                workers={data.workers}
                published={data.published_at}
                interval={config?.publish_seconds}
              />
              {route && (
                <div className="panel-heading">
                  <h2>
                    <span className="method">{method}</span>
                    {route}
                  </h2>
                  <button
                    onClick={() =>
                      navigate(
                        "/dashboards?" +
                          new URLSearchParams({ hours, service, group }),
                      )
                    }
                  >
                    Все ручки
                  </button>
                </div>
              )}
              <div className="cards">
                {[
                  {
                    label: "Всего запросов",
                    value: fmt(data.total),
                    foot: "За выбранный период",
                    icon: BarChart3,
                  },
                  {
                    label: "Запросов в минуту",
                    value: fmt(data.rpm),
                    foot: "Среднее за период",
                    icon: Activity,
                  },
                  {
                    label: "Ответы с ошибкой",
                    value:
                      (data.total
                        ? fmt((data.errors / data.total) * 100)
                        : "0") + "%",
                    foot: fmt(data.errors) + " ответов · коды 400–599",
                    icon: FileWarning,
                  },
                ].map((c) => (
                  <div className="card" key={c.label}>
                    <div className="card-label">
                      {c.label}
                      <c.icon size={15} />
                    </div>
                    <div className="metric">{c.value}</div>
                    <div className="card-foot">{c.foot}</div>
                  </div>
                ))}
                <div className="card">
                  <div className="card-label">
                    Время ответа
                    <Clock3 size={15} />
                  </div>
                  <dl className="latency-percentiles">
                    {(["p50", "p75", "p95"] as const).map((key) => (
                      <div
                        key={key}
                        title={`${key.slice(1)}% запросов завершились не дольше этого времени`}
                      >
                        <dt>{key}</dt>
                        <dd>{ms(data[key], data.latency_overflow)}</dd>
                      </div>
                    ))}
                  </dl>
                  <div className="card-foot">
                    p99 {ms(data.p99, data.latency_overflow)}
                  </div>
                </div>
              </div>
              <section className="panel">
                <StatusChart data={data} />
              </section>
              <section className="panel">
                <div className="panel-heading">
                  <div>
                    <h2>{route ? "Статистика ручки" : "Ручки приложения"}</h2>
                    <p>
                      Выберите ручку, чтобы увидеть все её коды на одном графике
                    </p>
                  </div>
                  <span className="badge">{data.routes.length} ручек</span>
                </div>
                <div className="table-scroll">
                  <table>
                    <thead>
                      <tr>
                        <th>Ручка</th>
                        <th>Сервис / группа</th>
                        <th>Запросы</th>
                        <th>В минуту</th>
                        <th>Ошибки</th>
                        <th>p50</th>
                        <th>p75</th>
                        <th>p95</th>
                        <th />
                      </tr>
                    </thead>
                    <tbody>
                      {data.routes.map((r) => (
                        <tr key={r.service + r.method + r.route}>
                          <td>
                            <a
                              href="#"
                              className="route-link"
                              onClick={(e) => {
                                e.preventDefault();
                                openRoute(r);
                              }}
                            >
                              <span className="method">{r.method}</span>
                              {r.route}
                            </a>
                          </td>
                          <td className="muted">
                            {r.service} / {r.group}
                          </td>
                          <td>{fmt(r.count)}</td>
                          <td>{fmt(r.rpm)}</td>
                          <td
                            style={{
                              color: r.errors ? "var(--amber)" : "var(--muted)",
                            }}
                          >
                            {fmt(r.errors)}
                          </td>
                          <td>{ms(r.p50, r.latency_overflow)}</td>
                          <td>{ms(r.p75, r.latency_overflow)}</td>
                          <td>{ms(r.p95, r.latency_overflow)}</td>
                          <td>
                            <a
                              href={`/incidents?${new URLSearchParams({ service: r.service, route: r.route, method: r.method })}`}
                              onClick={(e) => {
                                e.preventDefault();
                                navigate(e.currentTarget.getAttribute("href")!);
                              }}
                            >
                              Инциденты ↗
                            </a>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </section>
              <div className="footer-line">
                <span>
                  Срез опубликован {date(data.published_at)} · последние запросы{" "}
                  {date(data.newest_event)}
                </span>
                <span>Перцентили приблизительные · nginx request_time</span>
              </div>
            </>
          )}
          {page === "incidents" && !detail && !loading && (
            <section className="panel">
              <div className="panel-heading">
                <h2>
                  Инциденты <span className="muted">{incidentTotal}</span>
                </h2>
                <span className="small muted">Ручка + метод + код ответа</span>
              </div>
              {!incidents.length ? (
                <Empty title="Инцидентов не найдено">
                  Для выбранных фильтров пока нет опубликованных ответов с
                  кодами 400–599.
                </Empty>
              ) : (
                <>
                  <div className="table-scroll">
                    <table>
                      <thead>
                        <tr>
                          <th>Ручка</th>
                          <th>Код</th>
                          <th>События</th>
                          <th>Последнее событие</th>
                        </tr>
                      </thead>
                      <tbody>
                        {incidents.map((i) => (
                          <tr key={i.id}>
                            <td>
                              <a
                                className="route-link"
                                href={`/incidents/${i.id}`}
                                onClick={(e) => {
                                  e.preventDefault();
                                  navigate(`/incidents/${i.id}`);
                                }}
                              >
                                <span className="method">{i.method}</span>
                                {i.route}
                              </a>
                              <div
                                className="muted small"
                                style={{ marginTop: 6 }}
                              >
                                {i.service} / {i.route_group}
                              </div>
                            </td>
                            <td>
                              <Code code={i.status} />
                            </td>
                            <td>{fmt(i.payload.count)}</td>
                            <td className="muted">
                              {date(i.payload.last_seen)}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  <div className="footer-line">
                    <span>
                      {offset + 1}–{Math.min(offset + 100, incidentTotal)} из{" "}
                      {incidentTotal}
                    </span>
                    <span>
                      <button
                        disabled={!offset}
                        onClick={() =>
                          filter("offset", String(Math.max(0, offset - 100)))
                        }
                      >
                        Назад
                      </button>{" "}
                      <button
                        disabled={offset + 100 >= incidentTotal}
                        onClick={() => filter("offset", String(offset + 100))}
                      >
                        Далее
                      </button>
                    </span>
                  </div>
                </>
              )}
            </section>
          )}
          {page === "incidents" && incident && (
            <>
              <div className="panel-heading">
                <button onClick={() => navigate("/incidents")}>
                  <ArrowLeft size={14} /> К инцидентам
                </button>
              </div>
              <div className="detail-grid">
                <section className="panel">
                  <div className="panel-heading">
                    <h2>
                      <span className="method">{incident.method}</span>
                      {incident.route}
                    </h2>
                    <Code code={incident.status} />
                  </div>
                  <p
                    className="muted small"
                    style={{ lineHeight: 1.8, marginBottom: 20 }}
                  >
                    Nginx зафиксировал HTTP {incident.status}. Причина внутри
                    приложения доступна в его логах: используйте время и request
                    ID ниже.
                  </p>
                  {data && <StatusChart data={data} />}
                </section>
                <section className="panel">
                  <div className="panel-heading">
                    <h2>Информация</h2>
                  </div>
                  <div className="detail-list">
                    {[
                      ["Всего событий", fmt(incident.payload.count)],
                      ["Впервые", date(incident.payload.first_seen)],
                      ["Последнее", date(incident.payload.last_seen)],
                      ["Сервис", incident.service],
                      ["Группа", incident.route_group],
                    ].map(([l, v]) => (
                      <div key={l}>
                        <span className="muted">{l}</span>
                        <span>{v}</span>
                      </div>
                    ))}
                  </div>
                  <button
                    style={{ marginTop: 18 }}
                    onClick={() => openRoute(incident)}
                  >
                    Открыть Dashboard ↗
                  </button>
                </section>
              </div>
              <section className="panel">
                <div className="panel-heading">
                  <h2>Примеры запросов</h2>
                  <span className="small muted">До 5 последних request ID</span>
                </div>
                {incident.payload.samples.length ? (
                  <div className="table-scroll">
                    <table>
                      <thead>
                        <tr>
                          <th>Время</th>
                          <th>Request ID</th>
                        </tr>
                      </thead>
                      <tbody>
                        {incident.payload.samples.map((s, i) => (
                          <tr key={i}>
                            <td>{date(s.time)}</td>
                            <td>
                              <code>{s.request_id}</code>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                ) : (
                  <p className="muted small">
                    В логе нет request ID для этих запросов.
                  </p>
                )}
              </section>
            </>
          )}
          {page === "services_status" && !loading && (
            <>
              <Diagnostics workers={workers} />
              <div className="panel-heading">
                <h2>Health-проверки</h2>
                <span className="small muted">
                  Каждые 30 секунд · таймаут 3 секунды
                </span>
              </div>
              <div className="service-grid">
                {services.map((s) => (
                  <section className="card service-card" key={s.id}>
                    <div className="service-row">
                      <h2>
                        <Server size={17} /> {s.name}
                      </h2>
                      <Badge state={s.state} />
                    </div>
                    <p>
                      {!s.configured
                        ? "Health endpoint ещё не настроен."
                        : s.state === "unknown"
                          ? "Нет свежих результатов проверки."
                          : s.reason ||
                            "Health endpoint отвечает ожидаемым статусом."}
                    </p>
                    <div className="service-row small">
                      <span className="muted">Время ответа</span>
                      <span>{ms(s.latency_ms)}</span>
                    </div>
                    <div
                      className="timeline"
                      aria-label="Последние health-проверки"
                    >
                      {s.history.length
                        ? s.history.map((h, i) => (
                            <span
                              key={i}
                              className={h.state}
                              title={`${date(h.time)} · ${labels[h.state]}`}
                            />
                          ))
                        : Array.from({ length: 30 }, (_, i) => (
                            <span key={i} />
                          ))}
                    </div>
                    <div className="service-row small">
                      <span className="muted">Успешных проверок за 24ч</span>
                      <span>
                        {s.availability === null
                          ? "—"
                          : fmt(s.availability) + "%"}
                      </span>
                    </div>
                    <div className="small muted">
                      Покрытие периода: {fmt(s.coverage)}% · проверено{" "}
                      {date(s.checked_at)}
                    </div>
                  </section>
                ))}
              </div>
              <p className="small muted" style={{ marginTop: 20 }}>
                Отсутствие запросов не влияет на health. Unknown означает
                отсутствие свежей проверки, а не недоступность приложения.
              </p>
            </>
          )}
          {page === "db_monitoring" && (
            <section className="panel">
              <div className="empty">
                <Database size={42} />
                <span className="badge blue">Будет в будущем</span>
                <h2>Базы данных под наблюдением</h2>
                <p>
                  Здесь появятся показатели подключений, запросов и нагрузки на
                  базы данных приложения. Подключение и сбор метрик будут
                  доступны в следующем этапе развития.
                </p>
              </div>
            </section>
          )}
        </main>
      </div>
    </div>
  );
}
