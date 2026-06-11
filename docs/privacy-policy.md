# Política de Privacidade — Futebola

**Última atualização:** 11 de junho de 2026

Esta Política de Privacidade descreve como o bot de Discord **Futebola** (“Bot”, “nós”, “operador”) trata informações quando você o utiliza em um servidor Discord.

---

## 1. Quem somos

**Operador:** Cleber Randreazza  
**Projeto:** Futebola — bot de futebol para Discord  
**Repositório:** [github.com/cleberrandreazza/futebola](https://github.com/cleberrandreazza/futebola)

---

## 2. Dados que coletamos

### 2.1 Dados fornecidos pelo Discord

Quando você interage com o Bot, recebemos dados que o Discord disponibiliza à aplicação, como:

| Dado | Finalidade |
|------|------------|
| ID do usuário (`userId`) | Identificar sua conta de forma única |
| Nome de exibição | Mostrar no ranking, saldo e apostas |
| ID do servidor (`guildId`) | Operar comandos no servidor correto |
| Cargos e permissões | Verificar se você pode usar apostas ou funções restritas |
| Canais em que você usa comandos | Responder no contexto correto |

O Bot **não** solicita sua senha do Discord nem acesso à sua conta fora do escopo autorizado na convite.

### 2.2 Dados gerados pelo uso do Bot

| Dado | Finalidade |
|------|------------|
| Times que você segue | Enviar notícias, lembretes e alertas de jogos |
| Preferências de notificação | Respeitar o que você ativou/desativou |
| URLs de notícias já enviadas | Evitar duplicatas |
| Saldo, apostas e histórico fictício | Sistema de apostas simuladas e ranking |
| Lembretes já enviados | Não repetir avisos do mesmo jogo |

### 2.3 Mensagens diretas (DM)

Se você seguir times ou ativar lembretes, o Bot pode enviar **mensagens diretas** no Discord (notícias, lembretes de jogos). Você pode desativar preferências via comandos do Bot ou bloquear o Bot no Discord.

### 2.4 Dados técnicos

Podemos registrar logs operacionais (horário, comando usado, erros) para manutenção e segurança. Logs não são usados para publicidade.

---

## 3. Onde os dados são armazenados

- **Convex** (banco de dados na nuvem): seguidores de times, apostadores, apostas e configurações persistentes
- **Railway** (hospedagem): execução do Bot; disco efêmero pode conter cache temporário
- **Arquivos locais de fallback** (quando aplicável): cópia local de dados se o serviço de banco estiver indisponível

Dados são mantidos enquanto necessários para o funcionamento do Bot ou enquanto você utilizar o Serviço.

---

## 4. Compartilhamento com terceiros

Compartilhamos dados **apenas** na medida necessária para operar o Bot:

| Terceiro | Motivo |
|----------|--------|
| **Discord** | Plataforma onde o Bot roda; entrega de mensagens |
| **Convex** | Armazenamento de dados do Bot |
| **Railway** | Hospedagem da aplicação |
| **ESPN / Bzzoiro** | Consulta de jogos e estatísticas (enviamos consultas, não seu perfil completo) |

**Não vendemos** seus dados pessoais. **Não** usamos seus dados para publicidade direcionada.

Provedores de infraestrutura podem processar dados em servidores fora do Brasil, sujeitos às políticas deles e a cláusulas contratuais padrão.

---

## 5. Base legal (LGPD)

Tratamos dados com base em:

- **Execução do serviço** que você solicita ao usar o Bot
- **Legítimo interesse** para segurança, melhoria e prevenção de abuso
- **Consentimento**, quando aplicável (ex.: ao seguir times e receber DMs)

---

## 6. Seus direitos

Conforme a Lei Geral de Proteção de Dados (LGPD), você pode solicitar:

- Confirmação de tratamento e acesso aos dados
- Correção de dados incompletos ou desatualizados
- Eliminação de dados desnecessários ou tratados em desconformidade
- Informação sobre compartilhamento

Para exercer esses direitos, abra uma issue em  
[github.com/cleberrandreazza/futebola/issues](https://github.com/cleberrandreazza/futebola/issues)  
informando seu **ID do Discord** (ativável em Configurações → Avançado → Modo desenvolvedor).

Você também pode:

- Parar de seguir times e desativar notificações pelos comandos do Bot
- Remover o Bot do servidor (administradores)
- Bloquear o Bot no Discord para interromper DMs

---

## 7. Retenção e exclusão

- Dados de apostas e seguidores são mantidos enquanto o Bot estiver ativo no ecossistema
- Podemos excluir dados de teste ou contas inativas após período razoável
- Logs técnicos podem ser rotacionados ou descartados periodicamente

---

## 8. Segurança

Adotamos medidas como autenticação entre Bot e banco (segredo compartilhado), acesso restrito ao deployment e validação de IDs de usuário Discord no ranking.

Nenhum sistema é 100% seguro; notifique-nos via GitHub Issues se suspeitar de acesso indevido.

---

## 9. Menores

O Bot segue as regras do Discord quanto à idade mínima. Não coletamos intencionalmente dados de crianças abaixo do limite legal sem consentimento adequado.

---

## 10. Alterações desta política

Podemos atualizar esta Política. A data no topo indica a versão vigente. Alterações relevantes podem ser comunicadas no repositório do projeto.

---

## 11. Contato

**Privacidade e dados pessoais:**

- [github.com/cleberrandreazza/futebola/issues](https://github.com/cleberrandreazza/futebola/issues)

---

*Futebola — bot de futebol para Discord.*
