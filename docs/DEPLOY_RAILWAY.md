# Deploy no Railway

## Por que o push no GitHub nem sempre redeploya

O workflow `.github/workflows/railway-deploy.yml` só funciona se estes **secrets** existirem em  
**GitHub → repositório → Settings → Secrets and variables → Actions**:

| Secret | Onde obter |
|--------|------------|
| `RAILWAY_TOKEN` | [Railway](https://railway.app) → Account Settings → **Tokens** → Create token |
| `RAILWAY_SERVICE_ID` | Projeto → serviço do bot → Settings → copiar **Service ID** |
| `CONVEX_DEPLOY_KEY` | (opcional) Convex Dashboard → Settings → Deploy Key |

Sem `RAILWAY_TOKEN`, o deploy **não roda** (o workflow falha com mensagem explícita).

Alternativa: conectar o repositório direto no Railway (**Settings → Source → GitHub**). Nesse caso cada push em `main` gera build automático, sem GitHub Actions.

---

## Redeploy manual (agora)

1. Abra [railway.app](https://railway.app) → projeto **Futebola**
2. Clique no **serviço do bot**
3. Aba **Deployments**
4. No deploy de `main` mais recente, menu **⋯** → **Redeploy**  
   — ou botão **Deploy** se o GitHub estiver conectado (puxa o último commit)

Confirme que o commit em produção é `b59885b` ou mais recente (fix duplicata de apostas).

---

## Deploy pela CLI (local)

```bash
npm install -g @railway/cli
railway login
cd /caminho/football
railway link          # escolha projeto + serviço
railway up --detach   # envia o código atual
```

---

## Convex

Funções Convex são deployadas separadamente:

```bash
npx convex deploy
```

(O GitHub Actions também roda isso se `CONVEX_DEPLOY_KEY` estiver configurado.)
