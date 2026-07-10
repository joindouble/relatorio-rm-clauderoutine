# -*- coding: utf-8 -*-
"""
Testes de regressão do motor de relatório (report_engine).
Cobre os dois comportamentos críticos + as features novas:
  FIX 1 — segmentação por conta (Google/Flyweel)
  FIX 2 — só campanha que rodou na semana (investimento > 0), ignorando status
  FIX 3 — métricas diárias reais por campanha (parser ISO + PT-BR)
  Remoção da comparação com semana anterior
  Defaults de logo/contato da agência

Rodar:  python tests/test_report.py     (sai 1 se algo falhar)
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from datetime import datetime
from report_engine import (
    gerar_relatorio_html, normalizar_campanha_meta, normalizar_campanha_google,
    _parse_data, _serie_semanal, _parse_meta_number, calcular_periodo,
    AGENCIA_CONTATO, AGENCIA_LOGO_URL,
)

TEMPLATE = os.path.join(REPO, "template.html")
falhas = []


def check(cond, msg):
    print(("  OK  " if cond else "FALHA ") + msg)
    if not cond:
        falhas.append(msg)


# ---------------------------------------------------------------------------
# Fixtures sintéticas (cobrem os casos de borda de propósito)
# ---------------------------------------------------------------------------
meta_raw = [
    # ACTIVE mas R$0,00 -> deve SUMIR
    {"id": "1", "name": "[Mensagens][WhatsApp] parada", "objective": "OUTCOME_ENGAGEMENT",
     "effective_status": "ACTIVE", "amount_spent": "R$0,00 BRL",
     "cost_per_result": {"value": "R$0,00 BRL (Messaging conversations started)"},
     "results": {"value": "0 (Messaging conversations started)"},
     "impressions": "Not available", "clicks": "Not available", "cpc": "Not available",
     "cpm": "Not available", "reach": "0"},
    # PAUSED mas gastou na semana -> deve APARECER
    {"id": "2", "name": "[Mensagens][WhatsApp] rodou pausada", "objective": "OUTCOME_ENGAGEMENT",
     "effective_status": "PAUSED", "amount_spent": "R$150,00 BRL",
     "cost_per_result": {"value": "R$7,50 BRL (Messaging conversations started)"},
     "results": {"value": "20 (Messaging conversations started)"},
     "impressions": "12.000", "clicks": "300", "cpc": "R$0,50", "cpm": "R$12,50", "reach": "9.000",
     "serie_diaria": [{"date": "8 de julho de 2026", "spend": 150.0}]},
]
google_raw = [
    {"campaign": "Rede X", "campaign_id": "g1", "campaign_status": "ENABLED", "objective": "TARGET_SPEND",
     "spend": 71.95, "impressions": 7714, "clicks": 156, "conversions": 7, "cpc": 0.46, "cost_per_conversion": 10.28,
     "account": "Customer 3013993639",
     "serie_diaria": [{"date": "2026-07-06", "spend": 40.0}, {"date": "2026-07-09", "spend": 31.95}]},
    {"campaign": "Rede Zerada", "campaign_id": "g2", "campaign_status": "ENABLED", "objective": "TARGET_SPEND",
     "spend": 0, "impressions": 0, "clicks": 0, "conversions": 0, "cpc": 0, "cost_per_conversion": 0,
     "account": "Customer 3013993639"},
]
cm = [normalizar_campanha_meta(c) for c in meta_raw]
cg = [normalizar_campanha_google(c) for c in google_raw]

html = gerar_relatorio_html(
    template_path=TEMPLATE,
    cliente={"nome": "Teste", "periodo_label": "Semana teste"},
    campanhas_meta=cm, campanhas_google=cg,
    insight_texto="x",
)

print("\n[REMOCAO] variacao semana anterior")
check("{{dash_variacao_investimento}}" not in html, "placeholder de variacao removido")
check("vs. semana anterior" not in html, "texto 'vs. semana anterior' ausente")

print("\n[LOGO/CONTATO] defaults da agencia")
check(AGENCIA_LOGO_URL in html, "logo absoluta da Double no HTML")
check(AGENCIA_CONTATO in html, "contato da Double no HTML")

print("\n[FIX 3] metricas diarias (parser + serie semanal)")
check(_parse_data("2026-07-08").day == 8, "parse ISO")
check(_parse_data("9 de julho de 2026").month == 7, "parse PT-BR")
i06, i09 = datetime(2026, 7, 6).weekday(), datetime(2026, 7, 9).weekday()
s = _serie_semanal([{"date": "2026-07-06", "spend": 30.0}, {"date": "2026-07-09", "spend": 7.25}])
check(s[i06] == 30.0 and s[i09] == 7.25 and abs(sum(s) - 37.25) < 1e-6, f"serie por weekday: {s}")
i08 = datetime(2026, 7, 8).weekday()
check(_serie_semanal([{"date": "8 de julho de 2026", "spend": 10.13}])[i08] == 10.13, "serie PT-BR no weekday certo")

print("\n[BUG A] parsing de milhar do Meta (pt-BR)")
check(_parse_meta_number("1.327") == 1327, "'1.327' -> 1327 (milhar, nao decimal)")
check(_parse_meta_number("12.000") == 12000, "'12.000' -> 12000")
check(_parse_meta_number("R$1.234,56 BRL") == 1234.56, "'R$1.234,56' -> 1234.56")
check(_parse_meta_number("2,64%") == 2.64, "'2,64%' -> 2.64")
check(_parse_meta_number("R$0,85 BRL") == 0.85, "'R$0,85' -> 0.85")
# CTR de plataforma nunca pode passar de 100% (era 2637% com o bug)
import re as _re
for c in _re.findall(r">([\d\.]+,\d+)%<", html):
    check(float(c.replace(".", "").replace(",", ".")) <= 100, f"CTR <=100%: {c}")

print("\n[BUG B] campanha Meta (Mensagens) renderiza bloco de metrica")
check("Custo por mensagem" in html, "sub-bloco METRICA_MENSAGENS presente (nao vazio)")

print("\n[FIX 2] filtro de atividade")
check("rodou pausada" in html, "campanha PAUSED que gastou aparece")
check("parada" not in html, "campanha ACTIVE com R$0 nao aparece")
check("Rede X" in html, "campanha Google com gasto aparece")
check("Rede Zerada" not in html, "campanha Google zerada nao aparece")
check("{{" not in html and "}}" not in html, "nenhum placeholder cru sobrou")
check(html.count('padding:6px 14px;">') == 2, "2 blocos de plataforma renderizados")

print("\n[FIX 2b] plataforma sem campanha ativa some")
html2 = gerar_relatorio_html(
    template_path=TEMPLATE, cliente={"nome": "T", "periodo_label": "p"},
    campanhas_meta=[normalizar_campanha_meta(meta_raw[0])],
    campanhas_google=[normalizar_campanha_google(google_raw[0])],
    insight_texto="x",
)
check('padding:6px 14px;">Meta Ads' not in html2, "bloco Meta some quando sua unica campanha zerou")
check('padding:6px 14px;">Google Ads' in html2, "bloco Google permanece")

print("\n[PERIODO] semana anterior completa (seg-dom), deterministico")
# roda numa segunda (2026-07-13) -> semana passada = 06 a 12 de julho
p = calcular_periodo("2026-07-13")
check(p["start"] == "2026-07-06" and p["end"] == "2026-07-12", f"seg->semana passada seg-dom: {p}")
# roda numa sexta (2026-07-10) -> ultima semana fechada = 29/06 a 05/07
p2 = calcular_periodo("2026-07-10")
check(p2["start"] == "2026-06-29" and p2["end"] == "2026-07-05", f"sexta->ultima semana fechada: {p2}")
check(p2["label"] == "Semana de 29 de junho a 05 de julho de 2026", f"label cruzando mes: {p2['label']}")
# cruzando ano
p3 = calcular_periodo("2026-01-05")
check(p3["label"] == "Semana de 29 de dezembro de 2025 a 04 de janeiro de 2026", f"label cruzando ano: {p3['label']}")

print("\n[FIX 1] segmentacao por conta")
flyweel_account = "Customer 3013993639"
raw_misto = [
    {"campaign": "RM", "account": "Customer 3013993639", "spend": 50, "campaign_id": "a",
     "campaign_status": "ENABLED", "impressions": 100, "clicks": 5, "conversions": 1, "cpc": 10, "cost_per_conversion": 50},
    {"campaign": "HydroCenter", "account": "Customer 5672106894", "spend": 30, "campaign_id": "b",
     "campaign_status": "ENABLED", "impressions": 50, "clicks": 2, "conversions": 0, "cpc": 15, "cost_per_conversion": 0},
]
filtrado = [c for c in raw_misto if not flyweel_account or c.get("account") in (None, flyweel_account)]
check([c["campaign"] for c in filtrado] == ["RM"], "so a conta do cliente passa")

print("\n" + ("TODOS OS TESTES PASSARAM" if not falhas else f"{len(falhas)} FALHA(S)"))
sys.exit(1 if falhas else 0)
