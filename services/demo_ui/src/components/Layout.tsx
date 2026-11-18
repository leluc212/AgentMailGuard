import {
  Activity,
  BarChart3,
  BookOpen,
  Clock,
  FlaskConical,
  Inbox,
  Moon,
  ShieldCheck,
  Sun,
  Workflow,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import { NavLink, Outlet } from "react-router-dom";
import { TENANTS, useConsole } from "@/state";
import { Badge, Button, cn } from "./ui";

const NAV = [
  { to: "/", key: "pipeline", icon: Workflow },
  { to: "/scenarios", key: "scenarios", icon: FlaskConical },
  { to: "/guard", key: "guard", icon: ShieldCheck },
  { to: "/review", key: "review", icon: Inbox },
  { to: "/timeline", key: "timeline", icon: Clock },
  { to: "/knowledge", key: "knowledge", icon: BookOpen },
  { to: "/observability", key: "observability", icon: Activity },
  { to: "/benchmark", key: "benchmark", icon: BarChart3 },
];

export function Layout() {
  const { t, i18n } = useTranslation();
  const { tenant, setTenant, profile, setProfile, theme, toggleTheme, capabilities } = useConsole();
  const liveOk = capabilities?.live_model_available ?? false;

  return (
    <div className="grid min-h-screen grid-cols-[220px_1fr]">
      <aside className="border-r bg-card p-4">
        <div className="mb-6">
          <div className="text-lg font-bold">{t("app.title")}</div>
          <div className="text-xs text-muted-foreground">{t("app.subtitle")}</div>
        </div>
        <nav className="flex flex-col gap-1">
          {NAV.map(({ to, key, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              end={to === "/"}
              className={({ isActive }) =>
                cn(
                  "flex min-h-9 items-center gap-2 rounded-md px-2 text-sm",
                  isActive ? "bg-primary/15 font-medium text-primary" : "hover:bg-muted",
                )
              }
            >
              <Icon size={16} aria-hidden /> {t(`nav.${key}`)}
            </NavLink>
          ))}
        </nav>
      </aside>

      <div className="flex min-w-0 flex-col">
        <header className="flex flex-wrap items-center gap-4 border-b bg-card px-6 py-3">
          <label className="flex items-center gap-2 text-sm">
            {t("top.tenant")}
            <select
              className="min-h-9 rounded-md border bg-background px-2"
              value={tenant}
              onChange={(e) => setTenant(e.target.value)}
            >
              {TENANTS.map((x) => (
                <option key={x.id} value={x.id}>
                  {x.name}
                </option>
              ))}
            </select>
          </label>

          <div className="flex items-center gap-2 text-sm" role="radiogroup" aria-label={t("top.model")}>
            {t("top.model")}
            {(["fake", "live"] as const).map((p) => (
              <Button
                key={p}
                size="sm"
                role="radio"
                aria-checked={profile === p}
                variant={profile === p ? "default" : "outline"}
                disabled={p === "live" && !liveOk}
                title={p === "live" && !liveOk ? t("top.liveOff") : undefined}
                onClick={() => setProfile(p)}
              >
                {t(`top.${p}`)}
                {p === "live" && capabilities?.live_model ? ` · ${capabilities.live_model}` : ""}
              </Button>
            ))}
          </div>

          <div className="ml-auto flex items-center gap-2">
            <Button
              size="sm"
              variant="outline"
              aria-label={t("top.language")}
              onClick={() => void i18n.changeLanguage(i18n.language === "vi" ? "en" : "vi")}
            >
              {i18n.language === "vi" ? "EN" : "VI"}
            </Button>
            <Button size="sm" variant="outline" aria-label={t("top.theme")} onClick={toggleTheme}>
              {theme === "dark" ? <Sun size={16} /> : <Moon size={16} />}
            </Button>
          </div>
        </header>

        <div aria-live="polite" className="flex flex-wrap gap-2 px-6 pt-3">
          {capabilities === null && <Badge tone="danger">{t("top.noDemo")}</Badge>}
          {capabilities && !capabilities.demo_mailbox_id && <Badge tone="danger">{t("top.noDemo")}</Badge>}
          {capabilities && !capabilities.guard_available && <Badge tone="warning">{t("top.guardOff")}</Badge>}
          {capabilities?.guard_available && !capabilities.l1_classifier_loaded && (
            <Badge tone="warning">{t("top.l1Missing")}</Badge>
          )}
        </div>

        <main className="min-w-0 flex-1 p-6">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
