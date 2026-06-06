#!/usr/bin/env python3
# ============================================================
# VPS Framework — Instalador visual (Pacote 1)
# ============================================================
# Servidor web SEM dependencias (so stdlib) que serve uma tela
# estilo "instalador do Windows" com barra de progresso REAL.
# A barra anda conforme cada etapa termina de verdade (SSE).
#
# Subir:  sudo python3 server.py            (instalar)
#         sudo python3 server.py --uninstall (remover)
# Abre em http://0.0.0.0:9000/?key=<TOKEN>  (token sai no console)
#
# Seguranca: todo request exige ?key=<TOKEN> (gerado no boot).
# Roda como root (instala servicos), mas cria tudo como User=ubuntu.
# ============================================================
import json
import os
import queue
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

PORTA = 9000
ALVO_USER = os.environ.get("VPS_USER", "ubuntu")
HOME = f"/home/{ALVO_USER}"
REPO = os.environ.get("VPS_REPO", "https://github.com/diogobsbastos/vps-escola-parque-admin.git")
TOKEN = os.environ.get("VPS_KEY") or secrets.token_urlsafe(9)
DRY = os.environ.get("VPS_DRY") == "1"   # teste: nao executa, so simula

ESTADO = {
    "fase": "idle",        # idle | rodando | ok | erro
    "modo": "instalar",
    "passos": [],          # [{id,label,icon,status,pct}]
    "log": [],
    "pct": 0,
}
FILA = queue.Queue()       # eventos p/ o SSE
LOCK = threading.Lock()
CONFIG = {"token": "", "repo": REPO}   # preenchido pela tela (/start)

# Ambiente detectado na 1a etapa (o "ping"): tudo se adapta a partir daqui
DET = {"arch": "", "deb_arch": "", "codinome": "", "python": "", "pg_arch": ""}


# ---------- catalogo de componentes (id, label, icone, obrigatorio) ----------
COMPONENTES = [
    ("detectar",  "Detectar ambiente (arch/OS/python)", "ti-radar", True),
    ("sistema",   "Preparar o sistema (apt + deps)", "ti-refresh", True),
    ("nginx",     "Nginx (porteiro/rotas)",          "ti-world", True),
    ("postgres",  "PostgreSQL 17 + pgvector",        "ti-database", True),
    ("postgrest", "PostgREST (API do banco)",        "ti-api", False),
    ("painel",    "Painel VPS Admin",                "ti-layout-dashboard", True),
    ("provisionador", "Provisionador (Novo App)",    "ti-rocket", True),
    ("webhook",   "Webhook (push -> deploy)",        "ti-git-merge", False),
    ("mcp",       "VPS-MCP (ponte do Claude)",       "ti-plug", False),
    ("gateway",   "LLM Gateway",                     "ti-key", False),
    ("sentinela", "Sentinela + timers",              "ti-bell", False),
    ("ntfy",      "ntfy (push proprio)",             "ti-send", False),
    ("evolution", "Evolution API (WhatsApp)",        "ti-brand-whatsapp", False),
    ("ollama",    "Ollama (LLM local) — pesado",     "ti-cpu", False),
]
PADRAO_MARCADOS = {c[0] for c in COMPONENTES if c[0] != "ollama"}


def emit(ev: dict):
    with LOCK:
        if ev.get("tipo") == "log":
            ESTADO["log"].append(ev["msg"])
            ESTADO["log"] = ESTADO["log"][-400:]
        if "pct" in ev:
            ESTADO["pct"] = ev["pct"]
        if ev.get("tipo") == "passo":
            for p in ESTADO["passos"]:
                if p["id"] == ev["id"]:
                    p["status"] = ev["status"]
        if ev.get("tipo") == "fim":
            ESTADO["fase"] = ev["fase"]
    FILA.put(ev)


