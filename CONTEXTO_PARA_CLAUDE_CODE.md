# Contexto completo do projeto — Automação de Relatórios Double (para o Claude Code)

> ⚠️ **DOCUMENTO HISTÓRICO (handoff inicial).** Alguns pontos aqui já foram
> superados pela implementação atual — a fonte de verdade do estado atual é o
> **`README.md`** (arquitetura/regras) e o **`ROUTINE_PROMPT.md`** (fluxo da
> Routine). Já mudou desde este texto: gráficos são **HTML puro** (não mais
> QuickChart); **sem** comparação com semana anterior; **alcance do Google**
> vira **CTR** no card (Rede de Pesquisa devolve reach=0); **logo** é **data
> URI** embutida (não URL externa); o prompt determinístico da Routine já está
> escrito. Mantido apenas como registro das decisões de arquitetura.
>
> Cole este documento inteiro numa conversa do Claude Code, junto com o ZIP anexado (extraia ele num repositório). Este texto é o histórico consolidado de tudo que foi decidido e construído até agora — o objetivo é você (Claude Code) ter contexto completo para continuar a implementação sem precisar redescobrir nada do que já foi resolvido.

---

## 1. Objetivo do projeto

A Double (agência de marketing digital) precisa enviar, toda **segunda-feira às 8h**, um relatório de performance (Meta Ads + Google Ads) em HTML para cada cliente, como **rascunho no Gmail** (nunca envio automático — sempre fica pra revisão humana antes de mandar). Cada cliente tem sua própria automação isolada.

## 2. Decisão de arquitetura — já fechada, não reabrir esta discussão

**Ferramenta de orquestração: Claude Code Routines.** Não usamos n8n nem nenhum servidor próprio (VPS, Docker etc.) — essa rota foi tentada e abandonada por fricção de infraestrutura (Oracle Cloud e GCP deram problema recorrente de provisionamento/cobrança). Routines resolve isso porque roda na nuvem da própria Anthropic, sem servidor nosso.

**Divisão de responsabilidade dentro da Routine (isso é o que torna a arquitetura segura):**
- **Claude** busca os dados brutos via ferramentas MCP e depois **revisa** o resultado final antes de criar o rascunho (QA: campanhas faltando, valores zerados suspeitos, HTML quebrado).
- **Um script Python determinístico** (já escrito, ver seção 5) faz **toda a matemática** — soma, formatação, mapeamento de objetivo, montagem do HTML. Isso é proposital: números de relatório financeiro para cliente não podem depender de "interpretação" de IA, só de código determinístico. O papel do Claude é orquestrar e revisar, nunca calcular os números manualmente.

## 3. Conectores MCP decididos e já testados com dados reais

| Conector | Uso | Observação |
|---|---|---|
| **MCP Meta Ads** (oficial, `mcp.facebook.com/ads`) | Buscar campanhas/métricas do Meta Ads | OAuth direto com o Business Manager da Double, sem precisar de App Review |
| **flyweel-cloudfare-mcp** (Flyweel) | Buscar campanhas/métricas do Google Ads | Gratuito, ilimitado, sem cartão. Cobre Google+Meta+TikTok, mas usamos só pra Google (o Meta já está resolvido pelo conector oficial acima) |
| **Gmail** | Criar o rascunho do e-mail | Conector padrão do Google Workspace |
| **Google Calendar** | Notificação de "relatório pronto" | Cria um evento com `overrideReminders` (`method: popup, minutes: 0`) — dispara notificação push no celular assim que o relatório fica pronto |
| **GitHub** | Hospedar o repositório do projeto | Só está conectado no Claude Code, não no claude.ai — por isso este handoff está acontecendo por aqui |

**IMPORTANTE sobre o Google Ads MCP:** já tentamos e descartamos: (a) Porter Metrics — nunca conectou de forma confiável, mesmo após várias tentativas; (b) Adspirer — só 15 chamadas/mês no free tier, insuficiente; (c) servidor oficial do Google autohospedado no Cloud Run — funcionaria, mas exigiria cadastrar cartão de crédito no GCP, o que o usuário quer evitar. **Flyweel foi o que funcionou e ficou definido.**

## 4. Descobertas reais sobre os dados (validadas ao vivo, não são suposição)

