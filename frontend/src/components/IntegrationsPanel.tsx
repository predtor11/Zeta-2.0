import { useEffect, useState } from "react";
import { api } from "../services/api";
import type { OAuthState } from "../types";

/** Email OAuth connection controls (Gmail / Outlook). Used inside the setup wizard. */
export default function IntegrationsPanel() {
  const [st, setSt] = useState<{ gmail: OAuthState; outlook: OAuthState; email_provider: string; email_auth: string } | null>(null);
  const [busy, setBusy] = useState<string>("");
  const [error, setError] = useState("");

  const load = () => api.oauthStatus().then(setSt).catch((e) => setError((e as Error).message));
  useEffect(() => {
    load();
    const iv = setInterval(load, 5000);
    return () => clearInterval(iv);
  }, []);

  const connect = async (p: "gmail" | "outlook") => {
    setBusy(p);
    setError("");
    try {
      const { url } = await api.oauthStart(p);
      window.open(url, "_blank", "noopener");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy("");
    }
  };

  const disconnect = async (p: "gmail" | "outlook") => {
    await api.oauthDisconnect(p).catch(() => undefined);
    load();
  };

  const row = (p: "gmail" | "outlook", label: string) => {
    const s = st?.[p];
    return (
      <div className="perm-row" key={p}>
        <div>
          {label}
          <small>
            {!s ? "…" : s.connected ? `Connected as ${s.email || "(address unknown)"}` : s.configured ? "Not connected" : `Set ${s.missing.join(", ")} in .env first`}
          </small>
        </div>
        {s?.connected ? (
          <button className="btn sm danger" onClick={() => disconnect(p)}>Disconnect</button>
        ) : (
          <button className="btn sm primary" disabled={!s?.configured || busy === p} onClick={() => connect(p)}>
            {busy === p ? "Opening…" : "Connect"}
          </button>
        )}
      </div>
    );
  };

  return (
    <div>
      <div className="field">
        <label>Email (OAuth sign-in)</label>
        <div className="help" style={{ marginBottom: 8 }}>
          Current: provider <b>{st?.email_provider || "…"}</b>, auth <b>{st?.email_auth || "…"}</b>. Set <code>EMAIL_PROVIDER=gmail|outlook</code> and{" "}
          <code>EMAIL_AUTH=oauth</code> in .env, create the free OAuth client (see docs/INTEGRATIONS.md), then click Connect. A browser tab
          opens; after you approve, it redirects to <code>{st?.gmail.redirect_uri || "http://localhost:8765/api/email/oauth/callback"}</code>.
        </div>
        {row("gmail", "Gmail")}
        {row("outlook", "Outlook / Microsoft 365")}
        {error && <div style={{ color: "var(--err)", marginTop: 8 }}>{error}</div>}
      </div>
      <div className="field">
        <label>WhatsApp</label>
        <div className="help">
          With <code>WHATSAPP_PROVIDER=web</code> the first message opens WhatsApp Web in Zeta's browser window: scan the QR code with your phone once.
          Contacts come from <code>backend/config/contacts.json</code>.
        </div>
      </div>
    </div>
  );
}
