"""
VPS ADMIN v2.0 — Central de gestao do servidor (estilo "mini Locaweb")
======================================================================
Menu lateral multipaginas. Roda em http://IP/admin (porta interna 8500).

Seguranca (por design):
- Senha obrigatoria (~/.vps_admin_pass, chmod 600)
- SEM terminal livre: acoes limitadas a whitelist de servicos/acoes
- Restart/Stop via sudoers NOPASSWD especifico (/etc/sudoers.d/vpsadmin)
- Apps novos registrados em ~/.vps_admin_apps.json (sem editar codigo)

Este painel e a BASE replicavel para outras VPS (outros clientes):
basta clonar a pasta + rodar o SETUP_SERVIDOR.md em cada servidor novo.

Autor: Diogo + Claude (Mentor) — 06/2026
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import requests
import streamlit as st

try:
    import psutil
except ImportError:
    psutil = None

# ============================================================
# Config
# ============================================================

st.set_page_config(page_title="VPS Admin", page_icon="🛠️", layout="wide")

SENHA_PATH = Path.home() / ".vps_admin_pass"
USER_PATH = Path.home() / ".vps_admin_user.json"
APPS_PATH = Path.home() / ".vps_admin_apps.json"
OLLAMA_URL = "http://localhost:11434"
NGINX_CONF = Path("/etc/nginx/sites-available/apps")
# -----------------------------------------------------------------
# FONTE ÚNICA DE VERDADE (estilo WordPress "Site URL"):
# ~/.vps_config.json define ip/dominio UMA vez e o painel inteiro deriva.
# Em servidor novo: criar esse arquivo e NADA aqui precisa ser editado.
#   {"ip": "1.2.3.4", "dominio": "meuserver.duckdns.org"}
# -----------------------------------------------------------------
CONFIG_PATH = Path.home() / ".vps_config.json"
try:
    _cfg = json.loads(CONFIG_PATH.read_text())
except Exception:
    _cfg = {}
import platform as _plat
def _ip_auto() -> str:
    try:
        import socket
        sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sk.connect(("8.8.8.8", 80)); ip = sk.getsockname()[0]; sk.close()
        return ip
    except Exception:
        return "127.0.0.1"
IP_PUBLICO = _cfg.get("ip") or _ip_auto()
DOMINIO = _cfg.get("dominio", "")                # vazio = acesso por IP (http)
# Fonte unica: tem dominio -> https; sem dominio -> http://IP (links apontam p/ ESTA maquina)
URL_BASE = f"https://{DOMINIO}" if DOMINIO else f"http://{IP_PUBLICO}"
PROVEDOR = _cfg.get("provedor", "VPS")           # ex.: "Oracle Cloud", "GCP"
ARCH_LABEL = _cfg.get("arch") or _plat.machine() # ex.: "aarch64", "x86_64"
PLANO_LABEL = _cfg.get("plano", "")              # ex.: "Always Free" (so Oracle)
ARCH_CURTA = "ARM" if "aarch" in ARCH_LABEL or "arm" in ARCH_LABEL.lower() else "x86"

USUARIO_PADRAO = {"nome": "Diogo Brandão", "email": "diogobsbastos@gmail.com"}

# Servicos do FRAMEWORK (iguais em qualquer instalacao). Apps do USUARIO vem do APPS_PATH (json).
SERVICOS_BASE: dict[str, str] = {
    "vpsadmin":            "🛠️ VPS Admin (este painel)",
    "backendcentral":      "🧠 Backend Central (worker)",
    "nginx":               "🚪 Nginx (porteiro/rotas)",
    "ollama":              "🦙 Ollama (LLM local)",
    "llmgateway":          "🔑 LLM Gateway (API com chave)",
    "vpsmcp":              "🔌 VPS-MCP (ponte do Claude)",
    "vpswebhook":          "🪝 Webhook (campainha do deploy)",
    "postgresql":          "🗄️ PostgreSQL 17 (banco interno)",
    "postgrest":           "🔗 PostgREST (API do banco — /rest/v1)",
    "ntfy":                "📨 ntfy (push de marca própria)",
    "evolution":           "💬 Evolution API (Zap Push)",
}

ACOES = ("restart", "stop", "start")

# Apps com interface web acessível (serviço -> rota). Infra (nginx/ollama/worker) fica de fora.
ROTAS_APPS: dict[str, str] = {
    "vpsadmin":     "/admin/",
}

# Apps em DOMÍNIO PRÓPRIO (serviço -> URL completa) — vem de config, vazio em VM nova
URLS_EXTERNAS_PATH = Path.home() / ".vps_urls_externas.json"
try:
    URLS_EXTERNAS: dict[str, str] = json.loads(URLS_EXTERNAS_PATH.read_text())
except Exception:
    URLS_EXTERNAS = {}


@st.cache_data(ttl=55, show_spinner=False)
def ler_metricas(horas: int) -> list[dict]:
    """Lê o histórico de métricas das últimas N horas (~/.vps_metricas.csv)."""
    import time as _t
    p = Path.home() / ".vps_metricas.csv"
    if not p.exists():
        return []
    corte = _t.time() - horas * 3600
    linhas = p.read_text().splitlines()[1:]
    out = []
    for ln in linhas:
        try:
            ts, cpu, ram, disco, load = ln.split(",")
            if float(ts) >= corte:
                out.append({"hora": _t.strftime("%d/%m %H:%M",
                                                _t.localtime(float(ts))),
                            "CPU %": float(cpu), "RAM %": float(ram),
                            "Disco %": float(disco), "Load": float(load)})
        except Exception:
            continue
    return out


@st.cache_data(ttl=300, show_spinner=False)
def evolution_dashboard_url() -> str:
    """Monta o link do dashboard da instância 'sentinela' descobrindo o id via API."""
    base = _cfg.get("zap_url") or (f"https://zap.{DOMINIO}" if DOMINIO else "")
    if not base:
        return ""
    try:
        key = (Path.home() / ".evolution_api_key").read_text().strip()
        rc, out = _run(["curl", "-s", "-m", "8",
                        base + "/instance/fetchInstances",
                        "-H", "apikey: " + key], timeout=12)
        dados = json.loads(out)
        for it in (dados if isinstance(dados, list) else []):
            inst = it.get("instance", it)
            if inst.get("instanceName") == "sentinela" or inst.get("name") == "sentinela":
                iid = inst.get("instanceId") or inst.get("id") or it.get("id")
                if iid:
                    return f"{base}/manager/instance/{iid}/dashboard"
    except Exception:
        pass
    return base + "/manager"


def url_acesso(nome: str) -> str:
    """URL de acesso do app (subpasta do domínio-mãe OU domínio próprio)."""
    if nome == "evolution":
        return evolution_dashboard_url()
    if nome == "ntfy" and DOMINIO:
        return f"https://ntfy.{DOMINIO}"
    if nome in URLS_EXTERNAS:
        return URLS_EXTERNAS[nome]
    if nome in ROTAS_APPS:
        return URL_BASE + ROTAS_APPS[nome]
    return ""


# ============================================================
# Helpers — sistema
# ============================================================

def _run(cmd: list[str], timeout: int = 25) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except Exception as e:  # noqa: BLE001
        return 1, f"erro: {e}"


def carregar_apps_extras() -> dict[str, str]:
    try:
        return json.loads(APPS_PATH.read_text())
    except Exception:
        return {}


def salvar_apps_extras(apps: dict[str, str]) -> bool:
    try:
        APPS_PATH.write_text(json.dumps(apps, ensure_ascii=False, indent=2))
        return True
    except Exception:
        return False


def todos_servicos() -> dict[str, str]:
    return {**SERVICOS_BASE, **carregar_apps_extras()}


def status_servico(nome: str) -> str:
    _, out = _run(["systemctl", "is-active", nome])
    return out.splitlines()[0] if out else "?"


def acao_servico(nome: str, acao: str) -> tuple[bool, str]:
    if nome not in todos_servicos() or acao not in ACOES:
        return False, "acao ou servico fora da whitelist"
    rc, out = _run(["sudo", "-n", "/usr/bin/systemctl", acao, nome], timeout=60)
    return rc == 0, out or f"{acao} {nome}: ok"


def logs_servico(nome: str, linhas: int = 80) -> str:
    if nome not in todos_servicos():
        return "servico fora da whitelist"
    _, out = _run(["journalctl", "-u", nome, "-n", str(linhas), "--no-pager", "-o", "short-iso"])
    return out or "(sem logs)"


def ollama_modelos() -> list[dict]:
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=4)
        return r.json().get("models", [])
    except Exception:
        return []


def config_salvar(chave: str, valor) -> None:
    """Grava uma opção na fonte única de verdade (~/.vps_config.json)."""
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
    except Exception:
        cfg = {}
    cfg[chave] = valor
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
    except Exception:
        pass


def ollama_manter_na_ram(modelo: str, ligar: bool) -> bool:
    """ligar=True: carrega o modelo JÁ e fixa por 24h. ligar=False: descarrega JÁ."""
    try:
        r = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": modelo, "keep_alive": "24h" if ligar else 0},
            timeout=300,
        )
        return r.ok
    except Exception:
        return False


def ollama_show(nome: str) -> dict:
    """Ficha tecnica completa do modelo (API /api/show do Ollama)."""
    try:
        r = requests.post(f"{OLLAMA_URL}/api/show", json={"name": nome}, timeout=10)
        return r.json()
    except Exception:
        return {}


def ollama_tamanho_remoto(modelo_tag: str) -> str:
    """Tamanho REAL do download, consultado no registro oficial (sem baixar nada)."""
    try:
        nome, _, tag = modelo_tag.partition(":")
        tag = tag or "latest"
        r = requests.get(
            f"https://registry.ollama.ai/v2/library/{nome}/manifests/{tag}",
            headers={"Accept": "application/vnd.docker.distribution.manifest.v2+json"},
            timeout=10,
        )
        m = r.json()
        total = sum(c.get("size", 0) for c in m.get("layers", []))
        total += (m.get("config", {}) or {}).get("size", 0)
        return f"{total/1e9:.1f} GB" if total > 0 else "?"
    except Exception:
        return "?"


CATALOGO_PATH = Path.home() / ".vps_admin_ollama_catalog.json"

# Fallback se nunca atualizou a lista (populares, com tamanho aproximado)
CATALOGO_POPULAR = [
    {"nome": "qwen2.5:7b", "tamanho": "4.7 GB"},
    {"nome": "qwen2.5:14b", "tamanho": "9.0 GB"},
    {"nome": "qwen2.5vl:7b", "tamanho": "6.0 GB"},
    {"nome": "llama3.2:3b", "tamanho": "2.0 GB"},
    {"nome": "llama3.1:8b", "tamanho": "4.9 GB"},
    {"nome": "gemma3:4b", "tamanho": "3.3 GB"},
    {"nome": "mistral:7b", "tamanho": "4.1 GB"},
    {"nome": "phi4:14b", "tamanho": "9.1 GB"},
    {"nome": "deepseek-r1:7b", "tamanho": "4.7 GB"},
    {"nome": "llava:7b", "tamanho": "4.7 GB"},
    {"nome": "nomic-embed-text:latest", "tamanho": "0.3 GB"},
]


def catalogo_ollama() -> list[dict]:
    try:
        itens = json.loads(CATALOGO_PATH.read_text())
        if itens and isinstance(itens[0], str):
            # cache no formato antigo (so nomes) -> converte; tamanhos vem no proximo 🔄
            itens = [{"nome": f"{n}:latest", "tamanho": "?"} for n in itens]
        if itens and isinstance(itens[0], dict) and "nome" in itens[0]:
            return itens
        return CATALOGO_POPULAR
    except Exception:
        return CATALOGO_POPULAR


def atualizar_catalogo_ollama(barra=None) -> tuple[bool, int]:
    """Busca rapido a lista de modelos da biblioteca oficial (1 request).
    Tamanho exato nao e exposto de forma confiavel pelo site -> mostramos '—'
    no catalogo (os POPULARES tem tamanho curado; instalados tem tamanho real)."""
    import re as _re
    try:
        r = requests.get("https://ollama.com/library", timeout=20)
        nomes = sorted(set(_re.findall(r'href="/library/([a-z0-9\-\.]+)"', r.text)))
        if not nomes:
            return False, 0
        curados = {it["nome"].split(":")[0]: it for it in CATALOGO_POPULAR}
        itens: list[dict] = list(CATALOGO_POPULAR)  # populares com tamanho real no topo
        for n in nomes:
            if n not in curados:
                itens.append({"nome": f"{n}:latest", "tamanho": "—"})
        CATALOGO_PATH.write_text(json.dumps(itens))
        return True, len(itens)
    except Exception:
        return False, 0


def rotas_nginx() -> list[str]:
    try:
        texto = NGINX_CONF.read_text()
    except Exception:
        return []
    rotas = []
    for linha in texto.splitlines():
        s = linha.strip()
        if s.startswith("location") and "{" in s:
            rotas.append(s.split("{")[0].replace("location", "").strip())
    return rotas


def dominios_nginx() -> list[dict]:
    """Varre /etc/nginx/sites-enabled e extrai: dominio, destino e se tem SSL."""
    import glob as _g
    achados: dict[str, dict] = {}
    for f in sorted(_g.glob("/etc/nginx/sites-enabled/*")):
        try:
            txt = Path(f).read_text()
        except Exception:
            continue
        for bloco in txt.split("server {")[1:]:
            nome, alvo, ssl = "", "", False
            for ln in bloco.splitlines():
                ls = ln.strip()
                if ls.startswith("server_name") and not nome:
                    nome = ls.replace("server_name", "").strip(" ;")
                if "listen 443" in ls:
                    ssl = True
                if "proxy_pass" in ls and not alvo:
                    alvo = ls.split("proxy_pass")[1].strip(" ;")
            if not nome or nome == "_":
                continue
            atual = achados.get(nome, {"dominio": nome, "alvo": "", "ssl": False,
                                       "arquivo": Path(f).name})
            atual["ssl"] = atual["ssl"] or ssl
            if alvo and not atual["alvo"]:
                atual["alvo"] = alvo
            achados[nome] = atual
    return list(achados.values())


PORTAS_SERVICOS = {  # porta interna -> servico (traduz destinos do nginx p/ nome de app)
    "8500": "vpsadmin", "8501": "escolaparque", "8502": "sertanejolab",
    "3000": "innovafront", "8600": "llmgateway", "8700": "vpsmcp", "11434": "ollama",
}


def alvo_amigavel(alvo: str) -> str:
    """Converte 'http://127.0.0.1:3000' em '🚀 Innova Front (porta 3000)'."""
    import re as _re2
    m = _re2.search(r":(\d+)", alvo or "")
    svc = PORTAS_SERVICOS.get(m.group(1)) if m else None
    if svc:
        return f"{todos_servicos().get(svc, svc)} · porta {m.group(1)}"
    return alvo or "(rotas internas abaixo)"


@st.cache_data(ttl=600, show_spinner=False)
def cert_validade_cache(dominio: str) -> str | None:
    return cert_validade(dominio)


@st.cache_data(ttl=300, show_spinner=False)
def listar_bibliotecas() -> dict[str, list[dict]]:
    """Bibliotecas instaladas em CADA app (varre os venvs de /home/ubuntu/*/.venv).
    Cache de 5 min — apps novos aparecem sozinhos."""
    import glob
    res: dict[str, list[dict]] = {}
    for venv in sorted(glob.glob("/home/ubuntu/*/.venv")):
        app_nome = Path(venv).parent.name
        rc, out = _run([f"{venv}/bin/pip", "list", "--format=json",
                        "--disable-pip-version-check"], timeout=90)
        try:
            res[app_nome] = json.loads(out) if rc == 0 else []
        except Exception:
            res[app_nome] = []

    # apps Node (ex.: frontend Next.js): o que está REALMENTE instalado
    for pkg in sorted(glob.glob("/home/ubuntu/*/package.json")):
        pasta = Path(pkg).parent
        if not (pasta / "node_modules").is_dir():
            continue
        vistos: dict[str, str] = {}
        try:  # package-lock = inventário exato (inclui dependências indiretas)
            lock = json.loads((pasta / "package-lock.json").read_text())
            for cam, info in (lock.get("packages") or {}).items():
                if "node_modules/" not in cam:
                    continue
                nome_p = cam.split("node_modules/")[-1]
                vistos.setdefault(nome_p, (info or {}).get("version", "?"))
        except Exception:  # sem lock: varre o node_modules na unha
            for d in sorted((pasta / "node_modules").glob("*")):
                alvos = sorted(d.glob("*")) if d.name.startswith("@") else [d]
                for a in alvos:
                    try:
                        meta = json.loads((a / "package.json").read_text())
                        vistos.setdefault(
                            (f"{d.name}/{a.name}" if d.name.startswith("@")
                             else a.name), meta.get("version", "?"))
                    except Exception:
                        continue
        res[f"{pasta.name} (node)"] = [
            {"name": k, "version": v} for k, v in sorted(vistos.items())]
    return res


@st.cache_data(ttl=600, show_spinner=False)
def stack_node(pasta: str) -> str:
    """Resumo da stack de um app Node (deps principais do package.json)."""
    try:
        dep = json.loads((Path(pasta) / "package.json").read_text()
                         ).get("dependencies", {}) or {}
    except Exception:
        return ""
    princ = ["next", "react", "drizzle-orm", "@supabase/supabase-js",
             "tailwindcss", "typescript", "zod"]
    itens = [f"{p.split('/')[-1]} {str(dep[p]).lstrip('^~')}"
             for p in princ if p in dep]
    resto = len(dep) - sum(1 for p in princ if p in dep)
    if not itens:
        return ""
    return "🧩 " + " · ".join(itens) + (f" · +{resto} libs" if resto > 0 else "")


STACK_SERVICO = {  # servico -> pasta do app Node (mostra a stack no Dashboard)
    "innovafront": "/home/ubuntu/innova-front",
}


# ============================================================
# Helpers — usuario
# ============================================================

def carregar_usuario() -> dict:
    try:
        return {**USUARIO_PADRAO, **json.loads(USER_PATH.read_text())}
    except Exception:
        return dict(USUARIO_PADRAO)


def salvar_usuario(dados: dict) -> bool:
    try:
        USER_PATH.write_text(json.dumps(dados, ensure_ascii=False, indent=2))
        USER_PATH.chmod(0o600)
        return True
    except Exception:
        return False


def _mascarar_email(email: str) -> str:
    try:
        u, dom = email.split("@", 1)
        return f"{u[:3]}{'*' * max(len(u) - 3, 2)}@{dom}"
    except Exception:
        return "***"


API_KEYS_PATH = Path.home() / ".vps_admin_api_keys.json"
API_USAGE_PATH = Path.home() / ".vps_admin_api_usage.json"


def carregar_api_keys() -> list[dict]:
    try:
        return json.loads(API_KEYS_PATH.read_text()).get("keys", [])
    except Exception:
        return []


def salvar_api_keys(keys: list[dict]) -> bool:
    try:
        API_KEYS_PATH.write_text(json.dumps({"keys": keys}, ensure_ascii=False, indent=2))
        API_KEYS_PATH.chmod(0o600)
        return True
    except Exception:
        return False


def carregar_uso_api() -> dict:
    try:
        return json.loads(API_USAGE_PATH.read_text())
    except Exception:
        return {}


def gerar_api_key() -> str:
    import secrets
    return "sk-vps-" + secrets.token_hex(24)


def gateway_online() -> bool:
    try:
        return requests.get("http://localhost:8600/health", timeout=3).ok
    except Exception:
        return False


MCP_TOKEN_PATH = Path.home() / ".vps_mcp_token"


def mcp_token_atual() -> str:
    try:
        return MCP_TOKEN_PATH.read_text().strip()
    except Exception:
        return ""


def mcp_gerar_token() -> str:
    import secrets
    tok = secrets.token_urlsafe(32)
    try:
        MCP_TOKEN_PATH.write_text(tok)
        MCP_TOKEN_PATH.chmod(0o600)
        return tok
    except Exception:
        return ""


def cert_validade(dominio: str) -> str | None:
    """Lê a validade do certificado HTTPS direto do handshake TLS."""
    import socket
    import ssl
    try:
        ctx = ssl.create_default_context()
        with ctx.wrap_socket(socket.create_connection((dominio, 443), 5),
                             server_hostname=dominio) as s:
            exp = s.getpeercert().get("notAfter")  # ex: 'Sep  2 03:14:00 2026 GMT'
            return exp
    except Exception:
        return None


def mcp_ping_fluxo() -> list[tuple[bool, str]]:
    """Rastreia o fluxo do MCP etapa por etapa (como um log): serviço → porta → rota → mundo."""
    passos: list[tuple[bool, str]] = []
    _, ativo = _run(["systemctl", "is-active", "vpsmcp"])
    ativo = (ativo or "").strip()
    passos.append((ativo == "active", f"Serviço `vpsmcp` (systemd) → `{ativo or '?'}`"))
    try:
        code = requests.get("http://localhost:8700/mcp", timeout=4).status_code
    except Exception:
        code = None
    passos.append((code == 406,
                   f"Servidor MCP local `127.0.0.1:8700/mcp` → HTTP {code} *(406 = vivo)*"))
    token = mcp_token_atual()
    try:
        rota_ok = bool(token) and f"mcp-{token}" in NGINX_CONF.read_text()
    except Exception:
        rota_ok = False
    passos.append((rota_ok, "Rota no Nginx `/mcp-<token>/` → "
                            + ("encontrada" if rota_ok else "NÃO encontrada")))
    ext = None
    if token:
        try:
            ext = requests.post(
                f"{URL_BASE}/mcp-{token}/mcp",
                headers={"Content-Type": "application/json",
                         "Accept": "application/json, text/event-stream"},
                json={"jsonrpc": "2.0", "method": "initialize",
                      "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                 "clientInfo": {"name": "painel-ping", "version": "1"}},
                      "id": 1},
                timeout=10,
            ).status_code
        except Exception:
            ext = None
    passos.append((ext == 200,
                   f"Ponta a ponta `https://{DOMINIO}/mcp-…/mcp` (POST initialize, "
                   f"igual ao Claude) → HTTP {ext} *(200 = mundo conectado)*"))
    return passos


def ping_api_key(key: str, modelo: str | None) -> tuple[bool, str]:
    """Teste de fogo da chave: passa pelo gateway (valida a key) e faz a LLM responder."""
    try:
        r = requests.post(
            "http://localhost:8600/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": modelo or "qwen2.5:14b",
                "messages": [{"role": "user", "content": "Responda apenas: pong"}],
                "max_tokens": 10,
            },
            timeout=90,
        )
        if r.status_code == 200:
            txt = r.json()["choices"][0]["message"]["content"].strip()
            return True, txt[:60]
        return False, f"HTTP {r.status_code}: {r.text[:120]}"
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:120]


