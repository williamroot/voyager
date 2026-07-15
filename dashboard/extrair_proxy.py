"""Proxy fino da tela de extração de autos (Zordon) para dentro do Voyager.

A UI e o pipeline vivem no Zordon (GPU, modelos, doc_classificador). O navegador
do usuário nunca fala direto com o Zordon — todas as telas passam por aqui. Os
paths são espelhados 1:1 (``/extrair``, ``/api/extrair/...``) para que os links
absolutos do HTML servido pelo Zordon resolvam na origem do Voyager.

Somente autenticado. POST é ``csrf_exempt`` porque o form vem do Zordon (sem token
Django) — a proteção efetiva é o ``login_required`` + rede interna ao Zordon.
"""
from __future__ import annotations

import html

import requests
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.views.decorators.csrf import csrf_exempt

_ESTAGIO = {
    "PRECATORIO": ("Precatório expedido", "#22c55e"),
    "PRE_PRECATORIO": ("Pré-precatório (aguardando ofício)", "#f59e0b"),
    "DIREITO_CREDITORIO": ("Direito creditório (em formação)", "#3b82f6"),
}


def _base() -> str:
    return getattr(settings, "ZORDON_URL", "").rstrip("/")


def _resp(r: requests.Response, default_ct: str) -> HttpResponse:
    return HttpResponse(r.content, status=r.status_code,
                        content_type=r.headers.get("content-type", default_ct))


def _sem_zordon() -> HttpResponse:
    return HttpResponse("<h2>Extração indisponível</h2><p>ZORDON_URL não configurado.</p>",
                        status=503, content_type="text/html; charset=utf-8")


@csrf_exempt
@login_required
def extrair(request):
    base = _base()
    if not base:
        return _sem_zordon()
    if request.method == "POST" and request.FILES.get("arquivo"):
        up = request.FILES["arquivo"]
        files = {"arquivo": (up.name, up.file, up.content_type or "application/octet-stream")}
        data = {k: v for k, v in request.POST.items()}
        try:
            r = requests.post(f"{base}/extrair", data=data, files=files,
                              timeout=1200, allow_redirects=False)
        except requests.RequestException:
            return HttpResponse("<h2>Falha ao enviar ao Zordon</h2>", status=502)
        if r.status_code in (301, 302, 303):
            return HttpResponseRedirect(r.headers.get("location", "/extrair"))
        return _resp(r, "text/html; charset=utf-8")
    try:
        r = requests.get(f"{base}/extrair", timeout=30)
    except requests.RequestException:
        return _sem_zordon()
    return _resp(r, "text/html; charset=utf-8")


@login_required
def status(request, job_id):
    base = _base()
    if not base:
        return _sem_zordon()
    try:
        r = requests.get(f"{base}/extrair/{job_id}", timeout=30)
    except requests.RequestException:
        return _sem_zordon()
    return _resp(r, "text/html; charset=utf-8")


@login_required
def api_status(request, job_id):
    try:
        r = requests.get(f"{_base()}/api/extrair/{job_id}", timeout=30)
    except requests.RequestException:
        return HttpResponse('{"erro":"zordon_off"}', status=502, content_type="application/json")
    return _resp(r, "application/json")


@login_required
def api_modelos(request):
    try:
        r = requests.get(f"{_base()}/api/extrair/modelos", timeout=30)
    except requests.RequestException:
        return HttpResponse('{"local":[],"cloud":[]}', status=200, content_type="application/json")
    return _resp(r, "application/json")


@login_required
def arquivo(request, job_id):
    """Stream do arquivo original (ZIP/PDF) que veio do Zordon."""
    try:
        r = requests.get(f"{_base()}/extrair/{job_id}/arquivo", timeout=300, stream=True)
    except requests.RequestException:
        return HttpResponse("arquivo indisponível", status=502)
    resp = HttpResponse(r.raw.read() if r.status_code == 200 else r.content,
                        status=r.status_code,
                        content_type=r.headers.get("content-type", "application/octet-stream"))
    if r.headers.get("content-disposition"):
        resp["Content-Disposition"] = r.headers["content-disposition"]
    return resp


