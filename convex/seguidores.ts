import { query, mutation } from "./_generated/server";
import { v } from "convex/values";

// Estrutura de cada entrada (igual ao seguindo.json)
const prefsValidator = v.object({
  noticias: v.boolean(),
  jogos: v.boolean(),
  lembrete: v.boolean(),
});

const entryValidator = v.object({
  times: v.array(v.string()),
  noticiasVistas: v.record(v.string(), v.array(v.string())),
  prefs: v.optional(prefsValidator),
  lembretesEnviados: v.optional(v.array(v.string())),
});

// Proteção simples server-to-server: o bot é um servidor confiável, não há
// identidade de usuário final. Se BOT_SHARED_SECRET estiver definido no
// deployment Convex, exigimos que o bot envie o mesmo segredo.
function checkSecret(secret?: string): void {
  const expected = process.env.BOT_SHARED_SECRET;
  if (expected && secret !== expected) {
    throw new Error("Unauthorized: segredo inválido");
  }
}

// Retorna todos os seguidores como um mapa { userId: { times, noticiasVistas } }
export const getAll = query({
  args: { secret: v.optional(v.string()) },
  returns: v.record(v.string(), entryValidator),
  handler: async (ctx, args) => {
    checkSecret(args.secret);
    const docs = await ctx.db.query("seguidores").collect();
    const out: Record<
      string,
      {
        times: string[];
        noticiasVistas: Record<string, string[]>;
        prefs?: { noticias: boolean; jogos: boolean; lembrete: boolean };
        lembretesEnviados?: string[];
      }
    > = {};
    for (const d of docs) {
      out[d.userId] = {
        times: d.times,
        noticiasVistas: d.noticiasVistas,
        ...(d.prefs ? { prefs: d.prefs } : {}),
        ...(d.lembretesEnviados ? { lembretesEnviados: d.lembretesEnviados } : {}),
      };
    }
    return out;
  },
});

// Substitui todo o estado de seguidores de forma transacional.
// Mantém a mesma semântica do antigo _salvar_seguindo(dict inteiro).
export const replaceAll = mutation({
  args: {
    secret: v.optional(v.string()),
    dados: v.record(v.string(), entryValidator),
  },
  returns: v.null(),
  handler: async (ctx, args) => {
    checkSecret(args.secret);
    const existing = await ctx.db.query("seguidores").collect();
    for (const d of existing) {
      await ctx.db.delete(d._id);
    }
    for (const [userId, entry] of Object.entries(args.dados)) {
      await ctx.db.insert("seguidores", {
        userId,
        times: entry.times,
        noticiasVistas: entry.noticiasVistas,
        ...(entry.prefs ? { prefs: entry.prefs } : {}),
        ...(entry.lembretesEnviados
          ? { lembretesEnviados: entry.lembretesEnviados }
          : {}),
      });
    }
    return null;
  },
});