def mcp_online() -> bool:
    try:
        # FastMCP responde no /mcp; qualquer status < 500 = de pé
        return requests.get("http://localhost:8700/mcp", timeout=3).status_code < 500
    except Exception:
        return False


# ============================================================
# Helpers — Git & Deploys (ponte GitHub -> producao)
# ============================================================

GIT_USER = _cfg.get("github_user", "diogobsbastos")
GIT_STATE_PATH = Path.home() / ".vps_git_state.json"

# Mapa de cada projeto: o que vem do repo -> onde vive em producao.
# Projetos Git vem do ~/.vps_git_projetos.json (o "➕ Novo App" registra).
# VAZIO por padrao: VM nova nasce sem projetos chumbados (sem risco de sobrescrever).
GIT_PROJETOS: dict[str, dict] = {}


ABAS_CSS = (
    "<style>button[data-baseweb='tab'] p{font-size:1.0rem;font-weight:600;}"
    "button[data-baseweb='tab']{padding:0.7rem 1.6rem;min-height:2.8rem;"
    "justify-content:center;text-align:center;"
    "border-radius:10px 10px 0 0;margin-right:1px;}"
    "div[data-baseweb='tab-list']{gap:0.15rem;}"
    "button[data-baseweb='tab']:hover{background:#f3f4f6;}"
    "button[data-baseweb='tab'][aria-selected='true']{background:#fef2f2;}"
    "</style>"
)

GIT_PROJ_PATH = Path.home() / ".vps_git_projetos.json"


def git_projetos_extras() -> dict:
    try:
        return json.loads(GIT_PROJ_PATH.read_text())
    except Exception:
        return {}


def salvar_git_projetos(extras: dict) -> bool:
    try:
        GIT_PROJ_PATH.write_text(json.dumps(extras, ensure_ascii=False, indent=2))
        return True
    except Exception:
        return False


def todos_git_projetos() -> dict:
    return {**GIT_PROJETOS, **git_projetos_extras()}


def git_situ_curta(repo: str, conf: dict) -> str:
    """Resumo 🟢/🟠 do sync GitHub x producao (p/ Dashboard e Aplicativos)."""
    remoto = git_remote_head(repo)
    local = git_estado().get(repo, {}).get("commit", "—")
    if conf.get("pull"):
        _, _h = _run(["git", "-C", conf["pull"], "rev-parse", "--short=10", "HEAD"])
        if _h and "fatal" not in _h.lower():
            local = _h.strip()
    if remoto == "?":
        return "🟡 GitHub?"
    if local == "—":
        return "⚪ sem deploy"
    if remoto == local:
        return "🟢 em dia"
    return "🟠 update disponível!"


def autodeploy_proximo() -> int | None:
    """Segundos ate a proxima ronda do vigia. -1 = vigia TRABALHANDO agora.
    None = timer nao instalado. (Timer monotonico -> ler via list-timers.)"""
    _, ativo = _run(["systemctl", "is-active", "vpsautodeploy.service"], timeout=5)
    if (ativo or "").strip() == "active":
        return -1
    rc, out = _run(["systemctl", "list-timers", "vpsautodeploy.timer",
                    "--no-pager", "--no-legend"], timeout=5)
    out = (out or "").strip()
    if rc != 0 or not out:
        return None
    from datetime import datetime
    try:
        partes = out.split()  # ['Thu','2026-06-04','04:04:48','-03','1min','17s','left',...]
        alvo = datetime.strptime(partes[1] + " " + partes[2], "%Y-%m-%d %H:%M:%S")
        return max(0, int((alvo - datetime.now()).total_seconds()))
    except Exception:
        return None


@st.cache_data(ttl=60, show_spinner=False)
def git_remote_head(repo: str) -> str:
    """Ultimo commit no GitHub (sem clonar). Cache 60s."""
    rc, out = _run(["env", "GIT_TERMINAL_PROMPT=0", "git", "ls-remote",
                    f"https://github.com/{GIT_USER}/{repo}.git", "HEAD"], timeout=20)
    return out.split()[0][:10] if rc == 0 and out and "fatal" not in out else "?"


def git_estado() -> dict:
    try:
        return json.loads(GIT_STATE_PATH.read_text())
    except Exception:
        return {}


GIT_HIST_PATH = Path.home() / ".vps_git_historico.json"


def git_hist_add(repo: str, commit: str, origem: str) -> None:
    """Registra um deploy no histórico (mantém os 100 últimos)."""
    try:
        hist = json.loads(GIT_HIST_PATH.read_text()) if GIT_HIST_PATH.exists() else []
    except Exception:
        hist = []
    hist.append({"repo": repo, "commit": commit,
                 "quando": time.strftime("%Y-%m-%d %H:%M"), "origem": origem,
                 "status": "✅ ok"})
    try:
        GIT_HIST_PATH.write_text(json.dumps(hist[-100:], ensure_ascii=False, indent=1))
    except Exception:
        pass


def git_hist_ler() -> list:
    try:
        return json.loads(GIT_HIST_PATH.read_text())
    except Exception:
        return []


def webhook_ativo() -> bool:
    """True se o receptor push->deploy (vpswebhook) está no ar."""
    _, out = _run(["systemctl", "is-active", "vpswebhook.service"], timeout=5)
    return (out or "").strip() == "active"


def webhook_ultimo_push() -> str:
    """Último PUSH recebido pelo webhook (via journal). '' se nenhum."""
    rc, out = _run(["journalctl", "-u", "vpswebhook", "-n", "300",
                    "--no-pager", "-o", "short-iso"], timeout=8)
    if rc != 0 or not out:
        return ""
    for ln in reversed(out.splitlines()):
        if "PUSH " in ln:
            try:
                quando = ln.split()[0][:16].replace("T", " ")
                resto = ln.split("PUSH ", 1)[1].split(" -> ")[0]
                return f"`{resto}` · {quando}"
            except Exception:
                return ln[-80:]
    return ""


def _gh_api(metodo: str, rota: str, corpo: dict | None = None) -> tuple[int, object]:
    """Chamada crua na API do GitHub com o token do servidor."""
    import requests
    try:
        tok = (Path.home() / ".github_token").read_text().strip()
    except Exception:
        return 0, {"message": "sem ~/.github_token"}
    try:
        r = requests.request(metodo, "https://api.github.com" + rota,
                             headers={"Authorization": "Bearer " + tok,
                                      "Accept": "application/vnd.github+json"},
                             json=corpo, timeout=12)
        return r.status_code, (r.json() if r.text else {})
    except Exception as e:  # noqa: BLE001
        return 0, {"message": str(e)}


def webhook_url_atual() -> str:
    """URL pública da campainha deste servidor ('' se kit não instalado)."""
    try:
        rota = (Path.home() / ".vps_webhook_rota").read_text().strip()
    except Exception:
        return ""
    return f"{URL_BASE}/{rota}/" if rota else ""


@st.cache_data(ttl=300, show_spinner=False)
def gh_hook_do_repo(repo: str) -> tuple[int | None, str, int]:
    """(id, url, status_http) do webhook de deploy do repo (url com /hook-)."""
    sc, hooks = _gh_api("GET", f"/repos/{GIT_USER}/{repo}/hooks")
    if sc != 200 or not isinstance(hooks, list):
        return None, "", sc
    for h in hooks:
        u = (h.get("config", {}) or {}).get("url", "")
        if "/hook-" in u:
            return h.get("id"), u, sc
    return None, "", sc


def gh_hook_sincronizar(repo: str) -> str:
    """Cria ou aponta o webhook do repo para a campainha ATUAL."""
    alvo = webhook_url_atual()
    if not alvo:
        return "⚠️ kit do webhook não instalado neste servidor"
    try:
        segredo = (Path.home() / ".vps_webhook_secret").read_text().strip()
    except Exception:
        return "⚠️ sem ~/.vps_webhook_secret"
    cfg_h = {"url": alvo, "content_type": "json", "secret": segredo}
    hid, hurl, _ = gh_hook_do_repo(repo)
    if hid:
        sc, _r = _gh_api("PATCH", f"/repos/{GIT_USER}/{repo}/hooks/{hid}",
                         {"config": cfg_h, "events": ["push"], "active": True})
        ok = sc == 200
        msg = ("🟢 segredo/URL renovados" if hurl == alvo
               else "🔁 apontado pra campainha NOVA")
        return msg if ok else f"erro {sc}"
    sc, _r = _gh_api("POST", f"/repos/{GIT_USER}/{repo}/hooks",
                     {"config": cfg_h, "events": ["push"], "active": True})
    return "🟢 conectado" if sc == 201 else f"erro {sc}"


KIT_CAMPAINHA = r"""set -e
# 1) segredos da campainha (gerados AQUI no servidor, nunca em chat/repo)
test -s ~/.vps_webhook_secret || { openssl rand -hex 24 > ~/.vps_webhook_secret; chmod 600 ~/.vps_webhook_secret; }
test -s ~/.vps_webhook_rota   || { echo "hook-$(openssl rand -hex 8)" > ~/.vps_webhook_rota; chmod 600 ~/.vps_webhook_rota; }

# 2) servico systemd (o webhook.py ja vem com o painel, via git)
sudo tee /etc/systemd/system/vpswebhook.service >/dev/null <<'EOF'
[Unit]
Description=VPS Webhook (push->deploy estilo Vercel)
After=network.target

[Service]
User=ubuntu
ExecStart=/usr/bin/python3 /home/ubuntu/vps-admin/webhook.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload && sudo systemctl enable --now vpswebhook

# 3) rota secreta no Nginx (server 443, sites-AVAILABLE)
HOOKPATH=$(cat ~/.vps_webhook_rota)
sudo HOOKPATH="$HOOKPATH" python3 - <<'PYNGINX'
import os
p = "/etc/nginx/sites-available/apps"
rota = os.environ["HOOKPATH"]
conf = open(p).read()
if f"/{rota}/" not in conf:
    out, ok = [], False
    for ln in conf.splitlines(keepends=True):
        out.append(ln)
        if not ok and "listen 443" in ln:
            out.append(f"    location /{rota}/ {{ proxy_pass http://127.0.0.1:8800/; }}\n")
            ok = True
    assert ok, "listen 443 nao encontrado"
    open(p, "w").write("".join(out))
PYNGINX
sudo nginx -t && sudo systemctl reload nginx
echo "campainha instalada — volte ao painel e clique 🔁"
"""


@st.dialog("🪝 Webhook — a campainha do push→deploy", width="large")
def dialog_webhook() -> None:
    st.caption(
        "A campainha é **uma só por servidor** (rota secreta + segredo HMAC). "
        "Cada repo do GitHub aponta pra ela — push = deploy em ~5s. "
        "Tudo aqui é feito **via API**, sem abrir o site do GitHub."
    )
    if st.toggle("📦 Roteiro: levar o framework pra OUTRO VPS / OUTRO GitHub",
                 key="rot_mig_dlg"):
        st.markdown(ROTEIRO_MIGRACAO)
        st.code(KIT_CAMPAINHA, language="bash")
        st.markdown(
            "**4️⃣ Religar:** painel novo → 🪝 Webhook → **🔁**. Os webhooks são "
            "criados **na conta nova, via API**. ✅"
        )
        st.divider()
    url_alvo = webhook_url_atual()
    if not url_alvo:
        st.warning("⚪ Campainha ainda NÃO instalada neste servidor — normal em "
                   "servidor recém-migrado. Cole o bloco abaixo no SSH (1x) e "
                   "reabra este popup.")
        st.code(KIT_CAMPAINHA, language="bash")
        return
    st.markdown(f"<small>📍 campainha deste servidor: `{url_alvo}` · serviço "
                f"{'🟢' if webhook_ativo() else '🔴'} `vpswebhook`</small>",
                unsafe_allow_html=True)
    if st.button("🔁 Conectar/atualizar TODOS os repos para esta campainha",
                 type="primary", use_container_width=True):
        for r in todos_git_projetos():
            st.write(f"`{r}`: {gh_hook_sincronizar(r)}")
        gh_hook_do_repo.clear()
    st.divider()
    for r in todos_git_projetos():
        hid, hurl, sc = gh_hook_do_repo(r)
        if st.session_state.get(f"whconf_{r}"):
            cw1, cw2, cw3 = st.columns([3.0, 1.5, 1.2],
                                       vertical_alignment="center")
            cw1.markdown(f"`{r}` · ⚠️ **desconectar do GitHub?**")
            if cw2.button("✔ Sim, desconectar", key=f"whsim_{r}",
                          type="primary", use_container_width=True):
                st.toast(f"{r}: {gh_hook_desconectar(r)}")
                st.session_state.pop(f"whconf_{r}", None)
                gh_hook_do_repo.clear()
                st.rerun(scope="fragment")
            if cw3.button("✖ Cancelar", key=f"whnao_{r}",
                          use_container_width=True):
                st.session_state.pop(f"whconf_{r}", None)
                st.rerun(scope="fragment")
            continue
        if sc != 200:
            situ_h = f"🟡 GitHub: {sc or 'sem acesso'} (token com permissão Webhooks?)"
        elif not hid:
            situ_h = "⚪ sem campainha (push NÃO avisa este servidor)"
        elif hurl == url_alvo:
            situ_h = "🟢 conectado nesta campainha"
        else:
            situ_h = "🟠 aponta pra OUTRA campainha (servidor antigo?)"
        cA, cB = st.columns([4.7, 1.3], vertical_alignment="center")
        cA.markdown(f"`{r}` · {situ_h}")
        if bool(hid) and hurl == url_alvo:
            if cB.button("✂️ Desconectar", key=f"whd_{r}",
                         use_container_width=True,
                         help="Pede confirmação. Remove o webhook no GitHub — "
                              "sobra só a ronda de 2 min."):
                st.session_state[f"whconf_{r}"] = True
                st.rerun(scope="fragment")
        else:
            if cB.button("🔗 Conectar", key=f"whc_{r}", type="primary",
                         use_container_width=True,
                         help="Cria/aponta o webhook deste repo pra campainha "
                              "deste servidor — via API."):
                st.toast(f"{r}: {gh_hook_sincronizar(r)}")
                gh_hook_do_repo.clear()
                st.rerun(scope="fragment")


DB_CRED = Path.home() / ".innova_db.json"
JWT_SECRET_PG = Path.home() / ".postgrest_jwt_secret"


def db_cred() -> dict:
    try:
        return json.loads(DB_CRED.read_text())
    except Exception:
        return {}


def psql_run(query: str, banco: str = "innova", papel: str = "admin",
             timeout: int = 30) -> tuple[bool, str]:
    """Roda SQL no Postgres local via psql (saida CSV). papel: admin|worker|app."""
    cred = db_cred()
    u = cred.get(papel) or cred.get("worker") or {}
    if not u:
        return False, "credenciais ~/.innova_db.json não encontradas (FASE 1?)"
    env = dict(os.environ, PGPASSWORD=u.get("pass", ""))
    try:
        r = subprocess.run(
            ["psql", "-X", "-v", "ON_ERROR_STOP=1", "-P", "pager=off", "--csv",
             "-h", cred.get("host", "127.0.0.1"),
             "-p", str(cred.get("port", 5432)),
             "-U", u.get("user", ""), "-d", banco, "-c", query],
            capture_output=True, text=True, timeout=timeout, env=env)
        ok = r.returncode == 0
        return ok, (r.stdout if ok else (r.stdout + r.stderr)).strip()
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def csv_linhas(texto: str) -> list[dict]:
    import csv as _csv
    import io
    try:
        return list(_csv.DictReader(io.StringIO(texto)))
    except Exception:
        return []


def jwt_banco(role: str, dias: int = 3650) -> str:
    """Chave JWT (HS256) do PostgREST — igual anon/service_role do Supabase."""
    import base64 as _b64
    import hashlib as _hl
    import hmac as _hmac
    try:
        sec = JWT_SECRET_PG.read_text().strip().encode()
    except Exception:
        return ""
    def b64(d):
        return _b64.urlsafe_b64encode(d).rstrip(b"=")
    h = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    p = b64(json.dumps({"role": role, "iss": "vps-interno",
                        "exp": int(time.time()) + dias * 86400}).encode())
    s = b64(_hmac.new(sec, h + b"." + p, _hl.sha256).digest())
    return (h + b"." + p + b"." + s).decode()


@st.dialog("☁️ Conectar Google Drive — service account", width="large")
def dialog_drive() -> None:
    st.markdown(
        "<small>**Uma vez só, no navegador do PC:** "
        "1️⃣ console.cloud.google.com → projeto novo (ex.: vps-backups) · "
        "2️⃣ APIs & Services → Library → **Google Drive API** → Enable · "
        "3️⃣ Credentials → Create credentials → **Service account** → criar · "
        "4️⃣ na service account → Keys → Add key → **JSON** (baixa o arquivo) · "
        "5️⃣ no SEU Drive: crie a pasta de backups e **compartilhe** com o "
        "e-mail da service account (xxx@…iam.gserviceaccount.com) como "
        "**Editor** · 6️⃣ copie o ID da pasta (URL: /folders/<b>ID</b>).</small>",
        unsafe_allow_html=True)
    sa_up = st.file_uploader("Chave JSON da service account", type=["json"],
                             key="sa_up_dlg")
    fid = st.text_input("ID da pasta do Drive (compartilhada com o robô)",
                        key="sa_fid_dlg",
                        placeholder="ex.: 1AbC2dEf3GhI4jKl5MnO6pQr")
    if st.button("🔌 Conectar Drive", type="primary",
                 use_container_width=True):
        if not sa_up:
            st.error("Suba o arquivo JSON primeiro.")
            return
        try:
            sa_p = Path.home() / ".gdrive_sa.json"
            sa_p.write_bytes(sa_up.getvalue())
            os.chmod(sa_p, 0o600)
            rclone_conf_gdrive(str(sa_p), fid or "")
            rc_t, out_t = _run(["rclone", "lsd", "gdrive:"], timeout=30)
            if rc_t == 0:
                st.success("✅ Drive conectado! Use destino `gdrive:` (raiz) "
                           "ou `gdrive:Subpasta` nos perfis.")
            else:
                st.error("rclone não conectou: " + (out_t or "?")[-300:])
        except Exception as e:  # noqa: BLE001
            st.error(f"falha: {e}")


