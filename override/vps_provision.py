#!/usr/bin/python3
# ============================================================
# vps_provision — braço ROOT do "➕ Novo App" (Pacote C)
# ============================================================
# Chamado SOMENTE pelo painel VPS Admin via sudoers restrito:
#   ubuntu ALL=(root) NOPASSWD: /usr/local/bin/vps_provision
#
# Subcomandos:
#   criar <plano.json>  -> systemd unit + rota Nginx (atômico, rollback)
#   remover <nome>      -> desfaz unit + rota (SÓ apps com a marca)
#   listar              -> JSON dos apps gerenciados
#
# Segurança (por que este script pode ter sudo):
#   - User=ubuntu SEMPRE forçado na unit (nada roda como root)
#   - ExecStart é montado AQUI a partir de campos validados por
#     regex estrito (painel não injeta comando arbitrário)
#   - serviços do sistema em lista negra; só toca unit com a marca
#   - mexe só em sites-AVAILABLE (nunca sites-enabled) e roda
#     nginx -t com rollback automático antes de recarregar
# ============================================================
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

NGINX_CONF = Path("/etc/nginx/sites-available/apps")
SYSTEMD = Path("/etc/systemd/system")
MARCA = "managed-by: vps_provision"
HOME = Path("/home/ubuntu")

RX_NOME = re.compile(r"^[a-z][a-z0-9-]{2,29}$")
RX_ROTA = re.compile(r"^/[a-z][a-z0-9-]{1,29}$")
RX_PRINCIPAL = re.compile(r"^[A-Za-z0-9_.-]+\.py$")
RX_DOMINIO = re.compile(r"^(?=.{1,253}$)([a-z0-9](-?[a-z0-9])*\.)+[a-z]{2,}$")

BLACKLIST = {
    "nginx", "ssh", "sshd", "cron", "postgresql", "postgrest", "ollama",
    "ntfy", "evolution", "vpsadmin", "vpswebhook", "vpsmcp", "llmgateway",
    "escolaparque", "escolaparque-worker", "sertanejolab", "innovafront",
    "vpsautodeploy", "vpssentinela", "vpsbackup", "vpsmetricas", "duckdns",
    "certbot", "snapd", "unattended-upgrades",
}
TEMPLATES = ("streamlit",)


def die(msg: str) -> None:
    print(f"ERRO: {msg}")
    sys.exit(1)


def run(cmd: list, timeout: int = 60):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout + r.stderr).strip()


def unit_path(nome: str) -> Path:
    return SYSTEMD / f"{nome}.service"


def gerenciada(nome: str) -> bool:
    p = unit_path(nome)
    try:
        return p.exists() and MARCA in p.read_text()
    except Exception:
        return False


def porta_ocupada(porta: int) -> bool:
    _, out = run(["ss", "-ltnH"])
    alvo = f":{porta} "
    return any(alvo in (ln + " ") for ln in out.splitlines())


def bloco_nginx(nome: str, rota: str, porta: int) -> str:
    return (
        f"    # >>> vps_provision: {nome}\n"
        f"    location {rota}/ {{\n"
        f"        proxy_pass http://127.0.0.1:{porta}{rota}/;\n"
        "        proxy_http_version 1.1;\n"
        "        proxy_set_header Upgrade $http_upgrade;\n"
        "        proxy_set_header Connection \"upgrade\";\n"
        "        proxy_set_header Host $host;\n"
        "        proxy_read_timeout 86400;\n"
        "    }\n"
        f"    # <<< vps_provision: {nome}\n"
    )


def remover_bloco(conf: str, nome: str) -> str:
    ini = f"    # >>> vps_provision: {nome}\n"
    fim = f"    # <<< vps_provision: {nome}\n"
    if ini in conf and fim in conf:
        a = conf.index(ini)
        b = conf.index(fim) + len(fim)
        conf = conf[:a] + conf[b:]
    return conf


def inserir_bloco(conf: str, bloco: str) -> str:
    out, feito = [], False
    for ln in conf.splitlines(keepends=True):
        out.append(ln)
        if not feito and "listen 443" in ln:
            out.append(bloco)
            feito = True
    if not feito:
        die("não achei 'listen 443' em sites-available/apps")
    return "".join(out)