def sh(cmd: str, timeout: int = 1200):
    """Roda um comando shell, transmitindo cada linha pro log."""
    emit({"tipo": "log", "msg": f"$ {cmd}"})
    if DRY:
        time.sleep(0.25)
        emit({"tipo": "log", "msg": "(dry-run: nao executado)"})
        return 0
    p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, bufsize=1,
                         executable="/bin/bash")
    for linha in iter(p.stdout.readline, ""):
        emit({"tipo": "log", "msg": linha.rstrip()})
    p.wait(timeout=timeout)
    return p.returncode


def como_user(cmd: str) -> str:
    """Executa um comando como o usuario alvo (nao root)."""
    safe = cmd.replace("'", "'\\''")
    return f"sudo -u {ALVO_USER} bash -lc '{safe}'"


print(f"[instalador] token={TOKEN}  porta={PORTA}  dry={DRY}", flush=True)


# ============================================================
# ETAPAS DE INSTALACAO (cada uma idempotente)
# ============================================================
CLONE = f"{HOME}/.vps-framework-src"
INSTALADOR_DIR = os.path.dirname(os.path.abspath(__file__))
LOCKS_DIR = os.path.join(INSTALADOR_DIR, "..", "locks")

APT_DEPS = (
    "build-essential python3-venv python3-dev python3-pip libpq-dev "
    "curl gnupg ca-certificates git nginx certbot python3-certbot-nginx "
    "rclone ffmpeg fonts-dejavu-core fonts-noto-color-emoji iptables-persistent"
)


def p_detectar():
    """O 'ping' do ambiente — descobre arch/OS/python e adapta o resto."""
    import platform
    DET["arch"] = (subprocess.run("uname -m", shell=True, capture_output=True, text=True).stdout.strip() or platform.machine())
    DET["deb_arch"] = subprocess.run("dpkg --print-architecture", shell=True, capture_output=True, text=True).stdout.strip() or "amd64"
    DET["codinome"] = subprocess.run("lsb_release -cs", shell=True, capture_output=True, text=True).stdout.strip() or "jammy"
    DET["python"] = subprocess.run("python3 --version", shell=True, capture_output=True, text=True).stdout.strip()
    # nome de arquitetura que o PostgREST usa nos releases
    DET["pg_arch"] = "linux-static-x64" if DET["arch"] in ("x86_64", "amd64") else "ubuntu-aarch64"
    emit({"tipo": "log", "msg": f"=> Arch: {DET['arch']} (deb {DET['deb_arch']})"})
    emit({"tipo": "log", "msg": f"=> Ubuntu: {DET['codinome']} | {DET['python']}"})
    emit({"tipo": "log", "msg": f"=> Binarios serao baixados p/ {DET['arch']}; libs Python via wheel automatico."})


def p_sistema():
    sh("export DEBIAN_FRONTEND=noninteractive; apt-get update -y")
    sh(f"export DEBIAN_FRONTEND=noninteractive; apt-get install -y {APT_DEPS}")
    repo = CONFIG.get("repo") or REPO
    tok = (CONFIG.get("token") or "").strip()
    url = repo
    if tok and repo.startswith("https://github.com/"):
        url = repo.replace("https://", f"https://x-access-token:{tok}@")
        sh(como_user(f"printf '%s' '{tok}' > {HOME}/.github_token && chmod 600 {HOME}/.github_token"))
    sh(como_user(f"rm -rf {CLONE} && git clone --depth 1 {url} {CLONE}"))


def p_nginx():
    conf = f"""server {{
    listen 80 default_server;
    server_name _;
    location = / {{ return 302 /admin/; }}
    location /admin/ {{
        proxy_pass http://127.0.0.1:8500/admin/;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 86400;
    }}
}}
"""
    sh(f"cat > /etc/nginx/sites-available/apps <<'NG'\n{conf}NG")
    sh("ln -sf /etc/nginx/sites-available/apps /etc/nginx/sites-enabled/apps")
    sh("rm -f /etc/nginx/sites-enabled/default")
    sh("nginx -t && systemctl enable --now nginx && systemctl reload nginx")


