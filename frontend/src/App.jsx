import { Navigate, Route, Routes } from "react-router-dom";
import Layout from "./components/Layout";
import { useAuth } from "./auth/AuthContext";
import Login from "./pages/Login";
import TicketList from "./pages/TicketList";
import TicketNew from "./pages/TicketNew";
import TicketDetail from "./pages/TicketDetail";
import AdminUsers from "./pages/AdminUsers";
import Notifications from "./pages/Notifications";
import Profile from "./pages/Profile";

function Protected({ children, adminOnly = false }) {
  const { user, loading } = useAuth();
  if (loading) return <div className="centered">Loading…</div>;
  if (!user) return <Navigate to="/login" replace />;
  if (adminOnly && user.role !== "admin") return <Navigate to="/tickets" replace />;
  return children;
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route
        element={
          <Protected>
            <Layout />
          </Protected>
        }
      >
        <Route index element={<Navigate to="/tickets" replace />} />
        <Route path="/tickets" element={<TicketList />} />
        <Route path="/tickets/new" element={<TicketNew />} />
        <Route path="/tickets/:id" element={<TicketDetail />} />
        <Route path="/notifications" element={<Notifications />} />
        <Route path="/profile" element={<Profile />} />
        <Route
          path="/admin/users"
          element={
            <Protected adminOnly>
              <AdminUsers />
            </Protected>
          }
        />
      </Route>
      <Route path="*" element={<Navigate to="/tickets" replace />} />
    </Routes>
  );
}