Isso é crítico porque já está codificado no `report_engine.py` — não redescobrir, só usar:

### Meta Ads (`ads_get_ad_entities`)
- Os valores vêm como **texto formatado**, não número puro. Exemplos reais coletados da conta da R&M Higiene Profissional (`ad_account_id: 125319914616407`):
  - `"amount_spent": "R$0,00 BRL"`
  - `"cost_per_result": {"value": "R$0,00 BRL (Messaging conversations started)"}`
  - `"results": {"value": "0 (Messaging conversations started)"}`
  - Campanhas sem entrega no período retornam `"Not available"` em impressions/clicks/cpc/cpm/ctr.
  - Em alguns objetivos (ex: `VIDEO_VIEWS`) o formato muda pra `[{"indicator": "video_thruplay_watched_actions"}]`, sem valor numérico direto.
- O campo `results`/`cost_per_result` **já vem com o rótulo do tipo de resultado embutido** — isso é usado como sinal primário pra decidir qual bloco de métrica do relatório usar (mais confiável que só olhar `objective`).
- A conta real tem campanhas com objetivos **legados** (`MESSAGES`, `POST_ENGAGEMENT`, `VIDEO_VIEWS`) além dos novos `OUTCOME_*` — o mapeamento no script cobre os dois formatos.
- Validado com dado real: campanha `"[Mensagens] [WhatsApp] - 08/07"`, `objective: OUTCOME_ENGAGEMENT`, rótulo `"Messaging conversations started"` → mapeou corretamente pro bloco de Mensagens.
- Antes de passar qualquer nome de campo pra `ads_get_ad_entities`, é obrigatório verificar com `ads_get_field_context` (a ferramenta reclama se o campo não for verificado antes).

### Google Ads (via Flyweel `query_metrics`)
- Retorna número limpo (float), bem mais simples que o Meta.
- `dataSource: "ads"`, filtrar com `filters: {"channel": ["Google"]}`.
- Métricas disponíveis: spend, impressions, clicks, conversions, reach, ctr, cpc, cpm, conversion_rate, cost_per_conversion.
- Dimensões: channel, account, campaign, campaign_id, campaign_status, objective, currency, date, week, month.
- O campo `campaign_id` do Flyweel é um **UUID interno**, não o ID numérico do Google Ads.
- O campo `objective` do Google via Flyweel retorna o **nome da estratégia de lance** (ex: `MAXIMIZE_CONVERSIONS`, `TARGET_SPEND`), não um objetivo de marketing — por isso o Google **não tem variação de bloco de métrica por objetivo** (diferente do Meta): toda campanha do Google usa sempre o mesmo bloco único `METRICA_GOOGLE_PADRAO`.
- Google não tem métrica de alcance equivalente ao Meta — o campo fica `—` no relatório.
- Dado real coletado (conta R&M, últimos 7 dias): 2 campanhas ativas, spend total R$ 88,98.

## 5. O que já foi construído e testado (está no ZIP anexado)

```
double-relatorios-automacao/
├── template.html                          ← HTML final do e-mail, 100% verificado
├── report_engine.py                       ← motor compartilhado (parsing, mapeamento, montagem)
├── clientes/
│   └── rm_higiene.py                      ← script de entrada do 1º cliente (modelo a replicar)
├── dados_exemplo/                         ← dados reais coletados ao vivo, usados no teste
│   ├── rm_higiene_meta_raw.json
│   └── rm_higiene_google_raw.json
├── dados/                                 ← (vazia) é aqui que a Routine deve salvar os dados
│                                             brutos de cada execução real, antes de rodar o script
├── output/
│   └── relatorio_rm_higiene.html          ← resultado do teste, já validado linha por linha
└── assets/                                ← (vazia) só vai ter a logo da Double aqui
```

