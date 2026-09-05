# Lock Sensi Bot — Render Ready

Pacote limpo para deploy no Render.

## Segurança
- Nenhum valor de Secret foi colocado neste pacote.
- `.replit`, `.env`, `vendas.db`, `.local` e backups foram removidos.
- Não coloque tokens/senhas diretamente no GitHub.

## Persistência
`verification_system.py` foi ajustado para usar o mesmo Supabase/PostgreSQL do restante do bot. Isso evita perda de dados quando o filesystem efêmero do Render reinicia.

## Render
- Runtime: Python
- Build: `pip install -r requirements.txt`
- Start: `python bot.py`
- Health check: `/`
- Porta: o próprio `bot.py` usa `PORT` e `0.0.0.0`.

Depois do primeiro deploy, configure `PUBLIC_BASE_URL` com a URL HTTPS `.onrender.com` do serviço e redeploy.
