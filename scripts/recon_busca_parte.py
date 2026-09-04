"""RECON da busca POR PARTE na consulta pública — mede, não presume.

Os 16 enrichers do Voyager sabem uma coisa só: achar processo POR NÚMERO CNJ.
A busca por CPF/CNPJ, nome da parte, OAB ou nome do advogado é outro formulário,
com outros campos e outra página de resultado — e ninguém mediu se ela responde,
tribunal por tribunal. Este script mede.

Ele NÃO é código de produção e não escreve no banco: faz a requisição, salva a
resposta crua como fixture (`tests/fixtures/<sigla>/busca_<criterio>.<ext>`) e
imprime os marcadores que decidem o veredito — captcha, "nenhum registro",
contador de resultados declarado pela fonte, paginação, link de detalhe.

Por que fixture e não só o veredito: parser escrito contra HTML imaginado é a
forma mais barata de produzir um coletor que roda verde e traz metade. O parser
do WS-1/WS-2 nasce lendo o arquivo que este script gravou.

Como rodar (de um container com a rota de rede dos workers, com proxy):

    docker exec -w /app -e SIGLA=TJSP -e CRITERIO=documento \
        -e VALOR=29.979.036/0001-40 -e PROXY=cortex \
        voyager-worker_default-1 python /app/scripts/recon_busca_parte.py

Como rodar de máquina comum (sem Django, sem proxy — direto):

    SIGLA=TJSP CRITERIO=documento VALOR=29.979.036/0001-40 \
        python3 scripts/recon_busca_parte.py

Variáveis:
    SIGLA      TJSP|TJAL|TRF1|TRF3|TRF5|TJMG|TJMA|TJPA|TJMT
    CRITERIO   documento|nome|oab|advogado
    VALOR      o que buscar (CPF/CNPJ com ou sem máscara, nome, OAB "SP123456")
    PROXY      vazio (direto) | cortex | http://user:pass@host:porta
    FIXTURES   diretório raiz das fixtures (default: tests/fixtures)
    TIMEOUT    segundos (default 60)
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup

# ── catálogo das fontes ───────────────────────────────────────────────────────
#
# Os hosts saem dos enrichers que já rodam em produção (`enrichers/esaj.py`,
# `enrichers/trf*.py`, `enrichers/tjm*.py`), não de adivinhação. Duplicar aqui é
# deliberado: o script tem de rodar SEM Django, de qualquer máquina, senão o
# recon só acontece dentro do container e ninguém mede nada de fora.

ESAJ = {
    'TJSP': 'https://esaj.tjsp.jus.br',
    'TJAL': 'https://www2.tjal.jus.br',
}

PJE = {
    # (base, path do módulo de 1º grau)
    # O host do TRF1 é o `-consultapublica`, não o `pje1g` puro: o segundo
    # responde 200, serve um form fPP igualzinho e devolve a tabela VAZIA para
    # qualquer busca (medido com "INSTITUTO NACIONAL DO SEGURO SOCIAL", que
    # está em milhões de processos). Host errado aqui não dá erro — dá zero.
    'TRF1': ('https://pje1g-consultapublica.trf1.jus.br', '/consultapublica'),
    'TRF3': ('https://pje1g.trf3.jus.br', '/pje'),
    # `pje.trf5.jus.br` (sem o `1g`) é o PJe COM LOGIN: responde 200 e serve
    # uma consulta pública ANTIGA, com captcha de imagem e sem o form fPP. O
    # host da consulta pública de 1º grau é o `pje1g` — o mesmo do enricher.
    'TRF5': ('https://pje1g.trf5.jus.br', '/pjeconsulta'),
    'TJMG': ('https://pje-consulta-publica.tjmg.jus.br', '/pje'),
    'TJMA': ('https://pje.tjma.jus.br', '/pje'),
}

REST = {'TJPA', 'TJMT'}

# Campos do form JSF `fPP` por critério, casados por SUFIXO do `name`.
#
# Por que sufixo e não o name inteiro: metade dos ids do PJe é gerada pelo JSF
# (`fPP:j_id186:nomeAdv`) e o número MUDA por instalação — medido em 04/09/2026:
# nomeAdv é `j_id186` no TJMA, `j_id184` no TRF1 e `j_id180` no TRF5. Procurar o
# name literal faz o campo "não existir" no tribunal errado e a busca por
# advogado sumir do catálogo sem nenhum erro. O sufixo estável é o nome do
# componente, e é por ele que se procura.
CAMPOS_PJE = {
    'documento': [':documentoParte'],
    'nome': [':nomeParte'],
    'oab': [':numeroOAB', ':estadoComboOAB'],
    'advogado': [':nomeAdv'],
}

# `cbPesquisa` do e-SAJ por critério. DOCPARTE e NMPARTE são os que o JURISCOPE
# já roda em produção (`datamodel/processors/esajsp.py`); NMADVOGADO e NUMOAB
# estão no mesmo `<select id="cbPesquisa">` (lido da fixture real) e nunca
# foram exercitados — o formulário oferece 8 opções ao todo, as outras três
# (PRECATORIA, DOCDELEG, NUMCDA) não têm coluna correspondente no acervo.
CB_ESAJ = {
    'documento': 'DOCPARTE',
    'nome': 'NMPARTE',
    'oab': 'NUMOAB',
    'advogado': 'NMADVOGADO',
}

UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

HEADERS = {
    'User-Agent': UA,
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'pt-BR,pt;q=0.9,en;q=0.8',
}

# Marcadores procurados em TODA resposta HTML. Um veredito é a combinação
# deles — "0 resultados" com captcha na página não é "não tem processo".
MARCADORES = (
    'captcha', 'recaptcha', 'hcaptcha', 'turnstile', 'awswaf',
    'não existem informações', 'nao existem informacoes',
    'nenhum registro', 'não foram encontrados', 'nao foram encontrados',
    'linkprocesso', 'contadordeprocessos', 'trocarpagina',
    'javax.faces.viewstate', 'detalheprocesso', 'idprocesso',
)


def _env(nome: str, default: str = '') -> str:
    return (os.environ.get(nome) or default).strip()


def _so_digitos(valor: str) -> str:
    return re.sub(r'\D', '', valor or '')


def _sessao() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def _proxies() -> dict:
    """Proxy da rodada. `cortex` só resolve dentro do container (a URL vem do
    settings do Voyager); de fora, use uma URL explícita ou vá direto."""
    p = _env('PROXY')
    if not p:
        return {}
    if p == 'cortex':
        try:
            import django  # noqa: F401
            os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
            django.setup()
            from djen.proxies import cortex_proxy_url
            p = cortex_proxy_url()
        except Exception as exc:  # noqa: BLE001 — recon avisa e segue direto
            print(f'!! PROXY=cortex indisponível fora do container ({exc}); indo direto')
            return {}
    return {'http': p, 'https': p}


def _salvar(sigla: str, criterio: str, conteudo: str, ext: str = 'html') -> str:
    """Grava a fixture. `NOME` sobrescreve o nome do arquivo — o mesmo critério
    tem mais de um DESFECHO que o parser precisa conhecer (achou / não existe /
    "refine sua busca" / bateu no teto da fonte), e um só arquivo por critério
    apagaria o caso anterior a cada rodada."""
    raiz = _env('FIXTURES', 'tests/fixtures')
    pasta = os.path.join(raiz, sigla.lower())
    os.makedirs(pasta, exist_ok=True)
    nome = _env('NOME') or f'busca_{criterio}'
    caminho = os.path.join(pasta, f'{nome}.{ext}')
    with open(caminho, 'w', encoding='utf-8') as fh:
        fh.write(conteudo)
    return caminho


def _marcadores(html: str) -> dict:
    low = (html or '').lower()
    return {m: low.count(m) for m in MARCADORES if low.count(m)}


def _relatar(html: str, caminho: str, extra: dict | None = None) -> None:
    print(f'   fixture: {caminho}  ({len(html)} bytes)')
    marc = _marcadores(html)
    print(f'   marcadores: {marc or "nenhum"}')
    for chave, valor in (extra or {}).items():
        print(f'   {chave}: {valor}')


# ── e-SAJ ─────────────────────────────────────────────────────────────────────

def recon_esaj(sigla: str, criterio: str, valor: str, timeout: int) -> None:
    """`open.do` (JSESSIONID) → `search.do` com `cbPesquisa` do critério.

    O e-SAJ atrela a sessão ao IP: o `open.do` e o `search.do` saem pela MESMA
    sessão e pelo mesmo proxy, como em `enrichers/esaj.py::_fetch_processo`.
    """
    cb = CB_ESAJ.get(criterio)
    if not cb:
        print(f'!! e-SAJ não tem campo para o critério {criterio!r} '
              f'(o formulário oferece {sorted(CB_ESAJ)})')
        return

    base = ESAJ[sigla]
    s, prox = _sessao(), _proxies()

    r0 = s.get(f'{base}/cpopg/open.do', proxies=prox, timeout=timeout)
    print(f'   open.do: HTTP {r0.status_code}  cookies={len(s.cookies)}')

    params = {
        'conversationId': '',
        'cbPesquisa': cb,
        'dadosConsulta.valorConsulta': valor,
        'cdForo': '-1',
    }
    inicio = time.time()
    r = s.get(f'{base}/cpopg/search.do', params=params, proxies=prox,
              timeout=timeout, allow_redirects=True)
    print(f'   search.do: HTTP {r.status_code}  {time.time() - inicio:.1f}s  '
          f'url_final={r.url[:110]}')

    # Página seguinte pela MESMA sessão (o e-SAJ atrela o JSESSIONID ao IP):
    # é o que o coletor vai fazer, então o recon tem de exercitar isso e não
    # supor que funciona.
    pagina = int(_env('PAGINA', '1'))
    if pagina > 1:
        # PACING OBRIGATÓRIO, medido em 04/09/2026 no TJSP: pedir a página
        # seguinte na mesma sessão sem pausa devolve, 3 vezes em 3, a página
        # "Foram identificadas multiplas consultas simultâneas" — 0 resultados
        # e nenhum erro HTTP. Com 1,5 s, 3 em 3 vieram completas. Um coletor
        # sem essa pausa lê a página 1 e conclui que acabou.
        time.sleep(float(_env('PAUSA_S', '2.0')))
        params_pag = dict(params, paginaConsulta=str(pagina))
        inicio = time.time()
        r = s.get(f'{base}/cpopg/trocarPagina.do', params=params_pag, proxies=prox,
                  timeout=timeout, allow_redirects=True)
        print(f'   trocarPagina.do pagina={pagina}: HTTP {r.status_code}  '
              f'{time.time() - inicio:.1f}s')

    caminho = _salvar(sigla, criterio, r.text)
    soup = BeautifulSoup(r.text, 'html.parser')

    # O contador é o TOTAL DECLARADO PELA FONTE. É ele que diz se a paginação
    # terminou ou se paramos no meio — sem ele, "li 250" não se compara a nada.
    contador = soup.select_one('#contadorDeProcessos')
    links = soup.select('a.linkProcesso')
    exemplos = []
    for a in links[:3]:
        qs = parse_qs(urlparse(a.get('href', '')).query)
        exemplos.append({
            'cnj': a.get_text(strip=True),
            'codigo': (qs.get('processo.codigo') or [''])[0],
            'foro': (qs.get('processo.foro') or [''])[0],
        })

    # O e-SAJ tem um QUARTO desfecho, que nenhum enricher conhece hoje: em vez
    # de listar, ele responde "Foram encontrados muitos processos ... refine sua
    # busca" no `#mensagemRetorno`. Lido como lista vazia, isso vira um
    # "esta pessoa não tem processo" mentiroso.
    aviso = soup.select_one('#mensagemRetorno')
    _relatar(r.text, caminho, {
        'contadorDeProcessos': contador.get_text(strip=True) if contador else None,
        'mensagemRetorno': (re.sub(r'\s+', ' ', aviso.get_text(' ', strip=True))[:160]
                            if aviso else None),
        'a.linkProcesso': len(links),
        'exemplos': exemplos,
        'redirecionou_para_detalhe': 'show.do' in r.url,
        'paginas': sorted({int(m) for m in re.findall(r'paginaConsulta=(\d+)', r.text)}),
    })


# ── PJe (form JSF fPP) ────────────────────────────────────────────────────────

def _campo_por_sufixo(soup: BeautifulSoup, sufixo: str) -> str | None:
    """`name` real do campo cujo componente termina em `sufixo`, dentro do fPP."""
    form = soup.find('form', {'id': 'fPP'}) or soup
    for el in form.find_all(['input', 'select', 'textarea']):
        nome = el.get('name') or ''
        if nome.endswith(sufixo):
            return nome
    return None


def _campos_do_form(soup: BeautifulSoup) -> dict:
    """Mesma leitura do `_extract_form_fields` do enricher: o JSF exige que
    todo input do form volte no POST, senão a árvore de componentes não casa."""
    form = soup.find('form', {'id': 'fPP'})
    campos = {}
    if not form:
        return campos
    for inp in form.find_all('input'):
        nome = inp.get('name')
        if nome:
            campos[nome] = inp.get('value', '')
    for sel in form.find_all('select'):
        nome = sel.get('name')
        if not nome:
            continue
        opt = sel.find('option', selected=True) or sel.find('option')
        campos[nome] = opt.get('value', '') if opt else ''
    return campos


def _id_do_botao(soup: BeautifulSoup) -> str | None:
    """Id do botão de pesquisa.

    Quem manda é o `A4J.AJAX.Submit` do `executarPesquisa`: o id que ele passa
    em `parameters` é o componente que o JSF vai executar. No TRF1 esse id é
    `fPP:j_id248`, enquanto o `<input id="fPP:searchProcessos">` visível é
    `type=button` — só dispara o JS. Postar com o `searchProcessos` devolve
    HTTP 200 com uma resposta AJAX que atualiza APENAS a div de mensagens: sem
    tabela, sem erro. Lido como "0 resultados", é falso-negativo puro (medido
    em 04/09/2026 com "INSTITUTO NACIONAL DO SEGURO SOCIAL" no TRF1).

    Mesma heurística do `_find_search_script_id` do enricher que já roda em
    produção — o fallback do input nomeado só existe para instalações onde o
    script não aparece.
    """
    for script in soup.find_all('script'):
        conteudo = script.string or script.get_text() or ''
        if 'executarPesquisa' not in conteudo or 'A4J.AJAX.Submit' not in conteudo:
            continue
        m = re.search(r"'parameters':\s*\{'(fPP:[^']+)'", conteudo)
        if m:
            return m.group(1)
        m = re.search(r"'similarityGroupingId':'(fPP:[^']+)'", conteudo)
        if m:
            return m.group(1)
    return 'fPP:searchProcessos' if soup.find(
        'input', {'name': 'fPP:searchProcessos'}) else None


def _valor_da_uf(soup: BeautifulSoup, name_do_select: str, uf: str) -> str:
    """Value do `<option>` cuja legenda é a UF pedida (ver comentário na chamada)."""
    select = soup.find('select', {'name': name_do_select})
    if not select:
        return uf
    for opt in select.find_all('option'):
        if opt.get_text(strip=True).upper() == uf:
            return opt.get('value', '')
    return uf


def recon_pje(sigla: str, criterio: str, valor: str, timeout: int) -> None:
    campos_criterio = CAMPOS_PJE.get(criterio)
    if not campos_criterio:
        print(f'!! critério {criterio!r} desconhecido para o PJe')
        return

    base, path = PJE[sigla]
    list_url = f'{base}{path}/ConsultaPublica/listView.seam'
    s, prox = _sessao(), _proxies()

    r0 = s.get(list_url, proxies=prox, timeout=timeout)
    print(f'   listView.seam: HTTP {r0.status_code}  ({len(r0.text)} bytes)')
    soup = BeautifulSoup(r0.text, 'html.parser')

    vs = soup.find('input', {'name': 'javax.faces.ViewState'})
    if not vs or not vs.get('value'):
        caminho = _salvar(sigla, f'{criterio}_form', r0.text)
        _relatar(r0.text, caminho, {'veredito': 'sem ViewState — WAF, captcha ou layout novo'})
        return

    # O que o formulário oferece de verdade nesta instalação: se o campo do
    # critério não existe aqui, o tribunal não tem essa busca — e isso é um
    # achado, não um erro.
    achados = {sufixo: _campo_por_sufixo(soup, sufixo) for sufixo in campos_criterio}
    presentes = {s: bool(n) for s, n in achados.items()}
    botao = _id_do_botao(soup)
    print(f'   campos do critério: {presentes}')
    print(f'   botão de pesquisa: {botao}  '
          f'(reCaptcha no nome? {"executarPesquisaReCaptcha" in (r0.text or "")})')
    if not all(presentes.values()):
        caminho = _salvar(sigla, f'{criterio}_form', r0.text)
        _relatar(r0.text, caminho, {'veredito': 'campo do critério ausente no formulário'})
        return

    payload = _campos_do_form(soup)
    if criterio == 'oab':
        numero = re.sub(r'[^0-9]', '', valor)
        uf = (re.sub(r'[^A-Za-z]', '', valor) or sigla[-2:]).upper()[:2]
        payload[achados[':numeroOAB']] = numero
        # A UF fica SEM SELEÇÃO por padrão, e isso não é preguiça — é o que
        # devolve resultado. Medido no TJMG em 04/09/2026 com a OAB MG65417,
        # colhida de um processo real:
        #     UF = MG (índice 12 do combo) -> 0 resultados
        #     UF sem seleção               -> 6 resultados
        # O combo guarda ÍNDICE ("0"=AC, "1"=AL...), e mandar a sigla crua faz
        # o Seam redirecionar para `errorUnexpected.seam`. Preencher o índice
        # certo não dá erro: dá ZERO, que é pior — é falso-negativo mudo.
        # `OAB_UF=1` existe só para reproduzir a medição.
        if _env('OAB_UF') == '1':
            payload[achados[':estadoComboOAB']] = _valor_da_uf(
                soup, achados[':estadoComboOAB'], uf)
    else:
        payload[achados[campos_criterio[0]]] = valor

    payload['fPP'] = 'fPP'
    payload['AJAXREQUEST'] = '_viewRoot'
    payload['javax.faces.ViewState'] = vs['value']
    payload['AJAX:EVENTS_COUNT'] = '1'
    if botao:
        payload[botao] = botao

    time.sleep(0.4)
    r = s.post(list_url, data=payload, proxies=prox, timeout=timeout)
    print(f'   POST pesquisa: HTTP {r.status_code}  ({len(r.text)} bytes)')

    caminho = _salvar(sigla, criterio, r.text)
    res = BeautifulSoup(r.text, 'html.parser')

    # A tabela de resultados do PJe é um rich:dataTable; o id varia por
    # instalação, então listamos TODAS as tabelas com linhas para o parser do
    # WS-2 saber em qual mirar.
    tabelas = []
    for t in res.find_all('table'):
        linhas = t.find_all('tr')
        if len(linhas) > 1:
            tabelas.append({'id': t.get('id'), 'linhas': len(linhas)})

    _relatar(r.text, caminho, {
        'tabelas com linhas': tabelas[:6],
        'links de detalhe': len(re.findall(re.escape(path) + r'/\w+', r.text)),
        'cnjs visíveis': sorted(set(re.findall(
            r'\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}', r.text)))[:5],
    })


# ── REST próprio (TJPA, TJMT) ────────────────────────────────────────────────

def recon_rest(sigla: str, criterio: str, valor: str, timeout: int) -> None:
    """Sonda de EXISTÊNCIA: os dois clientes atuais só consultam por CNJ.

    Aqui não há formulário para ler — a pergunta é se a API tem um parâmetro de
    parte. Tentamos os nomes plausíveis e registramos a resposta de cada um.
    Nenhum funcionar é veredito válido: o tribunal entra como abstenção
    declarada, e a API do Voyager dirá `criterio_indisponivel_na_fonte`.
    """
    s, prox = _sessao(), _proxies()
    s.headers['Accept'] = 'application/json, text/plain, */*'
    tentativas: list[dict] = []

    if sigla == 'TJPA':
        base = 'https://consulta-processual-unificada-prd.tjpa.jus.br/consilium-rest'
        s.headers['Referer'] = 'https://consulta-processual-unificada-prd.tjpa.jus.br/'
        candidatos = [
            f'{base}/processobyparte/{valor}',
            f'{base}/processobydocumento/{valor}',
            f'{base}/processobynome/{valor}',
            f'{base}/processo?nomeParte={valor}',
            f'{base}/processo?cpfcnpj={_so_digitos(valor)}',
        ]
    else:  # TJMT
        base = ('https://hellsgate.tjmt.jus.br/consultaprocessual/'
                'ProcessosJudiciais/v2')
        s.headers['Referer'] = 'https://consultaprocessual.tjmt.jus.br/'
        chave = {'documento': 'documento', 'nome': 'nomeParte',
                 'oab': 'oab', 'advogado': 'nomeAdvogado'}[criterio]
        candidatos = [
            f'{base}?Skip=0&Take=10&{chave}={valor}',
            f'{base}?Skip=0&Take=10&nomeParte={valor}',
            f'{base}?Skip=0&Take=10&cpfCnpj={_so_digitos(valor)}',
        ]
        # O X-Fingerprint é gerado fresco por request (ver enrichers/tjmt.py).
        s.headers['X-Fingerprint'] = _fingerprint_tjmt()

    for url in candidatos:
        try:
            r = s.get(url, proxies=prox, timeout=timeout)
            corpo = (r.text or '')[:400]
            tentativas.append({'url': url, 'status': r.status_code,
                               'bytes': len(r.text or ''), 'amostra': corpo})
            print(f'   {r.status_code} {url[:110]}')
        except Exception as exc:  # noqa: BLE001 — recon registra a falha e segue
            tentativas.append({'url': url, 'erro': str(exc)[:200]})
            print(f'   ERRO {url[:110]} :: {str(exc)[:120]}')
        time.sleep(1.0)

    caminho = _salvar(sigla, criterio,
                      json.dumps(tentativas, ensure_ascii=False, indent=2), ext='json')
    print(f'   fixture: {caminho}')
    ok = [t for t in tentativas if t.get('status') == 200 and t.get('bytes', 0) > 200]
    print(f'   veredito: {"CANDIDATO A FUNCIONAR" if ok else "sem rota de busca por parte"}')


def _fingerprint_tjmt() -> str:
    """Mesmo algoritmo do `enrichers/tjmt.py` (extraído do bundle da SPA)."""
    import base64
    import hashlib
    import hmac

    chave = b'A_mesma_mao_que_aplaude_e_a_que_vaia!'
    ts = int(time.time() * 1000)
    tela, lang = '1920x1080', 'pt-BR'
    msg = f'{UA}-{tela}-{lang}-{ts}'.encode()
    assinatura = base64.b64encode(hmac.new(chave, msg, hashlib.sha256).digest()).decode()
    return json.dumps({'signature': assinatura, 'timestamp': ts, 'userAgent': UA,
                       'screenResolution': tela, 'language': lang})


def main() -> int:
    sigla = _env('SIGLA').upper()
    criterio = _env('CRITERIO', 'documento').lower()
    valor = _env('VALOR')
    timeout = int(_env('TIMEOUT', '60'))

    if not sigla or not valor:
        print(__doc__)
        return 2

    print(f'== {sigla} · {criterio} · {valor!r} · proxy={_env("PROXY") or "direto"}')
    inicio = time.time()
    if sigla in ESAJ:
        recon_esaj(sigla, criterio, valor, timeout)
    elif sigla in PJE:
        recon_pje(sigla, criterio, valor, timeout)
    elif sigla in REST:
        recon_rest(sigla, criterio, valor, timeout)
    else:
        print(f'!! {sigla} não está no escopo (e-SAJ {sorted(ESAJ)}, '
              f'PJe {sorted(PJE)}, REST {sorted(REST)})')
        return 2
    print(f'== levou {time.time() - inicio:.1f}s')
    return 0


if __name__ == '__main__':
    sys.exit(main())