### `template.html`
HTML de e-mail (tabelas, CSS inline, compatível com Gmail/Outlook), com placeholders `{{variavel}}` e marcadores de bloco repetível:
- `INICIO_BLOCO_PLATAFORMA` / `FIM_BLOCO_PLATAFORMA` — repete 1x por plataforma com investimento no período (Meta e/ou Google).
- `INICIO_BLOCO_CAMPANHA` / `FIM_BLOCO_CAMPANHA` — repete 1x por campanha ativa.
- Dentro de cada campanha, 4 sub-blocos possíveis (`METRICA_MENSAGENS`, `METRICA_TRAFEGO_PERFIL`, `METRICA_ALCANCE`, `METRICA_GOOGLE_PADRAO`) — só UM sobrevive por campanha, os outros 3 são removidos pelo script.
- **Só existe placeholder de logo da agência** (`{{agencia_logo_url}}`) — **não existe (e não deve ser criado) placeholder de logo de cliente**. Confirmado: só a logo da Double aparece no cabeçalho.
- Lista completa de placeholders (33 únicos) já documentada e verificada sem nenhum resíduo — ver `report_engine.py` para os nomes exatos usados na hora de preencher.

### `report_engine.py`
Funções principais:
- `parse_meta_amount_spent`, `parse_meta_result_field` — parsing robusto do formato de texto do Meta (cobre `"Not available"`, valores com rótulo embutido, formato de lista com `indicator`).
- `fmt_brl`, `fmt_int`, `fmt_compacto`, `fmt_pct`, `fmt_variacao` — formatação no padrão brasileiro.
- `mapear_bloco_meta(objective, resultado_rotulo)` — decide qual dos 3 blocos do Meta usar (rótulo do resultado como sinal primário, `objective` como fallback). Campanhas que não batem com nada caem em `BLOCO_DESCONHECIDO` e são sinalizadas no log em vez de renderizadas erradas silenciosamente.
- `normalizar_campanha_meta` / `normalizar_campanha_google` — convertem o JSON bruto de cada plataforma pra um formato interno comum.
- `agregar_plataforma` / `agregar_geral` — somas e médias corretas (cpc/ctr recalculados a partir dos totais, não é média simples de médias).
- `grafico_investimento_por_plataforma`, `grafico_investimento_por_dia`, `grafico_campanha` — geram as URLs do QuickChart replicando exatamente a config visual (cores, doughnut/bar, datalabels) que veio do design.
- `gerar_relatorio_html(...)` — função principal, monta o HTML final.

**Bug real encontrado e corrigido durante o teste:** a extração de bloco inicialmente só substituía a 1ª instância de exemplo do template, deixando as outras (3 campanhas de exemplo, 1 plataforma de exemplo) intocadas com placeholder cru no HTML final. Corrigido em `_extrair_bloco` — agora remove TODAS as instâncias de exemplo entre a primeira abertura e a última marca de fechamento antes de inserir os blocos gerados de verdade.

### `clientes/rm_higiene.py`
Script de entrada específico da R&M Higiene Profissional — é o **modelo a ser replicado por cliente** (ver seção 7 — "um script por cliente", mas reaproveitando o `report_engine.py` compartilhado). Contém:
- Config do cliente (nome, e-mail de destino, ID Meta, ID Google).
- Carrega `dados/<cliente>_meta_raw.json` e `dados/<cliente>_google_raw.json` (com fallback pra `dados_exemplo/` se não existir, só pra teste manual).
- Chama o motor e escreve `output/relatorio_<cliente>.html`.

## 6. Pendências reais deixadas explícitas no código (não escondidas)

1. **Gráfico por campanha ainda não usa histórico diário real** — hoje usa só o total da semana numa barra só, porque a chamada de teste não trouxe o detalhamento diário por campanha (só o total da conta por dia, via Flyweel). O prompt da Routine precisa incluir a busca desse detalhamento por campanha quando for gerar de verdade.
2. **`agencia_contato` e `agencia_logo_url` ainda estão com valor de placeholder** no `clientes/rm_higiene.py` — falta preencher com o e-mail real da Double e o link real da logo (que vai ser commitada em `assets/` dentro deste mesmo repositório, ver seção 8).
3. **`insight_texto` é texto genérico fixo** — a geração do insight de verdade (frase curta sobre o desempenho da semana) é tarefa da Routine/Claude, não do script — o script só recebe o texto pronto como parâmetro.
4. **Comparação com a semana anterior não está implementada** — `dash_variacao_investimento` hoje cai no fallback "Sem dado da semana anterior" porque não buscamos ainda os dados da semana retrasada pra comparar. Precisa decidir: a Routine busca os dados de 2 períodos (semana atual + anterior) toda vez, ou o script guarda o resultado da semana passada em algum lugar pra comparar depois?
5. **O mapeamento `POST_ENGAGEMENT → trafego_perfil`** foi definido por dedução (não validado contra uma campanha ativa real desse tipo ainda, só campanhas pausadas/legadas apareceram no teste) — vale confirmar assim que aparecer uma campanha real de "tráfego pro perfil" ativa em algum cliente.