@st.dialog("⬇ Exportar dumps", width="large")
def dialog_exportar() -> None:
    try:
        jobs_x = json.loads((Path.home() / ".vps_backup.json").read_text()
                            ).get("jobs", [])
    except Exception:
        jobs_x = []
    locais = {str(j.get("destino")) for j in jobs_x
              if str(j.get("destino", "")).startswith("/")}
    arqs = []
    for d in locais:
        dp = Path(d)
        if dp.exists():
            arqs += list(dp.glob("*.sql.gz")) + list(dp.glob("*.tgz"))
    arqs = sorted(arqs, key=lambda p: p.stat().st_mtime, reverse=True)[:100]
    if not arqs:
        st.info("Nenhum arquivo local ainda — rode um backup primeiro.")
        return
    import math as _math
    import re as _re
    grupos: dict = {}
    for a in arqs:
        _m = _re.search(r"(\d{4}-\d{2}-\d{2}_\d{4})", a.name)
        grupos.setdefault(_m.group(1) if _m else a.name, []).append(a)
    chaves = sorted(grupos, reverse=True)
    POR_PAG = 10
    total_pag = max(1, _math.ceil(len(chaves) / POR_PAG))
    c_cap, c_pag = st.columns([3.8, 1.2], vertical_alignment="center")
    c_cap.caption(f"{len(chaves)} execuções guardadas · cada uma gera o dump "
                  "do(s) banco(s) 🗄️ + o pacote de segredos 🔐 (recuperação "
                  "de desastre).")
    pag = (c_pag.number_input("Página", 1, total_pag, 1,
                              label_visibility="collapsed")
           if total_pag > 1 else 1)
    import base64 as _b64

    def _link_dl(a: Path) -> str:
        b64 = _b64.b64encode(a.read_bytes()).decode()
        return (f"<a download='{a.name}' "
                f"href='data:application/gzip;base64,{b64}' "
                f"style='text-decoration:none;background:#f3f4f6;"
                f"border:1px solid #d1d5db;border-radius:6px;"
                f"padding:2px 10px;color:#111827'>⬇</a>")

    _td = "padding:6px 8px;border-bottom:1px solid #e5e7eb;"
    _sep = "border-left:1px solid #d1d5db;"
    _az = "background:#eff6ff;"   # par Banco (azul clarinho)
    _am = "background:#fefce8;"   # par Segredos (amarelo clarinho)
    linhas_html = []
    for k in chaves[(int(pag) - 1) * POR_PAG: int(pag) * POR_PAG]:
        itens = grupos[k]
        dumps = [a for a in itens if a.name.endswith(".sql.gz")]
        confs = [a for a in itens if a.name.endswith(".tgz")]
        try:
            data_br = f"{k[8:10]}/{k[5:7]}/{k[0:4]} {k[11:13]}:{k[13:15]}"
        except Exception:
            data_br = k
        for i in range(max(len(dumps), 1)):
            cel_data = (f"<b>{data_br}</b>" if i == 0 else "")
            cel_d = lk_d = cel_c = lk_c = ""
            if i < len(dumps):
                _a = dumps[i]
                cel_d = (f"🗄️ <b>{_a.name.rsplit('_', 2)[0]}</b> · "
                         f"{max(1, _a.stat().st_size // 1024)} KB")
                lk_d = _link_dl(_a)
            if i == 0 and confs:
                _c = confs[0]
                cel_c = (f"🔐 segredos & configs · "
                         f"{max(1, _c.stat().st_size // 1024)} KB")
                lk_c = _link_dl(_c)
            linhas_html.append(
                f"<tr>"
                f"<td style='{_td}white-space:nowrap;width:130px'>{cel_data}</td>"
                f"<td style='{_td}{_sep}{_az}padding-left:12px;width:34%'>{cel_d}</td>"
                f"<td style='{_td}{_az}text-align:center;width:64px'>{lk_d}</td>"
                f"<td style='{_td}{_sep}{_am}padding-left:12px;width:34%'>{cel_c}</td>"
                f"<td style='{_td}{_am}text-align:center;width:64px'>{lk_c}</td>"
                f"</tr>")
    _th = ("padding:6px 8px;text-align:left;font-size:0.85em;color:#6b7280;"
           "border-bottom:2px solid #d1d5db;")
    st.markdown(
        "<table style='width:100%;border-collapse:collapse;font-size:0.92em'>"
        f"<tr><th style='{_th}width:130px'>Data</th>"
        f"<th style='{_th}{_sep}{_az}padding-left:12px;width:34%'>Banco</th>"
        f"<th style='{_th}{_az}text-align:center;width:64px'>Baixar</th>"
        f"<th style='{_th}{_sep}{_am}padding-left:12px;width:34%'>Segredos</th>"
        f"<th style='{_th}{_am}text-align:center;width:64px'>Baixar</th></tr>"
        + "".join(linhas_html) + "</table>",
        unsafe_allow_html=True)


@st.dialog("✏️ Editar canal de alerta", width="large")
def dialog_editar_canal(canal_id: str) -> None:
    AL_D = Path.home() / ".vps_alertas.json"
    try:
        cfg_d = json.loads(AL_D.read_text())
    except Exception:
        cfg_d = {}
    canais_d = cfg_d.get("canais", [])
    c = next((x for x in canais_d if x.get("id") == canal_id), None)
    if not c:
        st.error("Canal não encontrado.")
        return
    tipo = c.get("tipo")
    novos = dict(c)
    if tipo == "ntfy":
        novos["servidor"] = st.text_input("Servidor",
                                          value=c.get("servidor", "")).strip()
        novos["topico"] = st.text_input("Tópico",
                                        value=c.get("topico", "")).strip()
        novos["usuario"] = st.text_input("Usuário",
                                         value=c.get("usuario", "")).strip()
        s_n = st.text_input("Senha (vazio = manter atual)", type="password")
        if s_n.strip():
            novos["senha"] = s_n.strip()
    elif tipo == "whatsapp":
        novos["servidor"] = st.text_input("Servidor Evolution",
                                          value=c.get("servidor", "")).strip()
        novos["instancia"] = st.text_input("Instância",
                                           value=c.get("instancia",
                                                       "sentinela")).strip()
        k_n = st.text_input("API key (vazio = manter atual)", type="password")
        if k_n.strip():
            novos["apikey"] = k_n.strip()
        novos["numero"] = st.text_input("Número destino (55+DDD)",
                                        value=str(c.get("numero", ""))).strip()
    elif tipo == "webpush":
        novos["servidor"] = st.text_input("Servidor do app",
                                          value=c.get("servidor", "")).strip()
        s_wp = st.text_input("PUSH_SECRET (vazio = manter)", type="password")
        if s_wp.strip():
            novos["segredo"] = s_wp.strip()
    elif tipo == "telegram":
        t_n = st.text_input("Token do bot (vazio = manter)", type="password")
        if t_n.strip():
            novos["token"] = t_n.strip()
        novos["chat"] = st.text_input("Chat ID",
                                      value=str(c.get("chat", ""))).strip()
    elif tipo == "email":
        novos["usuario"] = st.text_input("Gmail (remetente)",
                                         value=c.get("usuario", "")).strip()
        s_e = st.text_input("Senha de app (vazio = manter)", type="password")
        if s_e.strip():
            novos["senha_app"] = s_e.strip().replace(" ", "")
        novos["para"] = st.text_input("Enviar para",
                                      value=c.get("para", "")).strip()
    if st.button("💾 Salvar canal", type="primary", use_container_width=True):
        c.update(novos)
        AL_D.write_text(json.dumps(cfg_d, ensure_ascii=False, indent=1))
        st.rerun()


@st.dialog("✏️ Editar perfil de backup", width="large")
def dialog_editar_backup(job_id: str) -> None:
    BK_CFG_D = Path.home() / ".vps_backup.json"
    try:
        cfg_d = json.loads(BK_CFG_D.read_text())
    except Exception:
        cfg_d = {"jobs": []}
    jobs_d = cfg_d.get("jobs", [])
    job = next((j for j in jobs_d if j.get("id") == job_id), None)
    if not job:
        st.error("Perfil não encontrado.")
        return
    DIAS_D = {1: "seg", 2: "ter", 3: "qua", 4: "qui", 5: "sex",
              6: "sáb", 7: "dom"}
    nome_e = st.text_input("Nome", value=job.get("nome", ""))
    ce1, ce2 = st.columns(2)
    hora_e = ce1.text_input("Horário (HH:MM)",
                            value=(job.get("horario")
                                   or str(job.get("hora", "03")) + ":30"),
                            placeholder="ex.: 09:15")
    ret_e = ce2.number_input("Guardar por (dias)", 1, 365,
                             int(job.get("manter_dias", 7)))
    dias_e = st.multiselect("Dias da semana", list(DIAS_D.values()),
                            default=[DIAS_D[d] for d in job.get("dias", [])
                                     if d in DIAS_D])
    ok_bd_e, out_bd_e = psql_run(
        "select datname from pg_database where not datistemplate "
        "and datname <> 'postgres' order by 1;", banco="postgres")
    _opts_bd_e = ([l["datname"] for l in csv_linhas(out_bd_e)]
                  if ok_bd_e else [])
    bancos_e = st.multiselect(
        "Bancos incluídos (vazio = TODOS, inclusive futuros)", _opts_bd_e,
        default=[b for b in job.get("bancos", []) if b in _opts_bd_e])
    dest_e = st.text_input("Destino (pasta local ou remote rclone)",
                           value=job.get("destino", ""))
    if st.button("💾 Salvar alterações", type="primary",
                 use_container_width=True):
        import re as _re2
        if not _re2.fullmatch(r"([01]\\d|2[0-3]):[0-5]\\d", hora_e.strip()):
            st.error("Horário inválido — use HH:MM (ex.: 09:15).")
            return
        job.update({
            "nome": nome_e.strip() or job.get("nome", ""),
            "horario": hora_e.strip(),
            "manter_dias": int(ret_e),
            "dias": [k for k, v in DIAS_D.items() if v in dias_e]
                    or job.get("dias", [1, 2, 3, 4, 5, 6, 7]),
            "bancos": bancos_e,
            "destino": dest_e.strip() or job.get("destino", "")})
        BK_CFG_D.write_text(json.dumps({"jobs": jobs_d},
                                       ensure_ascii=False, indent=1))
        st.rerun()


@st.dialog("🧰 Console SQL — Postgres local", width="large")
def dialog_console_sql() -> None:
    st.caption("Leitura liberada; escrita/DDL só com a chave 🔓 (cuidado: produção).")
    ok_b, out_b = psql_run("select datname from pg_database "
                           "where not datistemplate order by 1;", banco="postgres")
    _bancos = [l["datname"] for l in csv_linhas(out_b)] if ok_b else ["innova"]
    c_p, c_b = st.columns(2)
    papel = c_p.selectbox("Executar como", ["admin", "worker", "app"])
    bdsql = c_b.selectbox("Banco", _bancos)
    q = st.text_area("SQL", height=140, placeholder="select now();")
    escrita = st.toggle("🔓 Permitir escrita/DDL (INSERT/UPDATE/CREATE/DROP...)")
    if st.button("▶ Executar", type="primary", use_container_width=True):
        ql = (q or "").strip()
        if not ql:
            st.warning("Escreva um SQL primeiro.")
        elif not escrita and not ql.lower().lstrip("( ").startswith(
                ("select", "show", "explain", "with", "table ")):
            st.error("Console em modo LEITURA — ligue 🔓 pra rodar escrita/DDL.")
        else:
            ok_q, out_q = psql_run(ql, banco=bdsql, papel=papel, timeout=60)
            if ok_q:
                ls = csv_linhas(out_q)
                if ls:
                    st.dataframe(ls, use_container_width=True, hide_index=True)
                else:
                    st.code(out_q[:2000] or "(sem saída)", language="text")
                st.success(f"OK · {len(ls)} linha(s)")
            else:
                st.error(out_q[:1000])


def rclone_conf_gdrive(sa_path: str, folder_id: str) -> None:
    """Escreve/substitui o remote [gdrive] no rclone.conf (service account)."""
    conf_p = Path.home() / ".config" / "rclone" / "rclone.conf"
    conf_p.parent.mkdir(parents=True, exist_ok=True)
    txt = conf_p.read_text() if conf_p.exists() else ""
    blocos, atual = [], []
    for ln in txt.splitlines(keepends=True):
        if ln.startswith("["):
            if atual:
                blocos.append("".join(atual))
            atual = []
        atual.append(ln)
    if atual:
        blocos.append("".join(atual))
    blocos = [b for b in blocos if b.strip() and not b.startswith("[gdrive]")]
    novo = ("[gdrive]\ntype = drive\nscope = drive\n"
            f"service_account_file = {sa_path}\n")
    if folder_id.strip():
        novo += f"root_folder_id = {folder_id.strip()}\n"
    conf_p.write_text("".join(blocos) + ("\n" if blocos else "") + novo)


KIT_REST_LOCAL = r"""sudo tee /etc/nginx/sites-available/local-rest >/dev/null <<'EOF'
server {
    listen 127.0.0.1:8088;
    location /rest/v1/ { proxy_pass http://127.0.0.1:3001/; }
    location /db/ { proxy_pass http://127.0.0.1:3001/; }
}
EOF
sudo ln -sf /etc/nginx/sites-available/local-rest /etc/nginx/sites-enabled/local-rest
sudo nginx -t && sudo systemctl reload nginx
curl -s -o /dev/null -w "REST local: HTTP %{http_code}\n" http://127.0.0.1:8088/rest/v1/
"""


ROTEIRO_MIGRACAO = """
**0️⃣ GitHub novo** *(só se mudar de conta — ex.: cliente/sócio)* — crie a conta,
suba os repos do framework (`git push` a partir dos clones atuais) e gere um
**token fine-grained** (Settings → Developer settings → Fine-grained tokens)
marcando os repos do framework com permissões **Contents: Read/Write** e
**Webhooks: Read/Write**. No servidor novo:
`echo SEU_TOKEN > ~/.github_token && chmod 600 ~/.github_token`

**1️⃣ Painel no VPS novo** — receita `SETUP_SERVIDOR.md` (clone + venv +
systemd + Nginx + certbot). O `webhook.py` e o `criar_webhooks.sh` já vêm
junto com o painel, via git.

**2️⃣ Identidade do servidor** — crie `~/.vps_config.json`:
`{"ip": "IP_NOVO", "dominio": "novo.duckdns.org", "github_user": "CONTA_NOVA"}`
— o painel INTEIRO se adapta a partir desse arquivo (inclusive esta página).

**3️⃣ Campainha** — cole o kit abaixo no SSH do servidor novo (1x):
"""


def gh_hook_desconectar(repo: str) -> str:
    hid, _, _ = gh_hook_do_repo(repo)
    if not hid:
        return "não tinha campainha"
    sc, _r = _gh_api("DELETE", f"/repos/{GIT_USER}/{repo}/hooks/{hid}")
    return "✂️ desconectado" if sc == 204 else f"erro {sc}"


def git_deploy(repo: str, conf: dict) -> tuple[bool, str]:
    """Atualiza producao: modo 'pull' (pasta e clone) ou modo 'mapa' (clona e espalha)."""
    import shutil
    if conf.get("pull"):
        pasta = conf["pull"]
        rc, out = _run(["env", "GIT_TERMINAL_PROMPT=0", "git", "-C", pasta,
                        "pull", "--ff-only"], timeout=180)
        if rc != 0:
            return False, "pull falhou: " + out[-300:]
        if conf.get("build"):
            rc_b, out_b = _run(["bash", "-c", f"cd {pasta} && " + conf["build"]],
                               timeout=900)
            if rc_b != 0:
                return False, ("BUILD falhou — produção segue na versão anterior "
                               "(nada foi reiniciado): " + out_b[-300:])
        _, h = _run(["git", "-C", pasta, "rev-parse", "--short=10", "HEAD"])
        est = git_estado()
        est[repo] = {"commit": (h or "?").strip(), "quando": time.strftime("%Y-%m-%d %H:%M")}
        try:
            GIT_STATE_PATH.write_text(json.dumps(est, indent=2))
        except Exception:
            pass
        git_hist_add(repo, (h or "?").strip(), "painel (↻)")
        return True, (h or "?").strip()
    mapa = conf.get("mapa", {})
    tmp = f"/tmp/deploy-{repo}"
    shutil.rmtree(tmp, ignore_errors=True)
    rc, out = _run(["env", "GIT_TERMINAL_PROMPT=0", "git", "clone", "--depth", "1",
                    f"https://github.com/{GIT_USER}/{repo}.git", tmp], timeout=180)
    if rc != 0:
        return False, "clone falhou: " + out[-300:]
    _, h = _run(["git", "-C", tmp, "rev-parse", "--short=10", "HEAD"])
    erros = []
    for origem, destino in mapa.items():
        src, dst = Path(tmp) / origem.rstrip("/"), Path(destino)
        try:
            if origem.endswith("/"):
                for item in src.rglob("*"):
                    if item.is_file() and ".git" not in item.parts:
                        alvo = dst / item.relative_to(src)
                        alvo.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(item, alvo)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        except Exception as e:  # noqa: BLE001
            erros.append(f"{origem}: {e}")
    shutil.rmtree(tmp, ignore_errors=True)
    if erros:
        return False, " | ".join(erros)[:300]
    est = git_estado()
    est[repo] = {"commit": (h or "?").strip(), "quando": time.strftime("%Y-%m-%d %H:%M")}
    try:
        GIT_STATE_PATH.write_text(json.dumps(est, indent=2))
    except Exception:
        pass
    git_hist_add(repo, (h or "?").strip(), "painel (↻)")
    return True, (h or "?").strip()


def checar_senha(digitada: str) -> bool:
    try:
        real = SENHA_PATH.read_text().strip()
    except Exception:
        return False
    return bool(real) and digitada == real


# ---- Sessão persistente via cookie (mantém logado após F5) ----
import hashlib

def _assinatura_sessao() -> str:
    """Hash da senha atual — serve de 'cookie de sessão'. Se a senha mudar, desloga todos."""
    try:
        return hashlib.sha256(("vpsadmin::" + SENHA_PATH.read_text().strip()).encode()).hexdigest()[:32]
    except Exception:
        return ""

try:
    from streamlit_cookies_controller import CookieController
    _cookies = CookieController()
except Exception:
    _cookies = None

COOKIE_NOME = "vpsadmin_sessao"


# ============================================================
# Login
# ============================================================

if "autenticado" not in st.session_state:
    st.session_state["autenticado"] = False

# Login persistente após F5 via PARAMETRO NA URL (confiável atrás de proxy/subpath).
# A URL guarda só um HASH da senha (não a senha). F5 preserva o parametro -> segue logado.
if not st.session_state["autenticado"]:
    try:
        if st.query_params.get("k") == _assinatura_sessao() and _assinatura_sessao():
            st.session_state["autenticado"] = True
    except Exception:
        pass

if not st.session_state["autenticado"]:
    st.markdown("<br><br><br>", unsafe_allow_html=True)
    _esq, centro, _dir = st.columns([1.4, 1.2, 1.4])
    with centro:
        with st.container(border=True):
            st.markdown(
                "<div style='text-align:center; padding: 8px 0 2px 0;'>"
                "<div style='font-size:3em;'>🛠️</div>"
                "<h2 style='margin:0;'>VPS Admin</h2>"
                "<p style='color:#6b7280; font-size:0.9em; margin-top:4px;'>"
                "Central de gestão do servidor<br>"
                f"<code>{IP_PUBLICO}</code> · {PROVEDOR} {ARCH_CURTA}</p>"
                "</div>",
                unsafe_allow_html=True,
            )
            if not SENHA_PATH.exists():
                st.error(
                    "Arquivo de senha nao encontrado. Crie no servidor: "
                    "echo 'SUA_SENHA' > ~/.vps_admin_pass && chmod 600 ~/.vps_admin_pass"
                )
                st.stop()
            with st.form("login_form", border=False):
                senha = st.text_input("Senha", type="password",
                                      placeholder="Digite sua senha de administrador")
                entrar = st.form_submit_button("🔓 Entrar", type="primary",
                                               use_container_width=True)
            if entrar:
                if checar_senha(senha):
                    st.session_state["autenticado"] = True
                    try:
                        st.query_params["k"] = _assinatura_sessao()
                    except Exception:
                        pass
                    st.rerun()
                else:
                    time.sleep(1.5)
                    st.error("Senha incorreta.")
            with st.expander("🔑 Esqueci a senha"):
                _u = carregar_usuario()
                st.markdown(
                    f"Administrador: **{_u.get('nome', '—')}** "
                    f"(`{_mascarar_email(_u.get('email', ''))}`)\n\n"
                    "Redefinicao segura **via SSH** (so quem tem a chave do servidor):\n"
                    "```bash\necho 'NOVA_SENHA' > ~/.vps_admin_pass && chmod 600 ~/.vps_admin_pass\n```\n"
                    "*Recuperacao por e-mail: v3 (requer SMTP).*"
                )
        st.markdown(
            "<p style='text-align:center; color:#9ca3af; font-size:0.78em; margin-top:10px;'>"
            "VPS Admin v2.0 · acesso restrito · ações auditáveis</p>",
            unsafe_allow_html=True,
        )
    st.stop()


# ============================================================
# MENU LATERAL (estilo Maestro)
# ============================================================

with st.sidebar:
    st.markdown("<style>section[data-testid='stSidebar'] > div:first-child"
                "{padding-top:0.8rem;}</style>", unsafe_allow_html=True)
    st.markdown("## 🛠️ VPS Admin")
    _ident = f"`{IP_PUBLICO}` · {PROVEDOR} {ARCH_CURTA}" + (f" · {PLANO_LABEL}" if PLANO_LABEL else "")
    st.caption((f"🔒 `{DOMINIO}`  \n" if DOMINIO else "") + _ident)

    # mini-status no topo do menu
    svcs = todos_servicos()
    ativos = sum(1 for s in svcs if status_servico(s) == "active")
    if psutil:
        _cpu = psutil.cpu_percent(interval=0.2)
        _mem = psutil.virtual_memory().percent
        st.markdown(
            f"<div style='background:#e6f4ec;color:#0f6e56;border-radius:8px;padding:8px 12px;font-size:0.85em;'>"
            f"🟢 <b>{ativos}/{len(svcs)}</b> serviços ativos<br>"
            f"⚙️ CPU {_cpu:.0f}% &nbsp;·&nbsp; 🧠 RAM {_mem:.0f}%</div>",
            unsafe_allow_html=True,
        )
    # Status do MCP — a ponte do Claude com o servidor (sempre visível)
    _mcp_on = mcp_online()
    st.markdown(
        f"<div style='background:{'#e6f4ec' if _mcp_on else '#fbeae7'};color:{'#0f6e56' if _mcp_on else '#993c1d'};border-radius:8px;"
        f"padding:8px 12px;font-size:0.85em;margin-top:6px;'>"
        f"{'🟢 <b>MCP Online</b>' if _mcp_on else '🔴 <b>MCP Offline</b>'} "
        f"<span style='opacity:.7'>· conexão do Claude</span></div>",
        unsafe_allow_html=True,
    )
    st.divider()

    # Menu de botoes — mesmo padrao do Escola Parque (primary = pagina ativa)
    PAGINAS = [
        "📊 Dashboard",
        "🚀 Aplicativos",
        "🌐 Domínios & Rotas",
        "🌿 Git & Deploys",
        "🧠 IA & LLMs",
        "🐘 Supabase VPS",
        "🔔 Alertas",
        "🔌 Acesso MCP (Claude)",
        "💾 Servidor & Limites",
        "👤 Conta",
    ]
    if "pagina" not in st.session_state:
        try:
            _pq = st.query_params.get("p")
        except Exception:
            _pq = None
        st.session_state["pagina"] = _pq if _pq in PAGINAS else PAGINAS[0]
    for _p in PAGINAS:
        if st.button(
            _p,
            type="primary" if st.session_state["pagina"] == _p else "secondary",
            use_container_width=True,
            key=f"nav_{_p}",
        ):
            st.session_state["pagina"] = _p
            try:
                st.query_params["p"] = _p
            except Exception:
                pass
            st.rerun()
    pagina = st.session_state["pagina"]

    st.divider()
    if st.button("🔄 Atualizar", use_container_width=True):
        st.rerun()
    if st.button("🚪 Sair", use_container_width=True):
        st.session_state["autenticado"] = False
        try:
            st.query_params.clear()
        except Exception:
            pass
        st.rerun()


