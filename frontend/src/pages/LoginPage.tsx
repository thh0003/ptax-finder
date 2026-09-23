import { useState } from "react";
import type { FormEvent } from "react";
import { Navigate, useNavigate } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";

export default function LoginPage() {
  const { state, signIn, completeNewPassword } = useAuth();
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [challenge, setChallenge] = useState<{ session: string; email: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (state.status === "authenticated") return <Navigate to="/parcels" replace />;

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      if (challenge) {
        await completeNewPassword(challenge.email, newPassword, challenge.session);
      } else {
        const result = await signIn(email, password);
        if (result.kind === "new_password_required") {
          setChallenge({ session: result.session, email: result.email });
          return;
        }
      }
      navigate("/parcels", { replace: true });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Sign-in failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="min-h-screen flex items-center justify-center bg-slate-100">
      <form
        onSubmit={onSubmit}
        className="w-full max-w-sm bg-white shadow rounded-lg p-8 space-y-4"
        aria-labelledby="login-title"
      >
        <h1 id="login-title" className="text-2xl font-semibold text-slate-900">
          {challenge ? "Set a new password" : "Sign in to ptax-finder"}
        </h1>
        {challenge ? (
          <>
            <p className="text-sm text-slate-600">
              Your temporary password was accepted. Choose a permanent password for{" "}
              <span className="font-medium">{challenge.email}</span>.
            </p>
            <label className="block text-sm">
              <span className="text-slate-700">New password</span>
              <input
                type="password"
                autoComplete="new-password"
                required
                value={newPassword}
                onChange={(e) => setNewPassword(e.target.value)}
                className="mt-1 w-full rounded border border-slate-300 px-3 py-2"
              />
            </label>
          </>
        ) : (
          <>
            <label className="block text-sm">
              <span className="text-slate-700">Email</span>
              <input
                type="email"
                autoComplete="username"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                className="mt-1 w-full rounded border border-slate-300 px-3 py-2"
              />
            </label>
            <label className="block text-sm">
              <span className="text-slate-700">Password</span>
              <input
                type="password"
                autoComplete="current-password"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="mt-1 w-full rounded border border-slate-300 px-3 py-2"
              />
            </label>
          </>
        )}
        {error && (
          <p role="alert" className="text-sm text-red-700">
            {error}
          </p>
        )}
        <button
          type="submit"
          disabled={busy}
          className="w-full rounded bg-blue-700 text-white py-2 font-medium disabled:opacity-50"
        >
          {busy ? "Working..." : challenge ? "Set password" : "Sign in"}
        </button>
      </form>
    </main>
  );
}