@csrf_exempt
@login_required
def chat(request, job_id):
    """Proxy SSE do chat: repassa o POST e faz STREAM do text/event-stream do Zordon
    token-a-token (sem bufferizar) → o navegador recebe ao vivo."""
    from django.http import StreamingHttpResponse
    base = _base()
    if not base:
        return HttpResponse("indisponível", status=503)

    def gen():
        try:
            with requests.post(f"{base}/extrair/{job_id}/chat", data=request.body,
                               headers={"Content-Type": "application/json"},
                               stream=True, timeout=300) as r:
                for chunk in r.iter_content(chunk_size=None):
                    if chunk:
                        yield chunk
        except requests.RequestException:
            yield b'data: {"tipo": "erro", "erro": "conexao"}\n\n'
    resp = StreamingHttpResponse(gen(), content_type="text/event-stream")
    resp["Cache-Control"] = "no-cache"
    resp["X-Accel-Buffering"] = "no"
    return resp


@login_required
def jurimetria(request, job_id):
    """Projeção jurimétrica do processo: lê o estágio+features extraídos (Zordon) e,
    se ainda NÃO é precatório, estima via modelo de sobrevivência DC→precatório
    (KM estratificado, `survival_precatorio.prever`) quando/se sai o ofício."""
    try:
        r = requests.get(f"{_base()}/api/extrair/{job_id}", timeout=30).json()
    except (requests.RequestException, ValueError):
        return JsonResponse({"erro": "zordon_off"}, status=502)
    estagio = r.get("estagio")
    jm = r.get("jm") or {}
    lab, cor = _ESTAGIO.get(estagio, (estagio or "—", "#64748b"))
    est = None
    if estagio and estagio != "PRECATORIO":
        try:
            from dashboard.survival_precatorio import prever
            est = prever(jm.get("tribunal"), jm.get("natureza"), jm.get("ente_devedor"))
        except Exception:  # noqa: BLE001 — serving indisponível não quebra a tela
            est = None
    return JsonResponse({"estagio": estagio, "estagio_label": lab, "estagio_cor": cor,
                         "features": jm, "estimativa": est})


@csrf_exempt
@login_required
def reprocessar(request, job_id):
    try:
        r = requests.post(f"{_base()}/extrair/{job_id}/reprocessar", timeout=30, allow_redirects=False)
    except requests.RequestException:
        return HttpResponseRedirect(f"/extrair/{job_id}")
    return HttpResponseRedirect(r.headers.get("location", f"/extrair/{job_id}"))


# ═══════════════════════════ DOSSIÊ EXPORTÁVEL (jurimetria + achados) ═══════════════════════════
# Página standalone, print-otimizada (window.print → "Salvar como PDF", mesmo padrão do dossiê de
# jurimetria). Junta os achados ESTRUTURADOS do Zordon (com proveniência) + a projeção jurimétrica
# (survival_precatorio.prever, que só existe aqui no Voyager).
_esc = html.escape
_FLABEL = {
    "valor_total": "Valor total requisitado", "valor_principal": "Principal",
    "valor_liquido": "Valor líquido", "valor_juros_mora": "Juros de mora",
    "valor_juros_compensatorio": "Juros compensatórios", "honorarios": "Honorários advocatícios",
    "pss": "PSS / previdência", "rra": "RRA / IR", "ente_devedor": "Ente devedor",
    "beneficiario": "Beneficiário", "numero_precatorio": "Nº do precatório",
    "exercicio": "Exercício", "data_oficio": "Data do ofício", "tribunal": "Tribunal",
    "classe": "Classe processual", "natureza": "Natureza do crédito",
}
_VALOR_KEYS = ("valor_total", "valor_principal", "valor_liquido", "valor_juros_mora",
               "valor_juros_compensatorio", "honorarios", "pss", "rra")
_ID_KEYS = ("beneficiario", "tribunal", "ente_devedor", "numero_precatorio",
            "exercicio", "data_oficio", "classe")
_METODO = {"regra": "regra determinística", "llm": "leitura por IA", "join": "cruzamento",
           "agreg": "agregação", "doc_classe+llm": "documento roteado + IA", "tpu": "tabela TPU"}
