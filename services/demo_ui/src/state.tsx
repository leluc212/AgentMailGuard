// Console-wide settings: tenant, model switch, theme, language (task 9.9). Persisted locally.
import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { ApiClient, type Capabilities, type ModelProfile } from "@/api/client";

export const TENANTS = [
  { id: "00000000-0000-0000-0000-000000000001", name: "Acme Corp" },
  { id: "00000000-0000-0000-0000-000000000002", name: "Beta Industries" },
  { id: "00000000-0000-0000-0000-000000000003", name: "Gamma Logistics" },
];

interface ConsoleState {
  tenant: string;
  setTenant: (id: string) => void;
  profile: ModelProfile;
  setProfile: (p: ModelProfile) => void;
  theme: "light" | "dark";
  toggleTheme: () => void;
  api: ApiClient;
  capabilities: Capabilities | null;
}

const Ctx = createContext<ConsoleState | null>(null);

function stored<T extends string>(key: string, fallback: T): T {
  return (localStorage.getItem(key) as T | null) ?? fallback;
}

export function ConsoleProvider({ children }: { children: ReactNode }) {
  const { i18n } = useTranslation();
  const [tenant, setTenant] = useState(() => stored("tenant", TENANTS[0].id));
  const [profile, setProfile] = useState<ModelProfile>(() => stored("profile", "fake"));
  const [theme, setTheme] = useState<"light" | "dark">(() => stored("theme", "dark"));
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const api = useMemo(() => new ApiClient(tenant), [tenant]);

  useEffect(() => {
    localStorage.setItem("tenant", tenant);
    localStorage.setItem("profile", profile);
    localStorage.setItem("theme", theme);
    localStorage.setItem("lang", i18n.language);
    document.documentElement.classList.toggle("dark", theme === "dark");
    document.documentElement.lang = i18n.language;
  }, [tenant, profile, theme, i18n.language]);

  useEffect(() => {
    let alive = true;
    api
      .capabilities()
      .then((c) => {
        if (!alive) return;
        setCapabilities(c);
        if (!c.live_model_available) setProfile("fake"); // the switch is off without a live model
      })
      .catch(() => alive && setCapabilities(null));
    return () => {
      alive = false;
    };
  }, [api]);

  const value: ConsoleState = {
    tenant,
    setTenant,
    profile,
    setProfile,
    theme,
    toggleTheme: () => setTheme((t) => (t === "dark" ? "light" : "dark")),
    api,
    capabilities,
  };
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useConsole(): ConsoleState {
  const value = useContext(Ctx);
  if (!value) throw new Error("useConsole outside ConsoleProvider");
  return value;
}
