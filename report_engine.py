"""
report_engine.py
Motor compartilhado de geração do relatório semanal (Double).

Este módulo NÃO faz nenhuma chamada de rede/API. Ele recebe dados já
buscados (pelo Claude, via ferramentas MCP, dentro da Routine) em formato
bruto e faz todo o processamento determinístico:
  - parsing dos formatos de valor (Meta e Google/Flyweel vêm em formatos diferentes)
  - mapeamento de objetivo de campanha -> qual sub-bloco de métrica usar
  - agregação de totais (conta geral e por plataforma)
  - geração das URLs de gráfico (QuickChart)
  - montagem do HTML final a partir do template com marcadores

Testado com dados reais da conta da R&M Higiene Profissional
(Meta: 125319914616407 / Google via Flyweel, ambos coletados em 2026-07-09).
"""

import json
import re
from datetime import datetime, date, timedelta

# ============================================================================
# 0. DADOS DA AGÊNCIA (Double) — usados como default em todo relatório.
#    Cada script de cliente pode sobrescrever passando as chaves no dict
#    `cliente`, mas normalmente não precisa: é a mesma agência para todos.
#    A logo precisa ser uma URL ABSOLUTA https (e-mail não carrega caminho
#    relativo/local).
# ============================================================================
AGENCIA_NOME = "Double Marketing e Performance"
AGENCIA_CONTATO = "contato@joindouble.com.br"
# Logo hospedada como imagem no GitHub (raw.githubusercontent). URL absoluta que
# o Gmail carrega via proxy — ao contrário do data URI base64, que o Gmail
# bloqueia. O arquivo é servido como image/png em:
#   github.com/joindouble/logo-double  ->  branch main, double-logo.png
AGENCIA_LOGO_URL = "https://raw.githubusercontent.com/joindouble/logo-double/main/double-logo.png"

# ============================================================================
# 1. PARSING — Meta Ads (MCP Meta Ads) retorna valores como texto formatado,
#    às vezes com o rótulo do tipo de resultado embutido. Ex:
#      "R$0,00 BRL"                                  -> valor monetário
#      "R$0,00 BRL (Messaging conversations started)" -> valor + rótulo
#      "0 (Messaging conversations started)"          -> contagem + rótulo
#      "Not available"                                 -> sem entrega no período
#      [{"indicator": "video_thruplay_watched_actions"}] -> formato alternativo
# ============================================================================

def _parse_meta_number(raw):
    """Extrai um float de qualquer formato numérico que o Meta devolve.
    Retorna 0.0 se não houver dado disponível (em vez de quebrar)."""
    if raw is None:
        return 0.0
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        if raw.strip().lower() == "not available":
            return 0.0
        # remove tudo que não for dígito, vírgula, ponto ou sinal de menos
        limpo = re.sub(r"[^\d,.\-]", "", raw)
        if not limpo:
            return 0.0
        # Formato brasileiro do Meta: decimal com vírgula, milhar com ponto.
        # Se há vírgula, ela é o decimal e qualquer ponto é separador de milhar.
        # Se NÃO há vírgula, o ponto é sempre separador de milhar — ex: "1.327"
        # são 1327 impressões, não 1,327. (Bug real: sem isso, impressões e CTR
        # do Meta saíam completamente errados.)
        if "," in limpo:
            limpo = limpo.replace(".", "").replace(",", ".")
        else:
            limpo = limpo.replace(".", "")
        try:
            return float(limpo)
        except ValueError:
            return 0.0
    return 0.0


def parse_meta_amount_spent(raw):
    """'R$0,00\xa0BRL' -> 0.0 """
    return _parse_meta_number(raw)


def parse_meta_result_field(raw):
    """Trata os formatos de 'results' / 'cost_per_result' do Meta.
    Retorna (valor_numerico, rotulo_do_resultado | None).
    Cobre os 3 formatos observados em conta real:
      {"value": "8 (Messaging conversations started)"}
      {"value": "R$1,23 BRL (Post engagements)"}
      {"value": [{"indicator": "video_thruplay_watched_actions"}]}   <- sem valor numérico usável
    """
    if raw is None:
        return 0.0, None
    valor = raw.get("value") if isinstance(raw, dict) else raw

    if isinstance(valor, list):
        # formato "indicator only" (ex: video views) — não dá pra extrair
        # um número confiável aqui; devolve 0 e sinaliza o indicador como rótulo.
        if valor and isinstance(valor[0], dict) and "indicator" in valor[0]:
            return 0.0, valor[0]["indicator"]
        return 0.0, None

    if isinstance(valor, str):
        m = re.match(r"^\s*([^\(]+?)\s*(?:\(([^)]+)\))?\s*$", valor)
        if not m:
            return 0.0, None
        numero_txt, rotulo = m.group(1), m.group(2)
        return _parse_meta_number(numero_txt), rotulo

    return 0.0, None


# ============================================================================
# 2. FORMATAÇÃO — converte números "crus" para o formato exibido no e-mail
#    (padrão BR: milhar com ponto, decimal com vírgula).
# ============================================================================

def fmt_brl(valor):
    """1234.5 -> 'R$ 1.234,50'"""
    texto = f"{valor:,.2f}"
    texto = texto.replace(",", "§").replace(".", ",").replace("§", ".")
    return f"R$ {texto}"


