import { NavLink, Navigate, Outlet } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";

const linkClass = ({ isActive }: { isActive: boolean }) =>
  `px-3 py-2 rounded text-sm font-medium ${
    isActive ? "bg-blue-100 text-blue-900" : "text-slate-700 hover:bg-slate-100"
  }`;

export default function Layout() {
  const { state, signOut } = useAuth();

  if (state.status === "loading") {
    return <p className="p-8 text-slate-600">Loading...</p>;
  }
  if (state.status === "anonymous") {
    return <Navigate to="/login" replace />;
  }
  const { me } = state;

  return (
    <div className="min-h-screen bg-slate-50 text-slate-900">
      <header className="bg-white border-b border-slate-200">
        <div className="max-w-6xl mx-auto px-4 min-h-14 py-2 flex flex-wrap items-center gap-x-6 gap-y-2">
          <span className="font-semibold">ptax-finder</span>
          <span className="text-sm text-slate-600" data-testid="tenant-name">
            {me.tenant.name}
          </span>
          <nav className="flex gap-1 ml-4">
            <NavLink to="/parcels" className={linkClass}>
              Parcels
            </NavLink>
            <NavLink to="/imagery" className={linkClass}>
              Imagery
            </NavLink>
            <NavLink to="/runs" className={linkClass}>
              Runs
            </NavLink>
            {me.role === "admin" && (
              <NavLink to="/users" className={linkClass}>
                Users
              </NavLink>
            )}
          </nav>
          <div className="ml-auto flex items-center gap-3 text-sm">
            <span className="text-slate-600" data-testid="user-email">
              {me.email}
            </span>
            <span className="rounded bg-slate-200 px-2 py-0.5 text-xs uppercase">{me.role}</span>
            <button
              type="button"
              onClick={signOut}
              className="rounded border border-slate-300 px-3 py-1 hover:bg-slate-100"
            >
              Sign out
            </button>
          </div>
        </div>
      </header>
      <main className="max-w-6xl mx-auto px-4 py-6">
        <Outlet />
      </main>
    </div>
  );
}