def p_postgres():
    sh("install -d /usr/share/postgresql-common/pgdg")
    sh("curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc "
       "-o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc")
    sh('echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] '
       'https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" '
       '> /etc/apt/sources.list.d/pgdg.list')
    sh("export DEBIAN_FRONTEND=noninteractive; apt-get update -y")
    sh("export DEBIAN_FRONTEND=noninteractive; apt-get install -y postgresql-17 postgresql-17-pgvector")
    sh("systemctl enable --now postgresql")
    sh("sudo -u postgres psql -tc \"SELECT 1 FROM pg_database WHERE datname='evolution'\" "
       "| grep -q 1 || sudo -u postgres psql -c \"CREATE DATABASE evolution;\"")


def p_postgrest():
    sh('V=$(curl -fsSL https://api.github.com/repos/PostgREST/postgrest/releases/latest '
       '| grep -oP \'"tag_name":\\s*"v\\K[^"]+\'); '
       f'curl -fsSL "https://github.com/PostgREST/postgrest/releases/download/v${{V}}/postgrest-v${{V}}-{DET["pg_arch"]}.tar.xz" '
       '-o /tmp/postgrest.tar.xz; tar -C /usr/local/bin -xf /tmp/postgrest.tar.xz; chmod +x /usr/local/bin/postgrest')
    sh(como_user(f"test -s {HOME}/.postgrest_jwt_secret || (openssl rand -hex 32 > {HOME}/.postgrest_jwt_secret; chmod 600 {HOME}/.postgrest_jwt_secret)"))
    conf = (f"db-uri = \"postgres://postgres@/postgres\"\n"
            f"db-schemas = \"public\"\n"
            f"db-anon-role = \"postgres\"\n"
            f"server-port = 3001\n")
    sh(como_user(f"printf '{conf}' > {HOME}/postgrest.conf"))
    unit = f"""[Unit]
Description=PostgREST (API do banco interno)
After=postgresql.service network.target
[Service]
User={ALVO_USER}
ExecStart=/usr/local/bin/postgrest {HOME}/postgrest.conf
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
"""
    sh(f"cat > /etc/systemd/system/postgrest.service <<'U'\n{unit}U")
    sh("systemctl daemon-reload && systemctl enable --now postgrest || true")


def _venv(pasta: str, req_rel: str):
    """Cria venv e instala libs travadas (lock) da pasta clonada."""
    sh(como_user(f"cd {pasta} && python3 -m venv .venv"))
    lock = os.path.join(LOCKS_DIR, req_rel)
    sh(como_user(
        f"cd {pasta} && if [ -f {lock} ]; then .venv/bin/pip -q install -r {lock}; "
        f"elif [ -f requirements.txt ]; then .venv/bin/pip -q install -r requirements.txt; fi"))


def p_painel():
    d = f"{HOME}/vps-admin"
    sh(como_user(f"mkdir -p {d} && cp -rf {CLONE}/. {d}/ && rm -rf {d}/.git {d}/instalador"))
    _venv(d, "vps-admin.txt")
    # senha inicial do painel (gerada aqui, mostrada no fim)
    sh(como_user(f"test -s {HOME}/.vps_admin_pass || (openssl rand -hex 5 > {HOME}/.vps_admin_pass; chmod 600 {HOME}/.vps_admin_pass)"))
    unit = f"""[Unit]
Description=VPS Admin (painel Streamlit)
After=network.target
[Service]
User={ALVO_USER}
WorkingDirectory={d}
ExecStart={d}/.venv/bin/streamlit run app.py --server.port 8500 --server.address 127.0.0.1 --server.headless true --server.baseUrlPath admin
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
"""
    sh(f"cat > /etc/systemd/system/vpsadmin.service <<'U'\n{unit}U")
    sh("systemctl daemon-reload && systemctl enable --now vpsadmin")


def p_provisionador():
    sh(f"install -o root -g root -m 755 {HOME}/vps-admin/vps_provision.py /usr/local/bin/vps_provision")
    sh(f"echo '{ALVO_USER} ALL=(root) NOPASSWD: /usr/local/bin/vps_provision' > /etc/sudoers.d/vps-provision")
    sh("chmod 440 /etc/sudoers.d/vps-provision && visudo -c")
    # sudoers p/ o vigia reiniciar servicos (deploy)
    sh(f"echo '{ALVO_USER} ALL=(root) NOPASSWD: /usr/bin/systemctl restart *, /usr/bin/systemctl start *' "
       f"> /etc/sudoers.d/vps-deploy && chmod 440 /etc/sudoers.d/vps-deploy")