def fmt_int(valor):
    """1234567 -> '1.234.567'   |   1240000 -> '1,24 mi' quando compacto=True"""
    texto = f"{int(round(valor)):,}"
    return texto.replace(",", ".")


def fmt_compacto(valor):
    """Números grandes de impressões ficam mais legíveis como '1,24 mi'."""
    if valor >= 1_000_000:
        return f"{valor/1_000_000:.2f}".replace(".", ",") + " mi"
    if valor >= 10_000:
        return f"{valor/1_000:.1f}".replace(".", ",") + " mil"
    return fmt_int(valor)


def fmt_pct(valor, casas=2):
    return f"{valor:.{casas}f}".replace(".", ",") + "%"


# ============================================================================
# 3. MAPEAMENTO — qual sub-bloco METRICA_* usar para cada campanha
# ============================================================================
# Meta: usamos o rótulo que o próprio "results" devolve como sinal PRIMÁRIO
# (é a fonte mais confiável — o texto vem direto da API, por campanha real,
# não é uma suposição nossa) e o campo "objective" como sinal de apoio/fallback.
# Cobre tanto os objetivos novos (OUTCOME_*) quanto os legados (campanhas
# antigas na conta real da R&M ainda usam MESSAGES, POST_ENGAGEMENT etc.)

BLOCO_MENSAGENS = "mensagens"
BLOCO_TRAFEGO_PERFIL = "trafego_perfil"
BLOCO_ALCANCE = "alcance"
BLOCO_GOOGLE_PADRAO = "google_padrao"
BLOCO_DESCONHECIDO = "desconhecido"  # nunca deve sobrar em produção — ver aviso no final

OBJETIVO_LABEL_PT = {
    BLOCO_MENSAGENS: "Mensagens",  # refinado por canal (WhatsApp/Direct) em tempo de execução
    BLOCO_TRAFEGO_PERFIL: "Tráfego para o perfil",
    BLOCO_ALCANCE: "Alcance",
    BLOCO_GOOGLE_PADRAO: "Rede de Pesquisa",
    BLOCO_DESCONHECIDO: "Objetivo não mapeado",
}

# --- Canal da campanha de MENSAGENS (WhatsApp x Direct) --------------------
# Campanha de mensagem pode mandar a conversa pro WhatsApp OU pro Direct do
# Instagram. As MÉTRICAS são as mesmas (mesmo sub-bloco METRICA_MENSAGENS), o
# que muda é o RÓTULO — o cliente precisa saber pra onde a conversa foi. Com
# as duas rodando juntas, cada campanha é rotulada individualmente.
CANAL_WHATSAPP = "whatsapp"
CANAL_DIRECT = "direct"
CANAL_GENERICO = "generico"

CANAL_LABEL_PT = {
    CANAL_WHATSAPP: "Mensagens no WhatsApp",
    CANAL_DIRECT: "Mensagens no Direct",
    CANAL_GENERICO: "Mensagens",
}
CANAL_TITULO_PT = {
    CANAL_WHATSAPP: "Mensagens · WhatsApp",
    CANAL_DIRECT: "Mensagens · Direct",
    CANAL_GENERICO: "Mensagens",
}

# Valores do campo destination_type do Meta (sinal mais confiável quando existe).
_CANAL_POR_DESTINO = {
    "WHATSAPP": CANAL_WHATSAPP,
    "WHATSAPP_MESSAGE": CANAL_WHATSAPP,
    "INSTAGRAM_DIRECT": CANAL_DIRECT,
    "INSTAGRAM_DIRECT_MESSAGE": CANAL_DIRECT,
    "MESSENGER": CANAL_DIRECT,
    "MESSAGENGER": CANAL_DIRECT,
}

# Palavras no nome da campanha / rótulo de resultado. Ordem importa: WhatsApp
# primeiro (mais específico) para "[Mensagens][WhatsApp]" não cair em Direct.
_CANAL_KEYWORDS = [
    (CANAL_WHATSAPP, ["whatsapp", "whats app", "wpp", "zap", " wa ", "[wa]"]),
    (CANAL_DIRECT, ["direct", "instagram", " ig ", "[ig]", "dm"]),
]


def detectar_canal_mensagem(nome, destino=None, resultado_rotulo=None):
    """Descobre se a campanha de mensagem é WhatsApp ou Direct.

    Ordem de confiança:
      1. `destination_type` do Meta (dado da API, quando a Routine salvar);
      2. palavra-chave no NOME da campanha (padrão de nomenclatura da agência:
         "[MENSAGENS] [DIRECT]", "[Mensagens][WhatsApp]");
      3. palavra-chave no rótulo de resultado da API;
      4. genérico ("Mensagens") — nunca chuta WhatsApp por padrão, para não
         escrever no relatório do cliente um canal que pode estar errado.
    """
    if destino:
        canal = _CANAL_POR_DESTINO.get(str(destino).strip().upper())
        if canal:
            return canal
    for texto in (nome, resultado_rotulo):
        if not texto:
            continue
        alvo = f" {str(texto).lower()} "
        for canal, palavras in _CANAL_KEYWORDS:
            if any(p in alvo for p in palavras):
                return canal
    return CANAL_GENERICO

