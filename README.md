# Automação de Relatórios Double

Relatório semanal de performance (Meta Ads + Google Ads) em HTML, gerado por
script Python determinístico e enviado como **rascunho no Gmail** para cada
cliente. Orquestração via **Claude Cloud Routines** (sem servidor próprio).

Regra de ouro: **o Claude busca os dados e revisa; o Python faz toda a
matemática e monta o HTML.** Número de relatório de cliente não depende de
interpretação de IA.

## Estrutura

```
double-relatorios-automacao/
├── report_engine.py        Motor compartilhado (parsing, filtros, agregação, gráficos HTML, período, HTML)
├── template.html           HTML do e-mail (email-safe: cores sólidas + bgcolor, gráficos HTML puro)
├── ROUTINE_PROMPT.md       Prompt determinístico da Cloud Routine (passo a passo 1→8)
├── clientes/
│   └── rm_higiene.py       Script de entrada por cliente (modelo a replicar); `--info` emite o manifest
├── dados/                  Dados brutos de cada execução (NÃO versionado; a Routine salva aqui)
│   └── Dados clientes.txt  Lista de clientes (NÃO versionado — dado sensível, fica local)
├── dados_exemplo/          Fixtures reais versionadas (R&M) — usadas como fallback em teste
├── output/                 Relatórios gerados (NÃO versionado)
├── assets/
│   └── double-logo.png     Logo da Double (embutida no HTML como data URI pelo motor)
└── tests/
    └── test_report.py      Regressão dos comportamentos críticos
```

## Como rodar (teste local)

```bash
python clientes/rm_higiene.py --info   # imprime o manifest (período, IDs, assunto, caminhos)
python clientes/rm_higiene.py          # gera output/relatorio_rm_higiene.html
python tests/test_report.py            # regressão (sai 1 se algo falhar)
```

O script prioriza `dados/<cliente>_*.json`; se não existir, usa `dados_exemplo/`.
O período (semana anterior completa, seg–dom) é calculado sozinho por
`calcular_periodo` a partir da data de execução — sem edição manual.

## Fluxo da Routine (orquestração)

O prompt determinístico está em [`ROUTINE_PROMPT.md`](ROUTINE_PROMPT.md). Resumo
do que a Routine faz toda segunda-feira (só isto, nada além):

1. `python clientes/rm_higiene.py --info` → lê o **manifest** (janela de datas,
   IDs de conta, e-mail de destino, assunto, caminhos). Nada fica hardcoded no
   prompt: o script é a fonte única.
2. Busca Google (Flyweel `query_metrics`, 2 queries: agregado + série diária) e
   salva em `dados/rm_higiene_google_raw.json`.
3. Busca Meta (`ads_get_field_context` → `ads_get_ad_entities`, `time_increment:1`)
   e salva em `dados/rm_higiene_meta_raw.json`.
4. `python clientes/rm_higiene.py` → gera o HTML.
5. QA (sem placeholder cru, CTR ≤ 100%, período confere, < 100 KB).
6. Cria **rascunho** no Gmail (HTML verbatim). Nunca envia.
7. Cria evento no Calendar às **09:00** (`America/Sao_Paulo`, popup 0).

## Contrato de dados (o que a Routine salva em `dados/`)

**Meta** — `dados/<cliente>_meta_raw.json`: array de campanhas (nível campaign)
com os campos de `ads_get_ad_entities` (`amount_spent`, `impressions`,
`clicks`, `reach`, `cpc`, `cpm`, `cost_per_result`, `results`, `objective`,
`effective_status`). Valores vêm como **texto** (`"R$29,51 BRL"`, `"Not
available"`) — o motor faz o parsing. Cada campanha pode trazer
`serie_diaria: [{"date","spend"}]` (breakdown diário via `time_increment:1`;
datas em PT-BR são aceitas).

**Google** — `dados/<cliente>_google_raw.json`:
```json
{ "campanhas": [ { "campaign","campaign_id","campaign_status","objective",
                   "spend","impressions","clicks","conversions","cpc",
                   "reach","cost_per_conversion","account","serie_diaria":[...] } ],
  "por_dia": [ {"date","spend"} ] }
```
Buscar via Flyweel `query_metrics` **sempre com `filters.account`** da conta do
cliente (senão vaza campanha de outro cliente). `serie_diaria` por campanha vem
de `dimensions:["campaign_id","date"]`. **Alcance do Google** (`reach`): existe
na métrica do Flyweel, mas campanha de Rede de Pesquisa devolve `reach=0` (o
Google não mede usuários únicos em Search — só Display/Vídeo/Demand Gen). Quando
`reach=0`/ausente, o card de alcance do Google vira **CTR** no template; se um
dia entrar campanha de Display com reach real, o campo já é lido e exibido.