def p_webhook():
    sh(como_user(f"test -s {HOME}/.vps_webhook_secret || (openssl rand -hex 24 > {HOME}/.vps_webhook_secret; chmod 600 {HOME}/.vps_webhook_secret)"))
    sh(como_user(f"test -s {HOME}/.vps_webhook_rota || (echo hook-$(openssl rand -hex 8) > {HOME}/.vps_webhook_rota; chmod 600 {HOME}/.vps_webhook_rota)"))
    unit = f"""[Unit]
Description=VPS Webhook (push->deploy)
After=network.target
[Service]
User={ALVO_USER}
ExecStart=/usr/bin/python3 {HOME}/vps-admin/webhook.py
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
"""
    sh(f"cat > /etc/systemd/system/vpswebhook.service <<'U'\n{unit}U")
    sh("systemctl daemon-reload && systemctl enable --now vpswebhook")


def p_mcp():
    d = f"{HOME}/vps-mcp"
    sh(como_user(f"mkdir -p {d} && cp -rf {CLONE}/vps_mcp/. {d}/ 2>/dev/null || true"))
    _venv(d, "vps-mcp.txt")
    sh(como_user(f"test -s {HOME}/.vps_mcp_token || (openssl rand -hex 20 > {HOME}/.vps_mcp_token; chmod 600 {HOME}/.vps_mcp_token)"))
    unit = f"""[Unit]
Description=VPS-MCP (ponte do Claude)
After=network.target
[Service]
User={ALVO_USER}
WorkingDirectory={d}
ExecStart={d}/.venv/bin/python server.py
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
"""
    sh(f"cat > /etc/systemd/system/vpsmcp.service <<'U'\n{unit}U")
    sh("systemctl daemon-reload && systemctl enable --now vpsmcp")


def p_gateway():
    d = f"{HOME}/llm-gateway"
    sh(como_user(f"mkdir -p {d} && cp -rf {CLONE}/llm_gateway/. {d}/ 2>/dev/null || true"))
    _venv(d, "llm-gateway.txt")
    unit = f"""[Unit]
Description=LLM Gateway
After=network.target
[Service]
User={ALVO_USER}
WorkingDirectory={d}
ExecStart={d}/.venv/bin/uvicorn gateway:app --host 127.0.0.1 --port 8600
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
"""
    sh(f"cat > /etc/systemd/system/llmgateway.service <<'U'\n{unit}U")
    sh("systemctl daemon-reload && systemctl enable --now llmgateway || true")


def p_sentinela():
    for nome, py in [("vpssentinela", "sentinela.py"),
                     ("vpsautodeploy", "autodeploy.py"),
                     ("vpsbackup", "backup_pg.py"),
                     ("vpsmetricas", "coletor_metricas.py")]:
        svc = f"""[Unit]
Description={nome}
[Service]
Type=oneshot
User={ALVO_USER}
ExecStart=/usr/bin/python3 {HOME}/vps-admin/{py}
"""
        tmr = f"""[Unit]
Description=timer {nome}
[Timer]
OnBootSec=90
OnUnitActiveSec=120
[Install]
WantedBy=timers.target
"""
        sh(f"cat > /etc/systemd/system/{nome}.service <<'U'\n{svc}U")
        sh(f"cat > /etc/systemd/system/{nome}.timer <<'T'\n{tmr}T")
    sh("systemctl daemon-reload && systemctl enable --now "
       "vpssentinela.timer vpsautodeploy.timer vpsbackup.timer vpsmetricas.timer")