_LABEL_KEYWORDS = [
    (BLOCO_MENSAGENS, ["messag", "conversa"]),
    (BLOCO_TRAFEGO_PERFIL, ["profile", "perfil", "visit"]),
    (BLOCO_ALCANCE, ["reach", "alcance"]),
]

_OBJETIVO_FALLBACK = {
    "OUTCOME_ENGAGEMENT": BLOCO_MENSAGENS,   # validado com dado real da R&M
    "MESSAGES": BLOCO_MENSAGENS,             # objetivo legado
    "OUTCOME_LEADS": BLOCO_MENSAGENS,
    "OUTCOME_AWARENESS": BLOCO_ALCANCE,
    "POST_ENGAGEMENT": BLOCO_TRAFEGO_PERFIL,  # objetivo legado — validar em campo real ativo
    "OUTCOME_TRAFFIC": BLOCO_TRAFEGO_PERFIL,
    "LINK_CLICKS": BLOCO_TRAFEGO_PERFIL,      # validado com dado real da HydroCenter (tráfego p/ perfil)
}


def mapear_bloco_meta(objective, resultado_rotulo):
    """Decide qual dos 3 sub-blocos de métrica do Meta usar.
    1º critério: palavra-chave no rótulo que a própria API devolveu.
    2º critério (fallback): tabela de objetivo conhecido.
    Se nada bater, marca como DESCONHECIDO — a campanha ainda aparece no
    relatório (com os campos genéricos), mas fica sinalizada para revisão
    manual em vez de ser encaixada errada silenciosamente.
    """
    if resultado_rotulo:
        rotulo_lower = resultado_rotulo.lower()
        for bloco, palavras in _LABEL_KEYWORDS:
            if any(p in rotulo_lower for p in palavras):
                return bloco
    if objective in _OBJETIVO_FALLBACK:
        return _OBJETIVO_FALLBACK[objective]
    return BLOCO_DESCONHECIDO


# ============================================================================
# 4. NORMALIZAÇÃO — transforma o JSON bruto de cada plataforma numa mesma
#    estrutura interna, usada pelo resto do motor.
# ============================================================================

def normalizar_campanha_meta(raw):
    investimento = parse_meta_amount_spent(raw.get("amount_spent"))
    impressoes = _parse_meta_number(raw.get("impressions"))
    alcance = _parse_meta_number(raw.get("reach"))
    cliques = _parse_meta_number(raw.get("clicks"))
    cpc = _parse_meta_number(raw.get("cpc"))
    cpm = _parse_meta_number(raw.get("cpm"))
    ctr = (cliques / impressoes * 100) if impressoes else 0.0
    custo_resultado, rotulo = parse_meta_result_field(raw.get("cost_per_result"))
    qtd_resultado, _ = parse_meta_result_field(raw.get("results"))

    nome = raw.get("name", "")
    bloco = mapear_bloco_meta(raw.get("objective"), rotulo)

    # Campanha de mensagem: rotula o canal (WhatsApp x Direct) por campanha, para
    # o relatório dizer pro cliente onde a conversa cai. Demais objetivos usam o
    # rótulo padrão do bloco.
    if bloco == BLOCO_MENSAGENS:
        canal = detectar_canal_mensagem(
            nome, raw.get("destination_type") or raw.get("messaging_destination"), rotulo
        )
        objetivo_label = CANAL_LABEL_PT[canal]
        canal_titulo = CANAL_TITULO_PT[canal]
    else:
        canal = None
        objetivo_label = OBJETIVO_LABEL_PT.get(bloco, bloco)
        canal_titulo = objetivo_label

    return {
        "id": raw.get("id"),
        "nome": nome,
        "plataforma": "Meta Ads",
        "bloco": bloco,
        "canal_mensagem": canal,
        "canal_titulo": canal_titulo,
        "objetivo_label": objetivo_label,
        "status": raw.get("effective_status"),
        "investimento": investimento,
        "impressoes": impressoes,
        "alcance": alcance,
        "cliques": cliques,
        "cpc": cpc,
        "cpm": cpm,
        "ctr": ctr,
        "custo_por_resultado": custo_resultado,
        "qtd_resultado": qtd_resultado,
        "serie_diaria": raw.get("serie_diaria") or [],
    }