# ============================================================
# PAGINA: Dashboard
# ============================================================

if pagina == "📊 Dashboard":
    st.title("📊 Dashboard")
    if psutil:
        cpu = psutil.cpu_percent(interval=0.4)
        mem = psutil.virtual_memory()
        disco = psutil.disk_usage("/")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("CPU", f"{cpu:.0f}%")
        c1.caption(f"{psutil.cpu_count()} vCPUs {ARCH_CURTA}")
        c2.metric("RAM", f"{mem.percent:.0f}%")
        c2.caption(f"{mem.used/1e9:.1f} de {mem.total/1e9:.0f} GB")
        c3.metric("Disco /", f"{disco.percent:.0f}%")
        c3.caption(f"{disco.used/1e9:.1f} de {disco.total/1e9:.0f} GB")
        try:
            carga = ", ".join(f"{x:.2f}" for x in psutil.getloadavg())
        except Exception:
            carga = "—"
        c4.metric("Load (1/5/15m)", carga)
        c4.caption("média de processos na fila")

    st.divider()
    st.subheader("📱 Apps")
    st.markdown("<style>[class*='st-key-ativar_'] button{background:#fdecea !important;"
                "color:#c0392b !important;border-color:#f1b0b7 !important;}</style>",
                unsafe_allow_html=True)
    _svcs = todos_servicos()
    _destaque = ["ntfy", "evolution"]   # sempre aparecem na aba Apps
    _apps_web = [n for n in _svcs if url_acesso(n) or n in _destaque]
    cols_a = st.columns(min(4, max(1, len(_apps_web))))
    for i, nome in enumerate(_apps_web):
        stt = status_servico(nome)
        cor = {"active": "🟢", "inactive": "⚪", "failed": "🔴"}.get(stt, "🟡")
        _stk = (stack_node(STACK_SERVICO[nome])
                if nome in STACK_SERVICO else "")
        _url = url_acesso(nome)
        with cols_a[i % len(cols_a)]:
            with st.container(border=True):
                st.markdown(
                    f"**{cor} {_svcs[nome]}**  \n"
                    f"<small>`{nome}` · {stt}"
                    + (f"  \n{_stk}" if _stk else "") + "</small>",
                    unsafe_allow_html=True,
                )
                if stt != "active":
                    if st.button("▶ Ativar", key=f"ativar_{nome}", use_container_width=True):
                        ok, msg = acao_servico(nome, "start")
                        (st.success if ok else st.error)(msg[:300])
                        time.sleep(1)
                        st.rerun()
                elif _url:
                    st.link_button("↗ Acessar", _url, use_container_width=True)
                else:
                    st.caption("🟢 rodando")

    st.divider()
    st.subheader("🚦 Serviços")
    REGIOES = [
        ("🧰 Infra & IA", ["vpsadmin", "nginx", "ollama", "llmgateway",
                           "vpsmcp", "vpswebhook", "postgresql", "postgrest",
                           "ntfy", "evolution"]),
    ]
    _agrupados = {s for _, _ss in REGIOES for s in _ss}
    _sobras = [s for s in _svcs if s not in _agrupados]
    if _sobras:
        REGIOES.append(("📦 Outros apps", _sobras))
    _CORES_REG = {"🏫": "#dbeafe", "🚀": "#fee2e2", "🎸": "#fef9c3",
                  "🧰": "#e5e7eb", "📦": "#f3e8ff"}
    _ordem = [(reg, s) for reg, ss in REGIOES for s in ss
              if s in _svcs and not url_acesso(s)]
    cols = st.columns(3)
    for i, (_reg, nome) in enumerate(_ordem):
        stt = status_servico(nome)
        cor = {"active": "🟢", "inactive": "⚪", "failed": "🔴"}.get(stt, "🟡")
        _bg = _CORES_REG.get(_reg[:1], "#e5e7eb")
        with cols[i % 3]:
            with st.container(border=True):
                st.markdown(
                    f"**{cor} {_svcs[nome]}** "
                    f"<span style='background:{_bg};color:#1f2937;border-radius:8px;"
                    f"padding:1px 8px;font-size:0.68em;white-space:nowrap;"
                    f"vertical-align:middle'>{_reg}</span>",
                    unsafe_allow_html=True,
                )
                st.caption(f"`{nome}` · {stt}")

    st.divider()
    st.subheader("🌿 Git & Deploys")
    _cols_g = st.columns(3)
    for _i, (_repo, _conf) in enumerate(todos_git_projetos().items()):
        with _cols_g[_i % 3]:
            with st.container(border=True):
                st.markdown(f"**{_conf.get('rotulo', _repo)}**")
                st.caption(f"`{_repo}` · {git_situ_curta(_repo, _conf)}")

    st.divider()
    rotas = rotas_nginx()
    if rotas:
        st.subheader("🌐 Acessos rapidos")
        links = " · ".join(
            f"[{r}]({URL_BASE}{r})" for r in rotas
            if not r.startswith("=") and not r.startswith("/mcp-")
        )
        st.markdown(links)
        st.caption("🔒 A rota do MCP não aparece aqui de propósito — é segredo (veja em Acesso MCP).")


# ============================================================
# PAGINA: Aplicativos
# ============================================================

elif pagina == "🚀 Aplicativos":
    c_titulo, c_novo = st.columns([5, 1.4], vertical_alignment="center")
    with c_titulo:
        st.title("🚀 Aplicativos & Serviços")
    with c_novo:
        if st.button("➕ Novo App", type="primary", use_container_width=True):
            st.session_state["pagina"] = "➕ Novo App"
            st.rerun()
    st.markdown(ABAS_CSS, unsafe_allow_html=True)
    st.markdown(  # Start (servico parado) com vermelho levinho — sinaliza "apagado, clique p/ ligar"
        "<style>"
        "[class*='st-key-i_'] button{background:#fdecea !important;color:#c0392b !important;"
        "border-color:#f1b0b7 !important;}"
        "[class*='st-key-i_'] button:hover{background:#fadbd8 !important;border-color:#e08c93 !important;}"
        "</style>", unsafe_allow_html=True,
    )
    tab_apps, tab_libs = st.tabs(["🚀 Apps & Serviços", "📚 Bibliotecas"])

    with tab_apps:
        st.caption("Ações restritas à whitelist — sem terminal livre, por segurança.")
        extras = carregar_apps_extras()
        _git_svc: dict[str, str] = {}
        _git_situ: dict[str, str] = {}
        for _r, _c in todos_git_projetos().items():
            _git_situ[_r] = git_situ_curta(_r, _c)
            for _s in _c.get("servicos", []):
                _git_svc[_s] = _r
        _svcs_ord = sorted(todos_servicos().items(),
                           key=lambda kv: (not url_acesso(kv[0]), kv[1]))
        for nome, rotulo in _svcs_ord:
            stt = status_servico(nome)
            cor = {"active": "🟢", "inactive": "⚪", "failed": "🔴"}.get(stt, "🟡")
            with st.container(border=True):
                # Layout FIXO p/ todas as linhas (alinhamento consistente):
                # rótulo | Restart | Stop/Start | Logs | Acessar(verde, à direita)
                c1, c2, c3, c4, c5 = st.columns(
                    [3.6, 1.2, 1.2, 1.0, 1.3], vertical_alignment="center"
                )
                _g = _git_svc.get(nome)
                _gtxt = f" · 🌿 `{_g}` {_git_situ.get(_g, '')}" if _g else ""
                c1.markdown(f"**{cor} {rotulo}**  \n`{nome}` · status: `{stt}`{_gtxt}")
                if c2.button("Restart", key=f"r_{nome}", use_container_width=True):
                    ok, msg = acao_servico(nome, "restart")
                    (st.success if ok else st.error)(msg[:400])
                    time.sleep(1)
                    st.rerun()
                if stt == "active":
                    if c3.button("Stop", key=f"s_{nome}", use_container_width=True):
                        ok, msg = acao_servico(nome, "stop")
                        (st.success if ok else st.error)(msg[:400])
                        time.sleep(1)
                        st.rerun()
                else:
                    if c3.button("Start", key=f"i_{nome}", use_container_width=True):
                        ok, msg = acao_servico(nome, "start")
                        (st.success if ok else st.error)(msg[:400])
                        time.sleep(1)
                        st.rerun()
                mostrar = c4.toggle("Logs", key=f"l_{nome}")
                if url_acesso(nome):
                    c5.markdown(
                        f'<a href="{url_acesso(nome)}" target="_blank" '
                        f'style="display:inline-block;width:100%;box-sizing:border-box;'
                        f'background:#16a34a;color:#fff;text-decoration:none;'
                        f'padding:.34rem .2rem;border-radius:.5rem;font-weight:600;'
                        f'font-size:.84rem;text-align:center;white-space:nowrap;">'
                        f'↗ Acessar</a>',
                        unsafe_allow_html=True,
                    )
                if mostrar:
                    st.code(logs_servico(nome), language="log")
                if nome in extras:
                    if st.button("🗑️ Remover do painel (nao desinstala)", key=f"rm_{nome}"):
                        extras.pop(nome, None)
                        salvar_apps_extras(extras)
                        st.rerun()

    with tab_libs:
        with st.spinner("Varrendo os ambientes dos apps..."):
            libs = listar_bibliotecas()
        if not libs:
            st.caption("Nenhum venv encontrado em /home/ubuntu/*/.venv.")
        else:
            total = sum(len(v) for v in libs.values())
            cols_resumo = st.columns(len(libs) + 1)
            cols_resumo[0].metric("📚 Total", total)
            for i, (app_nome, pacotes) in enumerate(libs.items(), start=1):
                cols_resumo[i].metric(app_nome, len(pacotes))
            st.caption(
                "Cada app tem seu ambiente ISOLADO (venv Python ou node_modules) — "
                "versões podem diferir entre apps sem conflito. (node) = app "
                "JavaScript, contando dependências indiretas. Clique pra "
                "ver/filtrar. Atualiza a cada 5 min."
            )
            for app_nome, pacotes in libs.items():
                with st.expander(f"📦 **{app_nome}** — {len(pacotes)} bibliotecas"):
                    filtro = st.text_input("🔎 Filtrar por nome", key=f"libf_{app_nome}")
                    dados = (
                        [p for p in pacotes if filtro.lower() in p.get("name", "").lower()]
                        if filtro else pacotes
                    )
                    st.dataframe(dados, use_container_width=True, height=320, hide_index=True)


# ============================================================
# PAGINA: Novo App (gerador de kit de deploy)
# ============================================================

elif pagina == "➕ Novo App":
    if st.button("← Voltar aos Aplicativos"):
        st.session_state["pagina"] = "🚀 Aplicativos"
        st.rerun()
    st.title("➕ Novo App no servidor")
    st.caption(
        "Preencha e o painel gera o KIT DE DEPLOY completo (comandos prontos) "
        "+ registra o app aqui no painel. Padrao da casa: venv proprio + systemd + rota Nginx."
    )

    with st.form("novo_app"):
        c1, c2 = st.columns(2)
        nome = c1.text_input("Nome do serviço (sem espacos, ex.: sertanejolab)")
        porta = c2.number_input("Porta interna", min_value=8502, max_value=8599, value=8502)
        c3, c4 = st.columns(2)
        pasta = c3.text_input("Pasta no servidor", value="~/meu-app")
        principal = c4.text_input("Arquivo principal", value="app.py")
        rota = st.text_input("Rota no Nginx (ex.: /sertanejo)", value="/meu-app")
        rotulo = st.text_input("Rótulo no painel (com emoji!)", value="🎸 Meu App")
        gerar = st.form_submit_button("⚙️ Gerar kit de deploy", type="primary")

    if gerar and nome and rota.startswith("/"):
        pasta_abs = pasta.replace("~", "/home/ubuntu")
        st.success(f"Kit gerado para **{nome}** — siga os 3 passos:")

        st.markdown("**1️⃣ Enviar o projeto (PowerShell no PC):**")
        st.code(
            f'scp -i "$HOME\\.ssh\\ssh-key-2026-06-03.key" -r "C:\\CAMINHO\\DO\\PROJETO" '
            f"ubuntu@{IP_PUBLICO}:{pasta}",
            language="powershell",
        )

        st.markdown("**2️⃣ Instalar no servidor (terminal SSH, bloco único):**")
        st.code(
            f"""cd {pasta} && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
sudo tee /etc/systemd/system/{nome}.service > /dev/null <<'EOF'
[Unit]
Description={rotulo}
After=network.target

[Service]
User=ubuntu
WorkingDirectory={pasta_abs}
ExecStart={pasta_abs}/.venv/bin/streamlit run {principal} --server.port {porta} --server.address 127.0.0.1 --server.headless true --server.baseUrlPath {rota.strip('/')}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload && sudo systemctl enable --now {nome}
sudo sed -i '/^}}$/i\\
    location {rota}/ {{\\
        proxy_pass http://127.0.0.1:{porta}{rota}/;\\
        proxy_http_version 1.1;\\
        proxy_set_header Upgrade $http_upgrade;\\
        proxy_set_header Connection "upgrade";\\
        proxy_set_header Host $host;\\
        proxy_read_timeout 86400;\\
    }}' /etc/nginx/sites-available/apps
sudo nginx -t && sudo systemctl reload nginx""",
            language="bash",
        )

        st.markdown(f"**3️⃣ Testar:** `{URL_BASE}{rota}/`")

        extras = carregar_apps_extras()
        extras[nome] = rotulo
        if salvar_apps_extras(extras):
            st.info(f"✅ **{rotulo}** ja registrado no painel (aba Aplicativos).")
    elif gerar:
        st.error("Preencha o nome e uma rota começando com / .")


# ============================================================
# PAGINA: Rotas Nginx
# ============================================================

elif pagina == "🌐 Domínios & Rotas":
    c_tit_d, c_novo_d = st.columns([4.5, 1.6], vertical_alignment="center")
    with c_tit_d:
        st.title("🌐 Domínios & Rotas")
    with c_novo_d:
        if st.button("➕ Novo domínio", type="primary", use_container_width=True):
            st.session_state["form_dom"] = not st.session_state.get("form_dom", False)

    if st.session_state.get("form_dom"):
        with st.container(border=True):
            st.markdown("**Apontar um domínio novo pra um app deste servidor** "
                        "— 1 clique: cria a rota Nginx + HTTPS (certbot).")
            # Serviços do framework já vêm PRÉ-FABRICADOS (com a porta certa) — automático/didático
            _ger_d = {
                "VPS Admin (painel)": 8500,
                "Evolution (Zap/WhatsApp)": 8080,
                "ntfy (push)": 2586,
                "LLM Gateway": 8600,
                "PostgREST (API do banco)": 3001,
            }
            try:
                import subprocess as _spd, json as _jjd
                _od = _spd.run(["sudo", "-n", "/usr/local/bin/vps_provision", "listar"],
                               capture_output=True, text=True, timeout=15).stdout
                _ger_d.update({k: v.get("porta") for k, v in _jjd.loads(_od).items() if v.get("porta")})
            except Exception:
                pass
            with st.form("novo_dominio", border=False):
                dom_novo = st.text_input("Domínio completo (já apontado pro IP no seu DNS)",
                                         placeholder="www.meusite.com.br")
                st.caption("Os serviços do framework já aparecem com a porta certa. "
                           "Use '(porta manual)' só pra um app próprio em outra porta.")
                _opts_d = [f"{k} · porta {v}" for k, v in _ger_d.items()] + ["(porta manual)"]
                _sel_d = st.selectbox("App de destino", _opts_d)
                if _sel_d == "(porta manual)":
                    porta_dom = st.number_input("Porta interna do app", 1024, 65535, 3000)
                else:
                    porta_dom = _ger_d[_sel_d.split(" · ")[0]]
                    st.caption(f"Porta {porta_dom} — do app `{_sel_d.split(' · ')[0]}`")
                ok_dom = st.form_submit_button("🚀 Apontar domínio (rota + HTTPS)",
                                               type="primary", use_container_width=True)
            if ok_dom and dom_novo.strip():
                _d = dom_novo.strip().lower()
                with st.status(f"Apontando {_d}…", expanded=True) as _bxd:
                    st.write("🔧 Criando rota Nginx + rodando certbot (HTTPS)…")
                    import subprocess as _spd2
                    _rd = _spd2.run(["sudo", "-n", "/usr/local/bin/vps_provision",
                                     "dominio", _d, str(int(porta_dom))],
                                    capture_output=True, text=True, timeout=180)
                    st.code((_rd.stdout + _rd.stderr)[-1200:] or "(sem saída)")
                    if _rd.returncode == 0 and "PRONTO" in _rd.stdout:
                        _bxd.update(label=f"✅ {_d} no ar!", state="complete")
                        st.success(f"https://{_d} apontando pro app. "
                                   "(Se o certbot falhou, confira se o DNS já aponta pro IP.)")
                        time.sleep(1)
                        st.rerun()
                    else:
                        _bxd.update(label="Falhou — veja o log acima", state="error")

    st.subheader("🌍 Domínios deste servidor")
    for _dm in dominios_nginx():
        with st.container(border=True):
            _val_d = cert_validade_cache(_dm["dominio"]) if _dm["ssl"] else None
            c_dmi, c_dmx = st.columns([5.4, 0.4], vertical_alignment="center")
            c_dmi.markdown(
                f"**{'🔒' if _dm['ssl'] else '⚠️ sem SSL'} "
                f"[{_dm['dominio']}](https://{_dm['dominio']})**  \n"
                f"→ **{alvo_amigavel(_dm['alvo'])}**"
                + (f" <small><span style='color:#9ca3af'>· cert até {_val_d}"
                   f" · conf `{_dm['arquivo']}`</span></small>" if _val_d
                   else f" <small><span style='color:#9ca3af'>· conf `{_dm['arquivo']}`</span></small>"),
                unsafe_allow_html=True,
            )
            if _dm["arquivo"] != "apps":
                with c_dmx.popover("✕", use_container_width=True):
                    st.markdown(f"**Remover o domínio `{_dm['dominio']}`?**")
                    st.caption(
                        "O app continua rodando — só o ENDEREÇO deixa de existir. "
                        "Por segurança o painel não executa isso sozinho: rode no SSH:"
                    )
                    st.code(
                        f"sudo rm -f /etc/nginx/sites-enabled/{_dm['arquivo']} "
                        f"/etc/nginx/sites-available/{_dm['arquivo']}\n"
                        f"sudo nginx -t && sudo systemctl reload nginx\n"
                        f"sudo certbot delete --cert-name {_dm['dominio']} -n",
                        language="bash",
                    )
            else:
                c_dmx.markdown("<span title='Domínio principal — hospeda o painel; "
                               "não removível por aqui'>🏛️</span>", unsafe_allow_html=True)

    st.divider()
    st.subheader("🛣️ Rotas internas do domínio principal")
    with st.container(border=True):
        c_dom, c_duck = st.columns([4.2, 1.3], vertical_alignment="center")
        c_dom.markdown(
            f"**🔒 Domínio & HTTPS:** [`{DOMINIO}`]({URL_BASE}) → `{IP_PUBLICO}`  \n"
            f"DNS grátis **DuckDNS** (conta Google: `diogobsbastos@gmail.com`) · "
            f"certificado **Let's Encrypt** — renovação automática a cada 90 dias (certbot)."
        )
        c_duck.link_button("🦆 DuckDNS", "https://www.duckdns.org",
                           use_container_width=True)
    rotas = rotas_nginx()
    if rotas:
        for r in rotas:
            if r.startswith("/mcp-"):
                st.markdown("- 🔒 `/mcp-…/` → rota SECRETA do MCP (oculta de propósito — ver Acesso MCP)")
                continue
            _rr = "/" if r.startswith("=") else r
            _sufx = " *(página inicial)*" if r.startswith("=") else ""
            st.markdown(f"- `{_rr}`{_sufx} → [{URL_BASE}{_rr}]({URL_BASE}{_rr})")
    else:
        st.warning("Não consegui ler a config (permissão).")
    st.divider()
    st.caption(
        "Para criar rota nova use a aba ➕ Novo App. Edição manual: "
        "`sudo nano /etc/nginx/sites-available/apps` + `sudo nginx -t` + `sudo systemctl reload nginx`."
    )


# ============================================================
# PAGINA: Git & Deploys
# ============================================================

