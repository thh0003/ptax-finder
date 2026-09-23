import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { apiFetch, getTokens, setTokens, setUnauthorizedHandler } from "../api/client";
import { getConfig } from "../config";
import { makeClient, respondNewPassword, signIn, type SignInResult } from "./cognito";

export type Role = "admin" | "reviewer";

export type Me = {
  id: string;
  email: string;
  role: Role;
  tenant: { id: string; name: string; state: string; fips: string };
};

type AuthState =
  | { status: "loading" }
  | { status: "anonymous" }
  | { status: "authenticated"; me: Me };

type AuthContextValue = {
  state: AuthState;
  signIn: (email: string, password: string) => Promise<SignInResult>;
  completeNewPassword: (email: string, newPassword: string, session: string) => Promise<void>;
  signOut: () => void;
};

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>({ status: "loading" });

  const loadMe = useCallback(async () => {
    try {
      const me = await apiFetch<Me>("/api/me");
      setState({ status: "authenticated", me });
    } catch {
      setTokens(null);
      setState({ status: "anonymous" });
    }
  }, []);

  useEffect(() => {
    setUnauthorizedHandler(() => setState({ status: "anonymous" }));
    if (getTokens()) void loadMe();
    else setState({ status: "anonymous" });
  }, [loadMe]);

  const value = useMemo<AuthContextValue>(
    () => ({
      state,
      signIn: async (email, password) => {
        const config = await getConfig();
        const result = await signIn(makeClient(config), config.cognito_client_id, email, password);
        if (result.kind === "tokens") {
          setTokens(result.tokens);
          await loadMe();
        }
        return result;
      },
      completeNewPassword: async (email, newPassword, session) => {
        const config = await getConfig();
        setTokens(
          await respondNewPassword(
            makeClient(config),
            config.cognito_client_id,
            email,
            newPassword,
            session,
          ),
        );
        await loadMe();
      },
      signOut: () => {
        setTokens(null);
        setState({ status: "anonymous" });
      },
    }),
    [state, loadMe],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside AuthProvider");
  return ctx;
}