def normalizar_campanha_google(raw):
    impressoes = float(raw.get("impressions", 0) or 0)
    cliques = float(raw.get("clicks", 0) or 0)
    cpc = float(raw.get("cpc", 0) or 0)
    ctr = (cliques / impressoes * 100) if impressoes else 0.0
    # Alcance no Google só existe para campanhas de Display/Vídeo/Demand Gen.
    # Campanha de Rede de Pesquisa devolve reach=0 (o Google não mede usuários
    # únicos aqui) — nesse caso o card de alcance vira CTR no template (ver
    # METRICA_GOOGLE_PADRAO). Só guarda o alcance se houver valor real (>0).
    reach = float(raw.get("reach", 0) or 0)

    # "Custo por resultado" depende da estratégia de lance da campanha:
    # em campanha de MAXIMIZAÇÃO DE CLIQUES (Google reporta como "TARGET_SPEND"
    # ou "MAXIMIZE_CLICKS") o resultado É o clique — então o custo por resultado
    # é o próprio CPC, e o card muda o rótulo para "Custo por clique". Nas demais
    # (maximização de conversões etc.) usa o custo por conversão.
    objetivo = (raw.get("objective") or "").upper()
    maximiza_cliques = ("CLICK" in objetivo) or (objetivo == "TARGET_SPEND")
    if maximiza_cliques:
        custo_resultado = cpc
        custo_resultado_label = "Custo por clique"
    else:
        custo_resultado = float(raw.get("cost_per_conversion", 0) or 0)
        custo_resultado_label = "Custo por resultado"

    return {
        "id": raw.get("campaign_id"),
        "nome": raw.get("campaign", ""),
        "plataforma": "Google Ads",
        "bloco": BLOCO_GOOGLE_PADRAO,
        "canal_mensagem": None,
        "canal_titulo": OBJETIVO_LABEL_PT[BLOCO_GOOGLE_PADRAO],
        "objetivo_label": OBJETIVO_LABEL_PT[BLOCO_GOOGLE_PADRAO],
        "status": raw.get("campaign_status"),
        "investimento": float(raw.get("spend", 0) or 0),
        "impressoes": impressoes,
        "alcance": reach if reach > 0 else None,
        "cliques": cliques,
        "cpc": cpc,
        "cpm": None,
        "ctr": ctr,
        "custo_por_resultado": custo_resultado,
        "custo_resultado_label": custo_resultado_label,
        "qtd_resultado": float(raw.get("conversions", 0) or 0),
        "serie_diaria": raw.get("serie_diaria") or [],
    }


# ============================================================================
# 5. AGREGAÇÃO — totais gerais e por plataforma
# ============================================================================

def agregar_plataforma(campanhas):
    """Soma as campanhas de UMA plataforma e devolve o dict usado no
    INICIO_BLOCO_PLATAFORMA. cliques/impressoes somados; cpc e ctr
    recalculados a partir dos totais (não é média simples das campanhas)."""
    investimento = sum(c["investimento"] for c in campanhas)
    impressoes = sum(c["impressoes"] for c in campanhas)
    cliques = sum(c["cliques"] for c in campanhas)
    cpc = (investimento / cliques) if cliques else 0.0
    ctr = (cliques / impressoes * 100) if impressoes else 0.0
    return {
        "investimento": investimento,
        "impressoes": impressoes,
        "cliques": cliques,
        "cpc": cpc,
        "ctr": ctr,
    }


def agregar_geral(todas_campanhas):
    investimento = sum(c["investimento"] for c in todas_campanhas)
    impressoes = sum(c["impressoes"] for c in todas_campanhas)
    cliques = sum(c["cliques"] for c in todas_campanhas)
    cpc = (investimento / cliques) if cliques else 0.0
    return {
        "investimento": investimento,
        "impressoes": impressoes,
        "cliques": cliques,
        "cpc": cpc,
    }


# ============================================================================
# 6. GRÁFICOS — replica exatamente a config visual que veio do template
#    (cores, doughnut/bar, datalabels) só trocando os dados.
# ============================================================================

_DIAS_SEMANA = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]
_MESES_PT = {
    "janeiro": 1, "fevereiro": 2, "março": 3, "abril": 4, "maio": 5, "junho": 6,
    "julho": 7, "agosto": 8, "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12,
}


def _parse_data(s):
    """Converte data para datetime. Aceita ISO 'YYYY-MM-DD' (Google/Flyweel)
    e o formato PT-BR 'D de mês de AAAA' (Meta Ads devolve assim no breakdown
    diário). Retorna None se não reconhecer."""
    if not s:
        return None
    s = str(s).strip()
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        pass
    m = re.match(r"(\d{1,2})\s+de\s+([^\s]+)\s+de\s+(\d{4})", s.lower())
    if m:
        dia, mes_nome, ano = int(m.group(1)), m.group(2), int(m.group(3))
        mes = _MESES_PT.get(mes_nome)
        if mes:
            return datetime(ano, mes, dia)
    return None


def _serie_semanal(dados_por_dia, campo="spend"):
    """Recebe lista de {"date", <campo>} e devolve 7 valores na ordem
    Seg..Dom, somados por dia da semana (weekday). Datas não reconhecidas
    são ignoradas. Robusto a semanas que não começam na segunda: cada dia
    cai na sua coluna de weekday."""
    valores = [0.0] * 7
    for d in dados_por_dia or []:
        dt = _parse_data(d.get("date"))
        if dt is None:
            continue
        valores[dt.weekday()] += float(d.get(campo, 0) or 0)
    return [round(v, 2) for v in valores]


_MESES_NOME = {
    1: "janeiro", 2: "fevereiro", 3: "março", 4: "abril", 5: "maio", 6: "junho",
    7: "julho", 8: "agosto", 9: "setembro", 10: "outubro", 11: "novembro", 12: "dezembro",
}


