# Local deployment (default)

```
git clone <repository>
cd zeta
install.bat        # or: python scripts/setup.py
start.bat          # or: python scripts/start.py
```

Open http://127.0.0.1:8765. The first-run wizard configures the LLM, voice and permissions.

## Run at logon (Windows)

```
schtasks /Create /SC ONLOGON /TN "Zeta" /TR "\"C:\path\to\zeta\start.bat\" --no-browser" /RL LIMITED
```

## Host-agent architecture (Docker API + local desktop control)

A container cannot control the host desktop, so `docker-compose.yml` runs Zeta in cloud mode
with host-only tools disabled (`available_in_cloud=False` on each such tool). If you want a
hosted API *and* desktop control, run a native Zeta on the laptop (`ZETA_MODE=local`) and reach
it through a Cloudflare Tunnel or Tailscale. The hosted instance never pretends it can control
your desktop; the local one does the actual work.
