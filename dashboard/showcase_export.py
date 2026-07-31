"""Exportar a análise da tela de Showcase do Extrator em Markdown, PDF e JSON.

O front (showcase.html) já tem em mãos o objeto de análise devolvido por
``showcase_extrair`` (o proxy pro pod). Para exportar, ele faz **POST do próprio
payload** pra um destes endpoints e recebe o arquivo pra download:

    POST /dashboard/api/showcase/export/json  → JSON pretty (attachment)
    POST /dashboard/api/showcase/export/md    → Markdown formatado (attachment)
    POST /dashboard/api/showcase/export/pdf   → PDF premium (attachment)

O export é 100% derivado do payload — **sem consulta externa**. A ficha é a
extração on-device do modelo local; o rodapé reafirma isso.

Forma do payload (top-level, forward-compatible — campos podem faltar):
    {versao, label, elapsed_ms, tempos, fichas:[...], docs:[...],
     contexto:{decisoes,varas,datas_chave,desfecho_por_grau}, avisos:[...],
     alvaras_orfaos:[...], arquivo, estagio:{...}}

Cada ``ficha`` (parte): {nome, papel, cpf_cnpj, confianca,
    valor_a_receber:{valor,status,status_rotulo,...}, recebido:[...], saldo,
    espolio, ...}. O bloco ``estagio`` (estágio do crédito + linha do tempo +
    próximos passos) e ``fichas[].valor_a_receber.status`` são adicionados por
    outra frente — o export os inclui SE presentes e não quebra se ausentes.

A lógica de render é PURA (``render_md``/``render_html``/``render_pdf`` recebem o
dict e devolvem str/bytes), separada do request — dá pra gerar exemplos offline.
"""
from __future__ import annotations

import datetime as _dt
import html
import json
import logging
import re
from typing import Any

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

logger = logging.getLogger("voyager.showcase_export")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers puros (sem request)
# ─────────────────────────────────────────────────────────────────────────────

def _esc(s: Any) -> str:
    """Escapa HTML (pro PDF/HTML). None/'' → ''."""
    if s is None:
        return ""
    return html.escape(str(s), quote=True)


def _num(v: Any) -> float | None:
    """Converte valor BRL/num pra float. '1.234,56' → 1234.56. None se não der."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[^\d.,-]", "", str(v))
    if not s:
        return None
    # separador de milhar '.' seguido de 3 dígitos → remove; ',' decimal → '.'
    s = re.sub(r"\.(?=\d{3}\b)", "", s).replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _money(v: Any) -> str:
    """Float/num → 'R$ 1.234,56' (pt-BR). Sem valor → '—'."""
    n = _num(v)
    if n is None:
        return "—"
    s = f"{n:,.2f}"  # 1,234.56
    s = s.replace(",", "\x00").replace(".", ",").replace("\x00", ".")
    return f"R$ {s}"


def _get(d: Any, *keys, default=None):
    """Navega dict aninhado com tolerância (qualquer nível ausente → default)."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return cur if cur is not None else default


def _as_list(v: Any) -> list:
    if isinstance(v, list):
        return v
    if v in (None, ""):
        return []
    return [v]


def _partes(payload: dict) -> list[dict]:
    """Fichas que representam uma PARTE (têm nome/papel/valor/recebido/saldo)."""
    fichas = _as_list(payload.get("fichas"))
    out = []
    for f in fichas:
        if not isinstance(f, dict):
            continue
        if f.get("nome") or f.get("papel") or f.get("valor_a_receber") or \
           f.get("valores_a_receber") or f.get("recebido") or f.get("saldo") is not None:
            out.append(f)
    return out


def _valor_receber(p: dict) -> float | None:
    """valor_a_receber={valor} OU valores_a_receber=[{valor},...] (soma)."""
    vr = _get(p, "valor_a_receber", "valor")
    if vr is not None:
        return _num(vr)
    lst = _as_list(p.get("valores_a_receber"))
    total, achou = 0.0, False
    for x in lst:
        n = _num(x.get("valor") if isinstance(x, dict) else x)
        if n is not None:
            total += n
            achou = True
    return total if achou else None


def _recebido_total(p: dict) -> tuple[float | None, int]:
    """recebido = LISTA de pagamentos → (soma, qtd)."""
    lst = _as_list(p.get("recebido"))
    total, achou = 0.0, False
    for x in lst:
        n = _num(x.get("valor") if isinstance(x, dict) else x)
        if n is not None:
            total += n
            achou = True
    return (total if achou else None), len(lst)