def validar(plano: dict):
    nome = str(plano.get("nome", ""))
    if not RX_NOME.match(nome):
        die("nome inválido (minúsculas/números/'-', 3-30, começa com letra)")
    if nome in BLACKLIST:
        die(f"nome '{nome}' é reservado do sistema")
    if unit_path(nome).exists() and not gerenciada(nome):
        die(f"serviço '{nome}' já existe e NÃO é do provisionador — escolha outro nome")

    template = str(plano.get("template", ""))
    if template not in TEMPLATES:
        die(f"template '{template}' não suportado (disponível: {TEMPLATES})")

    rota = str(plano.get("rota", ""))
    if not RX_ROTA.match(rota):
        die("rota inválida (ex.: /meu-app)")

    principal = str(plano.get("principal", ""))
    if not RX_PRINCIPAL.match(principal):
        die("arquivo principal inválido (ex.: app.py)")

    try:
        porta = int(plano.get("porta", 0))
    except Exception:
        porta = 0
    if not (8502 <= porta <= 8599):
        die("porta deve estar entre 8502 e 8599")

    pasta = os.path.realpath(str(plano.get("pasta", "")))
    if not (pasta.startswith(str(HOME) + os.sep) and os.path.isdir(pasta)):
        die("pasta deve existir dentro de /home/ubuntu")
    exe = Path(pasta) / ".venv" / "bin" / "streamlit"
    if not exe.exists():
        die(f"venv sem streamlit em {exe} — o painel cria isso antes de me chamar")
    if not (Path(pasta) / principal).exists():
        die(f"'{principal}' não existe em {pasta}")

    _rot = re.sub(r"[\x00-\x1f\"\\]", " ", str(plano.get("rotulo", nome)))
    rotulo = re.sub(r"\s+", " ", _rot).strip()[:60] or nome

    conf = NGINX_CONF.read_text()
    atualizando = f"# >>> vps_provision: {nome}\n" in conf
    conf_sem_eu = remover_bloco(conf, nome)
    if f"location {rota}/" in conf_sem_eu:
        die(f"rota {rota}/ já existe no Nginx (de outro app)")

    for p in SYSTEMD.glob("*.service"):
        if p.stem == nome:
            continue
        try:
            txt = p.read_text()
        except Exception:
            continue
        if MARCA in txt and f"--server.port {porta} " in txt:
            die(f"porta {porta} já é do app '{p.stem}'")
    if not atualizando and porta_ocupada(porta):
        die(f"porta {porta} já está em uso no servidor")

    return nome, template, rota, principal, porta, pasta, rotulo


