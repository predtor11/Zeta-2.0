#!/bin/bash
# EC2 user-data for a single-instance Zeta deployment (Ubuntu 22.04/24.04, t3.small or larger).
# Installs Docker + Compose, clones the repo, and starts the stack.  Edit REPO_URL and the LLM settings.
set -euxo pipefail
REPO_URL="https://github.com/YOUR_USER/zeta.git"
apt-get update && apt-get install -y ca-certificates curl git
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
ARCH=$(dpkg --print-architecture)
CODENAME=$(. /etc/os-release && echo "$VERSION_CODENAME")
echo "deb [arch=$ARCH signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $CODENAME stable" > /etc/apt/sources.list.d/docker.list
apt-get update && apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
cd /opt && git clone "$REPO_URL" zeta && cd zeta
mkdir -p workspace
{
  echo "API_TOKEN=$(openssl rand -hex 24)"
  echo "LLM_PROVIDER=openai_compatible"
  echo "LLM_BASE_URL=https://api.groq.com/openai/v1"
  echo "LLM_MODEL=llama-3.3-70b-versatile"
  echo "LLM_API_KEY=REPLACE_ME"
} > .env
docker compose up -d --build
echo "Zeta is starting. UI on port 8080, API on 8765. API token is in /opt/zeta/.env"