def _status_parte(p: dict) -> tuple[str, str]:
    """(rótulo, chave) do status do valor_a_receber. '' se ausente."""
    var = p.get("valor_a_receber") if isinstance(p.get("valor_a_receber"), dict) else {}
    rot = var.get("status_rotulo") or var.get("status") or ""
    key = str(var.get("status") or rot or "").lower()
    return str(rot), key


# cores por status (paleta Voyager) — usado no PDF
_STATUS_COR = {
    "recebido": "#22c55e", "pago": "#22c55e", "quitado": "#22c55e",
    "a_receber": "#f59e0b", "pendente": "#f59e0b", "aguardando": "#f59e0b",
    "parcial": "#38bdf8", "em_pagamento": "#38bdf8",
    "cedido": "#06b6d4", "cessao": "#06b6d4",
    "sem_expedicao": "#a1a1aa", "indefinido": "#a1a1aa", "": "#a1a1aa",
}

_PAPEL_COR = {
    "EXEQUENTE": "#22c55e", "BENEFICIARIO": "#22c55e", "REQUERENTE": "#22c55e",
    "HERDEIRO": "#a855f7", "SUCESSOR": "#a855f7", "ESPOLIO": "#8b5cf6",
    "INVENTARIANTE": "#f59e0b", "CONJUGE": "#ec4899",
    "CESSIONARIO": "#06b6d4", "CEDENTE": "#0ea5e9",
    "ADVOGADO": "#94a3b8", "EXECUTADO": "#ef4444", "FALECIDO": "#9ca3af",
}


def _status_cor(key: str) -> str:
    return _STATUS_COR.get(key, "#a1a1aa")


def _papel_cor(papel: str) -> str:
    return _PAPEL_COR.get(str(papel or "").upper(), "#3b82f6")


def _now_str() -> str:
    return timezone.localtime(timezone.now()).strftime("%d/%m/%Y %H:%M")


def _stamp() -> str:
    """Timestamp curto pra nome de arquivo."""
    return timezone.localtime(timezone.now()).strftime("%Y%m%d-%H%M")


def _slug_arquivo(payload: dict) -> str:
    """'petição inicial.pdf' → 'peticao-inicial'. Sanitizado pra filename."""
    raw = str(payload.get("arquivo") or "analise").strip()
    raw = re.sub(r"\.(pdf|zip|docx?|png|jpe?g)$", "", raw, flags=re.I)
    raw = raw.lower()
    # remove acentos comuns
    for a, b in (("áàâã", "a"), ("éê", "e"), ("í", "i"), ("óôõ", "o"),
                 ("ú", "u"), ("ç", "c")):
        for ch in a:
            raw = raw.replace(ch, b)
    raw = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    return (raw or "analise")[:60]


def base_filename(payload: dict) -> str:
    return f"analise-{_slug_arquivo(payload)}-{_stamp()}"


# ─────────────────────────────────────────────────────────────────────────────
# Extração de blocos derivados (compartilhado entre MD e PDF)
# ─────────────────────────────────────────────────────────────────────────────

def _resumo_credito(payload: dict) -> dict:
    """Sintetiza valor total / natureza / ente / estágio pro cabeçalho do resumo."""
    partes = _partes(payload)
    total = 0.0
    achou = False
    for p in partes:
        vr = _valor_receber(p)
        if vr is not None:
            total += vr
            achou = True

    # natureza / ente vêm dos registros crus dos docs (mesma heurística do front)
    natureza = ente = None
    for d in _as_list(payload.get("docs")):
        for g in _as_list(d.get("registros") if isinstance(d, dict) else None):
            if not isinstance(g, dict):
                continue
            natureza = natureza or g.get("natureza")
            ente = ente or g.get("ente_devedor") or g.get("ente")
    if not ente:
        ex = next((p for p in partes if str(p.get("papel") or "").upper() == "EXECUTADO"), None)
        if ex:
            ente = ex.get("nome")

    est = payload.get("estagio") if isinstance(payload.get("estagio"), dict) else {}
    return {
        "valor_total": total if achou else None,
        "natureza": natureza,
        "ente": ente,
        "estagio_rotulo": est.get("rotulo") or est.get("label") or est.get("estagio"),
        "estagio_descricao": est.get("descricao") or est.get("veredito") or est.get("resumo"),
        "n_partes": len(partes),
    }