_CONF_COR = {"alta": "#16a34a", "media": "#d97706", "baixa": "#9ca3af"}
_TL_LAB = {
    "DISTRIBUICAO": "Distribuição", "SENTENCA": "Sentença", "ACORDAO": "Acórdão",
    "DECISAO": "Decisão", "TRANSITO_JULGADO": "Trânsito em julgado",
    "HOMOLOGACAO_CALCULOS": "Homologação de cálculos", "OFICIO_REQUISITORIO": "Ofício requisitório",
    "PRECATORIO_EXPEDIDO": "Precatório expedido", "RPV_EXPEDIDA": "RPV expedida",
    "PAGAMENTO": "Pagamento", "PENHORA": "Penhora", "FALECIMENTO": "Falecimento",
    "HABILITACAO_HERDEIROS": "Habilitação de herdeiros", "CESSAO_CREDITO": "Cessão de crédito",
    "EXPEDICAO_OFICIO": "Expedição de ofício", "PETICAO": "Petição", "OUTRO": "Evento",
}


def _moeda(v):
    try:
        return "R$ " + f"{float(v):,.2f}".replace(",", "·").replace(".", ",").replace("·", ".")
    except (TypeError, ValueError):
        return "—"


def _dbr(s):
    s = str(s or "")
    return f"{s[8:10]}/{s[5:7]}/{s[:4]}" if len(s) >= 10 else (s or "—")


def _prov(mf):
    """Pílula de proveniência de um MetaField: método + confiança (o 'detalhe do achado')."""
    met = _METODO.get(mf.get("metodo"), mf.get("metodo") or "—")
    conf = mf.get("confianca")
    cor = _CONF_COR.get(conf, "#9ca3af")
    cbadge = (f"<span class='conf' style='color:{cor};border-color:{cor}55;background:{cor}14'>"
              f"{_esc(conf)}</span>") if conf else ""
    return f"<span class='met'>{_esc(met)}</span>{cbadge}"


def _bloco_jurimetria(estagio, est):
    """A PEÇA CENTRAL: a projeção jurimétrica explicada."""
    if estagio == "PRECATORIO":
        return ("<div class='jm-done'>📌 <b>Ofício requisitório já expedido.</b> O crédito já é "
                "precatório e está na fila de pagamento do ente devedor — não há projeção de "
                "conversão a estimar. A próxima etapa é o pagamento (ordem cronológica / acordo).</div>")
    if not est:
        return ("<div class='jm-none'>Sem base estatística suficiente para projetar a conversão "
                "deste estrato (natureza/ente). Estimativa omitida para não induzir a erro.</div>")
    def bar(lab, v):
        if v is None:
            return ""
        return (f"<div class='jbar'><span class='jbl'>{lab}</span>"
                f"<div class='jtrack'><i style='width:{min(v, 100)}%'></i></div>"
                f"<b>{v}%</b></div>")
    tm = est.get("tempo_mediano_meses")
    n_br = f"{est.get('n') or 0:,}".replace(",", ".")
    ev_br = f"{est.get('eventos') or 0:,}".replace(",", ".")
    chances = "".join(bar(f"em {h} meses", est.get(f"chance_{h}m"))
                      for h in (12, 24, 36, 60) if est.get(f"chance_{h}m") is not None)
    return f"""<div class='jm-card'>
      <div class='jm-big'>~{tm if tm is not None else '—'} <span>meses</span></div>
      <div class='jm-cap'>tempo mediano estimado até a expedição do ofício requisitório (virar precatório)</div>
      <div class='jbars'>{chances}</div>
      <div class='jm-base'>Base histórica: <b>{n_br}</b> casos comparáveis · <b>{ev_br}</b> viraram precatório · estrato <span class=mono>{_esc(est.get('estrato') or '—')}</span></div>
    </div>
    <div class='method'><b>Como estimamos:</b> modelo de sobrevivência (Kaplan-Meier) estratificado
    por <i>tipo de ente</i> (federal/estadual/municipal) &times; <i>natureza do crédito</i>, treinado sobre o
    histórico de processos que percorreram o caminho direito creditório &rarr; precatório. As probabilidades
    são a chance acumulada de o ofício sair dentro de cada janela. É uma estimativa estatística
    populacional — não é aconselhamento jurídico nem garantia sobre este processo.</div>"""


