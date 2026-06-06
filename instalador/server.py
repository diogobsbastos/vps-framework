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
CONFIG = {"token": "", "repo": REPO, "provedor": "VPS", "dominio": "", "origem": "git", "arquivo_b64": "", "arquivo_nome": ""}  # da tela (/start)

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
    ("worker",    "Backend Central (worker)",        "ti-engine", False),
    ("libs",      "Biblioteca completa (IA/Visão/Mídia)", "ti-books", False),
    ("https",     "HTTPS + domínio (cadeado + rota MCP)",  "ti-lock", False),
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


def _detectar_ip_pub():
    try:
        r = subprocess.run("curl -fsSL --max-time 5 ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}'",
                           shell=True, capture_output=True, text=True).stdout.strip()
        return r or "SEU-IP"
    except Exception:
        return "SEU-IP"
IP_PUB = _detectar_ip_pub()
VERSAO = "v0.9.0"
try:
    import datetime as _dt
    _BUILD = _dt.datetime.fromtimestamp(os.path.getmtime(__file__)).strftime("%d/%m %H:%M")
except Exception:
    _BUILD = "?"


print(f"[instalador] token={TOKEN}  porta={PORTA}  dry={DRY}", flush=True)


# ============================================================
# ETAPAS DE INSTALACAO (cada uma idempotente)
# ============================================================
CLONE = f"{HOME}/.vps-framework-src"
INSTALADOR_DIR = os.path.dirname(os.path.abspath(__file__))
LOCKS_DIR = os.path.join(INSTALADOR_DIR, "..", "locks")
OVERRIDE_DIR = os.path.join(INSTALADOR_DIR, "..", "override")  # arquivos atualizados (ex.: app.py parametrizado)

