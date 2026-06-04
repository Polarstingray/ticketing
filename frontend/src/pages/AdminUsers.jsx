import { useEffect, useState } from "react";
import { api } from "../api";
import { useAuth } from "../auth/AuthContext";
import { formatDate } from "../constants";
import styles from "../styles/AdminUsers.module.css";

const emptyForm = () => ({
  username: "",
  display_name: "",
  email: "",
  password: "",
  role: "member",
});

export default function AdminUsers() {
  const { user: me } = useAuth();
  const [users, setUsers] = useState([]);
  const [form, setForm] = useState(emptyForm());
  const [showForm, setShowForm] = useState(false);
  const [newKey, setNewKey] = useState(null);
  const [error, setError] = useState("");

  async function load() {
    try {
      setUsers(await api.listUsers());
    } catch (e) {
      setError(e.message);
    }
  }

  useEffect(() => {
    load();
  }, []);

  async function createUser(e) {
    e.preventDefault();
    setError("");
    try {
      const created = await api.createUser(form);
      setForm(emptyForm());
      setShowForm(false);
      setNewKey({ username: created.username, api_key: created.api_key });
      load();
    } catch (e) {
      setError(e.message);
    }
  }

  async function changeRole(u, role) {
    setError("");
    try {
      await api.updateUser(u.id, { role });
      load();
    } catch (e) {
      setError(e.message);
    }
  }

  async function removeUser(u) {
    if (!window.confirm(`Delete user "${u.username}"?`)) return;
    setError("");
    try {
      await api.deleteUser(u.id);
      load();
    } catch (e) {
      setError(e.message);
    }
  }

  return (
    <div>
      <div className={styles.head}>
        <h1>Users</h1>
        <button className="primary" onClick={() => setShowForm((s) => !s)}>
          {showForm ? "Cancel" : "New user"}
        </button>
      </div>

      {error && <div className="error">{error}</div>}

      {newKey && (
        <div className={`card ${styles.keyNotice}`}>
          <strong>User “{newKey.username}” created.</strong> Their API key (shown once here —
          they can also view it on their profile):
          <code className={styles.key}>{newKey.api_key}</code>
          <button onClick={() => setNewKey(null)}>Dismiss</button>
        </div>
      )}

      {showForm && (
        <form onSubmit={createUser} className={`card ${styles.form}`}>
          <div className={styles.formGrid}>
            <div className="field">
              <label>Username</label>
              <input
                value={form.username}
                onChange={(e) => setForm({ ...form, username: e.target.value })}
                required
              />
            </div>
            <div className="field">
              <label>Display name</label>
              <input
                value={form.display_name}
                onChange={(e) => setForm({ ...form, display_name: e.target.value })}
                required
              />
            </div>
            <div className="field">
              <label>Email</label>
              <input
                type="email"
                value={form.email}
                onChange={(e) => setForm({ ...form, email: e.target.value })}
                required
              />
            </div>
            <div className="field">
              <label>Password (min 6 chars)</label>
              <input
                type="password"
                value={form.password}
                onChange={(e) => setForm({ ...form, password: e.target.value })}
                required
                minLength={6}
              />
            </div>
            <div className="field">
              <label>Role</label>
              <select
                value={form.role}
                onChange={(e) => setForm({ ...form, role: e.target.value })}
              >
                <option value="member">member</option>
                <option value="admin">admin</option>
              </select>
            </div>
          </div>
          <div style={{ textAlign: "right" }}>
            <button className="primary" type="submit">
              Create user
            </button>
          </div>
        </form>
      )}

      <table className={styles.table}>
        <thead>
          <tr>
            <th>Username</th>
            <th>Display name</th>
            <th>Email</th>
            <th>Role</th>
            <th>Created</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {users.map((u) => (
            <tr key={u.id}>
              <td>{u.username}</td>
              <td>{u.display_name}</td>
              <td className={styles.dim}>{u.email}</td>
              <td>
                <select
                  value={u.role}
                  disabled={u.id === me.id}
                  onChange={(e) => changeRole(u, e.target.value)}
                  className={styles.roleSelect}
                >
                  <option value="member">member</option>
                  <option value="admin">admin</option>
                </select>
              </td>
              <td className={styles.dim}>{formatDate(u.created_at)}</td>
              <td style={{ textAlign: "right" }}>
                {u.id !== me.id && (
                  <button className="danger" onClick={() => removeUser(u)}>
                    Delete
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
