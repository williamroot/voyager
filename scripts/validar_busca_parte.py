"""VALIDAÇÃO ao vivo da busca por parte: toda fonte × todo critério.

Roda os MOTORES DE PRODUÇÃO (`enrichers/busca/`) contra os tribunais de verdade
e imprime uma matriz. É o teste que os unitários não podem dar: eles provam que
sabemos ler a resposta, este prova que a fonte ainda responde e que os campos
continuam onde estavam.

Por que existe, além do gosto por medir: cada célula desta matriz já foi, em
algum momento de 04/09/2026, um **zero silencioso** — host quase-certo, botão
errado, página 0, UF da OAB, filtro ignorado. Nenhum deles deu erro; todos
devolveram "nenhum resultado" com HTTP 200. Uma matriz que roda inteira é a
forma barata de perceber a próxima.

    python3 scripts/validar_busca_parte.py                # tudo
    SIGLAS=TRF3,TJMG python3 scripts/validar_busca_parte.py
    CRITERIOS=oab python3 scripts/validar_busca_parte.py

Dentro do container roda com a malha de proxies e o pool do Voyager. De fora
roda direto (sem proxy) — o que basta para conferir fonte e parser, e é como a
matriz abaixo foi levantada.

Os ALVOS são dados reais, colhidos de processos reais de cada tribunal: buscar
por um valor que não existe naquele acervo devolve zero legítimo e não prova
nada. Quando um tribunal muda de layout, é aqui que aparece.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from enrichers.busca.base import BuscaError
from enrichers.busca.registry import CATALOGO

#: (critério, valor) por tribunal. Valor colhido de processo real daquele
#: acervo — e o comentário diz o que ele devolveu quando foi medido.
ALVOS: dict[str, dict[str, str]] = {
    'TJSP': {
        'documento': '60.746.948/0001-12',        # Bradesco — bate o teto de 1.000
        'nome': 'JOAO CARLOS DE OLIVEIRA SILVA',  # 34
        'oab': '329754/SP',                       # 823
        'advogado': 'GABRIELA GONCALVES MARTINS DE FREITAS',
    },
    'TJAL': {
        'documento': '60.746.948/0001-12',
        'nome': 'ANTONIO CARLOS DE OMENA',        # 4
        'oab': '10000/AL',                        # 2
        'advogado': 'JOSE CARLOS DE OLIVEIRA',    # 143
    },
    'TJMG': {
        'documento': '17.155.730/0001-64',        # Cemig — 30 (teto)
        'nome': 'JOAQUIM PEREIRA DE ALMEIDA',     # 12
        'oab': '65417/MG',                        # 6 — só SEM a UF
        'advogado': 'MARCELO DE OLIVEIRA',        # 30
    },
    'TJMA': {
        'documento': '17.155.730/0001-64',        # 6
        'nome': 'MARIA JOSE DOS SANTOS',          # 30
        'oab': '10000/MA',                        # 4
        'advogado': 'ALBA MARIA DE SOUZA LIMA',   # 30
    },
    'TRF1': {
        'documento': '17.155.730/0001-64',        # 30
        'nome': 'MARIA JOSE DOS SANTOS',          # 30
        'oab': '83437/MG',                        # 30
        'advogado': 'EVERTON RICARDO DA SILVA',   # 30
    },
    'TRF3': {
        'documento': '45.358.058/0001-40',        # UFSCar — 30 de 1.372
        'nome': 'MARIA JOSE DOS SANTOS',          # 30
        'oab': '59810/SP',                        # 30 — só COM a UF
        'advogado': 'ANTONIO CARLOS FLORIM',      # 30
    },
    'TRF5': {
        'documento': '29.979.036/0001-40',        # INSS — conta 30, mostra 1
        'nome': 'MARIA JOSE DOS SANTOS',          # conta 30, mostra 1
        'oab': '18191/PE',                        # conta 16, mostra 1
        'advogado': 'KLEBER TABOSA BRASILEIRO',   # conta 13, mostra 1
    },
    'TJPA': {
        'documento': '60.746.948/0001-12',        # Bradesco — 198, paginando
        'nome': 'MARIA JOSE DOS SANTOS',          # 45 (exato)
        'oab': '16499/PA',                        # 34 (só com zeros à esquerda)
    },
    'TJMT': {
        'documento': '60.746.948/0001-12',
        'nome': 'MARIA JOSE DOS SANTOS',          # 1.159
        'oab': '20688',                           # 112
        'advogado': 'JOAO',                       # 507.896 (filtro fraco)
    },
}

TETO_PAGINAS = int(os.environ.get('PAGINAS', '1'))


class _EnricherLeve:
    """Enricher de mentira: só as URLs e um `_next_proxy` que devolve nada.

    Existe para o motor rodar FORA do container — sem Django, sem pool. O que
    ele NÃO cobre é a integração com a malha de proxies; isso só a execução
    dentro do container exercita, e é por isso que este script também roda lá.
    """

    pool = None

    def __init__(self, *_, **__):
        import requests
        self.timeout = (10, 180)
        self.session = requests.Session()
        # Sem User-Agent fixo: quem decide é o motor, por tribunal
        # (`UA_POR_TRIBUNAL` em `enrichers/busca/pje.py`) — TRF3 e TRF5 querem
        # cabeçalhos opostos.
        self.session.headers.update({
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'pt-BR,pt;q=0.9,en;q=0.8',
        })

    def _next_proxy(self, _excluir, **__):
        return None

    def _get(self, url):
        return self.session.get(url, timeout=self.timeout)

    def _post(self, url, data):
        return self.session.post(url, data=data, timeout=self.timeout, headers={
            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'})

    def _extract_form_fields(self, soup):
        form = soup.find('form', {'id': 'fPP'})
        campos: dict = {}
        if not form:
            return campos
        for entrada in form.find_all('input'):
            nome = entrada.get('name')
            if not nome:
                continue
            tipo = (entrada.get('type') or 'text').lower()
            if tipo in ('checkbox', 'radio') and not entrada.get('checked'):
                continue
            campos[nome] = entrada.get('value', '')
        for select in form.find_all('select'):
            nome = select.get('name')
            if not nome:
                continue
            opcao = select.find('option', selected=True) or select.find('option')
            campos[nome] = opcao.get('value', '') if opcao else ''
        return campos

    def _find_search_script_id(self, soup):
        import re
        for script in soup.find_all('script'):
            conteudo = script.string or script.get_text() or ''
            if 'executarPesquisa' in conteudo and 'A4J.AJAX.Submit' in conteudo:
                m = re.search(r"'parameters':\s*\{'(fPP:[^']+)'", conteudo)
                if m:
                    return m.group(1)
        return None


def _fabricar(sigla: str):
    """Motor de produção, com o enricher real se houver Django, senão o leve."""
    try:
        import django
        os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
        django.setup()
        from enrichers.busca.registry import buscador
        return buscador(sigla), 'pool'
    except Exception:
        pass

    from enrichers.busca.esaj import BuscaEsaj
    from enrichers.busca.pje import BuscaPje
    from enrichers.busca.rest import BuscaTjmt, BuscaTjpa

    urls = {
        'TJSP': ('https://esaj.tjsp.jus.br', None, None),
        'TJAL': ('https://www2.tjal.jus.br', None, None),
        'TRF1': ('https://pje1g-consultapublica.trf1.jus.br',
                 '/consultapublica/ConsultaPublica/listView.seam',
                 '/consultapublica/ConsultaPublica/DetalheProcessoConsultaPublica'),
        'TRF3': ('https://pje1g.trf3.jus.br', '/pje/ConsultaPublica/listView.seam',
                 '/pje/ConsultaPublica/DetalheProcessoConsultaPublica'),
        'TRF5': ('https://pje1g.trf5.jus.br', '/pjeconsulta/ConsultaPublica/listView.seam',
                 '/pjeconsulta/ConsultaPublica/DetalheProcessoConsultaPublica'),
        'TJMG': ('https://pje-consulta-publica.tjmg.jus.br', '/pje/ConsultaPublica/listView.seam',
                 '/pje/ConsultaPublica/DetalheProcessoConsultaPublica'),
        'TJMA': ('https://pje.tjma.jus.br', '/pje/ConsultaPublica/listView.seam',
                 '/pje/ConsultaPublica/DetalheProcessoConsultaPublica'),
        'TJPA': ('https://consulta-processual-unificada-prd.tjpa.jus.br', None, None),
        'TJMT': ('https://hellsgate.tjmt.jus.br', None, None),
    }
    base, lista, detalhe = urls[sigla]
    classe = type(f'{sigla}Leve', (_EnricherLeve,), {
        # NÃO declare aqui atributo que o enricher real não tem: foi assim que
        # `PREFER_CORTEX` (que só existe no e-SAJ) passou batido e quebrou os
        # cinco PJe na primeira busca em produção.
        'TRIBUNAL_SIGLA': sigla, 'BASE_URL': base,
        'LIST_URL': base + (lista or ''), 'DETALHE_PATH': detalhe or '',
        'SEARCH_PATH': '/consultaprocessual/ProcessosJudiciais/v2',
    })
    motor = {'esaj': BuscaEsaj, 'pje': BuscaPje}.get(CATALOGO[sigla].motor)
    if motor is None:
        motor = BuscaTjmt if sigla == 'TJMT' else BuscaTjpa
    return motor(classe), 'direto'


def main() -> int:
    siglas = [s.strip().upper() for s in
              (os.environ.get('SIGLAS') or ','.join(sorted(CATALOGO))).split(',')]
    filtro = {c.strip() for c in (os.environ.get('CRITERIOS') or '').split(',') if c.strip()}

    print(f'{"tribunal":9} {"critério":10} {"itens":>6} {"declarado":>10} '
          f'{"teto":>5} {"tempo":>7}  observação')
    print('-' * 88)
    falhas = 0
    for sigla in siglas:
        fonte = CATALOGO.get(sigla)
        if not fonte:
            print(f'{sigla:9} — fora do catálogo')
            continue
        for criterio in ('documento', 'nome', 'oab', 'advogado'):
            if filtro and criterio not in filtro:
                continue
            if criterio not in fonte.criterios:
                print(f'{sigla:9} {criterio:10} {"—":>6} {"—":>10} {"—":>5} {"—":>7}'
                      f'  a fonte não oferece este critério')
                continue
            valor = ALVOS.get(sigla, {}).get(criterio)
            if not valor:
                print(f'{sigla:9} {criterio:10} {"—":>6} {"—":>10} {"—":>5} {"—":>7}'
                      f'  sem alvo conhecido (preencha ALVOS)')
                continue

            motor, via = _fabricar(sigla)
            inicio, itens, total, teto, obs = time.time(), 0, None, False, ''
            try:
                for pagina in motor.paginar(criterio, valor, teto_paginas=TETO_PAGINAS):
                    itens += len(pagina.itens)
                    total = pagina.total_declarado if total is None else total
                    teto = teto or pagina.total_e_teto
                    if pagina.aviso_fonte:
                        obs = pagina.aviso_fonte[:38]
                    break
            except BuscaError as exc:
                obs = f'{type(exc).__name__}: {str(exc)[:44]}'
                falhas += 1
            except Exception as exc:
                obs = f'!! {type(exc).__name__}: {str(exc)[:40]}'
                falhas += 1
            if not itens and not obs:
                obs = 'ZERO — conferir se o alvo ainda tem processo lá'
                falhas += 1
            print(f'{sigla:9} {criterio:10} {itens:6} {total!s:>10} '
                  f'{"sim" if teto else "não":>5} {time.time() - inicio:6.1f}s  {obs}')
            time.sleep(1.5)
    print('-' * 88)
    print(f'células com problema: {falhas}  ·  saída: {via}')
    return 1 if falhas else 0


if __name__ == '__main__':
    sys.exit(main())