def p_ntfy():
    sh('ARCH=$(dpkg --print-architecture); '
       'V=$(curl -fsSL https://api.github.com/repos/binwiederhier/ntfy/releases/latest | grep -oP \'"tag_name":\\s*"v\\K[^"]+\'); '
       'curl -fsSL "https://github.com/binwiederhier/ntfy/releases/download/v${V}/ntfy_${V}_linux_${ARCH}.tar.gz" -o /tmp/ntfy.tgz; '
       'tar -C /tmp -xzf /tmp/ntfy.tgz; cp /tmp/ntfy_*/ntfy /usr/local/bin/ntfy; chmod +x /usr/local/bin/ntfy')
    unit = """[Unit]
Description=ntfy (push proprio)
After=network.target
[Service]
ExecStart=/usr/local/bin/ntfy serve
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
"""
    sh(f"cat > /etc/systemd/system/ntfy.service <<'U'\n{unit}U")
    sh("systemctl daemon-reload && systemctl enable --now ntfy || true")


def p_evolution():
    sh("curl -fsSL https://deb.nodesource.com/setup_22.x | bash -")
    sh("export DEBIAN_FRONTEND=noninteractive; apt-get install -y nodejs")
    sh(como_user(f"rm -rf {HOME}/evolution-api && git clone --depth 1 "
                 f"https://github.com/EvolutionAPI/evolution-api.git {HOME}/evolution-api"))
    sh(como_user(f"cd {HOME}/evolution-api && npm install --omit=dev || npm install"))
    emit({"tipo": "log", "msg": "Evolution instalada — configure .env e DATABASE pelo painel depois."})


def p_ollama():
    sh("curl -fsSL https://ollama.com/install.sh | sh")


MAPA_PASSO = {
    "detectar": p_detectar,
    "sistema": p_sistema, "nginx": p_nginx, "postgres": p_postgres, "postgrest": p_postgrest,
    "painel": p_painel, "provisionador": p_provisionador, "webhook": p_webhook,
    "mcp": p_mcp, "gateway": p_gateway, "sentinela": p_sentinela,
    "ntfy": p_ntfy, "evolution": p_evolution, "ollama": p_ollama,
}


# ============================================================
# DESINSTALADOR (VM volta limpa)
# ============================================================
SERVICOS_DEL = ["vpsadmin", "vpsmcp", "llmgateway", "vpswebhook", "postgrest",
                "ntfy", "evolution", "vpssentinela.timer", "vpsautodeploy.timer",
                "vpsbackup.timer", "vpsmetricas.timer"]


def d_servicos():
    for s in SERVICOS_DEL:
        sh(f"systemctl disable --now {s} 2>/dev/null || true")
    sh("rm -f /etc/systemd/system/vps*.service /etc/systemd/system/vps*.timer "
       "/etc/systemd/system/llmgateway.service /etc/systemd/system/ntfy.service "
       "/etc/systemd/system/evolution.service /etc/systemd/system/postgrest.service")
    sh("systemctl daemon-reload")


def d_nginx():
    sh("rm -f /etc/nginx/sites-enabled/apps /etc/nginx/sites-available/apps")
    sh("systemctl reload nginx 2>/dev/null || true")


def d_banco():
    sh("sudo -u postgres psql -c \"DROP DATABASE IF EXISTS evolution;\" 2>/dev/null || true")


def d_arquivos():
    sh("rm -f /usr/local/bin/vps_provision /usr/local/bin/postgrest /usr/local/bin/ntfy")
    sh("rm -f /etc/sudoers.d/vps-provision /etc/sudoers.d/vps-deploy")
    sh(como_user(f"rm -rf {HOME}/vps-admin {HOME}/vps-mcp {HOME}/llm-gateway "
                 f"{HOME}/evolution-api {CLONE}"))
    sh(como_user(f"rm -f {HOME}/.vps_* {HOME}/.postgrest_jwt_secret {HOME}/.evolution_api_key 2>/dev/null || true"))


PASSOS_DESINSTALAR = [
    ("d_servicos", "Parar e remover serviços", "ti-player-stop", d_servicos),
    ("d_nginx",    "Remover rotas do Nginx",   "ti-world-off", d_nginx),
    ("d_banco",    "Remover banco evolution",  "ti-database-off", d_banco),
    ("d_arquivos", "Apagar pastas e binários", "ti-trash", d_arquivos),
]