def _dossie_html(d, est) -> str:
    campos = d.get("campos") or {}

    def mf(k):
        return campos.get(k) or {}

    def val(k):
        m = mf(k)
        return None if m.get("abstido") else m.get("valor")

    nat = val("natureza")
    est_lab, est_cor = _ESTAGIO.get(d.get("estagio"), (d.get("estagio_label") or "—", "#64748b"))
    ces = d.get("cessao") or {}
    # badges
    badges = [f"<span class='bdg nat'>{_esc(nat or 'natureza não identificada')}</span>",
              f"<span class='bdg' style='color:{est_cor};border-color:{est_cor}66;background:{est_cor}14'>{_esc(est_lab)}</span>"]
    if ces.get("presente"):
        badges.append("<span class='bdg' style='color:#ca8a04;border-color:#ca8a0466;background:#eab30814'>⇄ Cessão de crédito</span>")
    vt = val("valor_total") or val("valor_principal")

    # composição do valor (com proveniência)
    vrows = ""
    for k in _VALOR_KEYS:
        if val(k) is not None:
            vrows += (f"<tr><td>{_FLABEL.get(k, k)}</td><td class='num'>{_moeda(val(k))}</td>"
                      f"<td class='pv'>{_prov(mf(k))}</td></tr>")
    ded = mf("deducoes")
    if isinstance(ded, dict) and not ded.get("abstido") and isinstance(ded.get("valor"), list):
        for dd in ded["valor"]:
            vrows += (f"<tr><td class='sub'>↳ {_esc(str(dd.get('nome', '')))}</td>"
                      f"<td class='num'>{_moeda(dd.get('valor'))}</td><td class='pv'>dedução declarada</td></tr>")
    val_html = (f"<section><h2>Composição do valor</h2><table class='t'>"
                f"<thead><tr><th>Rubrica</th><th class='num'>Valor</th><th>Como foi obtido</th></tr></thead>"
                f"<tbody>{vrows}</tbody></table></section>") if vrows else ""

    # identificação
    idrows = "".join(f"<tr><td>{_FLABEL.get(k, k)}</td><td>{_esc(str(val(k)))}</td><td class='pv'>{_prov(mf(k))}</td></tr>"
                     for k in _ID_KEYS if val(k) is not None)
    id_html = (f"<section><h2>Identificação</h2><table class='t'><tbody>{idrows}</tbody></table></section>") if idrows else ""

    # partes
    partes = d.get("partes") or []
    prows = "".join(
        f"<tr><td><span class='papel'>{_esc(p.get('papel', '—'))}</span></td>"
        f"<td>{_esc(p.get('nome', ''))}</td>"
        f"<td class='mono'>{_esc(p.get('cpf_cnpj') or '')}{(' · OAB ' + _esc(p['oab'])) if p.get('oab') else ''}</td></tr>"
        for p in partes)
    partes_html = (f"<section><h2>Partes <span class=cnt>{len(partes)}</span></h2><table class='t'>"
                   f"<thead><tr><th>Papel</th><th>Nome</th><th>Documento</th></tr></thead>"
                   f"<tbody>{prows}</tbody></table></section>") if partes else ""

    # cessão — achado + justificativa
    ces_html = ""
    if ces.get("presente"):
        flux = ""
        if ces.get("cedente") or ces.get("cessionario"):
            flux = (f"<div class='ces-flux'><span>{_esc(ces.get('cedente') or 'cedente não identificado')}</span>"
                    f"<span class='arr'>⇄</span><span class='cess'>{_esc(ces.get('cessionario') or 'cessionário não identificado')}</span></div>")
        meta = " · ".join(x for x in [_dbr(ces['data']) if ces.get('data') else None,
                                      _esc(ces['instrumento']) if ces.get('instrumento') else None,
                                      f"confiança {_esc(ces.get('confianca', '—'))}"] if x)
        evs = "".join(f"<div class='ev'>“{_esc(e.get('trecho', ''))}”<div class='src mono'>{_esc(e.get('documento', ''))}</div></div>"
                      for e in (ces.get("evidencias") or []))
        ces_html = (f"<section><h2>Cessão de crédito <span class=cnt style='background:#eab30822;color:#ca8a04'>indício</span></h2>"
                    f"<div class='ces'>{flux}"
                    f"{('<div class=cesmeta>' + meta + '</div>') if meta else ''}"
                    f"<div class='ceslbl'>Por que identificamos — trechos dos autos:</div>{evs}</div></section>")

    # linha do tempo
    tl = d.get("linha_tempo") or []
    tl_rows = "".join(
        f"<tr><td class='mono'>{_dbr(e.get('data'))}</td><td><b>{_TL_LAB.get(e.get('tipo'), e.get('tipo') or '—')}</b>"
        f"{(' — ' + _esc(e['titulo'])) if e.get('titulo') else ''}</td>"
        f"<td class='mono src'>{_esc(e.get('documento') or '')}</td></tr>" for e in tl)
    tl_html = (f"<section><h2>Linha do tempo <span class=cnt>{len(tl)}</span></h2><table class='t'>"
               f"<tbody>{tl_rows}</tbody></table></section>") if tl else ""

    # eventos do ciclo de vida
    evs = d.get("eventos") or []
    ev_rows = "".join(f"<tr><td><b>{_TL_LAB.get(e.get('tipo'), e.get('tipo') or '—')}</b></td>"
                      f"<td class='mono'>{_dbr(e.get('data')) if e.get('data') else '—'}</td>"
                      f"<td>{_esc(e.get('parte') or '')}</td></tr>" for e in evs)
    ev_html = (f"<section><h2>Eventos do ciclo de vida do crédito <span class=cnt>{len(evs)}</span></h2>"
               f"<table class='t'><thead><tr><th>Evento</th><th>Data</th><th>Parte</th></tr></thead>"
               f"<tbody>{ev_rows}</tbody></table></section>") if evs else ""

    # abstenções (transparência: o que a IA NÃO cravou)
    abst = [(k, mf(k)) for k in campos if isinstance(campos.get(k), dict) and campos[k].get("abstido")]
    abst_html = ""
    if abst:
        arows = "".join(f"<li><b>{_FLABEL.get(k, k)}</b> — {_esc(m.get('motivo_abstencao') or 'sem base explícita nos autos')}</li>"
                        for k, m in abst)
        abst_html = (f"<section><h2>Campos não afirmados <span class=cnt>{len(abst)}</span></h2>"
                     f"<p class='note'>Por política de zero-erro, a IA se absteve (não “chutou”) nos campos abaixo:</p>"
                     f"<ul class='abst'>{arows}</ul></section>")

    # documentos
    dist = d.get("documentos_por_classe") or {}
    chips = "".join(f"<span class='chip'>{_esc(k.replace('_', ' ').title())} · {v}</span>"
                    for k, v in sorted(dist.items(), key=lambda x: -x[1]))
    docs_html = (f"<section><h2>Documentos analisados <span class=cnt>{d.get('n_documentos') or sum(dist.values())}</span></h2>"
                 f"<div class='chips'>{chips}</div></section>") if chips else ""

    cnj = d.get("cnj") or ""
    sub = " · ".join(x for x in [val("tribunal"), val("ente_devedor")] if x) or (cnj or "autos processuais")
    return f"""<!doctype html><html lang=pt-BR><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Dossiê · {_esc(d.get('nome') or 'autos')}</title>
<style>{_DOSSIE_CSS}</style></head><body>
<div class='bar no-print'>
  <div>Dossiê de análise processual — foco em jurimetria</div>
  <div><button onclick='window.print()'>📄 Salvar como PDF</button>
  <a href='/extrair/{d.get("id","")}'>← voltar</a></div>
</div>
<main>
  <header class='cover'>
    <div class='brand'><span class='g'></span> VOYAGER · <span class='muted'>jurimetria de precatórios</span></div>
    <h1>Dossiê de Análise Processual</h1>
    <div class='badges'>{''.join(badges)}</div>
    <div class='hero-val'>{_moeda(vt) if vt is not None else '—'}</div>
    <div class='hero-sub'>{_esc(sub)}</div>
    <div class='cover-meta'>{('CNJ ' + _esc(cnj) + ' · ') if cnj else ''}arquivo {_esc(d.get('nome') or '—')} · extraído {_esc(d.get('extraido_em') or '—')} · {_esc(str(d.get('modelo') or ''))}</div>
  </header>

  <section class='jm'><h2>Projeção jurimétrica <span class='cnt'>foco</span></h2>
    {_bloco_jurimetria(d.get('estagio'), est)}
  </section>

  {val_html}
  {ces_html}
  {partes_html}
  {tl_html}
  {ev_html}
  {id_html}
  {abst_html}
  {docs_html}

  <footer>Gerado por Voyager · voyager.was.dev.br — as estimativas jurimétricas são estatísticas
  populacionais (Kaplan-Meier), não constituem aconselhamento jurídico. Os achados de extração
  seguem política de zero-erro (abster &gt; presumir); confira sempre contra os autos originais.</footer>
</main></body></html>"""


