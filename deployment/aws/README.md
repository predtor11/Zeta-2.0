# AWS deployment (optional)

Zeta needs **zero** AWS services by default. Use this only for a cloud-hosted instance
(browser, internet, email, messaging and memory tools work there; host-computer tools do not).

## Simple: one EC2 instance + Docker Compose

```
EC2 (t3.small, Ubuntu)  ->  docker compose  ->  backend (FastAPI) + frontend (nginx) [+ ollama]
```

1. Launch an Ubuntu EC2 instance. Security group: allow 22 (your IP), 8080 and 8765 (your IP).
2. Paste `ec2-user-data.sh` as user data (edit `REPO_URL` and the LLM settings first),
   or SSH in and run the same commands manually.
3. Open `http://<public-ip>:8080`. The API token is in `/opt/zeta/.env`; set it in the browser
   with `localStorage.setItem("zeta_api_token", "<token>")` (a settings field for this is on the roadmap).
4. For HTTPS put CloudFront or an ALB in front, or run Caddy on the instance.

Running Ollama on EC2 needs a GPU instance (g5.xlarge or larger) or a big CPU box. A hosted
OpenAI-compatible API (Groq, OpenRouter, Together) is far cheaper for a single user.

## Optional pieces

| Service    | Use                                                    | Notes                                                        |
|------------|--------------------------------------------------------|--------------------------------------------------------------|
| RDS        | PostgreSQL instead of SQLite                           | `DATABASE_URL=postgresql+asyncpg://...` and `pip install asyncpg` |
| S3         | Backups of `/data` (db, file index) or a shared workspace | `AWS_S3_BUCKET`; run `aws s3 sync` from a scheduled request |
| CloudFront | HTTPS and caching for the frontend                     | origin = instance:8080                                       |
| Route53    | DNS                                                    |                                                              |
| Lambda     | EventBridge cron that calls `POST /api/chat`           | replaces LocalScheduler for serverless schedules             |

## Free and low-cost alternatives

- **Oracle Cloud Always-Free** (4 OCPU ARM VM): same docker-compose, enough for Zeta plus small Ollama models.
- **Fly.io / Railway / Render**: build from `deployment/docker/Dockerfile.backend`; attach a volume at `/data`.
- **Hetzner / DigitalOcean**: cheapest reliable VPS option; same steps as EC2.
- **Cloudflare Tunnel or Tailscale**: expose your *local* Zeta securely from your laptop instead of hosting it.
  Keep `HOST=127.0.0.1`, set `API_TOKEN`, run the tunnel. This keeps full computer control.