# ============================================================
# ORQUESTRADOR
# ============================================================
def orquestrar(selec: list, modo: str, cfg: dict = None):
    if cfg:
        CONFIG["token"] = cfg.get("token", "")
        CONFIG["repo"] = cfg.get("repo") or REPO
    if modo == "desinstalar":
        plano = [(i, l, ic, fn) for (i, l, ic, fn) in PASSOS_DESINSTALAR]
    else:
        ordem = [c[0] for c in COMPONENTES]
        sel = [c for c in COMPONENTES if c[0] in selec or c[3]]
        sel.sort(key=lambda c: ordem.index(c[0]))
        plano = [(c[0], c[1], c[2], MAPA_PASSO[c[0]]) for c in sel]

    with LOCK:
        ESTADO["fase"] = "rodando"
        ESTADO["modo"] = modo
        ESTADO["passos"] = [{"id": i, "label": l, "icon": ic, "status": "pendente"} for (i, l, ic, _) in plano]
        ESTADO["pct"] = 0
    emit({"tipo": "reset"})

    total = len(plano)
    for k, (i, l, ic, fn) in enumerate(plano):
        emit({"tipo": "passo", "id": i, "status": "rodando"})
        emit({"tipo": "log", "msg": f"### {l}"})
        try:
            fn()
        except Exception as e:
            emit({"tipo": "passo", "id": i, "status": "erro"})
            emit({"tipo": "log", "msg": f"ERRO em {l}: {e}"})
            emit({"tipo": "fim", "fase": "erro"})
            return
        emit({"tipo": "passo", "id": i, "status": "ok"})
        emit({"pct": round((k + 1) / total * 100)})
    # mensagem final
    if modo != "desinstalar":
        try:
            pw = open(f"{HOME}/.vps_admin_pass").read().strip()
        except Exception:
            pw = "(ver ~/.vps_admin_pass)"
        emit({"tipo": "log", "msg": f"PAINEL: http://SEU-IP/admin/  ·  senha: {pw}"})
    emit({"tipo": "fim", "fase": "ok"})


# ============================================================
# WIZARD (HTML embutido)
# ============================================================
def checkboxes_html():
    out = []
    for cid, label, icon, obrig in COMPONENTES:
        mark = "checked" if (cid in PADRAO_MARCADOS or obrig) else ""
        dis = "disabled" if obrig else ""
        tag = " <span class='req'>obrigatório</span>" if obrig else ""
        out.append(
            f"<label class='cmp'><input type='checkbox' value='{cid}' {mark} {dis}>"
            f"<i class='ti {icon}'></i><span>{label}{tag}</span></label>")
    return "\n".join(out)