elif pagina == "🌿 Git & Deploys":
    c_t, c_add, c_wh, c_gh = st.columns([3.0, 1.4, 1.1, 1.0],
                                        vertical_alignment="center")
    with c_wh:
        if st.button("🪝 Webhook", use_container_width=True,
                     help="Campainha do push→deploy: status por repo, "
                          "conectar/desconectar e kit de migração."):
            dialog_webhook()
    with c_t:
        st.title("🌿 Git & Deploys")
    st.markdown(ABAS_CSS, unsafe_allow_html=True)
    tab_git, tab_dep = st.tabs(["🐙 GitHub", "🚀 Deploys"])
    with tab_git:
        with c_add:
            _form_aberto = bool(st.session_state.get("form_repo"))
            if st.button("✖ Fechar formulário" if _form_aberto else "➕ Conectar repo",
                         type="secondary" if _form_aberto else "primary",
                         use_container_width=True):
                st.session_state["form_repo"] = not _form_aberto
                st.rerun()
        with c_gh:
            st.link_button("🐙 GitHub", f"https://github.com/{GIT_USER}?tab=repositories",
                           use_container_width=True)
        if st.session_state.get("form_repo"):
            with st.container(border=True):
                st.markdown("**Conectar um repositório do GitHub a uma pasta do servidor**")
                with st.form("conectar_repo", clear_on_submit=True, border=False):
                    f1, f2 = st.columns(2)
                    repo_novo = f1.text_input(f"Repo (em github.com/{GIT_USER}/...)",
                                              placeholder="ex.: sertanejo-lab")
                    rotulo_novo = f2.text_input("Rótulo no painel (com emoji!)",
                                                placeholder="🎸 Sertanejo Lab")
                    f3, f4 = st.columns(2)
                    pasta_nova = f3.text_input("Pasta no servidor (deve ser um clone do repo)",
                                               placeholder="/home/ubuntu/sertanejo-lab")
                    svc_novos = f4.multiselect("Serviços a reiniciar no deploy",
                                               list(todos_servicos().keys()))
                    build_novo = st.text_input(
                        "Comando de build (opcional — apps compilados, ex. Next.js)",
                        placeholder="npm install && npm run build",
                        help="Roda na pasta APÓS o pull e ANTES do restart. Se falhar, "
                             "nada é reiniciado (a produção continua na versão antiga).",
                    )
                    ok_repo = st.form_submit_button("Conectar 🌿", type="primary")
                if ok_repo and repo_novo.strip() and pasta_nova.strip():
                    extras_r = git_projetos_extras()
                    extras_r[repo_novo.strip()] = {
                        "rotulo": rotulo_novo.strip() or repo_novo.strip(),
                        "pull": pasta_nova.strip(),
                        "servicos": svc_novos,
                    }
                    if build_novo.strip():
                        extras_r[repo_novo.strip()]["build"] = build_novo.strip()
                    if salvar_git_projetos(extras_r):
                        _msg_hook = gh_hook_sincronizar(repo_novo.strip())
                        gh_hook_do_repo.clear()
                        st.toast(f"🪝 campainha do repo novo: {_msg_hook}")
                        st.session_state["form_repo"] = False
                        time.sleep(1.2)
                        st.rerun()
                    else:
                        st.error("Falha ao salvar o registro.")
                elif ok_repo:
                    st.error("Preencha pelo menos o repo e a pasta.")
        st.caption(
            "A ponte oficial da casa: **PC (oficina) → GitHub privado (cartório) → "
            "Servidor (produção)**. ↻ Atualizar = puxa o último commit, aplica nas "
            "pastas de produção (sem tocar nos venvs) e reinicia os serviços do projeto. "
            "Histórico e rollback ficam no GitHub."
        )

        @st.fragment(run_every=3)
        def _status_deploy():
            seg = autodeploy_proximo()
            if seg == -1:
                st.markdown("### 🔨 Deploy em andamento")
                st.progress(1.0, text="o vigia está aplicando (pull/build/restart) — "
                                      "esta faixa volta ao normal quando ele terminar")
                _rc_j, _out_j = _run(["journalctl", "-u", "vpsautodeploy", "-n", "14",
                                      "--no-pager", "-o", "cat"], timeout=5)
                if _rc_j == 0 and _out_j:
                    st.code(_out_j[-1600:], language="text")
                return
            hook_on = webhook_ativo()
            ultimo = webhook_ultimo_push()
            with st.container(border=True):
                c_w, c_u, c_r = st.columns([1.9, 2.7, 1.7], vertical_alignment="center")
                c_w.markdown(
                    ("⚡ **Webhook** 🟢 ativo  \n<small>push no GitHub → deploy em ~5s</small>")
                    if hook_on else
                    ("⚡ **Webhook** 🔴 fora do ar  \n<small>deploys só pela ronda — "
                     "conferir serviço `vpswebhook`</small>"),
                    unsafe_allow_html=True,
                )
                c_u.markdown(
                    ("📨 **Último push recebido**  \n<small>" + ultimo + "</small>")
                    if ultimo else
                    ("📨 **Último push recebido**  \n<small>nenhum ainda — faça um "
                     "commit e veja a mágica</small>"),
                    unsafe_allow_html=True,
                )
                if seg is None:
                    c_r.markdown("🕐 **Ronda de segurança**  \n<small>timer não "
                                 "instalado</small>", unsafe_allow_html=True)
                else:
                    m, s2 = divmod(int(seg), 60)
                    c_r.markdown(f"🕐 **Ronda de segurança**  \n<small>próxima em "
                                 f"`{m:02d}:{s2:02d}` · rede de segurança do webhook</small>",
                                 unsafe_allow_html=True)
        _status_deploy()

        estado = git_estado()
        _extras_git = git_projetos_extras()
        for repo, conf in todos_git_projetos().items():
            with st.container(border=True):
                remoto = git_remote_head(repo)
                info = estado.get(repo, {})
                local = info.get("commit", "—")
                if conf.get("pull"):
                    _, _h = _run(["git", "-C", conf["pull"], "rev-parse", "--short=10", "HEAD"])
                    local = (_h or "").strip() if _h and "fatal" not in _h else "—"
                if remoto == "?":
                    situ = "🟡 GitHub inacessível (credencial?)"
                elif local == "—":
                    situ = "⚪ nunca deployado pelo painel"
                elif remoto == local:
                    situ = "🟢 em dia com o GitHub"
                else:
                    situ = "🟠 atualização disponível!"
                c1, c0, c2, c3, cx = st.columns([3.2, 0.9, 1.2, 1.2, 0.4],
                                                vertical_alignment="center")
                c1.markdown(
                    f"**{conf['rotulo']}**  \n"
                    f"`{repo}` · GitHub `{remoto}` · produção `{local}`  \n"
                    f"{situ} <small><span style='color:#9ca3af'>· "
                    f"{info.get('quando', 'sem registro')}</span></small>",
                    unsafe_allow_html=True,
                )
                _auto_atual = bool(conf.get("auto"))
                _auto = c0.toggle("⚙️ auto", value=_auto_atual, key=f"auto_{repo}",
                                  help="Auto-deploy: push no GitHub → webhook dispara o vigia "
                                       "na hora (~5s); a ronda de 2 min cobre qualquer "
                                       "falha. Desligado = só deploy manual pelo ↻.")
                if _auto != _auto_atual:
                    _ex = git_projetos_extras()
                    _ex[repo] = {**conf, "auto": _auto}
                    salvar_git_projetos(_ex)
                    st.rerun()
                c2.link_button("Ver repo", f"https://github.com/{GIT_USER}/{repo}",
                               use_container_width=True)
                if c3.button("↻ Atualizar", key=f"dep_{repo}", type="primary",
                             use_container_width=True):
                    if remoto != "?" and local not in ("—", "") and remoto == local:
                        st.info("✅ Já está em dia com o GitHub — nada a atualizar. "
                                "Commit novo entra sozinho (webhook, ~5s). Precisa "
                                "reaplicar à força? Use ✏️ → ↻ Forçar redeploy.")
                    else:
                        st.info("⏳ Puxando do GitHub e aplicando... o painel vai PISCAR "
                                "no fim (reinicia a si mesmo). Dê F5 em ~10s.")
                        ok, msg = git_deploy(repo, conf)
                        if ok:
                            st.success(f"✅ Commit `{msg}` aplicado. Reiniciando: "
                                       + ", ".join(conf["servicos"]))
                            for s in conf["servicos"]:
                                acao_servico(s, "restart")
                                time.sleep(1)
                            st.rerun()
                        else:
                            st.error("Deploy falhou: " + msg)
                with cx.popover("✏️"):
                    st.markdown(f"**⚙️ Configurar `{repo}`**")
                    with st.form(f"edit_{repo}", border=False):
                        e_rot = st.text_input("Rótulo", value=conf.get("rotulo", repo))
                        e_pull = st.text_input(
                            "Pasta (clone) no servidor",
                            value=conf.get("pull", ""),
                            help="Vazio = mantém o modo atual (ex.: mapa do VPS Admin).",
                        )
                        e_build = st.text_input(
                            "Comando de build (opcional)",
                            value=conf.get("build", ""),
                            placeholder="npm install && npm run build",
                            help="Roda após o pull, antes do restart. Build falhou = "
                                 "nada reinicia (produção segue na versão anterior).",
                        )
                        e_svc = st.multiselect(
                            "Serviços a reiniciar",
                            list(todos_servicos().keys()),
                            default=[x for x in conf.get("servicos", [])
                                     if x in todos_servicos()],
                        )
                        sv_ed = st.form_submit_button("💾 Salvar", type="primary",
                                                      use_container_width=True)
                    if sv_ed:
                        _ex_ed = git_projetos_extras()
                        novo_conf = {**conf, "rotulo": e_rot.strip() or repo,
                                     "servicos": e_svc}
                        if e_pull.strip():
                            novo_conf["pull"] = e_pull.strip()
                        if e_build.strip():
                            novo_conf["build"] = e_build.strip()
                        else:
                            novo_conf.pop("build", None)
                        _ex_ed[repo] = novo_conf
                        salvar_git_projetos(_ex_ed)
                        st.rerun()
                    st.divider()
                    if st.button("↻ Forçar redeploy", key=f"force_{repo}",
                                 use_container_width=True,
                                 help="Reaplica o commit atual do GitHub mesmo já "
                                      "estando em dia (reinstala arquivos + restart)."):
                        ok_f, msg_f = git_deploy(repo, conf)
                        if ok_f:
                            for s in conf["servicos"]:
                                acao_servico(s, "restart")
                                time.sleep(1)
                            st.rerun()
                        else:
                            st.error("Forçar redeploy falhou: " + msg_f)
                    if repo in _extras_git and repo not in GIT_PROJETOS:
                        st.divider()
                        st.caption("Remover do painel — o app continua rodando; "
                                   "não mexe no GitHub nem nos arquivos.")
                        if st.button("✕ Remover do painel", key=f"rmconf_{repo}",
                                     use_container_width=True):
                            _extras_git.pop(repo, None)
                            salvar_git_projetos(_extras_git)
                            st.rerun()
        st.divider()
        st.caption(
            "⚡ Fluxo da casa: commit → GitHub toca a campainha (webhook) → vigia aplica "
            "em ~5s. A ronda de 2 min é rede de segurança. O Claude opera esta ponte via MCP."
        )

    with tab_dep:
        st.caption(
            "O **Git** (página 🌿) cuida do código; **aqui você acompanha o que foi "
            "PRO AR** — como no painel do Vercel: o que subiu, quando, por onde "
            "(webhook/ronda/manual) e se deu certo."
        )

        @st.fragment(run_every=4)
        def _deploys_live():
            seg = autodeploy_proximo()
            if seg == -1:
                st.markdown("#### 🔨 Deploy em andamento AGORA")
                st.progress(1.0, text="vigia aplicando (pull → build → restart) — "
                                      "log ao vivo abaixo")
                _rc_j, _out_j = _run(["journalctl", "-u", "vpsautodeploy", "-n", "16",
                                      "--no-pager", "-o", "cat"], timeout=5)
                if _rc_j == 0 and _out_j:
                    st.code(_out_j[-1800:], language="text")
            else:
                _hook = webhook_ativo()
                _ult = webhook_ultimo_push()
                with st.container(border=True):
                    _c1, _c2, _c3 = st.columns([1.6, 2.9, 1.5],
                                               vertical_alignment="center")
                    _c1.markdown("⚡ **Webhook** "
                                 + ("🟢 ativo" if _hook else "🔴 fora do ar"))
                    _c2.markdown(("📨 último push: " + _ult) if _ult
                                 else "📨 nenhum push recebido ainda")
                    _c3.markdown("💤 esteira livre")
            hist = git_hist_ler()
            if not hist:
                st.info("Nenhum deploy registrado ainda — faça um push que ele "
                        "aparece aqui sozinho.")
                return
            _repos_h = sorted({e.get("repo", "?") for e in hist})
            _f_h = st.selectbox("Projeto", ["📁 todos os projetos"] + _repos_h,
                                key="dep_filtro")
            _dados_h = [
                {"quando": e.get("quando", "?"), "projeto": e.get("repo", "?"),
                 "commit": e.get("commit", "?"),
                 "status": e.get("status", "✅ ok"),
                 "origem": e.get("origem", "?")}
                for e in reversed(hist)
                if _f_h == "📁 todos os projetos" or e.get("repo") == _f_h
            ]
            st.dataframe(_dados_h, use_container_width=True, height=420,
                         hide_index=True)
            st.caption(f"{len(hist)} registros (guarda os 100 últimos) · "
                       "✅ foi pro ar · ❌ falhou (produção segue na versão anterior) · "
                       "🔄 página viva: atualiza sozinha a cada 4s")
        _deploys_live()

        with st.expander("🧾 Log bruto do vigia (últimas 60 linhas)"):
            _rc_j, _out_j = _run(["journalctl", "-u", "vpsautodeploy", "-n", "60",
                                  "--no-pager", "-o", "short-iso"], timeout=6)
            st.code((_out_j or "sem acesso ao journal")[-4000:], language="text")


# ============================================================
# PAGINA: Ollama
# ============================================================

elif pagina == "🧠 IA & LLMs":
    st.title("🧠 IA & LLMs")
    st.markdown(ABAS_CSS, unsafe_allow_html=True)
    tab_oll, tab_api = st.tabs(["🦙 Ollama (local)", "🔑 API da LLM"])
    with tab_oll:

        # ---- Modo 24h na RAM (modelo residente) ----
        _24h_atual = bool(_cfg.get("ollama_24h"))
        with st.container(border=True):
            c_tg, c_tx = st.columns([1.3, 4.2], vertical_alignment="center")
            lig24 = c_tg.toggle("🔥 **24h na RAM**", value=_24h_atual, key="tg_24h")
            c_tx.markdown(
                "**Ligado:** o modelo fica **residente na memória** → resposta imediata, sem o "
                "\"modelo carregando\" (ocupa ~o tamanho do modelo em RAM — temos folga: 24 GB).  \n"
                "**Desligado:** o Ollama descarrega após ~5 min ocioso → economiza RAM, mas o "
                "1º pedido depois da pausa leva 30-60s recarregando do disco. "
                "*Cada uso pela API renova as 24h.*"
            )
        if lig24 != _24h_atual:
            config_salvar("ollama_24h", lig24)
            _alvos = [m.get("name", "") for m in ollama_modelos() if m.get("name")]
            with st.spinner(("Carregando modelo(s) na RAM (até 1 min)..." if lig24
                             else "Descarregando modelo(s) da RAM...")):
                _oks = [ollama_manter_na_ram(_m, lig24) for _m in _alvos]
            if all(_oks):
                st.success("🔥 Modelo(s) residentes na RAM por 24h — resposta imediata."
                           if lig24 else "💤 RAM liberada — modelos carregam sob demanda.")
            else:
                st.warning("Config salva, mas algum modelo não respondeu — confira o serviço ollama.")
            time.sleep(1.2)
            st.rerun()

        modelos = ollama_modelos()
        if modelos:
            st.subheader("Modelos instalados")
            for m in modelos:
                nome_m = m.get("name", "")
                with st.container(border=True):
                    c1, c2, c3 = st.columns([4.5, 1.3, 1.3])
                    c1.markdown(f"**`{nome_m}`** · {m.get('size', 0)/1e9:.1f} GB")
                    specs_on = c2.toggle("📋 Specs", key=f"olsp_{nome_m}")
                    if c3.button("🗑️ Remover", key=f"olrm_{nome_m}", use_container_width=True):
                        with st.spinner("Removendo..."):
                            rc, out = _run(["ollama", "rm", nome_m], timeout=120)
                        (st.success if rc == 0 else st.error)(out[:300] or "Removido.")
                        time.sleep(1)
                        st.rerun()

                    if specs_on:
                        info = ollama_show(nome_m)
                        det = info.get("details", {}) or {}
                        mi = info.get("model_info", {}) or {}
                        ctx = next((v for k, v in mi.items() if k.endswith("context_length")), "?")
                        emb = next((v for k, v in mi.items() if k.endswith("embedding_length")), "?")

                        st.markdown("##### 🧬 Especificações do modelo")
                        e1, e2, e3, e4 = st.columns(4)
                        e1.metric("Família", str(det.get("family", "?")))
                        e2.metric("Parâmetros", str(det.get("parameter_size", "?")))
                        e3.metric("Quantização", str(det.get("quantization_level", "?")))
                        e4.metric("Contexto máx.", f"{ctx:,}".replace(",", ".") if isinstance(ctx, int) else str(ctx))
                        st.caption(
                            f"Formato: `{det.get('format', '?')}` · Embedding: `{emb}` · "
                            f"⚠️ Limites desta máquina: CPU ARM (sem GPU) ≈ 2-5 tokens/s neste porte; "
                            f"1 requisição por vez (fila); RAM ocupada ao usar ≈ tamanho do modelo + contexto."
                        )

                        st.markdown("##### 📡 Endereços de acesso")
                        st.markdown(
                            f"""
    | De onde | Endereço | Uso |
    |---|---|---|
    | **Dentro do servidor** (apps deste VPS) | `http://localhost:11434` | É o que o LiteLLM/worker usam |
    | **API estilo OpenAI** (compatível) | `http://localhost:11434/v1` | base_url p/ LiteLLM/SDKs |
    | **Rede interna Oracle** (outra VM da VCN) | `http://10.0.0.237:11434` | entre máquinas suas |
    | **Do seu PC (seguro)** | túnel SSH ⤵️ | recomendado |
    """
                        )
                        st.markdown("**Acessar do seu PC via túnel SSH** (abre e deixa aberto):")
                        st.code(
                            'ssh -i "$HOME\\.ssh\\ssh-key-2026-06-03.key" -N -L 11434:localhost:11434 '
                            f'ubuntu@{IP_PUBLICO}',
                            language="powershell",
                        )
                        st.caption(
                            "Com o túnel ativo, seu PC enxerga este Ollama em `http://localhost:11434` "
                            "como se fosse local. 🔒 NÃO abrimos a porta 11434 pra internet de propósito: "
                            "o Ollama não tem senha — porta pública = qualquer um usando sua máquina."
                        )
                        st.markdown("**Teste rápido (dentro do servidor):**")
                        st.code(
                            f"curl http://localhost:11434/api/generate -d "
                            f"'{{\"model\": \"{nome_m}\", \"prompt\": \"Diga OK\", \"stream\": false}}'",
                            language="bash",
                        )
        else:
            st.warning("Ollama sem resposta em localhost:11434 (serviço parado?).")

        st.divider()
        c_tit, c_atu = st.columns([4, 1.6], vertical_alignment="center")
        with c_tit:
            st.subheader("⬇️ Baixar modelo novo")
        with c_atu:
            if st.button("🔄 Atualizar Lista", use_container_width=True):
                with st.spinner("Buscando lista de modelos (ollama.com)..."):
                    ok, qtd = atualizar_catalogo_ollama()
                if ok:
                    st.success(f"Catálogo atualizado: {qtd} modelos.")
                    time.sleep(1)
                    st.rerun()
                else:
                    st.error("Falha ao buscar o catálogo (rede?). Usando lista local.")

        catalogo = catalogo_ollama()
        st.caption(
            f"{len(catalogo)} modelos no catálogo · digite abaixo pra FILTRAR · "
            "populares têm tamanho real; demais mostram '—' (o tamanho exato confirma no download, "
            "e os instalados acima já exibem o tamanho real) · dica desta máquina (CPU ARM): até ~10 GB"
        )

        opcoes = [
            f"{it['nome']}   —   {it['tamanho']}"
            if it.get("tamanho") and it["tamanho"] not in ("—", "?")
            else it["nome"]
            for it in catalogo
        ]
        escolha = st.selectbox(
            "🔎 Buscar modelo (nome + tamanho juntos — digite pra filtrar)",
            opcoes,
        )
        modelo_final = escolha.split()[0] if escolha else ""

        # Consulta o tamanho REAL no registro oficial ao selecionar (com cache de sessao)
        if modelo_final:
            if st.session_state.get("_tam_nome") != modelo_final:
                with st.spinner("Consultando tamanho no registro oficial..."):
                    st.session_state["_tam_nome"] = modelo_final
                    st.session_state["_tam_val"] = ollama_tamanho_remoto(modelo_final)
            tam_real = st.session_state.get("_tam_val", "?")
            if tam_real != "?":
                gb = float(tam_real.split()[0])
                params_b = gb / 0.6  # Q4: ~0,6 GB por bilhao de parametros
                cor_tam = "🟢" if gb <= 10 else "⚠️"
                st.markdown(
                    f"📦 **Tamanho do download:** {tam_real} {cor_tam} &nbsp;·&nbsp; "
                    f"🧠 **≈ {params_b:.0f}B parâmetros** *(estimado p/ quantização Q4)*"
                )
                if gb > 10:
                    st.caption("⚠️ Acima de ~10 GB fica pesado nesta máquina (CPU ARM, 24 GB RAM compartilhada com os apps).")
            else:
                st.caption("📦 Tamanho não disponível no registro pra esta variante.")

        if modelo_final and st.button(f"⬇️ Baixar {modelo_final}", type="primary"):
            with st.spinner(f"Baixando {modelo_final} — modelos grandes levam minutos..."):
                rc, out = _run(["ollama", "pull", modelo_final], timeout=3600)
            (st.success if rc == 0 else st.error)((out or "Concluído.")[-500:])
            if rc == 0:
                time.sleep(1)
                st.rerun()

    with tab_api:
        on = gateway_online()
        c_status, c_ex = st.columns([4.8, 1.2], vertical_alignment="center")
        with c_status:
            st.markdown("🟢 **Gateway Online**" if on else "🔴 **Gateway Offline**")
        with c_ex:
            with st.popover("📋 Exemplos", use_container_width=True):
                st.caption("Endpoint OpenAI-compatible. Modelo = um dos instalados (aba Ollama).")
                st.code(
                    f'''# Python (openai sdk)
    from openai import OpenAI
    client = OpenAI(base_url="{URL_BASE}/llm/v1", api_key="SUA_CHAVE")
    r = client.chat.completions.create(
        model="qwen2.5:14b",
        messages=[{{"role": "user", "content": "Olá!"}}],
    )
    print(r.choices[0].message.content)''',
                    language="python",
                )
                st.code(
                    f'''curl {URL_BASE}/llm/v1/chat/completions \\
      -H "Authorization: Bearer SUA_CHAVE" \\
      -H "Content-Type: application/json" \\
      -d '{{"model":"qwen2.5:14b","messages":[{{"role":"user","content":"Oi"}}]}}' ''',
                    language="bash",
                )

        st.code(f"{URL_BASE}/llm/v1", language="text")
        _mods = ", ".join(f"`{m.get('name','')}`" for m in ollama_modelos()) or "*nenhum modelo instalado*"
        st.caption(
            f"📡 Endereço da API — entregue base_url acima + uma chave ao cliente/projeto.  \n"
            f"🦙 **LLMs disponíveis neste servidor (Ollama):** {_mods} — cada chave é amarrada a uma delas."
        )
        if not on:
            st.warning(
                "O Gateway não respondeu — chaves não funcionarão. "
                "Rode: `sudo systemctl restart llmgateway` (setup: `llm_gateway/SETUP.md`)."
            )

        st.divider()
        c_t, c_b = st.columns([5, 1.5], vertical_alignment="center")
        with c_t:
            st.subheader("🗝️ Chaves cadastradas")
        with c_b:
            if st.button("➕ Criar chave", type="primary", use_container_width=True):
                st.session_state["form_key_aberto"] = not st.session_state.get("form_key_aberto", False)

        if st.session_state.get("form_key_aberto"):
            with st.container(border=True):
                instalados = [m.get("name", "") for m in ollama_modelos() if m.get("name")]
                with st.form("nova_key", clear_on_submit=True, border=False):
                    c1, c2, c3 = st.columns([2.6, 1.8, 1], vertical_alignment="bottom")
                    nome_key = c1.text_input("Nome / cliente (ex.: 'Sertanejo Lab', 'Cliente João')")
                    modelo_key = c2.selectbox(
                        "🦙 LLM ativa da chave",
                        instalados or ["(nenhum modelo instalado)"],
                        help="A chave fica AMARRADA a este modelo: o gateway força ele em toda "
                             "requisição, mesmo que o cliente peça outro.",
                    )
                    criar = c3.form_submit_button("Gerar 🔑", type="primary", use_container_width=True)
                if criar and not instalados:
                    st.error("Nenhum modelo instalado no Ollama — baixe um na aba 🦙 Ollama primeiro.")
                elif criar and nome_key.strip():
                    keys = carregar_api_keys()
                    nova = {
                        "id": f"key_{int(time.time())}",
                        "nome": nome_key.strip(),
                        "key": gerar_api_key(),
                        "modelo": modelo_key,
                        "criada_em": time.strftime("%Y-%m-%d %H:%M"),
                        "ativa": True,
                    }
                    keys.append(nova)
                    if salvar_api_keys(keys):
                        st.session_state["chave_recem_criada"] = nova["key"]
                        st.session_state["form_key_aberto"] = False
                        st.rerun()
                    else:
                        st.error("Falha ao salvar a chave.")
                elif criar:
                    st.error("Dê um nome pra chave.")

        if st.session_state.get("chave_recem_criada"):
            st.success("Chave criada! **Copie AGORA** — ela também fica no 👁️ Ver, mas guarde em local seguro.")
            st.code(st.session_state["chave_recem_criada"], language="text")
            if st.button("✅ Copiei, pode esconder"):
                st.session_state.pop("chave_recem_criada", None)
                st.rerun()
        keys = carregar_api_keys()
        uso = carregar_uso_api()
        if not keys:
            st.caption("Nenhuma chave ainda. Crie a primeira acima.")
        for k in keys:
            kid = k["id"]
            u = uso.get(kid, {})
            ativa = k.get("ativa", True)
            with st.container(border=True):
                c1, c2, cp, c3, c4 = st.columns([3.5, 1.2, 0.5, 1.1, 1.1])
                estado = "🟢 Ativa" if ativa else "🔴 Revogada"
                c1.markdown(
                    f"**{k.get('nome','—')}** · {estado} · 🦙 `{k.get('modelo', 'qualquer')}`  \n"
                    f"`{k['key'][:14]}…{k['key'][-4:]}` · criada {k.get('criada_em','?')}"
                )
                c2.metric("Usos", u.get("usos", 0))
                if cp.button("⚡", key=f"ping_{kid}", use_container_width=True,
                             help="Ping — testa a chave de ponta a ponta (gateway → LLM)"):
                    with st.spinner("Pingando a LLM (1º uso pode demorar — modelo carregando)..."):
                        ok_p, msg_p = ping_api_key(k["key"], k.get("modelo"))
                    if ok_p:
                        st.success(f"⚡ Chave OK — `{k.get('modelo','?')}` respondeu: “{msg_p}”")
                    else:
                        st.error(f"⚡ Falhou: {msg_p}")
                with c3.popover("👁️ Ver", use_container_width=True):
                    st.markdown(f"**{k.get('nome','—')}** · 🦙 LLM: `{k.get('modelo', 'qualquer')}`")
                    st.code(k["key"], language="text")
                    st.caption(f"Último uso: {u.get('ultimo_uso', '—')} · criada {k.get('criada_em','?')}")
                if ativa:
                    if c4.button("Revogar", key=f"rev_{kid}", use_container_width=True):
                        k["ativa"] = False
                        salvar_api_keys(keys)
                        st.rerun()
                else:
                    if c4.button("Reativar", key=f"rea_{kid}", use_container_width=True):
                        k["ativa"] = True
                        salvar_api_keys(keys)
                        st.rerun()
                if not ativa:
                    if st.button("🗑️ Excluir definitivamente", key=f"del_{kid}"):
                        salvar_api_keys([x for x in keys if x["id"] != kid])
                        st.rerun()