APT_DEPS = (
    "build-essential python3-venv python3-dev python3-pip libpq-dev "
    "curl gnupg ca-certificates git nginx certbot python3-certbot-nginx "
    "rclone ffmpeg fonts-dejavu-core fonts-noto-color-emoji iptables-persistent unzip"
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
    arq = CONFIG.get("arquivo_b64") or ""
    if arq:
        import base64
        nome = CONFIG.get("arquivo_nome", "codigo.tar.gz")
        ext = ".zip" if nome.lower().endswith(".zip") else ".tar.gz"
        path = f"/tmp/codigo-upload{ext}"
        with open(path, "wb") as f:
            f.write(base64.b64decode(arq))
        os.chmod(path, 0o644)
        sh(como_user("rm -rf /tmp/codigo-x && mkdir -p /tmp/codigo-x"))
        if ext == ".zip":
            sh(como_user(f"unzip -q -o {path} -d /tmp/codigo-x"))
        else:
            sh(como_user(f"tar -C /tmp/codigo-x -xzf {path}"))
        sh(como_user(f"SRC=$(dirname $(find /tmp/codigo-x -maxdepth 4 -name app.py | head -1)); "
                     f"test -n \"$SRC\" && rm -rf {CLONE} && cp -rf \"$SRC\" {CLONE} && echo \"codigo extraido de: $SRC\""))
        emit({"tipo": "log", "msg": f"Código instalado do arquivo '{nome}' (sem Git, sem token)."})
        return
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
    arch_kw = "x86-64|x86_64|amd64|x64" if DET.get("arch") in ("x86_64", "amd64") else "aarch64|arm64"
    sh('set -e; URL=$(curl -fsSL https://api.github.com/repos/PostgREST/postgrest/releases/latest '
       '| grep -o \'"browser_download_url": *"[^"]*"\' | sed -E \'s/.*"(http[^"]+)"/\\1/\' '
       f'| grep -iE "linux|ubuntu" | grep -iE "{arch_kw}" | grep -iE "tar" | head -1); '
       'echo "PostgREST asset: $URL"; test -n "$URL"; '
       'curl -fsSL "$URL" -o /tmp/pgrst.tar.xz; tar -C /usr/local/bin -xf /tmp/pgrst.tar.xz; '
       'chmod +x /usr/local/bin/postgrest; /usr/local/bin/postgrest --version')
    sh(como_user(f"test -s {HOME}/.postgrest_pass || (openssl rand -hex 16 > {HOME}/.postgrest_pass; chmod 600 {HOME}/.postgrest_pass)"))
    sh(como_user(f"test -s {HOME}/.postgrest_jwt_secret || (openssl rand -hex 32 > {HOME}/.postgrest_jwt_secret; chmod 600 {HOME}/.postgrest_jwt_secret)"))
    pw = "DRYPASS" if DRY else subprocess.run(f"cat {HOME}/.postgrest_pass", shell=True, capture_output=True, text=True).stdout.strip()
    sh("sudo -u postgres psql -tc \"SELECT 1 FROM pg_roles WHERE rolname='anon'\" | grep -q 1 || sudo -u postgres psql -c \"CREATE ROLE anon NOLOGIN\"")
    sh(f"sudo -u postgres psql -tc \"SELECT 1 FROM pg_roles WHERE rolname='authenticator'\" | grep -q 1 || sudo -u postgres psql -c \"CREATE ROLE authenticator LOGIN NOINHERIT PASSWORD '{pw}'\"")
    sh(f"sudo -u postgres psql -c \"ALTER ROLE authenticator PASSWORD '{pw}'\"")
    sh("sudo -u postgres psql -c \"GRANT anon TO authenticator\"")
    conf = (f'db-uri = "postgres://authenticator:{pw}@localhost:5432/postgres"\n'
            'db-schemas = "public"\n'
            'db-anon-role = "anon"\n'
            'server-port = 3001\n')
    sh(como_user(f"cat > {HOME}/postgrest.conf <<'PGRST'\n{conf}PGRST"))
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
    sh("systemctl daemon-reload && systemctl enable postgrest && systemctl restart postgrest || true")

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
    sh(como_user(f"[ -d {d}/infra ] && cp -f {d}/infra/*.py {d}/infra/*.sh {d}/ 2>/dev/null; true"))
    if os.path.isdir(OVERRIDE_DIR):
        sh(f'cp -rf "{OVERRIDE_DIR}/." "{d}/" && chown -R {ALVO_USER}:{ALVO_USER} "{d}"')
        emit({"tipo": "log", "msg": "overlay aplicado (app.py parametrizado)"})
    _venv(d, "vps-admin.txt")
    # senha inicial do painel (gerada aqui, mostrada no fim)
    sh(como_user(f"test -s {HOME}/.vps_admin_pass || (openssl rand -hex 5 > {HOME}/.vps_admin_pass; chmod 600 {HOME}/.vps_admin_pass)"))
    # identidade desta maquina = fonte unica do painel (~/.vps_config.json)
    ipx = subprocess.run("curl -fsSL --max-time 6 ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}'",
                         shell=True, capture_output=True, text=True).stdout.strip()
    repo = CONFIG.get("repo") or REPO
    guser = ""
    try:
        guser = repo.split("github.com/")[1].split("/")[0]
    except Exception:
        guser = ""
    cfg = {"ip": ipx, "dominio": CONFIG.get("dominio", ""),
           "provedor": CONFIG.get("provedor", "VPS"), "arch": DET.get("arch", ""),
           "github_user": guser}
    cfgjson = json.dumps(cfg, ensure_ascii=False, indent=2)
    sh(como_user(f"cat > {HOME}/.vps_config.json <<'CFGJSON'\n{cfgjson}\nCFGJSON"))
    emit({"tipo": "log", "msg": f"identidade: {cfg}"})
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
    sh("mkdir -p /etc/ntfy /var/cache/ntfy")
    sh("printf 'base-url: http://127.0.0.1:2586\\nlisten-http: \":2586\"\\ncache-file: /var/cache/ntfy/cache.db\\n' > /etc/ntfy/server.yml")
    unit = """[Unit]
Description=ntfy (push proprio)
After=network.target
[Service]
ExecStart=/usr/local/bin/ntfy serve --config /etc/ntfy/server.yml
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
"""
    sh(f"cat > /etc/systemd/system/ntfy.service <<'U'\n{unit}U")
    sh("systemctl daemon-reload && systemctl enable ntfy && systemctl restart ntfy || true")

def p_evolution():
    sh("curl -fsSL https://deb.nodesource.com/setup_22.x | bash -")
    sh("export DEBIAN_FRONTEND=noninteractive; apt-get install -y nodejs")
    sh(como_user(f"rm -rf {HOME}/evolution-api && git clone --depth 1 "
                 f"https://github.com/EvolutionAPI/evolution-api.git {HOME}/evolution-api"))
    sh(como_user(f"cd {HOME}/evolution-api && npm install --omit=dev --no-audit --no-fund || npm install --force || true"))
    # chave da API + usuario/senha do banco
    sh(como_user(f"test -s {HOME}/.evolution_api_key || (openssl rand -hex 16 > {HOME}/.evolution_api_key; chmod 600 {HOME}/.evolution_api_key)"))
    sh(como_user(f"test -s {HOME}/.evolution_db_pass || (openssl rand -hex 12 > {HOME}/.evolution_db_pass; chmod 600 {HOME}/.evolution_db_pass)"))
    apikey = "DRY" if DRY else subprocess.run(f"cat {HOME}/.evolution_api_key", shell=True, capture_output=True, text=True).stdout.strip()
    dbpass = "DRY" if DRY else subprocess.run(f"cat {HOME}/.evolution_db_pass", shell=True, capture_output=True, text=True).stdout.strip()
    sh(f"sudo -u postgres psql -tc \"SELECT 1 FROM pg_roles WHERE rolname='evolution_user'\" | grep -q 1 || sudo -u postgres psql -c \"CREATE ROLE evolution_user LOGIN PASSWORD '{dbpass}'\"")
    sh(f"sudo -u postgres psql -c \"ALTER ROLE evolution_user PASSWORD '{dbpass}'\"")
    sh("sudo -u postgres psql -c \"ALTER DATABASE evolution OWNER TO evolution_user\" || true")
    env = ("SERVER_TYPE=http\nSERVER_PORT=8080\n"
           "DATABASE_ENABLED=true\nDATABASE_PROVIDER=postgresql\n"
           f"DATABASE_CONNECTION_URI=postgresql://evolution_user:{dbpass}@localhost:5432/evolution?schema=public\n"
           "DATABASE_CONNECTION_CLIENT_NAME=evolution\n"
           f"AUTHENTICATION_API_KEY={apikey}\n"
           "CACHE_REDIS_ENABLED=false\nCACHE_LOCAL_ENABLED=true\n")
    sh(como_user(f"cat > {HOME}/evolution-api/.env <<'ENV'\n{env}ENV"))
    unit = f"""[Unit]
Description=Evolution API (Zap Push)
After=network.target postgresql.service
[Service]
User={ALVO_USER}
WorkingDirectory={HOME}/evolution-api
ExecStart=/usr/bin/npm run start:prod
Restart=always
RestartSec=8
Environment=NODE_ENV=production
[Install]
WantedBy=multi-user.target
"""
    sh(f"cat > /etc/systemd/system/evolution.service <<'U'\n{unit}U")
    sh("systemctl daemon-reload && systemctl enable evolution || true")
    sh("systemctl start evolution || true")
    emit({"tipo": "log", "msg": "Evolution: unit + .env + usuario de banco criados. O pareamento do WhatsApp (QR) é feito depois."})


def p_worker():
    d = f"{HOME}/backend-central"
    sh(como_user(f"mkdir -p {d} && python3 -m venv {d}/.venv && {d}/.venv/bin/pip -q install psycopg2-binary"))
    worker = (
        "#!/usr/bin/env python3\n"
        "# Backend Central (worker) — slot generico do framework.\n"
        "# Roda em loop; coloque aqui a logica de sync/integracao\n"
        "# (ex.: Supabase local <-> outro sistema, filas, ETL).\n"
        "import time\n"
        "# import psycopg2  # banco local: postgres://authenticator@localhost:5432/postgres\n"
        "def ciclo():\n"
        "    # >>> SUA LOGICA DE SYNC/INTEGRACAO AQUI <<<\n"
        "    pass\n"
        "if __name__ == '__main__':\n"
        "    print('Backend Central worker iniciado', flush=True)\n"
        "    while True:\n"
        "        try:\n"
        "            ciclo()\n"
        "        except Exception as e:\n"
        "            print('erro no ciclo:', e, flush=True)\n"
        "        time.sleep(30)\n"
    )
    sh(como_user(f"cat > {d}/worker.py <<'WK'\n{worker}WK"))
    unit = f"""[Unit]
Description=Backend Central (worker do framework)
After=network.target postgresql.service
[Service]
User={ALVO_USER}
WorkingDirectory={d}
ExecStart={d}/.venv/bin/python worker.py
Restart=always
RestartSec=10
[Install]
WantedBy=multi-user.target
"""
    sh(f"cat > /etc/systemd/system/backendcentral.service <<'U'\n{unit}U")
    sh("systemctl daemon-reload && systemctl enable --now backendcentral")


def p_libs():
    d = f"{HOME}/libs-base"
    sh(como_user(f"mkdir -p {d} && python3 -m venv {d}/.venv"))
    pip = f"{d}/.venv/bin/pip"
    sh(como_user(f"{pip} install -q --upgrade pip"))
    # torch CPU primeiro (evita baixar o build CUDA gigante)
    sh(como_user(f"{pip} install -q torch==2.12.0 --index-url https://download.pytorch.org/whl/cpu"))
    lock = os.path.join(LOCKS_DIR, "libs-base.txt")
    sh(como_user(f"{pip} install -q -r {lock}"))
    emit({"tipo": "log", "msg": "Biblioteca completa pronta em ~/libs-base/.venv (playwright: rode 'playwright install chromium' se precisar do browser)."})


def p_https():
    dom = CONFIG.get("dominio", "").strip()
    if not dom:
        emit({"tipo": "log", "msg": "Sem domínio informado -> mantendo HTTP por IP (sem cadeado). Para HTTPS: aponte um domínio pro IP e reinstale com o campo Domínio preenchido."})
        return
    try:
        tok = open(f"{HOME}/.vps_mcp_token").read().strip()
    except Exception:
        tok = ""
    rota_mcp = ""
    if tok:
        rota_mcp = (f"    location /mcp-{tok}/ {{\n"
                    "        proxy_set_header Origin '';\n"
                    "        proxy_pass http://127.0.0.1:8700/;\n"
                    "        proxy_http_version 1.1;\n"
                    "        proxy_set_header Host 127.0.0.1:8700;\n"
                    "        proxy_set_header Connection '';\n"
                    "        proxy_buffering off;\n"
                    "        proxy_read_timeout 86400;\n"
                    "    }\n")
    conf = (f"server {{\n"
            f"    listen 80;\n    server_name {dom};\n"
            "    location = / { return 302 /admin/; }\n"
            "    location /admin/ { proxy_pass http://127.0.0.1:8500/admin/; proxy_http_version 1.1; proxy_set_header Upgrade $http_upgrade; proxy_set_header Connection \"upgrade\"; proxy_set_header Host $host; proxy_read_timeout 86400; }\n"
            "    location /llm/ { proxy_pass http://127.0.0.1:8600/; proxy_buffering off; proxy_read_timeout 86400; }\n"
            "    location /rest/v1/ { proxy_pass http://127.0.0.1:3001/; }\n"
            f"{rota_mcp}"
            "}\n")
    sh(f"cat > /etc/nginx/sites-available/apps <<'NG'\n{conf}NG")
    sh("nginx -t && systemctl reload nginx")
    sh(f"certbot --nginx -d {dom} --redirect --agree-tos --register-unsafely-without-email -n || echo 'certbot falhou (DNS aponta pro IP? porta 80 aberta?) — segue em HTTP'")
    if tok:
        emit({"tipo": "log", "msg": f"Rota MCP exposta. Conector p/ o Claude: https://{dom}/mcp-{tok}/mcp"})
    emit({"tipo": "log", "msg": f"HTTPS: se o certbot passou, painel em https://{dom}/admin/"})


def p_ollama():
    sh("curl -fsSL https://ollama.com/install.sh | sh")


MAPA_PASSO = {
    "detectar": p_detectar,
    "sistema": p_sistema, "nginx": p_nginx, "postgres": p_postgres, "postgrest": p_postgrest,
    "painel": p_painel, "provisionador": p_provisionador, "webhook": p_webhook,
    "mcp": p_mcp, "gateway": p_gateway, "sentinela": p_sentinela,
    "ntfy": p_ntfy, "evolution": p_evolution,
    "worker": p_worker, "libs": p_libs, "https": p_https, "ollama": p_ollama,
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
    # limpa credenciais do GitHub (IMPORTANTE: token nao pode ficar na VM do cliente)
    sh(como_user(f"rm -f {HOME}/.github_token {HOME}/.git-credentials 2>/dev/null; git config --global --unset credential.helper 2>/dev/null || true"))


PASSOS_DESINSTALAR = [
    ("d_servicos", "Parar e remover serviços", "ti-player-stop", d_servicos),
    ("d_nginx",    "Remover rotas do Nginx",   "ti-world-off", d_nginx),
    ("d_banco",    "Remover banco evolution",  "ti-database-off", d_banco),
    ("d_arquivos", "Apagar pastas e binários", "ti-trash", d_arquivos),
]


# ============================================================
# ORQUESTRADOR
# ============================================================
def _framework_instalado() -> bool:
    """True se há QUALQUER coisa do framework instalada nesta VM."""
    marcos = [
        "/usr/local/bin/vps_provision",
        "/etc/systemd/system/vpsadmin.service",
        f"{HOME}/vps-admin",
    ]
    return any(os.path.exists(m) for m in marcos)


INSPECT_ITENS = [
    ("Nginx", "ti-world", "svc", "nginx"),
    ("PostgreSQL", "ti-database", "svc", "postgresql"),
    ("PostgREST", "ti-api", "svc", "postgrest"),
    ("Painel VPS Admin", "ti-layout-dashboard", "svc", "vpsadmin"),
    ("Provisionador", "ti-rocket", "file", "/usr/local/bin/vps_provision"),
    ("Webhook (deploy)", "ti-git-merge", "svc", "vpswebhook"),
    ("VPS-MCP (Claude)", "ti-plug", "svc", "vpsmcp"),
    ("LLM Gateway", "ti-key", "svc", "llmgateway"),
    ("Sentinela", "ti-bell", "timer", "vpssentinela.timer"),
    ("ntfy (push)", "ti-send", "svc", "ntfy"),
    ("Evolution (WhatsApp)", "ti-brand-whatsapp", "svc", "evolution"),
    ("Backend Central", "ti-engine", "svc", "backendcentral"),
    ("Ollama (LLM local)", "ti-cpu", "svc", "ollama"),
]


def _status_unidade(tipo: str, alvo: str) -> str:
    """ativo (rodando) | inativo (instalado mas parado) | ausente (nao existe)."""
    if tipo == "file":
        return "ativo" if os.path.exists(alvo) else "ausente"
    unit = alvo if "." in alvo else alvo + ".service"
    existe = subprocess.run(f"systemctl cat {unit}", shell=True,
                            capture_output=True).returncode == 0
    if not existe:
        return "ausente"
    act = subprocess.run(f"systemctl is-active {unit}", shell=True,
                         capture_output=True, text=True).stdout.strip()
    return "ativo" if act == "active" else "inativo"


def inspecionar() -> dict:
    """Raio-X do servidor onde o instalador esta rodando (sem SSH: local)."""
    import socket
    cfg = {}
    try:
        cfg = json.loads(open(f"{HOME}/.vps_config.json").read())
    except Exception:
        cfg = {}

    def _sh(c):
        return subprocess.run(c, shell=True, capture_output=True, text=True).stdout.strip()

    itens = [{"label": lb, "icon": ic, "status": _status_unidade(tp, al)}
             for (lb, ic, tp, al) in INSPECT_ITENS]
    ativos = sum(1 for i in itens if i["status"] == "ativo")
    return {
        "instalado": _framework_instalado(),
        "host": socket.gethostname(),
        "ip": IP_PUB,
        "provedor": (cfg.get("provedor") or cfg.get("provider") or "").strip(),
        "dominio": (cfg.get("dominio") or "").strip(),
        "arch": _sh("uname -m") or "?",
        "os": (_sh("lsb_release -ds 2>/dev/null") or "").strip('"'),
        "python": _sh("python3 --version"),
        "disco": _sh("df -h / | tail -1 | awk '{print $3\" de \"$2\" (\"$5\" usado)\"}'"),
        "ativos": ativos,
        "total": len(itens),
        "itens": itens,
    }


def orquestrar(selec: list, modo: str, cfg: dict = None):
    if cfg:
        CONFIG["token"] = cfg.get("token", "")
        CONFIG["repo"] = cfg.get("repo") or REPO
        CONFIG["provedor"] = (cfg.get("provedor") or "VPS").strip()
        CONFIG["dominio"] = (cfg.get("dominio") or "").strip()
        CONFIG["origem"] = cfg.get("origem", "git")
        CONFIG["arquivo_b64"] = cfg.get("arquivo_b64", "")
        CONFIG["arquivo_nome"] = cfg.get("arquivo_nome", "")
    if modo == "desinstalar":
        if not _framework_instalado():
            with LOCK:
                ESTADO["fase"] = "ok"; ESTADO["passos"] = []; ESTADO["pct"] = 100
            emit({"tipo": "log", "msg": "Nada instalado — a VM já estava limpa. Nada a remover. ✓"})
            emit({"pct": 100})
            emit({"tipo": "fim", "fase": "ok"})
            return
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
        dom = CONFIG.get("dominio", "").strip()
        painel = f"https://{dom}/admin/" if dom else f"http://{IP_PUB}/admin/"
        emit({"tipo": "log", "msg": f"PAINEL: {painel}  ·  senha: {pw}"})
        emit({"tipo": "fim", "fase": "ok", "senha": pw, "painel": painel})
        return
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
<title>VPS Admin · Instalador</title>
<link rel=stylesheet href="https://cdnjs.cloudflare.com/ajax/libs/tabler-icons/3.34.0/iconfont/tabler-icons.min.css">
<style>
*,*::before,*::after{box-sizing:border-box}
.ver{position:fixed;top:7px;right:13px;font-size:10.5px;color:rgba(127,184,172,.6);letter-spacing:.4px;z-index:60;user-select:none;font-family:ui-monospace,monospace}
html,body{margin:0;height:100%;overflow:hidden;background:#081310;color:#dfeae6;font-family:system-ui,Segoe UI,sans-serif;scrollbar-width:thin;scrollbar-color:#2bbd9e rgba(255,255,255,.05)}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-track{background:rgba(255,255,255,.04);border-radius:99px}
::-webkit-scrollbar-thumb{background:linear-gradient(#2bbd9e,#16a085);border-radius:99px;border:2px solid transparent;background-clip:padding-box}
::-webkit-scrollbar-thumb:hover{background:#3ad6b0}
*{scrollbar-width:thin;scrollbar-color:#2bbd9e rgba(255,255,255,.05)}
.bg{position:fixed;inset:0;width:100%;height:100%;z-index:0}
.shell{position:relative;z-index:2;height:100vh;display:flex;flex-direction:column;padding:26px}
.main{flex:1;display:flex;gap:22px;min-height:0}
.footbar{margin-top:18px;display:flex;align-items:center;gap:18px;background:rgba(8,18,16,.62);backdrop-filter:blur(10px);border:1px solid rgba(43,189,158,.2);border-radius:14px;padding:13px 22px}
.left{flex:0 0 36%;max-width:420px;min-width:300px;display:flex;flex-direction:column;overflow-y:auto;overflow-x:hidden}
.emblem{width:62px;height:62px;border-radius:15px;border:1px solid rgba(43,189,158,.45);background:rgba(43,189,158,.12);display:flex;align-items:center;justify-content:center;margin-bottom:12px}
.emblem i{font-size:28px;color:#3ad6b0}
.left h1{margin:0;font-size:25px;font-weight:700;letter-spacing:3px;background:linear-gradient(180deg,#eafff9,#5f897e);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.left .tag{margin:5px 0 0;font-size:11px;letter-spacing:1.6px;color:#7fb8ac;text-transform:uppercase}
.tabs{display:flex;gap:6px;margin:22px 0 14px}
.tab{flex:1;text-align:center;padding:9px;border:1px solid rgba(255,255,255,.12);border-radius:9px;cursor:pointer;font-size:13px;color:#8fb0a8}
.tab.on{border-color:#2bbd9e;color:#eafff9;background:rgba(43,189,158,.08)}
.fld{display:block;margin-bottom:10px;font-size:12.5px;color:#8fb0a8}
.fld span{display:block;margin-bottom:4px}.fld small{color:#5f897e}
.fld input{width:100%;padding:9px 11px;border:1px solid rgba(255,255,255,.12);border-radius:8px;background:rgba(5,12,10,.6);color:#dfeae6;font-size:13px}
.right{flex:1;min-width:0;display:flex;flex-direction:column;background:rgba(8,18,16,.6);backdrop-filter:blur(11px);border:1px solid rgba(43,189,158,.2);border-radius:16px;overflow:hidden}
.rhead{padding:13px 22px 11px;font-size:11.5px;text-transform:uppercase;letter-spacing:1.3px;color:#7fb8ac;border-bottom:1px solid rgba(255,255,255,.07);display:flex;justify-content:space-between;align-items:center}
.rhactions button{background:rgba(43,189,158,.1);color:#7fb8ac;border:1px solid rgba(43,189,158,.3);border-radius:6px;cursor:pointer;font-size:10.5px;letter-spacing:.5px;padding:4px 9px;margin-left:6px}
.rhactions button:hover{background:rgba(43,189,158,.2);color:#eafff9}
.rbody{flex:1;display:flex;flex-direction:column;min-height:0;overflow:hidden;padding:14px 22px}
#pick{flex:1;overflow-y:auto;overflow-x:hidden;min-height:0}
.srvcard{flex:none;margin-bottom:13px;background:rgba(43,189,158,.05);border:1px solid rgba(43,189,158,.20);border-radius:11px;padding:12px 15px}
.srvtop{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px}
.srvttl{font-size:10.5px;letter-spacing:1.4px;color:#7fb8ac;text-transform:uppercase;font-weight:600}
.srvtog{font-size:11px;color:#3ad6b0;cursor:pointer;user-select:none}
.srvtog:hover{color:#eafff9}
.srvid{font-size:13.5px;color:#eafff9;font-weight:600;line-height:1.4}
.srvsum{font-size:12px;color:#9fb8b1;margin-top:3px}
.srvdet{margin-top:11px;display:grid;grid-template-columns:repeat(auto-fill,minmax(155px,1fr));gap:7px 14px;padding-top:11px;border-top:1px solid rgba(255,255,255,.07)}
.srvitem{font-size:12px;color:#c2d6d0;display:flex;align-items:center;gap:8px}
.srvdot{width:8px;height:8px;border-radius:50%;display:inline-block;flex:none;box-shadow:0 0 6px currentColor}
.srvload{font-size:12px;color:#7fb8ac}
#run{flex:1;display:flex;flex-direction:column;min-height:0}
.cmp{display:flex;align-items:center;gap:10px;padding:9px 12px;border:1px solid rgba(255,255,255,.08);border-radius:9px;margin-bottom:7px;cursor:pointer;font-size:13.5px;background:rgba(8,18,16,.45)}
.cmp:hover{border-color:rgba(43,189,158,.4)}.cmp input{width:16px;height:16px;accent-color:#2bbd9e;flex:none}
.cmp i{font-size:18px;color:#5f897e;flex:none}.cmp span{flex:1;min-width:0}.cmp .req{color:#5f897e;font-size:11px;margin-left:4px}
.steps{display:flex;flex-direction:column;gap:4px;margin-bottom:10px;flex:none;max-height:42%;overflow-y:auto}
.st{display:flex;align-items:center;gap:9px;font-size:12.5px;color:#8fb0a8;padding:5px 7px;border-radius:7px}
.st.run{color:#eafff9;background:rgba(43,189,158,.10)}.st.ok{color:#dfeae6}.st span{flex:1;min-width:0}.st .ic{font-size:14px;flex:none}
.st.ok .ic{color:#3ad6b0}.st.run .ic{color:#2bbd9e}.st.erro .ic{color:#ef6b6b}
.log{flex:1;min-height:0;background:rgba(5,12,10,.7);border:1px solid rgba(255,255,255,.08);border-radius:9px;padding:9px;overflow:auto;font-family:ui-monospace,monospace;font-size:11px;color:#9fb0a8;white-space:pre-wrap}
.foot{border-top:1px solid rgba(43,189,158,.18);padding:14px 22px;display:flex;align-items:center;gap:16px}
.prog{flex:1;min-width:0}.prow{display:flex;justify-content:space-between;font-size:12.5px;margin-bottom:7px}
.prow #sl{color:#bfe0d7;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.prow #pct{color:#2bbd9e;font-weight:600;font-variant-numeric:tabular-nums;flex:none;margin-left:10px}
.track{height:8px;background:rgba(255,255,255,.08);border-radius:99px;overflow:hidden}
.track #bar{height:100%;width:0;border-radius:99px;background:linear-gradient(90deg,#2bbd9e,#3ad6b0);box-shadow:0 0 12px rgba(43,189,158,.5);transition:width .45s}
.go{flex:none;background:linear-gradient(90deg,#2bbd9e,#16a085);color:#04130d;border:none;border-radius:10px;padding:11px 28px;font-size:14px;font-weight:700;cursor:pointer;white-space:nowrap;box-shadow:0 6px 18px rgba(43,189,158,.35)}
.go:disabled{opacity:.5;cursor:default}.go.uni{background:linear-gradient(90deg,#e06b6b,#c0392b);color:#fff}.go.done{background:linear-gradient(90deg,#3ad6b0,#16a085)}
.gouni{flex:none;background:transparent;color:#ef8b8b;border:1px solid rgba(224,107,107,.5);border-radius:10px;padding:11px 18px;font-size:13px;font-weight:600;cursor:pointer;white-space:nowrap}
.gouni:hover{background:rgba(224,107,107,.12);border-color:rgba(224,107,107,.8)}
.gouni:disabled{opacity:.4;cursor:default}
.help{display:inline-flex;align-items:center;justify-content:center;width:18px;height:18px;border-radius:50%;border:1px solid rgba(255,255,255,.3);color:#9fb0a8;font-size:11px;margin-left:7px;cursor:help;vertical-align:middle}
.help:hover{border-color:#2bbd9e;color:#2bbd9e}
.modolink:hover{text-decoration:underline}
.origemtabs{display:flex;gap:6px;margin-bottom:12px}
.otab{flex:1;padding:8px;border:1px solid rgba(255,255,255,.12);border-radius:8px;background:transparent;color:#8fb0a8;font-size:12px;cursor:pointer}
.otab.on{border-color:#2bbd9e;color:#eafff9;background:rgba(43,189,158,.08)}
.dropzone{border:1.5px dashed rgba(43,189,158,.4);border-radius:11px;padding:20px;text-align:center;cursor:pointer;background:rgba(43,189,158,.04);transition:.18s}
.dropzone:hover,.dropzone.over{border-color:#2bbd9e;background:rgba(43,189,158,.13)}
.dzicon{font-size:26px;margin-bottom:6px}
#dztxt{font-size:12.5px;color:#bfe0d7}#dztxt small{color:#5f897e}
.modal{position:fixed;inset:0;z-index:50;background:rgba(3,8,6,.8);backdrop-filter:blur(4px);display:none;align-items:center;justify-content:center}
.modal.show{display:flex}
.modalcard{background:#0d1f1c;border:1px solid rgba(224,107,107,.45);border-radius:16px;padding:26px 28px;max-width:440px;text-align:center;box-shadow:0 22px 60px rgba(0,0,0,.55)}
.modalicon{width:54px;height:54px;margin:0 auto 12px;border-radius:50%;background:rgba(224,107,107,.15);display:flex;align-items:center;justify-content:center}
.modalicon i{font-size:30px;color:#ef6b6b}
.modalcard h3{margin:0 0 10px;font-size:19px;color:#fff}
.modalcard p{font-size:13px;color:#bcd0c9;line-height:1.6;margin:0 0 9px}
.modalsafe{color:#7fb8ac !important}
.modalbtns{display:flex;gap:10px;margin-top:18px}
.mbcancel{flex:1;background:transparent;border:1px solid rgba(255,255,255,.22);color:#dfeae6;border-radius:10px;padding:11px;font-size:14px;cursor:pointer}
.mbcancel:hover{background:rgba(255,255,255,.06)}
.mbok{flex:1;background:linear-gradient(90deg,#e06b6b,#c0392b);border:none;color:#fff;border-radius:10px;padding:11px;font-size:14px;font-weight:700;cursor:pointer}
.hide{display:none !important}
</style></head><body>
<div class=ver>VPS Admin __VERSAO__ · build __BUILD__</div>
<svg class=bg viewBox="0 0 1000 600" preserveAspectRatio="xMidYMid slice">
 <defs>
  <radialGradient id="glow" cx="30%" cy="35%" r="62%"><stop offset="0%" stop-color="#1d5f55" stop-opacity=".5"/><stop offset="100%" stop-color="#1d5f55" stop-opacity="0"/></radialGradient>
  <linearGradient id="bgg" x1="0" y1="0" x2="1" y2="1"><stop offset="0%" stop-color="#0a1614"/><stop offset="50%" stop-color="#0c1f1c"/><stop offset="100%" stop-color="#081310"/></linearGradient>
  <linearGradient id="steel" x1="0" y1="0" x2="1" y2="1"><stop offset="0%" stop-color="#2a3a37"/><stop offset="45%" stop-color="#15211f"/><stop offset="100%" stop-color="#0c1513"/></linearGradient>
  <linearGradient id="teal" x1="0" y1="0" x2="1" y2="0"><stop offset="0%" stop-color="#2bbd9e"/><stop offset="100%" stop-color="#0e6e5c"/></linearGradient>
 </defs>
 <rect width="1000" height="600" fill="url(#bgg)"/><rect width="1000" height="600" fill="url(#glow)"/>
 <path d="M-50 130 C 250 50 430 250 730 130 S 1100 70 1080 280 L 1080 -40 L -50 -40 Z" fill="url(#steel)" opacity=".6"/>
 <path d="M-50 510 C 220 600 470 400 700 510 S 1060 580 1080 460 L 1080 660 L -50 660 Z" fill="url(#steel)" opacity=".65"/>
 <path d="M650 270 h130 l26 26 v95" stroke="#1f8f78" stroke-width="1.5" fill="none" opacity=".5"/>
 <path d="M940 130 C 908 182 846 182 820 214 C 826 150 878 108 940 130 Z" fill="url(#steel)" stroke="#2bbd9e" stroke-width="1" stroke-opacity=".5"/>
</svg>
<div class=shell>
  <div class=main>
  <div class=left>
    <div class=emblem><svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="#3ad6b0" stroke-width="1.5" stroke-linecap="round"><rect x="3" y="4" width="18" height="6.2" rx="1.6"/><rect x="3" y="13.8" width="18" height="6.2" rx="1.6"/><circle cx="6.6" cy="7.1" r="1" fill="#3ad6b0" stroke="none"/><circle cx="6.6" cy="16.9" r="1" fill="#3ad6b0" stroke="none"/><line x1="10" y1="7.1" x2="17.5" y2="7.1"/><line x1="10" y1="16.9" x2="17.5" y2="16.9"/></svg></div>
    <h1>VPS ADMIN</h1>
    <div class=tag>Sua central de servidor · completa e pré-moldada</div>
    <div id=cfg style="margin-top:22px">
      <div class=origemtabs><button type=button class="otab on" id=otab-git onclick="origem('git')">⬇️ Do Git</button><button type=button class=otab id=otab-arq onclick="origem('arquivo')">📁 De arquivo</button></div>
      <div id=org-git>
        <label class=fld><span>Repo do código (privado)</span><input id=repo type=text value="https://github.com/diogobsbastos/vps-escola-parque-admin.git"></label>
        <label class=fld><span>Token do GitHub <small>(clona o repo privado + liga o deploy; fica só na VM)</small></span><input id=tok type=password placeholder="ghp_..."></label>
      </div>
      <div id=org-arq class=hide>
        <label class=fld><span>Arquivo do código <small>(.zip ou .tar.gz — do pendrive)</small></span></label>
        <div id=dropzone class=dropzone onclick="document.getElementById('arq').click()">
          <div class=dzicon>📁</div>
          <div id=dztxt>Arraste o arquivo aqui<br><small>ou clique para escolher · .zip / .tar.gz</small></div>
        </div>
        <input id=arq type=file accept=".zip,.tar.gz,.tgz" style="display:none">
        <div style="font-size:11px;color:#5f897e;margin:8px 0 10px">Sem Git, sem token — instala do arquivo local.</div>
      </div>
      <label class=fld><span>Provedor <small>(rótulo no painel)</small></span><input id=prov type=text value="GCP" placeholder="GCP / Oracle / Hetzner..."></label>
      <label class=fld><span>Domínio <small>(opcional; vazio = acesso por IP)</small></span><input id=dom type=text placeholder="meuapp.duckdns.org"></label>
    </div>
    <div style="text-align:right;margin-top:16px">
      <button class=gouni id=removerbtn onclick="removerTudo()">🗑️ Remover tudo</button>
      <span class=help title="Desinstala TODOS os serviços e zera esta VM, pra reinstalar do zero." onclick="alert('REMOVER TUDO: desinstala TODOS os serviços do framework e zera esta VM, pra você reinstalar do zero. NAO toca no codigo do GitHub nem nos seus backups.')">?</span>
    </div>
  </div>
  <div class=right>
    <div class=rhead><span id=rhead-txt>Componentes a instalar</span><span class=rhactions id=rhactions><button type=button onclick="marcarTodos(1)">MARCAR TODOS</button><button type=button onclick="marcarTodos(0)">LIMPAR</button></span></div>
    <div class=rbody>
      <div id=servidor class=srvcard><div class=srvload>🔍 Lendo o servidor…</div></div>
      <div id=pick>__CHECKBOXES__</div>
      <div id=uni class=hide><div style="border:1px solid rgba(224,107,107,.4);background:rgba(224,107,107,.08);border-radius:10px;padding:14px 16px;font-size:13px;color:#f3c0c0;line-height:1.7"><b style="color:#ff9b9b"><i class="ti ti-alert-triangle"></i> Isto remove TODO o framework desta VM</b><br>Para e apaga: painel, PostgreSQL, PostgREST, MCP, Gateway, Webhook, Sentinela, ntfy, Evolution, Backend Central, provisionador, rotas Nginx e o banco <code>evolution</code>.<br><span style="color:#9fb0a8">A VM volta <b>limpa, do zero</b>. O código no GitHub e teus backups <b>não</b> são tocados.</span></div></div>
      <div id=run class=hide><div class=steps id=steps></div><div class=log id=log></div></div>
    </div>
  </div>
  </div>
  <div class=footbar>
    <div class=prog><div class=prow><span id=sl>Pronto para instalar</span><span id=pct>0%</span></div><div class=track><div id=bar></div></div></div>
    <button class=go id=go onclick=start()>Instalar</button>
  </div>
</div>
<div id=modal class=modal>
  <div class=modalcard>
    <div class=modalicon><svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="#ef6b6b" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M10.3 4 1.9 18.4a2 2 0 0 0 1.7 3h16.8a2 2 0 0 0 1.7-3L13.7 4a2 2 0 0 0-3.4 0z"/><line x1="12" y1="9.5" x2="12" y2="13.5"/><circle cx="12" cy="17.2" r="0.7" fill="#ef6b6b" stroke="none"/></svg></div>
    <h3>Remover tudo desta VM?</h3>
    <p>Isto <b>para e apaga TODOS</b> os serviços (Postgres, painel, MCP, Gateway, Webhook, Sentinela, ntfy, Evolution, Backend Central), as rotas Nginx e o banco <code>evolution</code>. A VM volta <b>limpa, do zero</b>.</p>
    <p class=modalsafe>✔ O código no GitHub e seus backups NÃO são tocados.</p>
    <div class=modalbtns>
      <button class=mbcancel onclick="fecharModal()">Cancelar</button>
      <button class=mbok onclick="confirmarRemover()">Sim, remover tudo</button>
    </div>
  </div>
</div>
<script>
var KEY=new URLSearchParams(location.search).get("key")||"";var IP="__IP__";var MODO="instalar";var INSTALADO=__INSTALADO__;var ORIGEM="git";
function modo(m){MODO=m;
 document.getElementById('cfg').classList.toggle('hide',m=='desinstalar');
 document.getElementById('pick').classList.toggle('hide',m=='desinstalar');
 document.getElementById('uni').classList.toggle('hide',m!='desinstalar');
 document.getElementById('rhead-txt').textContent=m=='instalar'?'Componentes a instalar':'Remover tudo desta VM';
 var g=document.getElementById('go');g.textContent=m=='instalar'?'Instalar':'Remover tudo';g.className=m=='instalar'?'go':'go uni';}
function origem(m){ORIGEM=m;document.getElementById("otab-git").classList.toggle("on",m=="git");document.getElementById("otab-arq").classList.toggle("on",m=="arquivo");document.getElementById("org-git").classList.toggle("hide",m=="arquivo");document.getElementById("org-arq").classList.toggle("hide",m=="git");}
function marcarTodos(v){[].slice.call(document.querySelectorAll('#pick input:not([disabled])')).forEach(function(x){x.checked=!!v;});}
function sel(){return [].slice.call(document.querySelectorAll('#pick input:checked')).map(function(x){return x.value;});}
function removerTudo(){document.getElementById('modal').classList.add('show');}
function fecharModal(){document.getElementById('modal').classList.remove('show');}
function confirmarRemover(){fecharModal();MODO='desinstalar';document.getElementById('rhead-txt').textContent='Removendo tudo…';start();}
function start(){var go=document.getElementById('go');go.disabled=true;
 document.getElementById('pick').classList.add('hide');document.getElementById('uni').classList.add('hide');document.getElementById('run').classList.remove('hide');var _sv=document.getElementById('servidor');if(_sv)_sv.classList.add('hide');var _ra=document.getElementById('rhactions');if(_ra)_ra.classList.add('hide');var _rb=document.getElementById('removerbtn');if(_rb)_rb.style.display='none';
 document.getElementById('rhead-txt').textContent=MODO=='instalar'?'Instalando…':'Removendo…';
 var payload={modo:MODO,componentes:sel(),origem:ORIGEM,token:(document.getElementById('tok')||{}).value||'',repo:(document.getElementById('repo')||{}).value||'',provedor:(document.getElementById('prov')||{}).value||'VPS',dominio:(document.getElementById('dom')||{}).value||''};
 if(MODO=='instalar'&&ORIGEM=='arquivo'){var fi=document.getElementById('arq');var f=(fi&&fi.files&&fi.files[0])||window._dropFile;
   if(!f){alert('Selecione o arquivo do codigo (.zip ou .tar.gz)');go.disabled=false;document.getElementById('run').classList.add('hide');document.getElementById('pick').classList.remove('hide');return;}
   document.getElementById('sl').textContent='Lendo arquivo '+f.name+'...';
   var rd=new FileReader();rd.onload=function(){payload.arquivo_b64=String(rd.result).split(',')[1];payload.arquivo_nome=f.name;enviar(payload);};rd.readAsDataURL(f);return;}
 enviar(payload);}
function enviar(payload){
 fetch('/start?key='+KEY,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
 var es=new EventSource('/progress?key='+KEY);
 es.onmessage=function(e){var d=JSON.parse(e.data);
   if(d.passos){render(d.passos);}
   if(d.tipo=='passo'){var el=document.getElementById('st-'+d.id);if(el){el.className='st '+(d.status=='ok'?'ok':d.status=='rodando'?'run':d.status=='erro'?'erro':'');
     el.querySelector('.ic').className='ic ti '+(d.status=='ok'?'ti-circle-check':d.status=='rodando'?'ti-loader-2':d.status=='erro'?'ti-alert-circle':'ti-circle');}}
   if(d.pct!=null){document.getElementById('bar').style.width=d.pct+'%';document.getElementById('pct').textContent=d.pct+'%';}
   if(d.tipo=='log'){var L=document.getElementById('log');L.textContent+=d.msg+'\\n';L.scrollTop=L.scrollHeight;document.getElementById('sl').textContent=d.msg.slice(0,54);}
   if(d.tipo=='fim'){es.close();var go=document.getElementById('go');go.disabled=false;
     if(d.fase=='ok'&&MODO=='desinstalar'){
       document.getElementById('sl').textContent='Remoção concluída — VM limpa';document.getElementById('rhead-txt').textContent='VM zerada ✓';
       go.textContent='↻ Recarregar p/ instalar';go.className='go';go.onclick=function(){location.reload();};INSTALADO=false;}
     else if(d.fase=='ok'){var dom=(document.getElementById('dom')||{}).value||'';var url=d.painel||(dom?('https://'+dom+'/admin/'):('http://'+IP+'/admin/'));var senha=d.senha||'';
       document.getElementById('sl').textContent='Concluído — ambiente no ar';document.getElementById('rhead-txt').textContent='Instalação concluída ✓';
       document.getElementById('run').innerHTML='<div class=successbox><div class=sok>✅ Instalação concluída!</div>'+
         '<div class=srow>🌐 Endereço do painel</div><div class=sval><a href="'+url+'" target=_blank>'+url+'</a></div>'+
         '<div class=srow>🔑 Senha do admin</div><div class=sval><span class=spw id=spw>'+senha+'</span> <button class=scopy id=scopybtn onclick="copiarSenha()">📋 copiar</button></div>'+
         '<div class=shint>⚠️ Guarde esta senha — ela é gerada só uma vez. Clique em <b>Entrar no painel</b> embaixo pra abrir.</div></div>';
       go.textContent='Entrar no painel →';go.className='go done';go.onclick=function(){window.open(url,'_blank');};INSTALADO=true;}
     else{go.textContent='Erro — ver log';go.className='go uni';}}
 };}
(function(){var dz=document.getElementById("dropzone"),arq=document.getElementById("arq"),txt=document.getElementById("dztxt");if(!dz)return;
function show(f){if(f)txt.innerHTML="✅ "+f.name+"<br><small>clique ou arraste pra trocar</small>";}
arq.addEventListener("change",function(){show(arq.files[0]);});
["dragover","dragenter"].forEach(function(ev){dz.addEventListener(ev,function(e){e.preventDefault();dz.classList.add("over");});});
["dragleave","drop"].forEach(function(ev){dz.addEventListener(ev,function(e){e.preventDefault();dz.classList.remove("over");});});
dz.addEventListener("drop",function(e){if(e.dataTransfer&&e.dataTransfer.files.length){try{arq.files=e.dataTransfer.files;}catch(_){}show(e.dataTransfer.files[0]);window._dropFile=e.dataTransfer.files[0];}});
})();
function copiarSenha(){var el=document.getElementById('spw');if(!el)return;var t=el.textContent;if(navigator.clipboard){navigator.clipboard.writeText(t);}var b=document.getElementById('scopybtn');if(b){b.textContent='✅ copiado';}}
function _syncRemover(){var rb=document.getElementById('removerbtn'),hp=document.querySelector('.help');var v=INSTALADO?'':'none';if(rb)rb.style.display=v;if(hp)hp.style.display=v;}
function corStatus(st){return st=="ativo"?"#2bbd9e":st=="inativo"?"#ef6b6b":"#52706a";}
function toggleSrv(){var d=document.getElementById("srvdet"),t=document.getElementById("srvtog");if(!d)return;var h=d.classList.toggle("hide");if(t)t.textContent=h?"ver tudo ▾":"ocultar ▴";}
function carregarServidor(){fetch("/inspecionar?key="+KEY).then(function(r){return r.json();}).then(function(d){
 var box=document.getElementById("servidor");if(!box)return;if(d.erro){box.innerHTML="";return;}
 var dots=d.itens.map(function(i){var c=corStatus(i.status);return "<span class=srvitem><span class=srvdot style='color:"+c+";background:"+c+"'></span>"+i.label+"</span>";}).join("");
 var idln="🖥 "+d.host+(d.provedor?" · "+d.provedor:"")+" · "+d.arch+(d.os?" · "+d.os:"");
 var resumo=d.instalado?("<b style='color:#2bbd9e'>"+d.ativos+"/"+d.total+"</b> serviços ativos"):("<b style='color:#e0b057'>VM limpa</b> — nada instalado ainda");
 box.innerHTML="<div class=srvtop><span class=srvttl>ESTE SERVIDOR</span><span class=srvtog id=srvtog onclick=toggleSrv()>ver tudo ▾</span></div>"+
   "<div class=srvid>"+idln+"</div>"+
   "<div class=srvsum>"+resumo+(d.ip?" · <span style='color:#7fb8ac'>IP "+d.ip+"</span>":"")+(d.disco?" · disco "+d.disco:"")+"</div>"+
   "<div id=srvdet class='srvdet hide'>"+dots+"</div>";
}).catch(function(){var box=document.getElementById("servidor");if(box)box.innerHTML="";});}
carregarServidor();
_syncRemover();
function render(passos){var c=document.getElementById('steps');if(c.dataset.done)return;c.dataset.done=1;
 c.innerHTML=passos.map(function(p){return '<div class="st" id="st-'+p.id+'"><i class="ti '+p.icon+'"></i><span>'+p.label+'</span><i class="ic ti ti-circle"></i></div>';}).join('');}
</script></body></html>""".replace("__CHECKBOXES__", checkboxes_html()).replace("__IP__", IP_PUB).replace("__INSTALADO__", "true" if _framework_instalado() else "false").replace("__VERSAO__", VERSAO).replace("__BUILD__", _BUILD)


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
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/estado":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            with LOCK:
                self.wfile.write(json.dumps(ESTADO).encode())
        elif path == "/inspecionar":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(json.dumps(inspecionar()).encode())
            except Exception as e:
                self.wfile.write(json.dumps({"erro": str(e)}).encode())
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