def pagina():
    return """<!DOCTYPE html><html lang=pt-br><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Instalador · VPS Framework</title>
<link rel=stylesheet href="https://cdnjs.cloudflare.com/ajax/libs/tabler-icons/3.34.0/iconfont/tabler-icons.min.css">
<style>
:root{--bg:#0f1115;--card:#171a21;--bd:#262b36;--tx:#e6e8ec;--mut:#9aa3b2;--ac:#3b82f6;--ok:#22c55e;--er:#ef4444}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font-family:system-ui,Segoe UI,sans-serif;line-height:1.5}
.wrap{max-width:680px;margin:32px auto;padding:0 16px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:14px;overflow:hidden}
.hd{display:flex;align-items:center;gap:12px;padding:18px 22px;border-bottom:1px solid var(--bd)}
.hd i{font-size:26px;color:var(--ac)}.hd b{font-size:17px;font-weight:600}.hd small{color:var(--mut);display:block;font-size:12px}
.bd{padding:20px 22px}
.cmp{display:flex;align-items:center;gap:10px;padding:9px 11px;border:1px solid var(--bd);border-radius:9px;margin-bottom:7px;cursor:pointer;font-size:14px}
.cmp:hover{border-color:#384152}.cmp input{width:17px;height:17px;accent-color:var(--ac)}
.cmp i{font-size:19px;color:var(--mut)}.cmp .req{color:var(--mut);font-size:11px;margin-left:4px}
.steps{display:flex;flex-direction:column;gap:8px;margin-bottom:16px}
.st{display:flex;align-items:center;gap:10px;font-size:13px;color:var(--mut)}
.st.run{color:var(--tx)}.st.ok{color:var(--tx)}.st .ic{font-size:16px;margin-left:auto}
.st.ok .ic{color:var(--ok)}.st.run .ic{color:var(--ac)}.st.erro .ic{color:var(--er)}
.barwrap{height:11px;background:#0b0d11;border-radius:99px;overflow:hidden;margin:4px 0 6px}
.bar{height:100%;width:0;background:var(--ac);border-radius:99px;transition:width .4s}
.row{display:flex;justify-content:space-between;font-size:12px;color:var(--mut)}
.btn{margin-top:18px;width:100%;border:none;border-radius:10px;padding:12px;font-size:15px;font-weight:600;cursor:pointer;background:var(--ac);color:#fff}
.btn.gho{background:transparent;border:1px solid var(--bd);color:var(--tx)}
.btn:disabled{opacity:.5;cursor:default}
.log{margin-top:14px;background:#0b0d11;border:1px solid var(--bd);border-radius:9px;padding:10px;height:170px;overflow:auto;font-family:ui-monospace,monospace;font-size:11.5px;color:#aeb6c2;white-space:pre-wrap}
.tabs{display:flex;gap:6px;margin-bottom:14px}
.tab{flex:1;text-align:center;padding:8px;border:1px solid var(--bd);border-radius:8px;cursor:pointer;font-size:13px;color:var(--mut)}
.tab.on{border-color:var(--ac);color:var(--tx)}
.fld{display:block;margin-bottom:10px;font-size:13px;color:var(--mut)}
.fld span{display:block;margin-bottom:4px}.fld small{color:#6b7280}
.fld input{width:100%;padding:8px 10px;border:1px solid var(--bd);border-radius:8px;background:#0b0d11;color:var(--tx);font-size:13px}
.hide{display:none}
</style></head><body><div class=wrap><div class=card>
<div class=hd><i class="ti ti-server-cog"></i><div><b>Instalador · VPS Framework</b><small>Ubuntu · clone do servidor (menos Ollama)</small></div></div>
<div class=bd>
  <div class=tabs><div class="tab on" id=tab-inst onclick="modo('instalar')">Instalar</div><div class=tab id=tab-uni onclick="modo('desinstalar')">Remover tudo</div></div>
  <div id=cfg>
    <label class=fld><span>Repo do código (privado) — padrão já preenchido</span>
      <input id=repo type=text value="https://github.com/diogobsbastos/vps-escola-parque-admin.git"></label>
    <label class=fld><span>Token do GitHub <small>(p/ clonar o repo privado + ligar o deploy; fica só na VM)</small></span>
      <input id=tok type=password placeholder="ghp_..."></label>
  </div>
  <div id=pick>__CHECKBOXES__</div>
  <div id=run class=hide>
    <div class=steps id=steps></div>
    <div class=row><span id=sl>Pronto</span><span id=pct>0%</span></div>
    <div class=barwrap><div class=bar id=bar></div></div>
    <div class=log id=log></div>
  </div>
  <button class=btn id=go onclick=start()>Instalar</button>
</div></div></div>
<script>
var KEY=new URLSearchParams(location.search).get("key")||"";
var MODO="instalar";
function modo(m){MODO=m;document.getElementById('tab-inst').classList.toggle('on',m=='instalar');
 document.getElementById('tab-uni').classList.toggle('on',m=='desinstalar');
 document.getElementById('pick').classList.toggle('hide',m=='desinstalar');
 document.getElementById('go').textContent=m=='instalar'?'Instalar':'Remover tudo';
 document.getElementById('go').style.background=m=='instalar'?'var(--ac)':'var(--er)';}
function sel(){return [...document.querySelectorAll('#pick input:checked')].map(x=>x.value);}
function start(){
 var go=document.getElementById('go');go.disabled=true;
 if(MODO=='desinstalar'&&!confirm('Remover TODOS os serviços e pastas do framework?')){go.disabled=false;return;}
 document.getElementById('pick').classList.add('hide');
 var cf=document.getElementById('cfg');if(cf)cf.classList.add('hide');
 document.getElementById('run').classList.remove('hide');
 fetch('/start?key='+KEY,{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({modo:MODO,componentes:sel(),
     token:(document.getElementById('tok')||{}).value||'',
     repo:(document.getElementById('repo')||{}).value||''})});
 var es=new EventSource('/progress?key='+KEY);
 es.onmessage=function(e){var d=JSON.parse(e.data);
   if(d.tipo=='reset'){render(d.passos);}
   if(d.passos){render(d.passos);}
   if(d.tipo=='passo'){var el=document.getElementById('st-'+d.id);if(el){el.className='st '+(d.status=='ok'?'ok':d.status=='rodando'?'run':d.status=='erro'?'erro':'');
     el.querySelector('.ic').className='ic ti '+(d.status=='ok'?'ti-circle-check':d.status=='rodando'?'ti-loader-2':d.status=='erro'?'ti-alert-circle':'ti-circle');}}
   if(d.pct!=null){document.getElementById('bar').style.width=d.pct+'%';document.getElementById('pct').textContent=d.pct+'%';}
   if(d.tipo=='log'){var L=document.getElementById('log');L.textContent+=d.msg+'\\n';L.scrollTop=L.scrollHeight;
     document.getElementById('sl').textContent=d.msg.slice(0,60);}
   if(d.tipo=='fim'){es.close();var go=document.getElementById('go');go.disabled=false;
     go.textContent=d.fase=='ok'?'✓ Concluído':'Erro — ver log';go.style.background=d.fase=='ok'?'var(--ok)':'var(--er)';}
 };
}
function render(passos){if(!passos)return;var c=document.getElementById('steps');if(c.dataset.done)return;c.dataset.done=1;
 c.innerHTML=passos.map(function(p){return '<div class="st" id="st-'+p.id+'"><i class="ti '+p.icon+'"></i><span>'+p.label+'</span><i class="ic ti ti-circle"></i></div>';}).join('');}
fetch('/estado?key='+KEY).then(r=>r.json()).then(d=>{}).catch(e=>{});
</script></body></html>""".replace("__CHECKBOXES__", checkboxes_html())