# ============================================================
# PAGINA: Supabase VPS (banco interno — Postgres + PostgREST)
# ============================================================

elif pagina == "🐘 Supabase VPS":
    st.title("🐘 Supabase VPS")
    st.markdown(ABAS_CSS, unsafe_allow_html=True)
    _pg_on = status_servico("postgresql") == "active"
    _api_on = status_servico("postgrest") == "active"
    st.caption(f"PostgreSQL 17 {'🟢' if _pg_on else '🔴'} · API REST (PostgREST) "
               f"{'🟢' if _api_on else '🔴'} · nosso banco com endereço e chaves, "
               "sem mensalidade.")
    if not db_cred():
        st.warning("Sem ~/.innova_db.json — rode a FASE 1 do banco interno (handoff).")

    tab_bd, tab_bk, tab_lg = st.tabs(["🗄️ Banco de Dados", "💾 Backups",
                                      "🧾 Logs"])
    with tab_bd:
        c_bd1, c_bd2, c_bd3 = st.columns([3.9, 1.4, 1.3],
                                         vertical_alignment="center")
        c_bd1.caption("Clique num banco pra ver tabelas, chaves e usuários.")
        with c_bd2:
            _f_nb = bool(st.session_state.get("form_novo_banco"))
            if st.button("✖ Fechar formulário" if _f_nb else "➕ Novo banco",
                         type="secondary" if _f_nb else "primary",
                         use_container_width=True):
                st.session_state["form_novo_banco"] = not _f_nb
                st.rerun()
        with c_bd3:
            if st.button("🧰 Console SQL", use_container_width=True):
                dialog_console_sql()
        if st.session_state.get("form_novo_banco"):
            with st.container(border=True):
                st.markdown("**Criar um banco novo no Postgres local** (com pgvector; "
                            "opcionalmente um usuário dono próprio).")
                with st.form("novo_bd", border=False):
                    f1, f2 = st.columns(2)
                    nb = f1.text_input("Nome do banco", placeholder="meu_app")
                    nu = f2.text_input("Usuário dono (opcional — vazio usa innova_app)",
                                       placeholder="meu_app_user")
                    okb = st.form_submit_button("➕ Criar banco", type="primary")
                if okb:
                    import re as _re
                    import secrets as _secrets
                    nome_b = (nb or "").strip().lower()
                    if not _re.fullmatch(r"[a-z_][a-z0-9_]{1,40}", nome_b):
                        st.error("Nome inválido — minúsculas, números e _ "
                                 "(começando por letra).")
                    else:
                        dono, senha_nova, errs = "innova_app", "", []
                        if (nu or "").strip():
                            dono = nu.strip().lower()
                            if not _re.fullmatch(r"[a-z_][a-z0-9_]{1,40}", dono):
                                errs.append("nome de usuário inválido")
                            else:
                                senha_nova = _secrets.token_hex(12)
                                ok_r, out_r = psql_run(
                                    f"create role {dono} login password "
                                    f"'{senha_nova}';", banco="postgres")
                                if not ok_r:
                                    errs.append(out_r[:300])
                        if not errs:
                            ok_c, out_c = psql_run(
                                f"create database {nome_b} owner {dono};",
                                banco="postgres")
                            if not ok_c:
                                errs.append(out_c[:300])
                        if errs:
                            st.error(" | ".join(errs))
                        else:
                            ok_x, _sx = psql_run(
                                "create extension if not exists vector;", banco=nome_b)
                            st.success(f"✅ Banco `{nome_b}` criado (dono `{dono}`"
                                       + (", pgvector ativado" if ok_x else "") + ").")
                            if senha_nova:
                                st.code(f"postgres://{dono}:{senha_nova}"
                                        f"@127.0.0.1:5432/{nome_b}", language="text")
                                st.caption("⚠️ Guarde a senha — mostrada SÓ agora.")

        ok_b, out_b = psql_run(
            "select d.datname as banco, pg_get_userbyid(d.datdba) as dono, "
            "pg_size_pretty(pg_database_size(d.datname)) as tamanho "
            "from pg_database d where not d.datistemplate order by 1;",
            banco="postgres")
        _lista_b = csv_linhas(out_b) if ok_b else []
        if not ok_b:
            st.error(out_b[:400])

        _bd_sel = st.session_state.get("bd_aberto")
        if _bd_sel and _bd_sel not in [b["banco"] for b in _lista_b]:
            _bd_sel = None
            st.session_state.pop("bd_aberto", None)

        if not _bd_sel:
            # ---- LISTA: um card por banco (estilo projetos do Git & Deploys) ----
            for b in _lista_b:
                with st.container(border=True):
                    cb1, cb2 = st.columns([4.4, 1.3], vertical_alignment="center")
                    _eh_innova = b["banco"] == "innova"
                    _eh_adm = b["banco"] == "postgres"
                    cb1.markdown(
                        f"**{'⚙️' if _eh_adm else '🗄️'} {b['banco']}**"
                        + (" · 🏠 banco do sistema (Innova/Escola Parque)"
                           if _eh_innova else "")
                        + (" · banco administrativo do motor (vem de fábrica — "
                           "não usar pra dados)" if _eh_adm else "")
                        + f"  \n<small>dono `{b['dono']}` · {b['tamanho']}"
                        + (" · API REST /rest/v1 ativa" if _eh_innova else
                           ("" if _eh_adm else " · acesso direto 5432")) + "</small>",
                        unsafe_allow_html=True,
                    )
                    if cb2.button("📂 Abrir", key=f"abrir_{b['banco']}",
                                  use_container_width=True):
                        st.session_state["bd_aberto"] = b["banco"]
                        st.rerun()

        else:
            # ---- DRILL-DOWN: dentro do banco ----
            if st.button("← Voltar aos bancos"):
                st.session_state.pop("bd_aberto", None)
                st.rerun()
            _info_b = next((b for b in _lista_b if b["banco"] == _bd_sel), {})
            st.subheader(f"🗄️ {_bd_sel} — {_info_b.get('tamanho', '?')} · "
                         f"dono `{_info_b.get('dono', '?')}`")
            t_tab, t_chave, t_adm = st.tabs(
                ["📋 Tabelas", "🔑 Conexão & Chaves", "👤 Usuários & Extensões"])

            with t_tab:
                ok_t, out_t = psql_run(
                    "select c.relname as tabela, "
                    "pg_size_pretty(pg_total_relation_size(c.oid)) as tamanho, "
                    "coalesce(s.n_live_tup,0) as linhas from pg_class c "
                    "join pg_namespace n on n.oid=c.relnamespace "
                    "left join pg_stat_user_tables s on s.relid=c.oid "
                    "where n.nspname='public' and c.relkind='r' "
                    "order by pg_total_relation_size(c.oid) desc;", banco=_bd_sel)
                _ts = csv_linhas(out_t) if ok_t else []
                if not ok_t:
                    st.error(out_t[:400])
                elif _ts:
                    st.dataframe(_ts, use_container_width=True, hide_index=True,
                                 height=min(420, 60 + 35 * len(_ts)))
                else:
                    st.info("Banco ainda sem tabelas"
                            + (" — o schema do Innova chega na FASE 3 da migração "
                               "(Drizzle)." if _bd_sel == "innova" else "."))

            with t_chave:
                cred = db_cred()
                _w, _a = cred.get("worker", {}), cred.get("app", {})
                if _bd_sel == "innova":
                    _anon, _serv = jwt_banco("anon"), jwt_banco("service_role")
                    if not _anon:
                        st.warning("Segredo do PostgREST não encontrado "
                                   "(~/.postgrest_jwt_secret) — FASE 2.5.")
                    else:
                        st.caption("⚠️ mostra segredos (painel logado). Duas "
                                   "fichas: 🌍 app FORA da VPS · 🏠 app NESTA VPS "
                                   "(conexão local — não sai pra internet pra voltar).")
                        st.markdown("**🌍 Apps EXTERNOS (fora desta VPS)**")
                        st.code(
                            f"Label        : 🏠 VPS Interno\n"
                            f"Supabase URL : {URL_BASE}\n"
                            f"Project ID   : vps-interno\n"
                            f"Region       : vps-local\n"
                            f"Anon Key     : {_anon}\n"
                            f"Service Key  : {_serv}\n"
                            f"DB Password  : {_w.get('pass', '?')}",
                            language="text")
                        st.caption(f"REST: `GET {URL_BASE}/rest/v1/<tabela>` com "
                                   "headers `apikey` + `Authorization: Bearer <key>`.")
                        st.markdown("**🏠 Apps NESTA VPS (preferir — latência ~0)**")
                        st.code(
                            f"Supabase URL : http://127.0.0.1:8088  (REST local — "
                            f"requer listener, kit abaixo)\n"
                            f"Anon/Service : as MESMAS chaves acima\n"
                            f"Worker/Python: postgres://{_w.get('user', '?')}:"
                            f"{_w.get('pass', '?')}@127.0.0.1:5432/innova\n"
                            f"App (Drizzle): postgres://{_a.get('user', '?')}:"
                            f"{_a.get('pass', '?')}@127.0.0.1:5432/innova",
                            language="text")
                        with st.expander("🔧 Kit do REST local (rodar 1x — só se "
                                         "for usar cliente Supabase DENTRO da VPS)"):
                            st.code(KIT_REST_LOCAL, language="bash")
                else:
                    st.caption("Banco sem API REST (a /rest/v1 serve o `innova`). "
                               "Acesso direto na porta local 5432:")
                    st.code(f"postgres://SEU_USUARIO:SENHA@127.0.0.1:5432/{_bd_sel}",
                            language="text")
                    st.markdown("<small>O usuário/senha são os criados junto com o "
                                "banco (➕ Novo banco) — senha mostrada na criação. "
                                "Apps no próprio servidor usam direto; do seu PC, "
                                "túnel SSH.</small>", unsafe_allow_html=True)
                st.markdown(f"<small>🔌 Do SEU PC (dev): "
                            f"<code>ssh -i CHAVE -L 5432:127.0.0.1:5432 "
                            f"ubuntu@{IP_PUBLICO}</code> e conecte em "
                            f"<code>localhost:5432</code>.</small>",
                            unsafe_allow_html=True)

            with t_adm:
                c_u, c_e = st.columns(2)
                ok_u, out_u = psql_run(
                    "select rolname as papel, rolcanlogin as faz_login, "
                    "rolcreatedb as cria_banco from pg_roles "
                    "where rolname not like 'pg\\_%' order by 1;", banco="postgres")
                if ok_u:
                    c_u.markdown("##### 👤 Usuários / papéis")
                    c_u.dataframe(csv_linhas(out_u), use_container_width=True,
                                  hide_index=True)
                ok_e, out_e = psql_run("select extname as extensao, extversion as "
                                       "versao from pg_extension order by 1;",
                                       banco=_bd_sel)
                if ok_e:
                    c_e.markdown("##### 🧩 Extensões")
                    c_e.dataframe(csv_linhas(out_e), use_container_width=True,
                                  hide_index=True)

    with tab_bk:
        BK_CFG = Path.home() / ".vps_backup.json"
        try:
            _bcfg = json.loads(BK_CFG.read_text())
        except Exception:
            _bcfg = {}
        _jobs = _bcfg.get("jobs") or []

        def _salvar_jobs():
            BK_CFG.write_text(json.dumps({"jobs": _jobs},
                                         ensure_ascii=False, indent=1))

        _DIAS_LBL = {1: "seg", 2: "ter", 3: "qua", 4: "qui", 5: "sex",
                     6: "sáb", 7: "dom"}
        c_bk1, c_bkd, c_bke, c_bk2 = st.columns([2.7, 1.0, 1.1, 1.3],
                                                vertical_alignment="center")
        c_bk1.caption(
            "Perfis independentes — agenda, destino e retenção próprios. "
            "O relógio confere a cada minuto e executa quem estiver no horário."
        )
        if c_bkd.button("☁️ Drive", use_container_width=True,
                        help="Conectar Google Drive (service account, sem "
                             "navegador no servidor)."):
            dialog_drive()
        if c_bke.button("⬇ Exportar", use_container_width=True,
                        help="Baixar dumps locais pelo navegador."):
            dialog_exportar()
        _f_nj = bool(st.session_state.get("form_novo_bk"))
        if c_bk2.button("✖ Fechar" if _f_nj else "➕ Novo backup",
                        type="secondary" if _f_nj else "primary",
                        use_container_width=True):
            st.session_state["form_novo_bk"] = not _f_nj
            st.rerun()
        if st.session_state.get("form_novo_bk"):
            with st.container(border=True):
                with st.form("novo_bk", border=False):
                    nbk1, nbk2, nbk3 = st.columns([2.4, 1.1, 1.4])
                    _nome_bk = nbk1.text_input("Nome do perfil",
                                               placeholder="☁️ Drive diário")
                    _hora_bk = nbk2.text_input("Horário (HH:MM)",
                                               value="03:30",
                                               placeholder="ex.: 09:15")
                    _ret_bk = nbk3.number_input("Guardar por (dias)", 1, 365, 7)
                    _dias_bk = st.multiselect("Dias da semana",
                                              list(_DIAS_LBL.values()),
                                              default=list(_DIAS_LBL.values()))
                    _bks_bk = st.multiselect(
                        "Bancos incluídos (vazio = TODOS, inclusive futuros)",
                        [b for b in _bancos if b != "postgres"], default=[])
                    _dest_bk = st.text_input(
                        "Destino — pasta local OU remote rclone",
                        placeholder="/home/ubuntu/backups_extra   ·   gdrive:BackupsVPS",
                        help="Pasta local começa com / . Nuvem usa remote:pasta "
                             "(configure antes com 'rclone config' no SSH — "
                             "Google Drive, OneDrive, S3...).")
                    _ok_nj = st.form_submit_button("Criar perfil 💾",
                                                   type="primary")
                if _ok_nj:
                    import re as _re
                    if not (_nome_bk.strip() and _dest_bk.strip()):
                        st.error("Preencha pelo menos nome e destino.")
                    elif not _re.fullmatch(r"([01]\\d|2[0-3]):[0-5]\\d",
                                           _hora_bk.strip()):
                        st.error("Horário inválido — use HH:MM (ex.: 09:15).")
                    else:
                        _jobs.append({
                            "id": (_re.sub(r"\\W+", "_",
                                           _nome_bk.strip().lower())[:28]
                                   + "_" + str(int(time.time()))[-4:]),
                            "nome": _nome_bk.strip(), "ativo": True,
                            "horario": _hora_bk.strip(),
                            "dias": [k for k, v in _DIAS_LBL.items()
                                     if v in _dias_bk] or [1, 2, 3, 4, 5, 6, 7],
                            "destino": _dest_bk.strip(),
                            "bancos": _bks_bk,
                            "manter_dias": int(_ret_bk)})
                        _salvar_jobs()
                        st.session_state["form_novo_bk"] = False
                        st.rerun()

        try:
            _est_bk = json.loads((Path.home() / ".vps_backup_estado.json"
                                  ).read_text())
        except Exception:
            _est_bk = {}
        for _j in list(_jobs):
            with st.container(border=True):
                cj1, cj2, cj3, cj5, cj4 = st.columns(
                    [3.4, 0.8, 1.0, 0.5, 0.4], vertical_alignment="center")
                _dias_txt = ("todos os dias" if len(_j.get("dias", [])) == 7
                             else "/".join(_DIAS_LBL[d]
                                           for d in _j.get("dias", [])))
                _ult_j = _est_bk.get(_j.get("id", ""), {})
                cj1.markdown(
                    f"**{_j.get('nome', _j.get('id', '?'))}**  \n"
                    f"<small>🕐 {_j.get('horario') or str(_j.get('hora', '03')) + ':30'} · {_dias_txt} · "
                    f"🗄️ {', '.join(_j.get('bancos')) if _j.get('bancos') else 'todos os bancos'} · "
                    f"📦 retém {_j.get('manter_dias', 7)}d · "
                    f"➡️ `{_j.get('destino', '?')}`  \n"
                    f"🧾 {_ult_j.get('quando', 'nunca rodou')} "
                    f"{_ult_j.get('resultado', '')}</small>",
                    unsafe_allow_html=True)
                _at_j = cj2.toggle("ligado", value=bool(_j.get("ativo")),
                                   key=f"bkat_{_j.get('id')}")
                if _at_j != bool(_j.get("ativo")):
                    _j["ativo"] = _at_j
                    _salvar_jobs()
                    st.rerun()
                if cj3.button("▶ Agora", key=f"bkrun_{_j.get('id')}",
                              use_container_width=True):
                    with st.spinner(f"Rodando {_j.get('nome')}..."):
                        _rc_j2, _out_j2 = _run(
                            ["python3", "/home/ubuntu/vps-admin/backup_pg.py",
                             "force", _j.get("id", "")], timeout=600)
                    (st.success if _rc_j2 == 0 else st.error)(
                        (_out_j2 or "")[-400:] or "ok")
                if cj5.button("✏️", key=f"bked_{_j.get('id')}",
                              help="Editar agenda, destino e retenção."):
                    dialog_editar_backup(_j.get("id", ""))
                if cj4.button("✕", key=f"bkdel_{_j.get('id')}",
                              help="Remove o perfil (não apaga os arquivos "
                                   "já gerados)."):
                    _jobs.remove(_j)
                    _salvar_jobs()
                    st.rerun()

        st.caption("Restaurar: `gunzip -c ARQ.sql.gz | sudo -u postgres psql "
                   "-d BANCO` · cada execução também guarda `configs_*.tgz` "
                   "(segredos do servidor).")

    with tab_lg:
        @st.fragment(run_every=6)
        def _logs_banco():
            c_lg1, c_lg2 = st.columns([2.6, 1.6])
            _fontes = {"💾 Backups (vpsbackup)": "vpsbackup",
                       "🐘 PostgreSQL": "postgresql",
                       "🔗 PostgREST (API)": "postgrest"}
            _f_lg = c_lg1.selectbox("Fonte do log", list(_fontes.keys()),
                                    key="lg_fonte")
            _n_lg = c_lg2.slider("Linhas", 20, 300, 80, step=20, key="lg_n")
            _rc_lg, _out_lg = _run(["journalctl", "-u", _fontes[_f_lg], "-n",
                                    str(_n_lg), "--no-pager", "-o", "short-iso"],
                                   timeout=8)
            _txt_lg = (_out_lg or "").strip()

            def _data_br(iso: str) -> str:
                """2026-06-04T08:30:24-0300 -> 04/06/2026 08:30"""
                try:
                    d, h = iso.split("T")
                    a, m, dd = d.split("-")
                    return f"{dd}/{m}/{a} {h[:5]}"
                except Exception:
                    return iso

            if _fontes[_f_lg] == "vpsbackup":
                _logf = Path.home() / ".vps_backup_log.jsonl"
                _evts = []
                try:
                    _evts = [json.loads(l) for l in
                             _logf.read_text().splitlines() if l.strip()]
                except Exception:
                    pass
                _linhas_l = []
                for _e in reversed(_evts[-int(_n_lg):]):
                    _res_e = _e.get("resultado", "")
                    _icone = ("✅" if _res_e.startswith("✅") else
                              "❌" if _res_e.startswith("❌") else "💤")
                    _q_e = _e.get("quando", "?")
                    try:
                        _d_e, _h_e = _q_e.split(" ")
                        _a_e, _m_e, _dd_e = _d_e.split("-")
                        _q_e = f"{_dd_e}/{_m_e}/{_a_e} {_h_e}"
                    except Exception:
                        pass
                    _det = _res_e.lstrip("✅❌💤 ").strip()
                    _linhas_l.append(f"{_q_e}  {_icone} "
                                     f"{_e.get('job', '—')} "
                                     f"({_e.get('modo', '?')}) · {_det}")
                with st.container(height=420):
                    st.code("\n".join(_linhas_l)
                            or "nenhuma execução registrada ainda — rode um "
                               "▶ Agora ou espere a ronda (xx:30)",
                            language="text")
            else:
                with st.container(height=420):
                    st.code((_txt_lg or "sem registros ainda")[-12000:],
                            language="text")
            st.caption("🔄 atualiza sozinho a cada 6s · cada linha = uma "
                       "execução (manual ou do relógio).")
        _logs_banco()


