# VPS Framework — instalador visual (Pacote 1)

Sobe em qualquer Ubuntu 22.04 zerado um clone do framework (painel VPS Admin,
PostgreSQL+pgvector, PostgREST, MCP, LLM Gateway, webhook, sentinela, ntfy,
Evolution/WhatsApp e o provisionador "Novo App") — **menos o Ollama**, que se
instala depois pelo próprio painel.

## Como usar (na VM nova)
```bash
curl -fsSL https://raw.githubusercontent.com/SEU-USER/vps-framework/main/bootstrap.sh | bash
```
Aparece no terminal: `http://SEU-IP:9000/?key=XXXX`. Abra no navegador → tela
de instalação com **checkboxes** (escolha os componentes) e **barra de progresso
ao vivo**. No fim: `http://SEU-IP/admin/` com a senha mostrada.

## Detecção automática de ambiente ("ping")
A 1ª etapa detecta **arquitetura (ARM/x86), versão do Ubuntu e do Python** e
adapta tudo: binários (PostgREST, ntfy, Ollama) baixam a versão certa da arch,
e as libs Python instalam o wheel correto automaticamente. Os locks em `locks/`
travam as versões exatas (iguais à produção).

## Remover tudo (VM volta limpa)
Na mesma tela, aba **"Remover tudo"** — ou:
```bash
sudo VPS_KEY=XXXX python3 /tmp/vps-framework/instalador/server.py --uninstall
```

## Estrutura
- `bootstrap.sh` — a fagulha (curl|bash)
- `instalador/server.py` — servidor web stdlib (wizard + progresso SSE + motor)
- `locks/*.txt` — versões travadas das libs Python (do inventário da produção)