# ============================================================
# HTTP
# ============================================================
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _auth(self):
        q = parse_qs(urlparse(self.path).query)
        if q.get("key", [""])[0] != TOKEN:
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b"token invalido (use ?key=...)")
            return False
        return True

    def do_GET(self):
        path = urlparse(self.path).path
        if not self._auth():
            return
        if path == "/":
            body = pagina().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/estado":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            with LOCK:
                self.wfile.write(json.dumps(ESTADO).encode())
        elif path == "/progress":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            with LOCK:
                snap = {"passos": ESTADO["passos"], "pct": ESTADO["pct"]}
            self.wfile.write(f"data: {json.dumps(snap)}\n\n".encode())
            self.wfile.flush()
            while True:
                try:
                    ev = FILA.get(timeout=20)
                    self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
                    self.wfile.flush()
                    if ev.get("tipo") == "fim":
                        break
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    break
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if not self._auth():
            return
        if urlparse(self.path).path == "/start":
            n = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(n) or "{}")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
            threading.Thread(target=orquestrar,
                             args=(data.get("componentes", []), data.get("modo", "instalar"), data),
                             daemon=True).start()
        else:
            self.send_response(404)
            self.end_headers()


def main():
    modo = "desinstalar" if "--uninstall" in sys.argv else "instalar"
    ESTADO["modo"] = modo
    srv = ThreadingHTTPServer(("0.0.0.0", PORTA), H)
    print(f"\n  Abra no navegador:  http://SEU-IP:{PORTA}/?key={TOKEN}\n", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