# ============================================================
# PAGINA: Alertas & Sentinela
# ============================================================

elif pagina == "🔔 Alertas":
    st.title("🔔 Alertas & Sentinela")
    st.markdown(ABAS_CSS, unsafe_allow_html=True)
    AL_CFG = Path.home() / ".vps_alertas.json"
    try:
        _acfg = json.loads(AL_CFG.read_text())
    except Exception:
        _acfg = {}
    _canais = _acfg.setdefault("canais", [])

    def _salvar_acfg():
        AL_CFG.write_text(json.dumps(_acfg, ensure_ascii=False, indent=1))

    _, _tm_s = _run(["systemctl", "is-active", "vpssentinela.timer"], timeout=5)
    _sent_on = _tm_s.strip() == "active"
    st.caption(f"Sentinela {'🟢 de ronda a cada 2 min' if _sent_on else '🔴 timer não instalado (kit no handoff)'} · "
               "monitora serviços (e REINICIA sozinha), worker travado, disco, "
               "certificado, backups atrasados e deploys ❌ — avisando por "
               "TODOS os canais ligados.")
    if st.button("💬 Enviar TESTE por todos os canais ligados", type="primary"):
        with st.spinner("Disparando teste..."):
            _rc_t, _out_t = _run(["python3",
                                  "/home/ubuntu/vps-admin/sentinela.py",
                                  "teste"], timeout=90)
        st.code(_out_t or "(sem saída)", language="text")

    tab_can, tab_reg, tab_push, tab_dia = st.tabs(
        ["📡 Canais", "⚙️ Regras", "📨 Servidor push", "🧾 Diário"])

    with tab_push:
        _ntfy_on = status_servico("ntfy") == "active"
        st.caption(f"Servidor de push de marca própria {'🟢' if _ntfy_on else '🔴 (instalar: bash vps-admin/instalar_ntfy.sh)'} · "
                   f"`https://ntfy.{DOMINIO}` · usuários e tópicos gerenciados "
                   "AQUI (sem SSH). Apps do framework publicam com 1 POST.")
        if _ntfy_on:
            _rc_u, _out_u = _run(["ntfy", "user", "list"], timeout=10)
            if _rc_u == 0 and _out_u:
                st.markdown("##### 👤 Usuários do push")
                with st.container(height=160):
                    st.code(_out_u, language="text")
            c_pu1, c_pu2 = st.columns(2)
            with c_pu1:
                with st.form("ntfy_user_add", border=True):
                    st.markdown("**➕ Criar usuário**")
                    _nu_n = st.text_input("Nome (ex.: sócio, app-innova)")
                    _nu_s = st.text_input("Senha", type="password")
                    _nu_t = st.text_input("Acesso aos tópicos (padrão)",
                                          value="vps-*",
                                          help="Curinga * vale: vps-*, escola-*…")
                    _ok_nu = st.form_submit_button("Criar", type="primary")
                if _ok_nu:
                    if not (_nu_n.strip() and _nu_s):
                        st.error("Nome e senha obrigatórios.")
                    else:
                        import os as _os2
                        _env_n = dict(_os2.environ, NTFY_PASSWORD=_nu_s)
                        try:
                            _r_add = subprocess.run(
                                ["ntfy", "user", "add", _nu_n.strip()],
                                capture_output=True, text=True,
                                timeout=15, env=_env_n)
                            _r_acl = subprocess.run(
                                ["ntfy", "access", _nu_n.strip(),
                                 _nu_t.strip() or "vps-*", "rw"],
                                capture_output=True, text=True, timeout=15)
                            if _r_add.returncode == 0 and _r_acl.returncode == 0:
                                st.success(f"✅ `{_nu_n.strip()}` criado com "
                                           f"acesso rw a `{_nu_t.strip()}`")
                            else:
                                st.error((_r_add.stderr + _r_acl.stderr)[-300:])
                        except Exception as e:  # noqa: BLE001
                            st.error(f"falha: {e}")
            with c_pu2:
                with st.form("ntfy_user_del", border=True):
                    st.markdown("**✕ Remover usuário**")
                    _du_n = st.text_input("Nome exato")
                    _ok_du = st.form_submit_button("Remover")
                if _ok_du and _du_n.strip():
                    _rc_d, _out_d = _run(["ntfy", "user", "del",
                                          _du_n.strip()], timeout=15)
                    (st.success if _rc_d == 0 else st.error)(
                        ("removido ✓" if _rc_d == 0 else (_out_d or "?")[-200:]))
            with st.form("ntfy_test_push", border=True):
                st.markdown("**💬 Enviar push de teste**")
                _tp1, _tp2, _tp3 = st.columns([1.4, 1.2, 1.2])
                _tp_top = _tp1.text_input("Tópico", value="vps-alertas")
                _tp_usu = _tp2.text_input("Usuário", value="diogo")
                _tp_sen = _tp3.text_input("Senha", type="password",
                                          key="tp_sen")
                _ok_tp = st.form_submit_button("📨 Enviar", type="primary")
            if _ok_tp:
                import base64 as _b64x
                import urllib.request as _ur
                try:
                    _req_t = _ur.Request(
                        "http://127.0.0.1:2586/" + _tp_top.strip(),
                        data="👋 Teste direto do painel VPS Admin!".encode(),
                        headers={"Title": "VPS Admin",
                                 "Authorization": "Basic " + _b64x.b64encode(
                                     f"{_tp_usu}:{_tp_sen}".encode()).decode()})
                    _ur.urlopen(_req_t, timeout=10)
                    st.success("✅ enviado — olha o celular!")
                except Exception as e:  # noqa: BLE001
                    st.error(f"falhou: {e}")
            st.caption("📲 No app ntfy (iPhone/Android): Subscribe → Use another "
                       f"server → `https://ntfy.{DOMINIO}` + tópico + usuário/senha.")

        st.divider()
        st.markdown("##### 💬 Evolution (Zap Push)")
        _evo_on = status_servico("evolution") == "active"
        st.caption(f"{'🟢' if _evo_on else '🔴'} serviço `evolution` · "
                   f"Manager: https://zap.{DOMINIO}/manager · instância "
                   "`sentinela` (chip-robô) · o botão ↗ Acessar dos "
                   "Aplicativos cai direto no dashboard dela.")
        try:
            _evo_key = (Path.home() / ".evolution_api_key").read_text().strip()
        except Exception:
            _evo_key = ""
        if _evo_key:
            with st.expander("🔑 Chave de API global (login do Manager — "
                             "mostra segredo)"):
                st.code(_evo_key, language="text")
                st.caption("Rotacionar: trocar AUTHENTICATION_API_KEY no "
                           "~/evolution-api/.env + `restart evolution` + "
                           "atualizar a chave no canal 💬 (✏️) e relogar o Manager.")
        else:
            st.info("Sem ~/.evolution_api_key — rode instalar_zap.sh (handoff).")



    with tab_can:
        c_ca1, c_ca2 = st.columns([4.2, 1.5], vertical_alignment="center")
        c_ca1.caption("Cada alerta sai por todos os canais 🟢. WhatsApp = fase 2 "
                      "(Evolution API no nosso VPS).")
        _f_nc = bool(st.session_state.get("form_novo_canal"))
        if c_ca2.button("✖ Fechar" if _f_nc else "➕ Novo canal",
                        type="secondary" if _f_nc else "primary",
                        use_container_width=True):
            st.session_state["form_novo_canal"] = not _f_nc
            st.rerun()
        if st.session_state.get("form_novo_canal"):
            with st.container(border=True):
                _tipo_c = st.selectbox("Tipo", ["📱 ntfy.sh (push no celular)",
                                                "💬 WhatsApp (Evolution)",
                                                "🌐 Web Push (Innova/navegadores)",
                                                "✈️ Telegram (bot)",
                                                "📧 E-mail (Gmail/SMTP)"])
                _novo_c = {"ativo": True,
                           "id": f"c{int(time.time())}"}
                if _tipo_c.startswith("📱"):
                    st.caption("No app **ntfy** do celular: Subscribe to topic → "
                               "**Use another server** → cole o servidor abaixo + "
                               "tópico + usuário/senha. (Servidor vazio = ntfy.sh "
                               "público, sem senha.)")
                    _srv_c = st.text_input(
                        "Servidor ntfy",
                        value=f"https://ntfy.{DOMINIO}",
                        help="O NOSSO servidor de push (Pacote 5). Apague pra "
                             "usar o ntfy.sh público.")
                    _top_c = st.text_input("Tópico",
                                           placeholder="ex.: vps-alertas")
                    _cu1, _cu2 = st.columns(2)
                    _usu_n = _cu1.text_input("Usuário (se o servidor exigir)")
                    _sen_n = _cu2.text_input("Senha", type="password",
                                             key="ntfy_pw")
                    _novo_c.update({"tipo": "ntfy", "nome": "📱 ntfy",
                                    "servidor": _srv_c.strip(),
                                    "topico": _top_c.strip(),
                                    "usuario": _usu_n.strip(),
                                    "senha": _sen_n.strip()})
                    _valido = bool(_top_c.strip())
                elif _tipo_c.startswith("💬"):
                    st.caption("Usa a Evolution API do framework (instância já "
                               "conectada com o chip-robô). O alerta chega como "
                               "mensagem de WhatsApp no número destino.")
                    _srv_w = st.text_input("Servidor Evolution",
                                           value=f"https://zap.{DOMINIO}")
                    _ins_w = st.text_input("Instância", value="sentinela")
                    _key_w = st.text_input("API key (em ~/.evolution_api_key)",
                                           type="password")
                    _num_w = st.text_input("Número DESTINO (com 55+DDD)",
                                           placeholder="ex.: 5521999999999")
                    _novo_c.update({"tipo": "whatsapp", "nome": "💬 WhatsApp",
                                    "servidor": _srv_w.strip(),
                                    "instancia": _ins_w.strip() or "sentinela",
                                    "apikey": _key_w.strip(),
                                    "numero": _num_w.strip()})
                    _valido = bool(_key_w.strip() and _num_w.strip())
                elif _tipo_c.startswith("🌐"):
                    st.caption("Manda o alerta como notificação de NAVEGADOR "
                               "para todos os dispositivos inscritos no Innova "
                               "(o card 🔔 das Configurações do app). Requer "
                               "PUSH_SECRET no .env.local do frontend.")
                    _srv_wp = st.text_input(
                        "Servidor do app",
                        value="https://escolaparque-app.duckdns.org")
                    _seg_wp = st.text_input("PUSH_SECRET", type="password")
                    _novo_c.update({"tipo": "webpush", "nome": "🌐 Web Push",
                                    "servidor": _srv_wp.strip(),
                                    "segredo": _seg_wp.strip()})
                    _valido = bool(_seg_wp.strip())
                elif _tipo_c.startswith("✈️"):
                    st.caption("No Telegram: fale com **@BotFather** → /newbot → "
                               "copie o token. Depois mande um 'oi' pro seu bot "
                               "e pegue seu chat_id falando com **@userinfobot**.")
                    _tok_c = st.text_input("Token do bot", type="password")
                    _chat_c = st.text_input("Chat ID", placeholder="ex.: 123456789")
                    _novo_c.update({"tipo": "telegram", "nome": "✈️ Telegram",
                                    "token": _tok_c.strip(),
                                    "chat": _chat_c.strip()})
                    _valido = bool(_tok_c.strip() and _chat_c.strip())
                else:
                    st.caption("Gmail: ative verificação em 2 etapas → "
                               "myaccount.google.com/apppasswords → gere uma "
                               "'senha de app' (16 letras) e cole aqui.")
                    _usu_c = st.text_input("Seu Gmail (remetente)",
                                           placeholder="voce@gmail.com")
                    _sen_c = st.text_input("Senha de app", type="password")
                    _para_c = st.text_input("Enviar para (vazio = você mesmo)")
                    _novo_c.update({"tipo": "email", "nome": "📧 E-mail",
                                    "usuario": _usu_c.strip(),
                                    "senha_app": _sen_c.strip().replace(" ", ""),
                                    "para": _para_c.strip()})
                    _valido = bool(_usu_c.strip() and _sen_c.strip())
                if st.button("➕ Adicionar canal", type="primary"):
                    if not _valido:
                        st.error("Preencha os campos do canal.")
                    else:
                        _canais.append(_novo_c)
                        _salvar_acfg()
                        st.session_state["form_novo_canal"] = False
                        st.rerun()
        for _c in list(_canais):
            with st.container(border=True):
                cc1, cc2, cc5, cc4, cc3 = st.columns(
                    [3.4, 0.9, 0.5, 0.5, 0.5], vertical_alignment="center")
                _det_c = {"ntfy": f"tópico `{_c.get('topico', '?')}` em "
                                  f"`{(_c.get('servidor') or 'ntfy.sh').replace('https://', '')}`",
                          "whatsapp": f"instância `{_c.get('instancia', '?')}` → "
                                      f"`{_c.get('numero', '?')}`",
                          "webpush": f"navegadores inscritos no "
                                     f"`{(_c.get('servidor') or '?').replace('https://', '')}`",
                          "telegram": f"chat `{_c.get('chat', '?')}`",
                          "email": f"`{_c.get('usuario', '?')}` → "
                                   f"`{_c.get('para') or _c.get('usuario', '?')}`"
                          }.get(_c.get("tipo"), "")
                cc1.markdown(f"**{_c.get('nome', _c.get('tipo'))}**  \n"
                             f"<small>{_det_c}</small>", unsafe_allow_html=True)
                _at_c = cc2.toggle("ligado", value=bool(_c.get("ativo", True)),
                                   key=f"cal_{_c.get('id')}")
                if _at_c != bool(_c.get("ativo", True)):
                    _c["ativo"] = _at_c
                    _salvar_acfg()
                    st.rerun()
                if cc5.button("⚡", key=f"cping_{_c.get('id')}",
                              help="Testar SÓ este canal agora."):
                    import json as _jp
                    _msg_p = ("Ping do canal "
                              + str(_c.get("nome", _c.get("tipo")))
                              + " - testando!")
                    _rc_p, _out_p = _run(
                        ["python3",
                         "/home/ubuntu/vps-admin/sentinela.py",
                         "ping1", _jp.dumps(_c), _msg_p], timeout=40)
                    _txt_p = (_out_p or "").strip().splitlines()
                    _res_p = _txt_p[-1] if _txt_p else "(sem saída)"
                    st.toast(f"{_c.get('nome')}: {_res_p}")
                if cc4.button("✏️", key=f"ced_{_c.get('id')}",
                              help="Editar este canal."):
                    dialog_editar_canal(_c.get("id", ""))
                if cc3.button("✕", key=f"cdel_{_c.get('id')}"):
                    _canais.remove(_c)
                    _salvar_acfg()
                    st.rerun()
        if not _canais:
            st.info("Nenhum canal ainda — ➕ Novo canal (o ntfy leva 1 minuto).")

    with tab_reg:
        _r1, _r2 = st.columns(2)
        _g_on = _r1.toggle("🟢 Sentinela ativa",
                           value=bool(_acfg.get("ativo", True)))
        _g_ar = _r2.toggle("🔧 Auto-restart (cura sozinha)",
                           value=bool(_acfg.get("auto_restart", True)))
        _n1, _n2, _n3, _n4 = st.columns(4)
        _hb = _n1.number_input("Worker mudo (min)", 2, 60,
                               int(_acfg.get("heartbeat_min", 5)))
        _dc = _n2.number_input("Disco cheio (%)", 50, 99,
                               int(_acfg.get("disco_pct", 85)))
        _ct = _n3.number_input("Cert vence (dias)", 3, 60,
                               int(_acfg.get("cert_dias", 14)))
        _bk = _n4.number_input("Backup atrasado (h)", 2, 168,
                               int(_acfg.get("backup_horas", 26)))
        _svc_mon = st.multiselect(
            "Serviços vigiados", list(todos_servicos().keys()),
            default=[s for s in _acfg.get("servicos",
                     list(todos_servicos().keys()))
                     if s in todos_servicos()])
        if st.button("💾 Salvar regras", type="primary"):
            _acfg.update({"ativo": _g_on, "auto_restart": _g_ar,
                          "heartbeat_min": int(_hb), "disco_pct": int(_dc),
                          "cert_dias": int(_ct), "backup_horas": int(_bk),
                          "servicos": _svc_mon})
            _salvar_acfg()
            st.success("Regras salvas — valem na próxima ronda (≤2 min).")
        st.caption("Anti-spam: avisa na hora do problema e re-avisa a cada 6h "
                   "enquanto durar; manda ✅ quando resolve.")

    with tab_dia:
        try:
            _evts_a = [json.loads(l) for l in
                       (Path.home() / ".vps_alertas_log.jsonl"
                        ).read_text().splitlines() if l.strip()]
        except Exception:
            _evts_a = []
        if not _evts_a:
            st.info("Nenhum alerta registrado ainda — bom sinal. 😴")
        else:
            _linhas_a = []
            for _e in reversed(_evts_a[-100:]):
                _q_a = _e.get("quando", "?")
                try:
                    _d_a, _h_a = _q_a.split(" ")
                    _a_a, _m_a, _dd_a = _d_a.split("-")
                    _q_a = f"{_dd_a}/{_m_a}/{_a_a} {_h_a}"
                except Exception:
                    pass
                _linhas_a.append(f"{_q_a}  "
                                 + ("📤" if _e.get("enviado") else "📵")
                                 + f" {_e.get('msg', '')}")
            with st.container(height=380):
                st.code("\n".join(_linhas_a), language="text")
            st.caption("📤 = entregue nos canais · 📵 = gerado mas nenhum canal "
                       "entregou (conferir credenciais)")