_DOSSIE_CSS = """
*{box-sizing:border-box}
body{margin:0;background:#eef1f5;color:#111827;font-family:'Inter',system-ui,-apple-system,Segoe UI,sans-serif;font-size:14px;line-height:1.5}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.bar{position:sticky;top:0;display:flex;justify-content:space-between;align-items:center;gap:1rem;
  padding:.7rem 1.2rem;background:#0f172a;color:#e2e8f0;font-size:.85rem}
.bar button{background:linear-gradient(135deg,#2563eb,#3b82f6);color:#fff;border:0;border-radius:8px;padding:.5rem .9rem;font-weight:600;cursor:pointer;font-size:.85rem}
.bar a{color:#94a3b8;text-decoration:none;margin-left:1rem}
main{max-width:860px;margin:1.5rem auto;background:#fff;padding:2.4rem 2.6rem 3rem;
  box-shadow:0 10px 40px rgba(15,23,42,.12);border-radius:14px}
.cover{border-bottom:2px solid #eef1f5;padding-bottom:1.4rem;margin-bottom:1.6rem}
.brand{display:flex;align-items:center;gap:.5rem;font-weight:800;letter-spacing:.04em;font-size:.82rem;color:#334155}
.brand .g{width:20px;height:20px;border-radius:6px;background:linear-gradient(135deg,#3b82f6,#22d3ee)}
.brand .muted{font-weight:500;color:#94a3b8;letter-spacing:.02em}
h1{font-size:1.8rem;margin:.6rem 0 .8rem}
.badges{display:flex;flex-wrap:wrap;gap:.4rem;margin-bottom:1rem}
.bdg{font-size:.72rem;font-weight:700;padding:.22rem .6rem;border-radius:999px;border:1px solid #cbd5e1;text-transform:uppercase;letter-spacing:.02em}
.bdg.nat{background:#eff6ff;color:#1d4ed8;border-color:#bfdbfe}
.hero-val{font-size:2.4rem;font-weight:800;letter-spacing:-.02em}
.hero-sub{color:#64748b;font-size:.95rem}
.cover-meta{color:#94a3b8;font-size:.78rem;margin-top:.6rem}
section{margin:1.6rem 0;break-inside:avoid}
h2{font-size:1.05rem;margin:0 0 .7rem;padding-bottom:.35rem;border-bottom:1px solid #eef1f5;display:flex;align-items:center;gap:.5rem}
.cnt{font-size:.62rem;font-weight:700;background:#eef1f5;color:#475569;border-radius:999px;padding:.1rem .5rem;text-transform:uppercase}
.jm h2 .cnt{background:#dcfce7;color:#15803d}
.jm-card{background:linear-gradient(135deg,#f0fdf4,#ecfeff);border:1px solid #bbf7d0;border-radius:12px;padding:1.2rem 1.4rem}
.jm-big{font-size:2.6rem;font-weight:800;color:#047857;line-height:1}
.jm-big span{font-size:1.1rem;font-weight:600;color:#059669}
.jm-cap{color:#475569;font-size:.9rem;margin:.3rem 0 1rem}
.jbars{display:flex;flex-direction:column;gap:.5rem;margin:.4rem 0 1rem}
.jbar{display:flex;align-items:center;gap:.7rem;font-size:.85rem}
.jbl{width:8.5rem;color:#475569}
.jtrack{flex:1;height:9px;background:#d1fae5;border-radius:99px;overflow:hidden}
.jtrack i{display:block;height:100%;background:linear-gradient(90deg,#10b981,#059669);border-radius:99px}
.jbar b{width:2.6rem;text-align:right;color:#047857}
.jm-base{font-size:.82rem;color:#64748b;border-top:1px solid #bbf7d0;padding-top:.7rem}
.method{font-size:.82rem;color:#475569;background:#f8fafc;border-left:3px solid #cbd5e1;padding:.7rem .9rem;margin-top:.8rem;border-radius:0 8px 8px 0}
.jm-done{background:#f0fdf4;border:1px solid #bbf7d0;border-radius:10px;padding:1rem 1.2rem;color:#166534}
.jm-none{background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:1rem 1.2rem;color:#64748b}
table.t{width:100%;border-collapse:collapse;font-size:.88rem}
table.t th{text-align:left;font-size:.7rem;text-transform:uppercase;letter-spacing:.03em;color:#94a3b8;font-weight:700;padding:.3rem .5rem;border-bottom:1px solid #eef1f5}
table.t td{padding:.4rem .5rem;border-bottom:1px solid #f4f6f9;vertical-align:top}
table.t td.num{text-align:right;font-variant-numeric:tabular-nums;font-weight:600;white-space:nowrap}
table.t td.sub{color:#64748b;padding-left:1.2rem}
.pv{white-space:nowrap}
.met{font-size:.72rem;color:#64748b}
.conf{font-size:.62rem;font-weight:700;border:1px solid;border-radius:999px;padding:.05rem .4rem;margin-left:.35rem;text-transform:uppercase}
.papel{font-size:.66rem;font-weight:700;background:#f1f5f9;color:#475569;border-radius:6px;padding:.12rem .45rem;text-transform:uppercase}
.ces{border:1px solid #fde68a;background:#fffbeb;border-radius:10px;padding:1rem 1.2rem}
.ces-flux{display:flex;align-items:center;gap:.8rem;font-weight:700;flex-wrap:wrap;margin-bottom:.5rem}
.ces-flux .arr{color:#ca8a04;font-size:1.2rem}.ces-flux .cess{color:#a16207}
.cesmeta{color:#78716c;font-size:.85rem;margin-bottom:.6rem}
.ceslbl{font-size:.72rem;text-transform:uppercase;letter-spacing:.04em;color:#a8a29e;margin-bottom:.3rem}
.ces .ev{border-left:2px solid #fcd34d;padding:.3rem 0 .3rem .7rem;margin-top:.5rem;font-style:italic;color:#57534e}
.ces .src,.src{font-style:normal;font-size:.74rem;color:#a8a29e;margin-top:.15rem}
.note{color:#64748b;font-size:.85rem;margin:.2rem 0 .6rem}
ul.abst{margin:0;padding-left:1.1rem;font-size:.86rem;color:#475569}
ul.abst li{margin:.2rem 0}
.chips{display:flex;flex-wrap:wrap;gap:.4rem}
.chip{font-size:.74rem;background:#f1f5f9;color:#475569;border-radius:7px;padding:.2rem .55rem}
footer{margin-top:2.4rem;padding-top:1rem;border-top:1px solid #eef1f5;font-size:.74rem;color:#94a3b8;line-height:1.5}
@media print{
  @page{margin:14mm}
  body{background:#fff;font-size:12px}
  .no-print{display:none!important}
  main{max-width:100%;margin:0;padding:0;box-shadow:none;border-radius:0}
  section,.jm-card,.ces{break-inside:avoid}
  .jm-card{background:#f0fdf4!important;-webkit-print-color-adjust:exact;print-color-adjust:exact}
  .jtrack i,.bdg,.jtrack,.chip,.papel{-webkit-print-color-adjust:exact;print-color-adjust:exact}
}
"""