def _marcos(estagio: dict) -> list[dict]:
    """Normaliza a linha do tempo → lista de {data, titulo, status}."""
    if not isinstance(estagio, dict):
        return []
    raw = (estagio.get("linha_do_tempo") or estagio.get("timeline")
           or estagio.get("marcos") or [])
    out = []
    for m in _as_list(raw):
        if isinstance(m, dict):
            out.append({
                "data": m.get("data") or m.get("quando") or "",
                "titulo": m.get("titulo") or m.get("marco") or m.get("label")
                          or m.get("evento") or m.get("descricao") or "",
                "status": m.get("status") or m.get("estado") or "",
            })
        elif m:
            out.append({"data": "", "titulo": str(m), "status": ""})
    return out


def _proximos_passos(estagio: dict) -> list[str]:
    if not isinstance(estagio, dict):
        return []
    raw = (estagio.get("proximos_passos") or estagio.get("next_steps")
           or estagio.get("passos") or [])
    out = []
    for s in _as_list(raw):
        if isinstance(s, dict):
            out.append(s.get("titulo") or s.get("label") or s.get("descricao") or str(s))
        elif s:
            out.append(str(s))
    return out


def _docs_lidos(payload: dict) -> list[tuple[str, int]]:
    """Documentos lidos agregados por classe → [(classe, n), ...] desc."""
    counts: dict[str, int] = {}
    for d in _as_list(payload.get("docs")):
        if not isinstance(d, dict):
            continue
        classe = d.get("classe") or d.get("tipo") or d.get("nome") or "documento"
        counts[str(classe)] = counts.get(str(classe), 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


# ─────────────────────────────────────────────────────────────────────────────
# Render: Markdown
# ─────────────────────────────────────────────────────────────────────────────

def render_md(payload: dict) -> str:
    arquivo = payload.get("arquivo") or "—"
    versao = payload.get("versao") or "—"
    label = payload.get("label") or versao
    resumo = _resumo_credito(payload)
    partes = _partes(payload)
    estagio = payload.get("estagio") if isinstance(payload.get("estagio"), dict) else {}

    L: list[str] = []
    L.append(f"# Análise da extração — {arquivo}")
    L.append("")
    L.append(f"- **Arquivo:** {arquivo}")
    L.append(f"- **Modelo:** {label} (`{versao}`)")
    L.append(f"- **Gerado em:** {_now_str()}")
    tempos = payload.get("tempos") or {}
    if isinstance(tempos, dict) and tempos.get("total_s") is not None:
        L.append(f"- **Tempo do modelo:** {tempos.get('total_s')}s"
                 + (f" · {tempos.get('paginas_ocr')} pág. OCR" if tempos.get("paginas_ocr") else ""))
    elif payload.get("elapsed_ms"):
        L.append(f"- **Tempo (round-trip):** {payload['elapsed_ms']} ms")
    L.append("")

    # ── Resumo do crédito ──
    L.append("## Resumo do crédito")
    L.append("")
    L.append("| Campo | Valor |")
    L.append("|---|---|")
    L.append(f"| Valor total a receber | {_money(resumo['valor_total']) if resumo['valor_total'] is not None else '—'} |")
    L.append(f"| Natureza | {resumo['natureza'] or '—'} |")
    L.append(f"| Ente devedor | {resumo['ente'] or '—'} |")
    L.append(f"| Estágio do crédito | {resumo['estagio_rotulo'] or '—'} |")
    L.append(f"| Partes identificadas | {resumo['n_partes']} |")
    L.append("")
    if resumo["estagio_descricao"]:
        L.append(f"> {resumo['estagio_descricao']}")
        L.append("")

    # ── Partes ──
    if partes:
        L.append("## Partes")
        L.append("")
        L.append("| Nome | Papel | Documento | A receber | Status |")
        L.append("|---|---|---|---|---|")
        for p in partes:
            nome = str(p.get("nome") or "—").replace("|", "\\|")
            papel = str(p.get("papel") or "—").replace("|", "\\|")
            doc = str(p.get("cpf_cnpj") or p.get("documento") or "—").replace("|", "\\|")
            vr = _valor_receber(p)
            if vr is None and _get(p, "valor_a_receber", "abstido"):
                vr_txt = "modelo absteve"
            else:
                vr_txt = _money(vr) if vr is not None else "—"
            rot, _ = _status_parte(p)
            L.append(f"| {nome} | {papel} | {doc} | {vr_txt} | {rot or '—'} |")
        L.append("")

    # ── Estágio + Linha do tempo ──
    marcos = _marcos(estagio)
    passos = _proximos_passos(estagio)
    if estagio or marcos or passos:
        L.append("## Estágio do crédito")
        L.append("")
        if resumo["estagio_rotulo"]:
            L.append(f"**{resumo['estagio_rotulo']}**"
                     + (f" — {resumo['estagio_descricao']}" if resumo["estagio_descricao"] else ""))
            L.append("")
        if marcos:
            L.append("### Linha do tempo")
            L.append("")
            for m in marcos:
                data = f"`{m['data']}` " if m["data"] else ""
                st = f" _({m['status']})_" if m["status"] else ""
                L.append(f"- {data}{m['titulo']}{st}")
            L.append("")
        if passos:
            L.append("### Próximos passos")
            L.append("")
            for s in passos:
                L.append(f"- {s}")
            L.append("")

    # ── Documentos lidos ──
    docs = _docs_lidos(payload)
    if docs:
        L.append("## Documentos lidos")
        L.append("")
        for classe, n in docs:
            L.append(f"- {classe} ×{n}")
        L.append("")

    # ── Avisos ──
    avisos = _as_list(payload.get("avisos"))
    if avisos:
        L.append("## Avisos")
        L.append("")
        for a in avisos:
            txt = a.get("texto") or a.get("msg") or a if isinstance(a, dict) else a
            L.append(f"- {txt}")
        L.append("")

    # ── Rodapé ──
    L.append("---")
    L.append("")
    L.append(f"_Gerado on-device pelo modelo {label} (`{versao}`), sem consulta externa._")
    L.append("")
    return "\n".join(L)


# ─────────────────────────────────────────────────────────────────────────────
# Render: HTML (pro PDF) — dark, tipografia fina, premium
# ─────────────────────────────────────────────────────────────────────────────

_PDF_CSS = """
@page { size: A4; margin: 20mm 16mm 18mm 16mm;
  @bottom-center { content: "VOYAGER · Extrator on-device"; font-family: 'DejaVu Sans Mono', monospace;
    font-size: 7pt; color: #52525b; letter-spacing: .12em; }
  @bottom-right { content: counter(page) " / " counter(pages);
    font-family: 'DejaVu Sans Mono', monospace; font-size: 7pt; color: #52525b; } }
* { box-sizing: border-box; }
html { -weasy-hyphens: none; }
body { font-family: 'DejaVu Sans', 'Helvetica', sans-serif; color: #e4e4e7;
  background: #0a0a0f; font-size: 9.5pt; line-height: 1.5; margin: 0; }
.wrap { background: #0a0a0f; }
.kicker { font-family: 'DejaVu Sans Mono', monospace; font-size: 7.5pt;
  letter-spacing: .28em; text-transform: uppercase; color: #f97316; margin: 0 0 4pt; }
h1 { font-size: 20pt; font-weight: 600; margin: 0 0 3pt; color: #fafafa; letter-spacing: -.01em; }
.sub { color: #a1a1aa; font-size: 9pt; margin: 0 0 14pt; }
.meta { font-family: 'DejaVu Sans Mono', monospace; font-size: 7.8pt; color: #71717a;
  border-top: .5pt solid #27272a; border-bottom: .5pt solid #27272a; padding: 6pt 0; margin: 0 0 16pt;
  display: flex; gap: 18pt; flex-wrap: wrap; }
.meta b { color: #d4d4d8; font-weight: 600; }
h2 { font-size: 11.5pt; font-weight: 600; color: #fafafa; margin: 18pt 0 8pt;
  padding-bottom: 3pt; border-bottom: .5pt solid #27272a; letter-spacing: -.005em; }
h3 { font-size: 9.5pt; font-weight: 600; color: #d4d4d8; margin: 12pt 0 6pt; }
/* cartões de resumo */
.cards { display: flex; gap: 8pt; flex-wrap: wrap; margin: 4pt 0 6pt; }
.card { flex: 1 1 30%; min-width: 120pt; background: #131318; border: .5pt solid #27272a;
  border-radius: 5pt; padding: 8pt 10pt; }
.card .k { font-family: 'DejaVu Sans Mono', monospace; font-size: 6.8pt; letter-spacing: .16em;
  text-transform: uppercase; color: #71717a; margin: 0 0 3pt; }
.card .v { font-size: 13pt; font-weight: 600; color: #fafafa; }
.card .v.small { font-size: 10pt; }
.verdict { background: #131318; border-left: 2.5pt solid #f97316; border-radius: 3pt;
  padding: 8pt 11pt; margin: 8pt 0 2pt; color: #d4d4d8; font-size: 9pt; }
/* tabela de partes */
table { width: 100%; border-collapse: collapse; margin: 4pt 0 6pt; font-size: 8.6pt; }
thead th { text-align: left; font-family: 'DejaVu Sans Mono', monospace; font-size: 6.8pt;
  letter-spacing: .14em; text-transform: uppercase; color: #71717a; font-weight: 400;
  padding: 5pt 6pt; border-bottom: .5pt solid #3f3f46; }
tbody td { padding: 6pt 6pt; border-bottom: .5pt solid #1c1c22; vertical-align: top; }
tbody tr:nth-child(even) td { background: #101015; }
td.nome { color: #fafafa; font-weight: 600; }
td.doc { font-family: 'DejaVu Sans Mono', monospace; font-size: 7.6pt; color: #a1a1aa; }
td.val { font-family: 'DejaVu Sans Mono', monospace; font-weight: 600; color: #e4e4e7; text-align: right; white-space: nowrap; }
td.val.abst { color: #71717a; font-weight: 400; font-style: italic; }
.pill { display: inline-block; padding: 1.5pt 6pt; border-radius: 8pt; font-size: 7pt;
  font-weight: 600; letter-spacing: .04em; }
.rolechip { display: inline-block; padding: 1pt 5pt; border-radius: 3pt; font-size: 7pt;
  font-weight: 600; letter-spacing: .03em; }
/* linha do tempo */
.timeline { margin: 4pt 0 4pt; padding: 0; list-style: none; }
.timeline li { position: relative; padding: 0 0 8pt 16pt; border-left: 1pt solid #27272a; margin-left: 3pt; }
.timeline li:last-child { border-left: 1pt solid transparent; }
.timeline .dot { position: absolute; left: -3.5pt; top: 1.5pt; width: 6pt; height: 6pt;
  border-radius: 50%; background: #f97316; border: 1pt solid #0a0a0f; }
.timeline .t-data { font-family: 'DejaVu Sans Mono', monospace; font-size: 7.4pt; color: #71717a; }
.timeline .t-tit { color: #e4e4e7; font-size: 8.8pt; }
.timeline .t-st { font-size: 7pt; }
/* listas */
ul.plain { margin: 2pt 0 4pt; padding-left: 14pt; }
ul.plain li { margin: 2pt 0; color: #d4d4d8; font-size: 8.8pt; }
.docs { display: flex; gap: 5pt; flex-wrap: wrap; margin: 4pt 0; }
.docchip { background: #131318; border: .5pt solid #27272a; border-radius: 3pt; padding: 3pt 7pt;
  font-size: 7.8pt; color: #d4d4d8; }
.docchip b { color: #f97316; font-family: 'DejaVu Sans Mono', monospace; }
.avisos { background: #17130c; border: .5pt solid #422006; border-radius: 4pt; padding: 6pt 10pt; margin: 4pt 0; }
.avisos li { color: #fbbf24; font-size: 8.4pt; }
.footer { margin-top: 20pt; padding-top: 8pt; border-top: .5pt solid #27272a;
  font-family: 'DejaVu Sans Mono', monospace; font-size: 7.2pt; color: #52525b; letter-spacing: .04em; }
.empty { color: #71717a; font-size: 8.6pt; font-style: italic; }
"""


def render_html(payload: dict) -> str:
    """HTML autocontido (CSS inline) pra alimentar o WeasyPrint. Tudo escapado."""
    arquivo = _esc(payload.get("arquivo") or "—")
    versao = _esc(payload.get("versao") or "—")
    label = _esc(payload.get("label") or payload.get("versao") or "—")
    resumo = _resumo_credito(payload)
    partes = _partes(payload)
    estagio = payload.get("estagio") if isinstance(payload.get("estagio"), dict) else {}

    tempos = payload.get("tempos") or {}
    if isinstance(tempos, dict) and tempos.get("total_s") is not None:
        tempo_txt = f"{_esc(tempos.get('total_s'))}s modelo"
        if tempos.get("paginas_ocr"):
            tempo_txt += f" · {_esc(tempos.get('paginas_ocr'))} pág. OCR"
    elif payload.get("elapsed_ms"):
        tempo_txt = f"{_esc(payload['elapsed_ms'])} ms round-trip"
    else:
        tempo_txt = "—"

    P: list[str] = []
    P.append('<!doctype html><html lang="pt-br"><head><meta charset="utf-8">')
    P.append(f"<style>{_PDF_CSS}</style></head><body><div class='wrap'>")

    # cabeçalho
    P.append("<p class='kicker'>Voyager · Extrator on-device</p>")
    P.append(f"<h1>Análise da extração</h1>")
    P.append(f"<p class='sub'>{arquivo}</p>")
    P.append("<div class='meta'>")
    P.append(f"<span>Modelo <b>{label}</b> ({versao})</span>")
    P.append(f"<span>Gerado em <b>{_esc(_now_str())}</b></span>")
    P.append(f"<span>{tempo_txt}</span>")
    P.append("</div>")

    # resumo do crédito
    P.append("<h2>Resumo do crédito</h2>")
    P.append("<div class='cards'>")
    vt = _money(resumo["valor_total"]) if resumo["valor_total"] is not None else "—"
    P.append(f"<div class='card'><p class='k'>Valor total a receber</p><div class='v'>{_esc(vt)}</div></div>")
    P.append(f"<div class='card'><p class='k'>Natureza</p><div class='v small'>{_esc(resumo['natureza'] or '—')}</div></div>")
    P.append(f"<div class='card'><p class='k'>Estágio</p><div class='v small'>{_esc(resumo['estagio_rotulo'] or '—')}</div></div>")
    P.append("</div>")
    P.append("<div class='cards'>")
    P.append(f"<div class='card'><p class='k'>Ente devedor</p><div class='v small'>{_esc(resumo['ente'] or '—')}</div></div>")
    P.append(f"<div class='card'><p class='k'>Partes identificadas</p><div class='v'>{resumo['n_partes']}</div></div>")
    P.append("</div>")
    if resumo["estagio_descricao"]:
        P.append(f"<div class='verdict'>{_esc(resumo['estagio_descricao'])}</div>")

    # partes
    if partes:
        P.append("<h2>Partes</h2>")
        P.append("<table><thead><tr><th>Nome</th><th>Papel</th><th>Documento</th>"
                 "<th style='text-align:right'>A receber</th><th>Status</th></tr></thead><tbody>")
        for p in partes:
            nome = _esc(p.get("nome") or "—")
            papel = p.get("papel") or ""
            papel_html = (f"<span class='rolechip' style='color:{_papel_cor(papel)};"
                          f"background:{_papel_cor(papel)}22'>{_esc(papel)}</span>") if papel else "—"
            doc = _esc(p.get("cpf_cnpj") or p.get("documento") or "—")
            vr = _valor_receber(p)
            if vr is None and _get(p, "valor_a_receber", "abstido"):
                val_html = "<td class='val abst'>modelo absteve</td>"
            else:
                val_html = f"<td class='val'>{_esc(_money(vr) if vr is not None else '—')}</td>"
            rot, key = _status_parte(p)
            if rot:
                cor = _status_cor(key)
                st_html = f"<span class='pill' style='color:{cor};background:{cor}22'>{_esc(rot)}</span>"
            else:
                st_html = "<span class='empty'>—</span>"
            P.append(f"<tr><td class='nome'>{nome}</td><td>{papel_html}</td>"
                     f"<td class='doc'>{doc}</td>{val_html}<td>{st_html}</td></tr>")
        P.append("</tbody></table>")

    # estágio + linha do tempo + próximos passos
    marcos = _marcos(estagio)
    passos = _proximos_passos(estagio)
    if estagio or marcos or passos:
        P.append("<h2>Estágio do crédito</h2>")
        if resumo["estagio_rotulo"]:
            desc = f" — {_esc(resumo['estagio_descricao'])}" if resumo["estagio_descricao"] else ""
            P.append(f"<div class='verdict'><b>{_esc(resumo['estagio_rotulo'])}</b>{desc}</div>")
        if marcos:
            P.append("<h3>Linha do tempo</h3><ul class='timeline'>")
            for m in marcos:
                data = f"<span class='t-data'>{_esc(m['data'])}</span> " if m["data"] else ""
                key = str(m["status"]).lower()
                st = (f" <span class='t-st' style='color:{_status_cor(key)}'>· {_esc(m['status'])}</span>"
                      if m["status"] else "")
                P.append(f"<li><span class='dot'></span>{data}"
                         f"<span class='t-tit'>{_esc(m['titulo'])}</span>{st}</li>")
            P.append("</ul>")
        if passos:
            P.append("<h3>Próximos passos</h3><ul class='plain'>")
            for s in passos:
                P.append(f"<li>{_esc(s)}</li>")
            P.append("</ul>")

    # documentos lidos
    docs = _docs_lidos(payload)
    if docs:
        P.append("<h2>Documentos lidos</h2><div class='docs'>")
        for classe, n in docs:
            P.append(f"<span class='docchip'>{_esc(classe)} <b>×{n}</b></span>")
        P.append("</div>")

    # avisos
    avisos = _as_list(payload.get("avisos"))
    if avisos:
        P.append("<h2>Avisos</h2><div class='avisos'><ul class='plain'>")
        for a in avisos:
            txt = (a.get("texto") or a.get("msg") or "") if isinstance(a, dict) else a
            P.append(f"<li>{_esc(txt)}</li>")
        P.append("</ul></div>")

    # rodapé
    P.append(f"<div class='footer'>Gerado on-device pelo modelo {label} ({versao}), "
             f"sem consulta externa. · {_esc(_now_str())}</div>")
    P.append("</div></body></html>")
    return "".join(P)


def render_pdf(payload: dict) -> bytes:
    """HTML → PDF via WeasyPrint. Levanta RuntimeError se a lib não está disponível."""
    try:
        from weasyprint import HTML
    except Exception as e:  # noqa: BLE001 — ImportError ou falha de libs nativas
        raise RuntimeError(f"WeasyPrint indisponível: {e}") from e
    return HTML(string=render_html(payload)).write_pdf()


# ─────────────────────────────────────────────────────────────────────────────
# Views (POST do payload → arquivo pra download)
# ─────────────────────────────────────────────────────────────────────────────

def _payload_do_request(request) -> dict | None:
    try:
        data = json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _attach(resp: HttpResponse, filename: str) -> HttpResponse:
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


@csrf_exempt
@login_required
@require_POST
def export_json(request):
    payload = _payload_do_request(request)
    if payload is None:
        return JsonResponse({"erro": "envie o JSON da análise no corpo (POST)"}, status=400)
    body = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    resp = HttpResponse(body, content_type="application/json; charset=utf-8")
    return _attach(resp, f"{base_filename(payload)}.json")


@csrf_exempt
@login_required
@require_POST
def export_md(request):
    payload = _payload_do_request(request)
    if payload is None:
        return JsonResponse({"erro": "envie o JSON da análise no corpo (POST)"}, status=400)
    try:
        body = render_md(payload)
    except Exception:  # noqa: BLE001 — um render nunca deve dar 500 opaco
        logger.exception("render_md falhou")
        return JsonResponse({"erro": "falha ao gerar o Markdown"}, status=500)
    resp = HttpResponse(body, content_type="text/markdown; charset=utf-8")
    return _attach(resp, f"{base_filename(payload)}.md")


@csrf_exempt
@login_required
@require_POST
def export_pdf(request):
    payload = _payload_do_request(request)
    if payload is None:
        return JsonResponse({"erro": "envie o JSON da análise no corpo (POST)"}, status=400)
    try:
        pdf = render_pdf(payload)
    except RuntimeError as e:
        logger.error("render_pdf indisponível: %s", e)
        return JsonResponse({"erro": "geração de PDF indisponível no servidor",
                             "detalhe": str(e)[:160]}, status=501)
    except Exception:  # noqa: BLE001
        logger.exception("render_pdf falhou")
        return JsonResponse({"erro": "falha ao gerar o PDF"}, status=500)
    resp = HttpResponse(pdf, content_type="application/pdf")
    return _attach(resp, f"{base_filename(payload)}.pdf")