# ============================================================
# PAGINA: Acesso MCP (Claude)
# ============================================================

elif pagina == "🔌 Acesso MCP (Claude)":
    on = mcp_online()
    c_t, c_s, c_p = st.columns([4, 1.6, 0.5], vertical_alignment="center")
    with c_t:
        st.title("🔌 Acesso MCP")
    with c_s:
        st.markdown("🟢 **Servidor MCP Online**" if on else "🔴 **MCP Offline**")
    ping_mcp = c_p.button("⚡", key="ping_mcp", use_container_width=True,
                          help="Ping — rastreia o fluxo interno do MCP (serviço → porta → rota → mundo)")
    if ping_mcp:
        with st.spinner("Rastreando o fluxo do MCP..."):
            _passos = mcp_ping_fluxo()
        with st.container(border=True):
            st.markdown("**📋 Log do fluxo:**")
            for _ok, _txt in _passos:
                st.markdown(("✅ " if _ok else "❌ ") + _txt)
            if all(p[0] for p in _passos):
                st.success("⚡ Fluxo 100% — o Claude consegue chegar até aqui.")
            else:
                st.error("Fluxo interrompido na primeira etapa com ❌ — comece a investigar por ela.")
    st.caption(
        "Dê ao Claude (ou outro agente) acesso DIRETO e seguro ao seu servidor via MCP. "
        "Ele poderá ler/editar arquivos dos apps, reiniciar serviços e ver logs — sem você "
        "ficar fazendo scp na mão. **Nível 1 (Operador):** sem shell livre, sem root, só as pastas dos apps."
    )

    token = mcp_token_atual()
    st.divider()
    st.subheader("🔑 Token de acesso")
    if not token:
        st.warning("Nenhum token ativo. Gere um pra liberar a conexão.")
    else:
        c_msg, c_ver = st.columns([4.6, 1.2], vertical_alignment="center")
        c_msg.success("Token ativo — cole a URL no conector do Claude.")
        with c_ver.popover("👁️ Ver URL", use_container_width=True):
            st.code(f"{URL_BASE}/mcp-{token}/mcp", language="text")
            st.caption("⚠️ Quem tiver esta URL controla os apps do servidor. Trate como senha.")

    c1, c2 = st.columns(2)
    if c1.button("🔄 Gerar/Renovar token", type="primary", use_container_width=True):
        novo = mcp_gerar_token()
        if novo:
            st.session_state["mcp_novo"] = novo
            st.rerun()
        else:
            st.error("Falha ao gravar o token.")
    if token and c2.button("🗑️ Revogar acesso", use_container_width=True):
        try:
            MCP_TOKEN_PATH.unlink()
        except Exception:
            pass
        st.rerun()

    if st.session_state.get("mcp_novo"):
        st.info("Novo token gerado! URL de conexão (copie e cole no Claude):")
        st.code(f"{URL_BASE}/mcp-{st.session_state['mcp_novo']}/mcp", language="text")
        st.caption("⚠️ Renovar o token INVALIDA a URL antiga. Atualize no conector se já estava conectado.")
        if st.button("✅ Copiei"):
            st.session_state.pop("mcp_novo", None)
            st.rerun()

    st.divider()
    st.subheader("🧰 Ferramentas liberadas (Nível 1 — Operador)")
    st.markdown(
        "- 📂 **listar_pastas / ler_arquivo / escrever_arquivo** — só dentro das pastas dos apps (faz backup .bak)\n"
        "- 🚦 **servico** (status/restart/stop/start) e **logs** — só serviços da whitelist\n"
        "- 📊 **recursos** — CPU/RAM/disco/uptime\n"
        "- 🌿 **git** (status/pull/log/diff/fetch) — nas pastas dos apps\n\n"
        "🔒 *Sem shell livre, sem root, sem sair das pastas permitidas. Para subir de nível, é decisão consciente.*"
    )

    st.divider()
    with st.expander("📋 Como conectar no app do Claude"):
        st.markdown(
            "1. Gere o token acima e copie a **URL de conexão**.\n"
            "2. No app do Claude: **Configurações → Conectores → Adicionar conector personalizado**.\n"
            "3. Cole a URL. Pronto — nas conversas, o Claude ganha as ferramentas do seu VPS.\n\n"
            "Setup do serviço no servidor: `vps_mcp/SETUP.md`."
        )


# ============================================================
# PAGINA: Disco & Sistema
# ============================================================

elif pagina == "💾 Servidor & Limites":
    _oracle = "oracle" in PROVEDOR.lower()
    st.title("💾 Servidor & Limites" + (" (Always Free)" if _oracle else ""))

    # ---- Histórico de uso (coletor a cada 1 min) ----
    st.subheader("📈 Histórico de uso")
    _hist_ok = (Path.home() / ".vps_metricas.csv").exists()
    if not _hist_ok:
        st.info("Coletor de métricas ainda não instalado — kit no handoff "
                "(timer vpsmetricas). Os gráficos aparecem após a 1ª coleta.")
    else:
        _jan = st.radio("Janela", ["6h", "24h", "7 dias"], horizontal=True,
                        index=1, key="met_jan")
        _horas = {"6h": 6, "24h": 24, "7 dias": 168}[_jan]
        _dados_m = ler_metricas(_horas)
        if not _dados_m:
            st.caption("Sem dados nessa janela ainda — aguarde o coletor.")
        else:
            import pandas as _pd
            _df = _pd.DataFrame(_dados_m).set_index("hora")
            c_g1, c_g2 = st.columns(2)
            c_g1.markdown("**CPU & RAM (%)**")
            c_g1.line_chart(_df[["CPU %", "RAM %"]], height=220)
            c_g2.markdown("**Disco (%) & Load**")
            c_g2.line_chart(_df[["Disco %", "Load"]], height=220)
            _u = _dados_m[-1]
            st.caption(f"🔎 {len(_dados_m)} pontos · agora: CPU {_u['CPU %']:.0f}% "
                       f"· RAM {_u['RAM %']:.0f}% · Disco {_u['Disco %']:.0f}% "
                       f"· Load {_u['Load']:.2f} · coleta a cada 1 min")
    st.divider()

    # ---- Identidade do Servidor (fonte única de verdade — estilo WordPress "Site URL") ----
    st.subheader("🌍 Identidade do Servidor")
    with st.container(border=True):
        _val = cert_validade(DOMINIO)
        c_id, c_mig = st.columns([4, 1.4], vertical_alignment="center")
        if _oracle:
            c_mig.link_button(
                "🔶 Abrir na Oracle",
                "https://cloud.oracle.com/compute/instances?region=sa-saopaulo-1",
                use_container_width=True,
                help="Console da Oracle Cloud.")
        c_id.markdown(
            f"**Domínio:** [`{DOMINIO}`]({URL_BASE}) · **IP:** `{IP_PUBLICO}`  \n"
            f"🔒 **HTTPS Let's Encrypt** — "
            + (f"certificado válido até `{_val}`" if _val else "⚠️ não consegui ler o certificado")
            + " · renovação automática (certbot)  \n"
            f"🦆 DNS: **DuckDNS** · "
            f"📄 Fonte única: `~/.vps_config.json` — mudou lá, o painel INTEIRO se adapta.  \n"
            f"⭐ Este é o **domínio-mãe** (painel, API LLM, MCP, campainha do webhook). "
            f"Domínios extras de apps — ex.: frontend — vivem na página 🌐 Domínios & Rotas."
        )
        if c_mig.button("🔁 Migrar domínio", use_container_width=True,
                        help="Troca o domínio/HTTPS do servidor: grava a nova config e "
                             "gera o kit de comandos (certbot + Nginx)."):
            st.session_state["form_migrar"] = not st.session_state.get("form_migrar", False)

    # ---- Página inicial do domínio (pra onde a raiz / redireciona) ----
    KIT_ROTA_RAIZ = """sudo tee /usr/local/bin/vps_rota_raiz.sh > /dev/null <<'EOF'
#!/bin/bash
set -e
ROTA="$1"
echo "$ROTA" | grep -Eq '^/[a-zA-Z0-9/_-]*$' || { echo "rota invalida"; exit 1; }
CONF=/etc/nginx/sites-available/apps
if grep -q "location = /" "$CONF"; then
  sed -i "s|location = / { return 302 [^;]*; }|location = / { return 302 $ROTA; }|" "$CONF"
else
  sed -i "/listen 443 ssl/a\\    location = / { return 302 $ROTA; }" "$CONF"
fi
nginx -t && systemctl reload nginx
EOF
sudo chmod 755 /usr/local/bin/vps_rota_raiz.sh
echo 'ubuntu ALL=(ALL) NOPASSWD: /usr/local/bin/vps_rota_raiz.sh' | sudo tee /etc/sudoers.d/vpsadmin-rota > /dev/null
sudo chmod 440 /etc/sudoers.d/vpsadmin-rota"""
    with st.container(border=True):
        c_h, c_sel, c_ok = st.columns([2.7, 1.8, 1], vertical_alignment="bottom")
        c_h.markdown(
            "**🏠 Página inicial do domínio**  \n"
            f"Quem abre `{DOMINIO}/` cai em qual app?"
        )
        _rotas_disp = sorted(set(ROTAS_APPS.values()))
        _raiz_atual = _cfg.get("rota_raiz", "/escola-parque/")
        _idx_raiz = _rotas_disp.index(_raiz_atual) if _raiz_atual in _rotas_disp else 0
        rota_home = c_sel.selectbox("Rota padrão da raiz", _rotas_disp, index=_idx_raiz)
        if c_ok.button("Salvar 🏠", type="primary", use_container_width=True):
            rc_h, out_h = _run(["sudo", "-n", "/usr/local/bin/vps_rota_raiz.sh", rota_home],
                               timeout=30)
            if rc_h == 0:
                config_salvar("rota_raiz", rota_home)
                st.success(f"✅ Raiz `/` agora abre **{rota_home}** — testa: {URL_BASE}/")
            else:
                st.error("Helper não instalado ainda (ou falhou). Instala com o kit abaixo "
                         "(uma vez só, no SSH): " + (out_h or "")[:200])
                st.code(KIT_ROTA_RAIZ, language="bash")

    if st.session_state.get("form_migrar"):
        with st.container(border=True):
            with st.form("migrar_https", border=False):
                m1, m2, m3 = st.columns([2.4, 1.6, 1], vertical_alignment="bottom")
                novo_dom = m1.text_input("Novo domínio (DNS já apontando pro IP)", value=DOMINIO)
                novo_ip = m2.text_input("IP público", value=IP_PUBLICO)
                gerar_mig = m3.form_submit_button("Gerar kit 🔧", type="primary",
                                                  use_container_width=True)
            if gerar_mig and novo_dom.strip():
                _nd, _ni = novo_dom.strip(), novo_ip.strip()
                try:
                    CONFIG_PATH.write_text(json.dumps({"ip": _ni, "dominio": _nd}, indent=2))
                    st.success(f"`~/.vps_config.json` gravado → painel passa a usar "
                               f"**https://{_nd}** após o passo 3 do kit.")
                except Exception as e:  # noqa: BLE001
                    st.error(f"Falha ao gravar a config: {e}")
                st.markdown("**KIT DE MIGRAÇÃO — rode no terminal SSH (bloco único):**")
                st.code(
                    f"""# 1) Nginx atende pelo novo nome
sudo sed -i 's/server_name[^;]*;/server_name {_nd};/' /etc/nginx/sites-available/apps
sudo nginx -t && sudo systemctl reload nginx

# 2) Certificado HTTPS do novo dominio (renovacao automatica inclusa)
sudo certbot --nginx -d {_nd} --redirect -m diogobsbastos@gmail.com --agree-tos --no-eff-email

# 3) Painel rele a config
sudo systemctl restart vpsadmin

# 4) Teste
curl -s -o /dev/null -w "%{{http_code}}\\n" https://{_nd}/admin/""",
                    language="bash",
                )
                st.caption("⚠️ Depois da migração: atualizar a URL do conector MCP no Claude "
                           "e o base_url dos clientes da API da LLM (o domínio antigo para de valer).")

    st.divider()

    # ---- Specs da maquina ----
    st.subheader("🖥️ Especificações desta instância")
    disco = psutil.disk_usage("/") if psutil else None
    if psutil:
        n_ocpu = psutil.cpu_count(logical=True) or 4
        ram_total = psutil.virtual_memory().total / 1e9
        _, arch = _run(["uname", "-m"])
        s1, s2, s3, s4 = st.columns(4)
        s1.metric("OCPUs (vCPU)", n_ocpu)
        s2.metric("RAM total", f"{ram_total:.0f} GB")
        s3.metric("Disco /", f"{disco.total/1e9:.0f} GB")
        s4.metric("Arquitetura", (arch or "aarch64"))
        st.caption(
            (f"Shape **VM.Standard.A1.Flex** (Ampere ARM) · Brazil East (São Paulo) · "
             if _oracle else f"**{PROVEDOR}** · {ARCH_CURTA} · ")
            + f"IP `{IP_PUBLICO}`" + (" · conta **Always Free**" if _oracle else "")
        )

    st.divider()
    st.subheader("📊 Uso em tempo real")
    if psutil:
        cpu = psutil.cpu_percent(interval=0.5)
        mem = psutil.virtual_memory()
        r1, r2, r3, r4 = st.columns(4)
        r1.metric("CPU", f"{cpu:.0f}%"); r1.progress(min(cpu/100, 1.0))
        r1.caption(f"{psutil.cpu_count()} vCPUs {ARCH_CURTA}")
        r2.metric("RAM", f"{mem.percent:.0f}%"); r2.progress(min(mem.percent/100, 1.0))
        r2.caption(f"{mem.used/1e9:.1f} de {mem.total/1e9:.0f} GB")
        r3.metric("Disco", f"{disco.percent:.0f}%"); r3.progress(min(disco.percent/100, 1.0))
        r3.caption(f"{disco.used/1e9:.1f} de {disco.total/1e9:.0f} GB")
        try:
            carga = ", ".join(f"{x:.2f}" for x in psutil.getloadavg())
        except Exception:
            carga = "—"
        _, uptime = _run(["uptime", "-p"])
        r4.metric("Load 1/5/15m", carga); r4.caption(f"⏱️ {uptime}")

    if _oracle:
        st.divider()
        st.subheader("💰 Cota gratuita × cobrança")
        st.caption(
            "A Oracle cobra se você ULTRAPASSAR estes limites mensais. Consumo ESTIMADO desta "
            "instância 24/7 — pra você ver a folga ANTES de criar mais recursos."
        )
        st.caption("🟢 Folga · 🟡 No teto do gratuito (continua R$ 0) · 🔴 Ultrapassou (gera cobrança)")

        AJUDA_OCPU = (
            "Pense num plano pré-pago de CPU: você ganha 3.000 'horas-de-CPU' grátis por mês. "
            "Sua máquina tem 4 CPUs, então cada hora ligada gasta 4 horas do bolo. "
            "4 CPUs × ~720h do mês = ~2.880h. Está DENTRO do limite = R$ 0. "
            "Só cobraria se passasse de 3.000 (ex.: ligando uma 2ª máquina ARM 24/7)."
        )
        AJUDA_RAM = (
            "Mesma lógica, mas pra memória: 18.000 'GB-horas' grátis por mês. "
            "Seus 24 GB ligados o mês todo = ~17.300 GB-h. Dentro do limite = R$ 0. "
            "É o teto esperado de quem usa a máquina máxima do gratuito — está tudo certo."
        )

        import calendar as _cal
        from datetime import datetime as _dt
        _h = _cal.monthrange(_dt.utcnow().year, _dt.utcnow().month)[1] * 24
        n_ocpu = (psutil.cpu_count(logical=True) or 4) if psutil else 4
        ram_gb = round((psutil.virtual_memory().total / 1e9) if psutil else 24)
        ocpu_h, gb_h, LIM_O, LIM_G = n_ocpu * _h, ram_gb * _h, 3000, 18000
        egress_gb = (psutil.net_io_counters().bytes_sent / 1e9) if psutil else 0.0

        def _lim(nome, usado, limite, un, ajuda=None):
            pct_real = (usado / limite) if limite else 0
            if pct_real > 1.0:
                tag, cor = "🔴 Ultrapassou (cobrança)", "#fbeae7"
            elif pct_real >= 0.8:
                tag, cor = "🟡 No teto do gratuito (R$ 0)", "#fdf3df"
            else:
                tag, cor = "🟢 Folga", "#e6f4ec"
            with st.container(border=True):
                c1, c2, c3 = st.columns([3, 1.4, 1.6])
                c1.markdown(f"**{nome}**"); c1.progress(min(pct_real, 1.0))
                c2.metric("Usado (est.)", f"{usado:,.0f} {un}".replace(",", "."), help=ajuda)
                c3.markdown(
                    f"<div style='background:{cor};border-radius:8px;padding:6px 10px;text-align:center;font-size:0.85em;'>"
                    f"{tag}<br>{pct_real*100:.0f}% de {limite:,} {un}</div>".replace(",", "."),
                    unsafe_allow_html=True,
                )

        _lim("Compute ARM — OCPU-horas/mês ❓", ocpu_h, LIM_O, "h", ajuda=AJUDA_OCPU)
        _lim("Compute ARM — GB-horas/mês (RAM) ❓", gb_h, LIM_G, "h", ajuda=AJUDA_RAM)
        _lim("Block Storage (disco usado)", (disco.used/1e9) if psutil else 47, 200, "GB")
        _lim("Tráfego de saída (desde o boot)", egress_gb, 10000, "GB")

        st.warning(
            f"⚠️ **Leitura crítica:** esta instância (4 OCPU / 24 GB, 24/7) já consome "
            f"~{ocpu_h/LIM_O*100:.0f}% da cota gratuita de **compute ARM** sozinha. "
            "Criar uma SEGUNDA instância ARM ligada o tempo todo **passa do limite e gera cobrança**. "
            "Instâncias desligadas não contam horas. Disco (200 GB) e tráfego (10 TB) têm muita folga."
        )

    st.divider()
    st.subheader("📁 Maiores pastas (home)")
    rc, out = _run(["bash", "-c", "du -sh /home/ubuntu/*/ 2>/dev/null | sort -rh | head -10"], timeout=60)
    st.code(out or "—")
    st.caption("Cota de disco: 200 GB no total (boot volume ~48 GB; resto disponível p/ expandir/anexar, grátis).")


# ============================================================
# PAGINA: Conta
# ============================================================

elif pagina == "👤 Conta":
    st.title("👤 Conta do administrador")
    usuario = carregar_usuario()
    col_dados, col_senha = st.columns(2)

    with col_dados:
        with st.container(border=True):
            st.markdown("**Dados cadastrados**")
            novo_nome = st.text_input("Nome", value=usuario.get("nome", ""))
            novo_email = st.text_input("E-mail (recuperação/contato)", value=usuario.get("email", ""))
            if st.button("💾 Salvar dados", use_container_width=True):
                if salvar_usuario({"nome": novo_nome.strip(), "email": novo_email.strip()}):
                    st.success("Dados salvos.")
                    time.sleep(1)
                    st.rerun()
                else:
                    st.error("Falha ao salvar (permissões?).")

    with col_senha:
        with st.container(border=True):
            st.markdown("**Trocar senha do painel**")
            s_atual = st.text_input("Senha atual", type="password", key="pw_atual")
            s_nova = st.text_input("Nova senha (mín. 8 caracteres)", type="password", key="pw_nova")
            s_conf = st.text_input("Confirmar nova senha", type="password", key="pw_conf")
            if st.button("🔒 Trocar senha", type="primary", use_container_width=True):
                if not checar_senha(s_atual):
                    st.error("Senha atual incorreta.")
                elif len(s_nova) < 8:
                    st.error("A nova senha precisa ter pelo menos 8 caracteres.")
                elif s_nova != s_conf:
                    st.error("A confirmação não confere.")
                else:
                    try:
                        SENHA_PATH.write_text(s_nova)
                        SENHA_PATH.chmod(0o600)
                        st.success("Senha trocada! Use a nova no próximo login.")
                    except Exception as e:  # noqa: BLE001
                        st.error(f"Falha ao trocar senha: {e}")

st.sidebar.caption("VPS Admin v2.3-educado · base replicável p/ futuras VPS")