## Regras já implementadas

- **Segmentação por conta:** Meta já é escopado pelo `ad_account_id` na chamada;
  Google é filtrado por `account` na busca + trava no script do cliente
  (`flyweel_account`).
- **Só campanha que rodou na semana:** entra no relatório quem teve
  **investimento > 0** no período — independente de status. Campanha ACTIVE sem
  entrega some; campanha PAUSED que gastou aparece. Plataforma sem nenhuma
  campanha ativa some do relatório.
- **Métricas diárias reais** por campanha (barras Seg–Dom) e no gráfico
  "investimento por dia" do topo (soma Meta + Google).
- **Sem comparação com semana anterior** — o relatório é só da semana.
- **Logo/contato da agência** vêm de `AGENCIA_*` no `report_engine.py`. A logo
  é **embutida como data URI base64** a partir de `assets/double-logo.png`
  (`_logo_data_uri()`) — vai "colada" no HTML (fallback: URL https). Renderiza
  no navegador e na maioria dos clientes; o **Gmail não exibe `data:`** (no
  envio real a logo precisa ir como anexo inline CID).
- **Período automático:** `calcular_periodo` calcula a semana anterior completa
  (seg–dom) pela data de execução; `rm_higiene.py --info` expõe isso no manifest
  para a Routine usar a mesma janela na busca de dados.

## Renderização no Gmail (email-safe)

Regras que o `template.html` já segue e que NÃO podem ser quebradas:
- **Sem `background-image:linear-gradient` inline** — Gmail ignora. Todo fundo é
  cor sólida via `background-color` + atributo `bgcolor`.
- **Fundo escuro do canvas** com `bgcolor="#020617"` no wrapper 100%, na td
  central e no container 600px (Gmail descarta o bg do `<body>`).
- **Gráficos 100% HTML/CSS** (tabelas + células coloridas), sem imagem externa
  — Gmail bloqueia/proxya imagens externas (QuickChart não renderiza nem com
  short URL). `report_engine` gera as barras: `_barras_verticais_html` (por dia
  e por campanha, com o valor gasto sobre cada barra) e barra horizontal
  segmentada (por plataforma). A única imagem do e-mail é a logo da Double
  (data URI). Gmail **não** exibe `data:` — no navegador aparece; no envio real
  vira anexo inline CID.
- **Não acessar outra conta de anúncio** além da do cliente. Pegar só os dados
  daquela conta (Meta `ad_account_id`, Google `filters.account`); nunca listar
  ou varrer outras contas.
- **Bolinhas/marcadores das métricas** são `<td>` 6px com `bgcolor`+`height`,
  nunca `<div>` com dimensão — Gmail descarta width/height de `<div>` (mesmo
  motivo das barras), então bolinha em `<div>` não aparece no Gmail.
- **Nomes das métricas são amigáveis p/ o cliente** (o cliente não sabe siglas):
  Impressões → "Impressões / Visualizações"; CPC → "Custo por clique (CPC)";
  CTR → "Taxa de cliques (CTR)"; CPM → "Custo por mil impressões (CPM)";
  Alcance continua "Alcance". Os labels são texto literal no `template.html`
  (front-end); os placeholders `{{campanha_*}}` do back-end NÃO mudam.

## Adicionar um cliente novo

Copiar `clientes/rm_higiene.py`, trocar o dict `CLIENTE`
(`nome`, `email_destino`, `meta_ad_account_id`, `google_ads_customer_id`,
`flyweel_account`). Cliente sem Meta ou sem Google: o motor lida com a lista
vazia (a plataforma ausente simplesmente não aparece).

Clientes atuais (`dados/Dados clientes.txt`):

| cliente | Meta Ads | Google Ads |
|---|---|---|
| R&M Higiene Profissional | 125319914616407 | 301-399-3639 (`Customer 3013993639`) |
| HydroCenter Piscinas | — | 567-210-6894 (`Customer 5672106894`) |
| Schneider Diesel | 989051650304262 | — (não mostrar) |

## Pendências

- **Prompt da Routine** — ✅ pronto em [`ROUTINE_PROMPT.md`](ROUTINE_PROMPT.md).
- **Insight** — hoje texto fixo; a frase real é gerada pela Routine e passada
  como `insight_texto`.
- **Logo no Gmail** — data URI não renderiza no Gmail; no envio real (quando
  deixar de ser só rascunho) anexar a logo como inline CID.
- **Scripts `clientes/hydrocenter.py` e `clientes/schneider.py`** — replicar o
  modelo quando for escalar.