def _label_periodo(inicio, fim):
    """Monta o rótulo em PT-BR do intervalo (ex: 'Semana de 06 a 12 de julho de
    2026'; ou cruzando mês: 'Semana de 29 de junho a 05 de julho de 2026')."""
    if inicio.month == fim.month and inicio.year == fim.year:
        return (f"Semana de {inicio.day:02d} a {fim.day:02d} "
                f"de {_MESES_NOME[fim.month]} de {fim.year}")
    if inicio.year == fim.year:
        return (f"Semana de {inicio.day:02d} de {_MESES_NOME[inicio.month]} "
                f"a {fim.day:02d} de {_MESES_NOME[fim.month]} de {fim.year}")
    return (f"Semana de {inicio.day:02d} de {_MESES_NOME[inicio.month]} de {inicio.year} "
            f"a {fim.day:02d} de {_MESES_NOME[fim.month]} de {fim.year}")


def calcular_periodo(hoje=None):
    """Período da SEMANA ANTERIOR COMPLETA (segunda a domingo) relativa a
    `hoje` (default: data de execução). Feito para rodar na segunda-feira:
    devolve a segunda..domingo da semana que acabou de fechar. Determinístico —
    a Routine usa EXATAMENTE estas datas na busca de dados e o mesmo `label` no
    relatório, sem cálculo de data do lado da IA.

    Retorna {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD", "label": "..."}.
    """
    if hoje is None:
        hoje = date.today()
    elif isinstance(hoje, str):
        hoje = datetime.strptime(hoje, "%Y-%m-%d").date()
    segunda_desta = hoje - timedelta(days=hoje.weekday())  # segunda desta semana
    inicio = segunda_desta - timedelta(days=7)             # segunda da semana passada
    fim = segunda_desta - timedelta(days=1)                # domingo da semana passada
    return {"start": inicio.isoformat(), "end": fim.isoformat(),
            "label": _label_periodo(inicio, fim)}


def _fmt_valor_barra(v):
    """Valor da barra no padrão BR sem 'R$' (compacto p/ caber sobre a barra)."""
    if not v:
        return "0"
    return f"{v:,.2f}".replace(",", "§").replace(".", ",").replace("§", ".")


def _barras_verticais_html(valores, labels, plot=150, cor="#3B82F6"):
    """Mini gráfico de barras 100% HTML — cada barra é um <td> colorido com
    altura (bgcolor + height). O VALOR GASTO no dia aparece logo acima da barra
    (dado real da série, sem cálculo). NÃO usa <div> com height (Gmail descarta).
    `plot` = altura da área de plotagem em px; `topo` reserva espaço p/ o rótulo."""
    maxv = max(valores) if valores else 0
    n = len(valores) or 1
    w = round(100.0 / n, 4)
    topo = 24  # px reservados no topo para o rótulo de valor caber sobre a barra
    colunas, rotulos = [], []
    for v, l in zip(valores, labels):
        h_px = int(round((v / maxv) * (plot - topo))) if maxv > 0 else 0
        h_px = max(h_px, 3)                 # sliver mínimo p/ dias sem gasto
        espac = max(plot - h_px, topo)      # espaço acima da barra (>= topo)
        val_txt = _fmt_valor_barra(v)
        colunas.append(
            f'<td width="{w}%" valign="bottom" style="padding:0 3px;">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
            f'<tr><td valign="bottom" align="center" height="{espac}" '
            f'style="height:{espac}px;font-family:\'Courier New\',Courier,monospace;'
            f'font-size:10px;line-height:12px;color:#CBD5F5;white-space:nowrap;'
            f'padding:0 0 3px 0;">{val_txt}</td></tr>'
            f'<tr><td height="{h_px}" bgcolor="{cor}" style="height:{h_px}px;'
            f'line-height:{h_px}px;font-size:1px;mso-line-height-rule:exactly;'
            f'background-color:{cor};border-radius:4px 4px 0 0;">&nbsp;</td></tr>'
            f'</table></td>'
        )
        rotulos.append(
            f'<td width="{w}%" align="center" style="padding:7px 0 0 0;'
            f"font-family:\'Courier New\',Courier,monospace;font-size:11px;"
            f'line-height:14px;color:#94A3B8;">{l}</td>'
        )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
        "<tr>" + "".join(colunas) + "</tr>"
        "<tr>" + "".join(rotulos) + "</tr></table>"
    )


def _grafico_sem_serie(total=None):
    """Aviso honesto para quando a série por dia NÃO veio da API.

    Antes existia um fallback que jogava o total do período numa única barra
    (domingo no semanal, última semana no mensal). Visualmente isso mentia:
    o cliente lia "gastou tudo no domingo". Sem série, não se desenha barra —
    mostra-se o aviso e, se houver, o total agregado (esse sim é real)."""
    txt = "Detalhamento por dia indisponível para este período."
    if total:
        txt += " Investimento total: R$ " + _fmt_valor_barra(round(total, 2)) + "."
    return ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            'border="0"><tr><td align="center" bgcolor="#0F172A" '
            'style="background-color:#0F172A;border-radius:6px;padding:22px 12px;'
            "font-family:Arial,Helvetica,sans-serif;font-size:12px;line-height:17px;"
            'color:#94A3B8;">' + txt + '</td></tr></table>')