@login_required
def dossie(request, job_id):
    """Dossiê exportável (print → PDF) de um upload extraído: junta os achados estruturados do
    Zordon (proveniência de cada campo) com a projeção jurimétrica (DC→precatório) calculada aqui."""
    base = _base()
    if not base:
        return _sem_zordon()
    try:
        d = requests.get(f"{base}/api/extrair/{job_id}/dossie", timeout=30).json()
    except (requests.RequestException, ValueError):
        return HttpResponse("<h2>Dossiê indisponível</h2><p>Não foi possível ler os achados no Zordon.</p>",
                            status=502, content_type="text/html; charset=utf-8")
    if d.get("erro"):
        return HttpResponse(f"<h2>Dossiê indisponível</h2><p>Processo ainda não concluído "
                            f"({d.get('status', d['erro'])}).</p>", status=409,
                            content_type="text/html; charset=utf-8")
    est = None
    if d.get("estagio") and d["estagio"] != "PRECATORIO":
        try:
            from dashboard.survival_precatorio import prever
            jm = d.get("campos") or {}

            def _v(k):
                m = jm.get(k) or {}
                return None if m.get("abstido") else m.get("valor")
            est = prever(_v("tribunal"), _v("natureza"), _v("ente_devedor"))
        except Exception:  # noqa: BLE001
            est = None
    return HttpResponse(_dossie_html(d, est), content_type="text/html; charset=utf-8")