## 7. Parametrização por cliente — como isso escala

Cada cliente = 1 conta Meta Ads + 1 conta Google Ads + 1 Routine própria, tudo isolado. O padrão a seguir pra cada cliente novo:

1. Criar `clientes/<nome_do_cliente>.py` seguindo exatamente o modelo de `clientes/rm_higiene.py` — só troca o dicionário `CLIENTE` (nome, e-mail, ID Meta, ID Google) e os caminhos de arquivo de dados.
2. Dentro da Routine desse cliente (uma Routine por cliente, ver seção 9), o prompt instrui o Claude a:
   - Buscar os dados do Meta usando o `meta_ad_account_id` específico daquele cliente.
   - Buscar os dados do Google usando o `google_ads_customer_id` específico daquele cliente (via Flyweel).
   - Salvar em `dados/<nome_do_cliente>_meta_raw.json` e `dados/<nome_do_cliente>_google_raw.json`.
   - Rodar `python clientes/<nome_do_cliente>.py`.
   - Revisar o HTML gerado em `output/relatorio_<nome_do_cliente>.html`.
   - Criar o rascunho no Gmail com esse HTML, endereçado ao `email_destino` do cliente.
   - Criar o evento no Google Calendar de notificação ("Relatório pronto — [Nome do Cliente]").

## 8. Organização de arquivos que o usuário está preparando (fora do Claude Code, manual)

O usuário está organizando, num bloco de notas / planilha simples, a lista de todos os clientes com estas colunas: **nome do cliente, e-mail de destino, ID da conta Meta Ads, ID da conta Google Ads**. Exemplo real já fornecido:

| cliente | email | meta ads | google ads |
|---|---|---|---|
| R&M Higiene Profissional | financeiro@rmhigieneprofissional.com.br | 125319914616407 | 301-399-3639 |

Ele vai colar essa lista aqui pro Claude Code processar e gerar os `clientes/<nome>.py` de cada um automaticamente, seguindo o modelo da seção 7.

**Sobre logos: só existe a logo da própria Double** (agência) — vai ser um único arquivo dentro de `assets/` neste repositório, referenciado pelo placeholder `{{agencia_logo_url}}`. **Não crie placeholder de logo por cliente** — isso foi decidido explicitamente, não é uma omissão.

## 9. O que falta fazer — este é o trabalho para o Claude Code a partir daqui

- [ ] Extrair o ZIP anexado e inicializar/atualizar o repositório GitHub do projeto com essa estrutura.
- [ ] Escrever o **prompt definitivo da Routine** (o texto que o Claude vai seguir toda segunda-feira, dentro de cada Routine) cobrindo: busca de dados (semana atual + anterior, Meta + Google, incluindo o detalhamento diário por campanha que falta — pendência #1), geração do insight (pendência #3), execução do script, checagem de QA antes de finalizar, criação do rascunho no Gmail, criação do evento de notificação no Calendar.
- [ ] Resolver as pendências da seção 6 (uma a uma, com o usuário confirmando cada decisão).
- [ ] Processar a lista de clientes que o usuário vai colar (seção 8) e gerar um `clientes/<nome>.py` para cada um.
- [ ] Configurar a logo da Double em `assets/` e atualizar `agencia_logo_url` e `agencia_contato` em todos os scripts de cliente.
- [ ] Criar as Routines de verdade (uma por cliente): repositório + os 4 conectores (Meta Ads, Flyweel, Gmail, Google Calendar) + gatilho semanal (segunda-feira, 8h, fuso `America/Sao_Paulo`).
- [ ] Rodar em modo piloto com a R&M por 2-3 semanas antes de replicar pros demais clientes.

---

**Resumo em uma frase para você (Claude Code):** a arquitetura, o HTML e o motor Python já estão prontos e testados contra dados reais de um cliente — falta escrever o prompt da Routine que orquestra tudo, resolver as 5 pendências listadas na seção 6, e replicar o padrão de script por cliente conforme a lista que o usuário vai fornecer.