def grafico_investimento_por_dia(dados_por_dia, total=None):
    """Barras Seg..Dom com o investimento de cada dia (HTML puro). Sem série,
    avisa que o detalhamento diário não veio — não inventa barra."""
    valores = _serie_semanal(dados_por_dia, "spend")
    if not any(valores):
        return _grafico_sem_serie(total)
    return _barras_verticais_html(valores, _DIAS_SEMANA, plot=150, cor="#3B82F6")


def grafico_campanha(serie_diaria, total=None, cor="#3B82F6"):
    """Barras Seg..Dom do gasto diário da campanha (HTML puro). Sem série
    diária, mostra aviso de dado indisponível (nunca uma barra falsa)."""
    valores = _serie_semanal(serie_diaria, "spend") if serie_diaria else []
    if not any(valores):
        return _grafico_sem_serie(total)
    return _barras_verticais_html(valores, _DIAS_SEMANA, plot=120, cor=cor)


def grafico_investimento_por_plataforma(total_meta, total_google):
    """Barra horizontal segmentada Meta vs Google + legenda (HTML puro),
    substitui o donut. Só entram na legenda plataformas com investimento."""
    tm = round(total_meta or 0, 2)
    tg = round(total_google or 0, 2)
    total = tm + tg
    if total <= 0:
        return ('<div style="font-family:Arial,Helvetica,sans-serif;font-size:13px;'
                'color:#94A3B8;">Sem investimento no período.</div>')
    mp = int(round(tm / total * 100))
    gp = 100 - mp

    segs = []
    if mp > 0:
        raio = "8px" if gp == 0 else "8px 0 0 8px"
        segs.append(f'<td width="{mp}%" height="18" bgcolor="#3B82F6" '
                    f'style="height:18px;background-color:#3B82F6;border-radius:{raio};'
                    f'font-size:1px;line-height:1px;mso-line-height-rule:exactly;">&nbsp;</td>')
    if gp > 0:
        raio = "8px" if mp == 0 else "0 8px 8px 0"
        segs.append(f'<td width="{gp}%" height="18" bgcolor="#22D3EE" '
                    f'style="height:18px;background-color:#22D3EE;border-radius:{raio};'
                    f'font-size:1px;line-height:1px;mso-line-height-rule:exactly;">&nbsp;</td>')
    barra = ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
             'border="0"><tr>' + "".join(segs) + "</tr></table>")

    itens = []
    if tm > 0:
        itens.append(("#3B82F6", "Meta Ads", fmt_brl(tm), mp))
    if tg > 0:
        itens.append(("#22D3EE", "Google Ads", fmt_brl(tg), gp))
    celulas = []
    for i, (cor, nome, valor, pct) in enumerate(itens):
        al = "left" if i == 0 else "right"
        celulas.append(
            f'<td align="{al}" valign="middle" style="font-family:Arial,Helvetica,sans-serif;'
            f'font-size:13px;line-height:18px;color:#CBD5F5;">'
            f'<span style="color:{cor};font-size:14px;">&#9632;</span>&nbsp; '
            f'{nome} — {valor} &middot; {pct}%</td>'
        )
    legenda = ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
               'border="0" style="margin-top:14px;"><tr>' + "".join(celulas) + "</tr></table>")
    return barra + legenda


# ============================================================================
# 7. TEMPLATE — extrai os blocos-molde e monta o HTML final
# ============================================================================

def _extrair_bloco(template, marcador_inicio, marcador_fim):
    """Devolve (antes, bloco_molde, depois) a partir dos comentários
    delimitadores. Usa a PRIMEIRA ocorrência como molde, mas descarta
    do documento final QUALQUER outra instância de exemplo repetida que
    exista entre a primeira abertura e a ÚLTIMA marca de fechamento —
    o template de referência normalmente vem com vários exemplos
    (um por objetivo/plataforma) para o time de design conferir
    visualmente, e todos eles precisam ser substituídos, não só o 1º."""
    ini_tag = f"<!-- {marcador_inicio} -->"
    fim_tag = f"<!-- {marcador_fim} -->"
    primeiro_inicio = template.index(ini_tag)
    primeiro_fim = template.index(fim_tag) + len(fim_tag)
    ultimo_fim = template.rindex(fim_tag) + len(fim_tag)
    molde = template[primeiro_inicio:primeiro_fim]
    return template[:primeiro_inicio], molde, template[ultimo_fim:]


def _manter_apenas_metrica(bloco_html, bloco_manter):
    """Dentro de um card de campanha, remove os sub-blocos METRICA_* que
    não correspondem ao objetivo desta campanha específica."""
    mapa_tag = {
        BLOCO_MENSAGENS: "METRICA_MENSAGENS",
        BLOCO_TRAFEGO_PERFIL: "METRICA_TRAFEGO_PERFIL",
        BLOCO_ALCANCE: "METRICA_ALCANCE",
        BLOCO_GOOGLE_PADRAO: "METRICA_GOOGLE_PADRAO",
    }
    tag_manter = mapa_tag.get(bloco_manter)
    resultado = bloco_html
    for chave, tag in mapa_tag.items():
        padrao = re.compile(
            r"<!--\s*" + re.escape(tag) + r"\s*-->.*?<!--\s*/" + re.escape(tag) + r"\s*-->",
            re.DOTALL,
        )
        if tag == tag_manter:
            # mantém o conteúdo, só remove os comentários delimitadores
            resultado = padrao.sub(lambda m: re.sub(r"<!--\s*/?" + re.escape(tag) + r"\s*-->", "", m.group(0)), resultado)
        else:
            resultado = padrao.sub("", resultado)
    return resultado


