import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { AuthProvider } from "./auth/AuthContext";
import Layout from "./components/Layout";
import ImageryPage from "./pages/ImageryPage";
import LoginPage from "./pages/LoginPage";
import ParcelsPage from "./pages/ParcelsPage";
import RunsPage from "./pages/RunsPage";
import UsersPage from "./pages/UsersPage";

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route element={<Layout />}>
            <Route path="/parcels" element={<ParcelsPage />} />
            <Route path="/imagery" element={<ImageryPage />} />
            <Route path="/runs" element={<RunsPage />} />
            <Route path="/users" element={<UsersPage />} />
            <Route path="/" element={<Navigate to="/parcels" replace />} />
          </Route>
          <Route path="*" element={<Navigate to="/parcels" replace />} />
        </Routes>
      </AuthProvider>
    </BrowserRouter>
  );
}
