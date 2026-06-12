# Deploy no Railway

## Modo atual (Opção B — recomendado)

No Railway → **Settings → Source**:

- Repositório: `cleberrandreazza/futebola`
- Branch: `main`
- **Auto deploys when pushed to GitHub**: ligado

Cada `git push` em `main` dispara build **direto no Railway**.  
**Não precisa** de `RAILWAY_TOKEN` no GitHub Actions.

**Wait for CI**: pode ficar **desligado** (como está). O deploy não depende do workflow Actions.

---

## Se o bot não atualizou após o push

1. Railway → serviço do bot → aba **Deployments**
2. Veja se há deploy **novo** após seu push (commit `b59885b` ou `49df1e0`)
3. Status:
   - **Success** — bot rodando; confira logs se comportamento antigo (cache Discord, etc.)
   - **Failed** — abra o log do build (Playwright/chromium costuma demorar ou falhar)
   - **Nenhum deploy novo** — webhook GitHub: Settings → Source → **Disconnect** e reconecte o repo

### Forçar deploy agora

Na aba **Deployments**, botão **Deploy** (puxa o último commit de `main`).

---

## Opção A — GitHub Actions com CLI (alternativa)

Só use se **não** quiser auto-deploy pelo Railway. Secrets em GitHub → Actions:

| Secret | Onde obter |
|--------|------------|
| `RAILWAY_TOKEN` | Railway → Account → Tokens |
| `RAILWAY_SERVICE_ID` | Serviço → Settings → Service ID |

---

## Convex

Backend Convex é separado do Railway:

```bash
npx convex deploy
```

Opcional: secret `CONVEX_DEPLOY_KEY` no GitHub para deploy automático no push.

---

## Commits recentes relevantes

| Commit | Conteúdo |
|--------|----------|
| `b59885b` | Fix apostas duplicadas (`matchKey`) |
| `0d59335` | Ranking no resumo do dia |
| `a5fbafb` | Termos/privacidade via Railway URL |