_METRICA_TAGS = ["METRICA_GOOGLE_PADRAO", "METRICA_MENSAGENS", "METRICA_TRAFEGO_PERFIL", "METRICA_ALCANCE"]


def _coletar_metricas(template_full):
    """Coleta os 4 sub-blocos METRICA_* (com delimitadores) de onde estiverem
    no template — eles vêm espalhados em blocos de campanha de exemplo
    diferentes (o 1º bloco só tem GOOGLE_PADRAO, o 2º só MENSAGENS, etc.)."""
    blocos = {}
    for tag in _METRICA_TAGS:
        m = re.search(
            r"<!--\s*" + re.escape(tag) + r"\s*-->.*?<!--\s*/" + re.escape(tag) + r"\s*-->",
            template_full, re.DOTALL,
        )
        if m:
            blocos[tag] = m.group(0)
    return blocos


def _enriquecer_molde_campanha(molde_campanha, metricas):
    """O molde (1º bloco de campanha do template) só traz UM sub-bloco de
    métrica. Para qualquer objetivo poder ser renderizado, troca esse único
    sub-bloco pela concatenação dos 4 — depois _manter_apenas_metrica remove
    os 3 que não servem. Sem isso, campanha de Meta (Mensagens/Tráfego/Alcance)
    renderizava com o bloco de métricas VAZIO."""
    combinado = "\n".join(metricas[t] for t in _METRICA_TAGS if t in metricas)
    for tag in _METRICA_TAGS:
        padrao = re.compile(
            r"<!--\s*" + re.escape(tag) + r"\s*-->.*?<!--\s*/" + re.escape(tag) + r"\s*-->",
            re.DOTALL,
        )
        if padrao.search(molde_campanha):
            return padrao.sub(lambda m: combinado, molde_campanha, count=1)
    return molde_campanha


def _preencher(texto, valores):
    for chave, valor in valores.items():
        texto = texto.replace("{{" + chave + "}}", str(valor))
    return texto


def montar_bloco_campanha(molde_campanha, campanha):
    bloco = molde_campanha
    bloco = _manter_apenas_metrica(bloco, campanha["bloco"])

    valores = {
        "campanha_plataforma": campanha["plataforma"],
        "campanha_objetivo_label": campanha["objetivo_label"],
        "campanha_canal_titulo": campanha.get("canal_titulo", campanha["objetivo_label"]),
        "campanha_nome": campanha["nome"],
        "campanha_investimento": fmt_brl(campanha["investimento"]),
        "campanha_grafico": campanha["grafico_html"],
        "campanha_impressoes": fmt_int(campanha["impressoes"]),
        "campanha_alcance": fmt_int(campanha["alcance"]) if campanha["alcance"] is not None else "\u2014",
        "campanha_cliques": fmt_int(campanha["cliques"]),
        "campanha_cpc": fmt_brl(campanha["cpc"]),
        "campanha_ctr": fmt_pct(campanha.get("ctr", 0)),
        "campanha_cpm": fmt_brl(campanha["cpm"]) if campanha["cpm"] is not None else "\u2014",
        "campanha_custo_mensagem": fmt_brl(campanha["custo_por_resultado"]),
        "campanha_custo_trafego_perfil": fmt_brl(campanha["custo_por_resultado"]),
        "campanha_custo_por_resultado": fmt_brl(campanha["custo_por_resultado"]),
        "campanha_custo_resultado_label": campanha.get("custo_resultado_label", "Custo por resultado"),
    }
    return _preencher(bloco, valores)


def montar_bloco_plataforma(molde_plataforma, nome_plataforma, totais):
    valores = {
        "plataforma_nome": nome_plataforma,
        "plataforma_investimento_total": fmt_brl(totais["investimento"]),
        "plataforma_impressoes": fmt_int(totais["impressoes"]),
        "plataforma_cliques": fmt_int(totais["cliques"]),
        "plataforma_cpc": fmt_brl(totais["cpc"]),
        "plataforma_ctr": fmt_pct(totais["ctr"]),
    }
    return _preencher(molde_plataforma, valores)


