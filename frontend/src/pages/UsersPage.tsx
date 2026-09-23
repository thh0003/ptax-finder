import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";
import { ApiError, apiFetch } from "../api/client";
import { useAuth, type Role } from "../auth/AuthContext";

type User = { id: string; email: string; role: Role; created_at: string };

export default function UsersPage() {
  const { state } = useAuth();
  const isAdmin = state.status === "authenticated" && state.me.role === "admin";

  const [users, setUsers] = useState<User[] | null>(null);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<Role>("reviewer");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const reload = useCallback(async () => {
    try {
      setUsers(await apiFetch<User[]>("/api/users"));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load users");
    }
  }, []);

  useEffect(() => {
    if (isAdmin) void reload();
  }, [isAdmin, reload]);

  if (!isAdmin) {
    return <p className="text-slate-700">Admins only</p>;
  }

  async function onInvite(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setNotice(null);
    setBusy(true);
    try {
      const created = await apiFetch<User>("/api/users", {
        method: "POST",
        body: JSON.stringify({ email, role }),
      });
      setNotice(`Invitation sent to ${created.email}; Cognito emails a temporary password.`);
      setEmail("");
      await reload();
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setError("That email already has an account");
      } else {
        setError(err instanceof Error ? err.message : "Could not invite user");
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-6">
      <h1 className="text-xl font-semibold">Users</h1>

      <form
        onSubmit={onInvite}
        className="flex flex-wrap items-end gap-3 rounded-lg bg-white p-4 shadow"
        aria-label="Invite a user"
      >
        <label className="text-sm">
          <span className="block text-slate-700">Email</span>
          <input
            type="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="mt-1 w-72 rounded border border-slate-300 px-3 py-2"
          />
        </label>
        <label className="text-sm">
          <span className="block text-slate-700">Role</span>
          <select
            value={role}
            onChange={(e) => setRole(e.target.value as Role)}
            className="mt-1 rounded border border-slate-300 px-3 py-2"
          >
            <option value="reviewer">reviewer</option>
            <option value="admin">admin</option>
          </select>
        </label>
        <button
          type="submit"
          disabled={busy}
          className="rounded bg-blue-700 px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
        >
          Invite
        </button>
      </form>

      {notice && (
        <p role="status" className="rounded border border-green-200 bg-green-50 p-3 text-sm text-green-800">
          {notice}
        </p>
      )}
      {error && (
        <p role="alert" className="rounded border border-red-200 bg-red-50 p-3 text-sm text-red-800">
          {error}
        </p>
      )}

      {users === null ? (
        <p className="text-slate-600">Loading...</p>
      ) : (
        <table className="w-full rounded-lg bg-white text-sm shadow">
          <thead>
            <tr className="text-left text-slate-600">
              <th className="px-4 py-2 font-medium">Email</th>
              <th className="px-4 py-2 font-medium">Role</th>
              <th className="px-4 py-2 font-medium">Added</th>
            </tr>
          </thead>
          <tbody>
            {users.map((u) => (
              <tr key={u.id} className="border-t border-slate-200" data-testid="user-row">
                <td className="px-4 py-2">{u.email}</td>
                <td className="px-4 py-2">{u.role}</td>
                <td className="px-4 py-2 text-slate-500">
                  {new Date(u.created_at).toLocaleDateString()}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