def cmd_criar(arq: str) -> None:
    try:
        plano = json.loads(Path(arq).read_text())
    except Exception as e:
        die(f"plano ilegível: {e}")
    nome, template, rota, principal, porta, pasta, rotulo = validar(plano)

    unit = (
        f"# {MARCA} (NÃO editar na mão — use o painel ➕ Novo App)\n"
        "[Unit]\n"
        f"Description={rotulo}\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
        "User=ubuntu\n"
        f"WorkingDirectory={pasta}\n"
        f"ExecStart={pasta}/.venv/bin/streamlit run {principal} "
        f"--server.port {porta} --server.address 127.0.0.1 "
        f"--server.headless true --server.baseUrlPath {rota.lstrip('/')}\n"
        "Restart=always\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )

    backup_conf = NGINX_CONF.read_text()
    backup_unit = unit_path(nome).read_text() if unit_path(nome).exists() else None

    unit_path(nome).write_text(unit)
    novo_conf = inserir_bloco(remover_bloco(backup_conf, nome),
                              bloco_nginx(nome, rota, porta))
    NGINX_CONF.write_text(novo_conf)

    rc, out = run(["nginx", "-t"])
    if rc != 0:
        NGINX_CONF.write_text(backup_conf)
        if backup_unit is None:
            unit_path(nome).unlink(missing_ok=True)
        else:
            unit_path(nome).write_text(backup_unit)
        die(f"nginx -t reprovou — TUDO revertido. Detalhe: {out[:400]}")

    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", f"{nome}.service"])
    run(["systemctl", "restart", f"{nome}.service"])
    run(["systemctl", "reload", "nginx"])
    time.sleep(2)
    _, situ = run(["systemctl", "is-active", nome])
    print(f"unit : {nome}.service ({situ})")
    print(f"rota : {rota}/ -> 127.0.0.1:{porta}")
    if situ != "active":
        _, lg = run(["journalctl", "-u", nome, "-n", "15", "--no-pager"])
        print("AVISO: serviço não subiu — últimas linhas do log:")
        print(lg[-1500:])
        sys.exit(1)
    print("PRONTO")


def cmd_remover(nome: str) -> None:
    if not RX_NOME.match(nome or ""):
        die("nome inválido")
    if not gerenciada(nome):
        die(f"'{nome}' não é um app gerenciado pelo provisionador")
    run(["systemctl", "disable", "--now", f"{nome}.service"])
    unit_path(nome).unlink(missing_ok=True)
    run(["systemctl", "daemon-reload"])
    conf = NGINX_CONF.read_text()
    novo = remover_bloco(conf, nome)
    if novo != conf:
        NGINX_CONF.write_text(novo)
        rc, out = run(["nginx", "-t"])
        if rc != 0:
            NGINX_CONF.write_text(conf)
            die(f"nginx -t reprovou ao remover rota (revertido): {out[:300]}")
        run(["systemctl", "reload", "nginx"])
    print(f"REMOVIDO {nome} (pasta do app fica intacta)")


def cmd_listar() -> None:
    apps = {}
    for p in sorted(SYSTEMD.glob("*.service")):
        try:
            txt = p.read_text()
        except Exception:
            continue
        if MARCA not in txt:
            continue
        nome = p.stem
        porta = re.search(r"--server\.port (\d+)", txt)
        rota = re.search(r"--server\.baseUrlPath (\S+)", txt)
        desc = re.search(r"Description=(.*)", txt)
        _, situ = run(["systemctl", "is-active", nome])
        apps[nome] = {
            "rotulo": desc.group(1).strip() if desc else nome,
            "porta": int(porta.group(1)) if porta else None,
            "rota": ("/" + rota.group(1)) if rota else None,
            "status": situ,
        }
    print(json.dumps(apps, ensure_ascii=False, indent=2))


def cmd_dominio(dom: str, porta_s: str) -> None:
    dom = (dom or "").strip().lower()
    if not RX_DOMINIO.match(dom):
        die("dominio invalido (ex.: www.meusite.com.br)")
    try:
        porta = int(porta_s)
    except Exception:
        die("porta invalida")
    if not (1024 <= porta <= 65535):
        die("porta deve estar entre 1024 e 65535")
    slug = "dom-" + re.sub(r"[^a-z0-9]+", "-", dom).strip("-")
    av = Path("/etc/nginx/sites-available") / slug
    en = Path("/etc/nginx/sites-enabled") / slug
    conf = (f"server {{\n    listen 80;\n    server_name {dom};\n"
            f"    location / {{\n        proxy_pass http://127.0.0.1:{porta};\n"
            "        proxy_http_version 1.1;\n        proxy_set_header Upgrade $http_upgrade;\n"
            "        proxy_set_header Connection \"upgrade\";\n        proxy_set_header Host $host;\n"
            "        proxy_set_header X-Real-IP $remote_addr;\n        proxy_read_timeout 86400;\n    }}\n}}\n")
    backup = av.read_text() if av.exists() else None
    av.write_text(conf)
    run(["ln", "-sf", str(av), str(en)])
    rc, out = run(["nginx", "-t"])
    if rc != 0:
        if backup is None:
            av.unlink(missing_ok=True)
            en.unlink(missing_ok=True)
        else:
            av.write_text(backup)
        die(f"nginx -t reprovou (revertido): {out[:300]}")
    run(["systemctl", "reload", "nginx"])
    rc, out = run(["certbot", "--nginx", "-d", dom, "--redirect", "--agree-tos",
                   "--register-unsafely-without-email", "-n"], timeout=120)
    print(f"dominio {dom} -> 127.0.0.1:{porta}")
    print("HTTPS: " + ("ok (certbot)" if rc == 0
          else "certbot falhou (DNS aponta pro IP? porta 80 aberta?) — segue em HTTP. " + out[-200:]))
    print("PRONTO")


def main() -> None:
    if os.geteuid() != 0:
        die("rode via sudo (sudoers restrito do painel)")
    args = sys.argv[1:]
    if len(args) == 2 and args[0] == "criar":
        cmd_criar(args[1])
    elif len(args) == 2 and args[0] == "remover":
        cmd_remover(args[1])
    elif args and args[0] == "listar":
        cmd_listar()
    elif len(args) == 3 and args[0] == "dominio":
        cmd_dominio(args[1], args[2])
    else:
        die("uso: vps_provision criar <plano.json> | remover <nome> | listar | dominio <dom> <porta>")


if __name__ == "__main__":
    main()