def gerar_relatorio_html(template_path, cliente, campanhas_meta, campanhas_google,
                          insight_texto, dados_por_dia=None):
    """Função principal: monta o HTML final pronto para o rascunho do Gmail.

    cliente: dict com nome, periodo_label (logo/contato/nome da agência têm
      default no motor; só passar se quiser sobrescrever)
    campanhas_meta / campanhas_google: listas já normalizadas
      (normalizar_campanha_meta / normalizar_campanha_google), cada uma
      podendo carregar "serie_diaria" (lista de {"date","spend"}).
    dados_por_dia: opcional. Fallback para o gráfico "Investimento por dia"
      do topo quando as campanhas não trazem serie_diaria. Normalmente não é
      necessário — o motor soma a serie_diaria das campanhas.
    """
    with open(template_path, encoding="utf-8") as f:
        template = f.read()
    template_full = template  # guarda o original para colher os sub-blocos METRICA_*

    # --- Filtro de atividade no período: só entram no relatório campanhas
    #     que rodaram na semana (investimento > 0). Uma campanha pode estar
    #     ACTIVE mas sem entrega no período (o Meta devolve ACTIVE com
    #     "R$0,00") — essas não aparecem. Uma campanha PAUSADA que gastou
    #     durante a semana continua aparecendo, porque o critério é gasto,
    #     não status. Plataforma que ficar sem nenhuma campanha some sozinha
    #     (os blocos abaixo só renderizam se a lista tiver itens). ---
    def _rodou(c):
        return (c.get("investimento") or 0) > 0

    ocultas_meta = [c["nome"] for c in campanhas_meta if not _rodou(c)]
    ocultas_google = [c["nome"] for c in campanhas_google if not _rodou(c)]
    campanhas_meta = [c for c in campanhas_meta if _rodou(c)]
    campanhas_google = [c for c in campanhas_google if _rodou(c)]
    if ocultas_meta or ocultas_google:
        print(f"[filtro] Campanhas sem investimento no período (ocultadas) — "
              f"Meta: {ocultas_meta} | Google: {ocultas_google}")

    todas = campanhas_meta + campanhas_google

    if any(c["bloco"] == BLOCO_DESCONHECIDO for c in todas):
        nomes = [c["nome"] for c in todas if c["bloco"] == BLOCO_DESCONHECIDO]
        print(f"[AVISO] {len(nomes)} campanha(s) com objetivo não mapeado, revisar manualmente: {nomes}")

    # gráfico por campanha: usa a série diária real da campanha (barras por
    # dia da semana). Se a campanha não trouxer serie_diaria, cai no fallback
    # de barra única com o total (dentro de grafico_campanha).
    for c in todas:
        c["grafico_html"] = grafico_campanha(c.get("serie_diaria") or [], total=c["investimento"])

    # --- Bloco de plataforma ---
    antes, molde_plataforma, resto = _extrair_bloco(template, "INICIO_BLOCO_PLATAFORMA", "FIM_BLOCO_PLATAFORMA")
    blocos_plataforma = ""
    if campanhas_meta:
        blocos_plataforma += montar_bloco_plataforma(molde_plataforma, "Meta Ads", agregar_plataforma(campanhas_meta))
    if campanhas_google:
        blocos_plataforma += montar_bloco_plataforma(molde_plataforma, "Google Ads", agregar_plataforma(campanhas_google))
    template = antes + blocos_plataforma + resto

    # --- Bloco de campanha ---
    antes, molde_campanha, resto = _extrair_bloco(template, "INICIO_BLOCO_CAMPANHA", "FIM_BLOCO_CAMPANHA")
    # o molde só traz 1 sub-bloco de métrica; enriquece com os 4 para que
    # campanhas de qualquer objetivo (Meta e Google) renderizem corretamente.
    molde_campanha = _enriquecer_molde_campanha(molde_campanha, _coletar_metricas(template_full))
    campanhas_ordenadas = sorted(todas, key=lambda c: c["investimento"], reverse=True)
    blocos_campanha = "".join(montar_bloco_campanha(molde_campanha, c) for c in campanhas_ordenadas)
    template = antes + blocos_campanha + resto

    # --- Totais gerais + gráficos de topo ---
    geral = agregar_geral(todas)
    total_meta = sum(c["investimento"] for c in campanhas_meta)
    total_google = sum(c["investimento"] for c in campanhas_google)

    # gráfico "investimento por dia" do topo = soma da série diária de TODAS
    # as campanhas (Meta + Google combinados). Se nenhuma campanha trouxe
    # série, usa o fallback dados_por_dia (ex: por_dia da conta no Flyweel).
    serie_diaria_geral = []
    for c in todas:
        serie_diaria_geral.extend(c.get("serie_diaria") or [])
    if not serie_diaria_geral:
        serie_diaria_geral = dados_por_dia or []

    valores_topo = {
        "cliente_nome": cliente["nome"],
        "periodo_label": cliente["periodo_label"],
        "agencia_logo_url": cliente.get("agencia_logo_url", AGENCIA_LOGO_URL),
        "dash_investimento_total": fmt_brl(geral["investimento"]),
        "dash_impressoes_totais": fmt_compacto(geral["impressoes"]),
        "dash_cliques_totais": fmt_int(geral["cliques"]),
        "dash_cpc_medio": fmt_brl(geral["cpc"]),
        "dash_grafico_investimento_por_plataforma": grafico_investimento_por_plataforma(total_meta, total_google),
        "dash_grafico_investimento_por_dia": grafico_investimento_por_dia(
            serie_diaria_geral, total=geral["investimento"]),
        "insight_texto": insight_texto,
        "agencia_nome": cliente.get("agencia_nome", AGENCIA_NOME),
        "agencia_contato": cliente.get("agencia_contato", AGENCIA_CONTATO),
        "ano_atual": str(datetime.now().year),
    }
    template = _preencher(template, valores_topo)

    return template
