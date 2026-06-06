#!/usr/bin/env bash
# ============================================================
# VPS Framework — fagulha (curl | bash) do instalador visual
# ============================================================
# Uso na VM nova (Ubuntu 22.04 zerado):
#   curl -fsSL https://raw.githubusercontent.com/SEU-USER/vps-framework/main/bootstrap.sh | bash
#
# (repo de CODIGO via VPS_REPO; se for privado, exporte VPS_GH_TOKEN)
# ============================================================
set -e
REPO_INSTALADOR="${VPS_FRAMEWORK_REPO:-https://github.com/diogobsbastos/vps-framework.git}"
export VPS_REPO="${VPS_REPO:-https://github.com/diogobsbastos/vps-escola-parque-admin.git}"
export VPS_KEY="${VPS_KEY:-$(openssl rand -hex 6 2>/dev/null || echo vpskey$$)}"

echo "==> Instalando pré-requisitos (python3, git, curl)..."
sudo apt-get update -y -qq
sudo apt-get install -y -qq python3 python3-venv git curl openssl ca-certificates

echo "==> Baixando o instalador..."
rm -rf /tmp/vps-framework
git clone --depth 1 "$REPO_INSTALADOR" /tmp/vps-framework

IP=$(curl -fsSL --max-time 5 ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')
echo
echo "=================================================================="
echo "  INSTALADOR NO AR — abra no navegador:"
echo
echo "     http://$IP:9000/?key=$VPS_KEY"
echo
echo "  (se a porta 9000 estiver fechada na nuvem, libere TCP 9000"
echo "   OU use túnel SSH:  ssh -L 9000:localhost:9000 user@$IP )"
echo "=================================================================="
echo
cd /tmp/vps-framework/instalador
exec sudo VPS_KEY="$VPS_KEY" VPS_REPO="$VPS_REPO" python3 server.py
