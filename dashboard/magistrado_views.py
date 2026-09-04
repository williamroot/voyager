"""Jurimetria de magistrado — a tela.

Responde uma pergunta que o escritório faz antes de peticionar: **quem é o
magistrado deste caso, o que ele julga, e em que volume.**

A medição vive em `dashboard/dossie_magistrado.py` (funções puras). Aqui só se
orquestra: valida entrada, chama com teto, guarda no cache e entrega. O PDF sai
do MESMO payload da tela — se os dois divergirem um dia, é porque alguém montou
número em dois lugares, que é o defeito que este arquivo existe para evitar.

## O que esta tela é PROIBIDA de fazer

Cada regra saiu de uma medição desta semana, não de preferência de estilo.

1. **Não ranquear, não pontuar, não comparar magistrados.** O produto é
   descrição de padrão de atuação. Não há `order_by` de juiz, não há nota, e
   não há tela de "top magistrados" — se um dia alguém pedir, a resposta é
   este parágrafo.

2. **Não somar condenação com absolvição.** Medido em 03/09/2026 nos 132
   processos da juíza que originou a tela: φ entre as duas é **+0,03**, ou
   seja, praticamente independentes — porque **absolvição parcial é rotina**
   ("condeno pelo art. X … por outro lado, ABSOLVO da imputação do art. Y").
   Os percentuais coexistem no mesmo processo e **não somam 100%**.

3. **Não chamar de "taxa" o que é frequência de menção.** A base são
   intimações: 133 de 141 no caso medido. Marcador diz que o TERMO aparece,
   não que o ato foi praticado — uma decisão cita "prisão preventiva" para
   negá-la. Taxa de mérito exige o dispositivo, que a intimação não traz.

4. **Não buscar por nome sem tribunal.** Medido: das 195 publicações que casam
   com `Rafaela Caldeira Gonçalves`, **56 são de outros tribunais** e são
   HOMÔNIMOS. Sem o filtro, a ficha mistura quatro pessoas e parece correta.
   Por isso o tribunal é obrigatório no formulário, não um filtro opcional.

## Custo e teto

O `match_phrase` sobre `voyager-movimentacoes` (1,6 bi docs) custa segundos e o
ES divide disco com os backfills. A view tem teto explícito e, ao estourar,
devolve a tela com o aviso — **nunca 500, nunca espera infinita** (regra nº 7:
uma medição de rodapé sem teto derrubou o site em julho).
"""
from __future__ import annotations

import hashlib
import logging

from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

logger = logging.getLogger('voyager.dashboard.magistrado')

#: Fallback de tribunais quando o cadastro não pode ser consultado. NÃO é a
#: lista oferecida — ver `tribunais()`.
#:
#: Já foi `['TJSP']` fixo, e isso quebrou a tela de um jeito que só apareceu
#: medindo: em 04/09/2026 o cadastro tinha 164.630 linhas e **zero do TJSP**
#: (TJMG 139.501, TRF3 14.029, TRF1 8.858, TJCE 2.177, TJAM 65). O seletor
#: oferecia o único tribunal sem gente, e todo resultado do localizar levava a
#: `'Tribunal fora da cobertura medida'` — 123.673 pessoas achadas, nenhuma
#: ficha abrível. Constante escrita à mão envelhece; o cadastro, não.
TRIBUNAIS_FALLBACK = ['TJSP']

#: Quanto tempo o seletor confia na medição. O backfill acrescenta tribunal
#: novo em horas, não em segundos — meia hora de atraso no `<select>` é barato,
#: um `COUNT` por request não é.
CACHE_TTL_TRIBUNAIS = 60 * 30


def tribunais() -> list[str]:
    """Os tribunais que o cadastro REALMENTE tem, medidos, não declarados.

    Ordenado por volume: quem abre a tela vê primeiro onde há mais gente.
    """
    chave = f'{_PREFIXO}:tribunais'
    try:
        guardado = cache.get(chave)
    except Exception:  # noqa: BLE001 — Redis fora não derruba a tela
        guardado = None
    if guardado:
        return guardado
    try:
        from django.db.models import Count

        from tribunals.models import Magistrado
        achados = [r['tribunal_id'] for r in (
            Magistrado.objects.values('tribunal_id')
            .annotate(n=Count('id')).order_by('-n'))]
    except Exception:  # noqa: BLE001
        logger.exception('magistrado: não consegui medir os tribunais do cadastro')
        return list(TRIBUNAIS_FALLBACK)
    if not achados:
        # cadastro vazio (backfill ainda não passou) — devolver `[]` deixaria o
        # `<select>` sem opção nenhuma e a tela pareceria quebrada
        return list(TRIBUNAIS_FALLBACK)
    try:
        cache.set(chave, achados, CACHE_TTL_TRIBUNAIS)
    except Exception:  # noqa: BLE001
        logger.warning('magistrado: cache.set dos tribunais falhou', exc_info=True)
    return achados


#: Onde os dois shards do backfill guardam o cursor. Mesma chave do
#: `backfill_magistrados` — se ela mudar lá, a barra de cobertura para de
#: informar e a tela volta a mentir por omissão.
CURSOR_KEY = 'bf:magistrados:%s:cursor'

#: Os shards e a faixa de pk de cada um. Tem de casar com o `--ate` do
#: container: `nacional` [40M, 464M), `tjsp` [464M, 2,4bi).
SHARDS = (('nacional', 0, 464_000_000), ('tjsp', 464_000_000, 2_400_000_000))


def cobertura() -> dict:
    """Quanto do acervo o extrator já varreu — para a tela DIZER, não omitir.

    ## Por que esta função existe

    Em 04/09/2026 a busca por magistrado do TJSP não trazia nada, e a tela não
    tinha como dizer por quê. Medido: as movimentações do TJSP moram entre os
    pk **464.177.590 e 2.198.876.932**, e o backfill tinha varrido só até
    **104.050.890** — 4,3% da tabela. Não havia TJSP no cadastro porque ninguém
    tinha chegado lá, e a tela apresentava isso como "nenhum magistrado com
    esse nome".

    Isso é o `'não medido'` com cara de `'medido e vazio'` (`.ia/DIARIOS.md`
    §18). O seletor de tribunal é MEDIDO no cadastro, então um tribunal ainda
    não varrido simplesmente some da lista — a omissão é mais convincente que
    um erro, e por isso pior.

    Só lê Redis: dois `GET`, sem tocar no banco.
    """
    total = sum(fim - ini for _, ini, fim in SHARDS)
    varrido = 0
    fatias = []
    for nome, ini, fim in SHARDS:
        try:
            cur = cache.get(CURSOR_KEY % nome)
        except Exception:  # noqa: BLE001
            cur = None
        # cursor ausente = shard nunca rodou; nunca contar como se tivesse
        # varrido a faixa inteira (seria o erro que ENCERRA a investigação)
        pos = max(ini, min(int(cur), fim)) if cur is not None else ini
        varrido += pos - ini
        fatias.append({'nome': nome, 'de': ini, 'ate': fim, 'cursor': pos,
                       'pct': round(100 * (pos - ini) / (fim - ini), 1)})
    return {'pct': round(100 * varrido / total, 1), 'shards': fatias,
            'completo': varrido >= total}

CACHE_TTL = 60 * 30
_PREFIXO = 'magistrado:dossie:v1'


def _chave(nome: str, tribunal: str) -> str:
    crua = f'{nome.strip().lower()}|{tribunal.upper()}'
    return f'{_PREFIXO}:{hashlib.sha1(crua.encode()).hexdigest()[:16]}'


def _valida(nome: str, tribunal: str) -> str | None:
    """Devolve o motivo da recusa, ou `None` se pode consultar."""
    if not nome or len(nome.strip()) < 6:
        return ('Informe o nome completo do magistrado — nome curto casa com '
                'meio acervo e a ficha sai sem sentido.')
    if ' ' not in nome.strip():
        return ('Informe nome e sobrenome. Um termo só não identifica ninguém: '
                'a busca é por frase exata no texto da publicação.')
    disponiveis = tribunais()
    if tribunal not in disponiveis:
        return (f'{tribunal} não está no cadastro de magistrados. '
                f'Disponíveis hoje: {", ".join(disponiveis[:8])}'
                + (' …' if len(disponiveis) > 8 else '') + '.')
    return None


def _dossie(nome: str, tribunal: str) -> dict | None:
    """Cache → medição. `None` quando a medição falhou (o motivo vai no log)."""
    chave = _chave(nome, tribunal)
    try:
        guardado = cache.get(chave)
    except Exception:  # noqa: BLE001 — Redis fora não pode derrubar a tela
        guardado = None
    if guardado:
        return guardado

    from . import dossie_magistrado as D
    try:
        payload = D.dossie(nome.strip(), tribunal)
    except Exception:  # noqa: BLE001
        logger.exception('magistrado: falha ao montar o dossiê',
                         extra={'nome': nome, 'tribunal': tribunal})
        return None
    try:
        cache.set(chave, payload, CACHE_TTL)
    except Exception:  # noqa: BLE001 — perder o cache é aceitável
        logger.warning('magistrado: cache.set falhou', exc_info=True)
    return payload


@login_required
@require_GET
def magistrado(request):
    """GET /dashboard/magistrado/?nome=…&tribunal=TJSP — a ficha."""
    nome = (request.GET.get('nome') or '').strip()
    # sem `tribunal` na URL, o padrão é o PRIMEIRO do cadastro (o de maior
    # volume), nunca uma sigla escrita à mão que pode não existir mais
    tribunal = (request.GET.get('tribunal') or tribunais()[0]).upper()

    contexto = {'nome': nome, 'tribunal': tribunal, 'tribunais': tribunais(),
                'cobertura': cobertura(),
                'dossie': None, 'erro': None, 'buscou': bool(nome)}
    if not nome:
        return render(request, 'dashboard/magistrado.html', contexto)

    recusa = _valida(nome, tribunal)
    if recusa:
        contexto['erro'] = recusa
        return render(request, 'dashboard/magistrado.html', contexto)

    payload = _dossie(nome, tribunal)
    if payload is None:
        contexto['erro'] = ('A consulta ao índice não respondeu no tempo. Não é '
                            'ausência de dado — é o índice ocupado. Tente de novo '
                            'em instantes.')
        return render(request, 'dashboard/magistrado.html', contexto)
    if not payload.get('n_processos'):
        cob = contexto['cobertura']
        contexto['erro'] = (
            f'Nenhuma publicação de "{nome}" no {tribunal}. '
            f'Confira a grafia completa do nome — a busca é por frase exata, e '
            f'abreviações não casam.'
            # a ficha lê o ES (que está completo), mas quem chegou aqui veio da
            # busca, que lê o cadastro. Dizer só "não achei" faria o leitor
            # concluir ausência quando a causa pode ser varredura incompleta.
            + ('' if cob.get('completo') else
               f' A varredura do cadastro está em {cob.get("pct")}% do acervo — '
               f'se o nome veio de outra fonte, ele pode existir e ainda não '
               f'ter sido lido.'))
        return render(request, 'dashboard/magistrado.html', contexto)

    contexto['dossie'] = payload
    return render(request, 'dashboard/magistrado.html', contexto)


@login_required
@require_GET
def magistrado_pdf(request):
    """GET /dashboard/magistrado/pdf/?nome=…&tribunal=… — o MESMO payload.

    Sai do cache que a tela populou: PDF e tela não podem divergir, e a única
    forma de garantir isso é não medir duas vezes.
    """
    nome = (request.GET.get('nome') or '').strip()
    tribunal = (request.GET.get('tribunal') or tribunais()[0]).upper()
    if _valida(nome, tribunal):
        return HttpResponse('parâmetros inválidos', status=400)

    payload = _dossie(nome, tribunal)
    if not payload or not payload.get('n_processos'):
        return HttpResponse('sem dados para este magistrado', status=404)

    from . import dossie_magistrado as D
    try:
        pdf = D.render_pdf(payload)
    except RuntimeError as e:
        logger.error('magistrado_pdf: %s', e)
        return HttpResponse('geração de PDF indisponível nesta instância',
                            status=503)
    slug = ''.join(c if c.isalnum() else '-' for c in nome.lower())[:60]
    resp = HttpResponse(pdf, content_type='application/pdf')
    resp['Content-Disposition'] = f'attachment; filename="dossie-{slug}.pdf"'
    return resp

# ─────────────────────────────────────────────────────────────────────────────
# localizar
# ─────────────────────────────────────────────────────────────────────────────

#: Quantas PESSOAS a busca devolve por vez. Não é corte mudo: a tela diz
#: quantas achou e quantas está mostrando (regra nº 2).
TETO_RESULTADOS = 60

#: Abaixo disto a busca é recusada — dois caracteres casam meio cadastro e a
#: varredura fica cara à toa.
MINIMO_BUSCA = 3


def localizar(termo: str, tribunal: str = '') -> dict:
    """Magistrados cujo nome contém `termo`, agrupados por PESSOA.

    ## Por que agrupar aqui também

    `Magistrado` é a tripla `(tribunal, órgão, nome)` — a unidade PROVADA, o
    que a fonte afirma. Uma listagem crua repetiria a mesma pessoa uma vez por
    órgão: medido, `LUIS AUGUSTO SAMPAIO ARRUDA` tem **77 linhas** porque
    `nome_orgao` no TJSP é a subseção do diário, com andar e sala. Sem o
    agrupamento, buscar "Sampaio" devolve o mesmo nome dezenas de vezes e a
    tela parece quebrada.

    ## O teto, e por que ele é declarado

    `nome_chave__contains` **não usa** o índice btree (curinga à esquerda). Com
    876 pessoas isso custa 2 ms; com o backfill nacional a tabela cresce muito,
    e aí custa. O teto existe desde já, e a tela **diz** quando o atingiu — em
    vez de mostrar 60 e deixar quem lê achar que são todos.
    """
    from django.db.models import Count, Max, Min

    from tribunals.models import Magistrado
    from tribunals.services.magistrados import normalizar_nome_magistrado

    chave = normalizar_nome_magistrado(termo or '')
    if len(chave) < MINIMO_BUSCA:
        return {'erro': f'Digite ao menos {MINIMO_BUSCA} letras do nome.',
                'pessoas': [], 'total': 0}

    qs = Magistrado.objects.filter(nome_chave__contains=chave)
    if tribunal:
        qs = qs.filter(tribunal_id=tribunal.upper())

    # uma linha por PESSOA; `orgaos` é a fan-out da tripla, que a tela mostra
    # para o leitor saber que uma pessoa pode ter dezenas de registros de órgão
    agrupado = (qs.values('tribunal_id', 'nome_chave')
                  .annotate(orgaos=Count('id'),
                            desde=Min('primeira_em'), ate=Max('ultima_em'))
                  .order_by('-orgaos', 'nome_chave'))
    total = agrupado.count()
    pagina = list(agrupado[:TETO_RESULTADOS])

    # o nome de EXIBIÇÃO vem de uma linha real, nunca da chave normalizada: a
    # chave é maiúscula, sem acento e sem conectivo, e mostrá-la na tela seria
    # entregar ao usuário o artefato interno em vez do nome da pessoa
    for p in pagina:
        amostra = (Magistrado.objects
                   .filter(tribunal_id=p['tribunal_id'], nome_chave=p['nome_chave'])
                   .values('nome', 'orgao', 'cargo').first()) or {}
        p['nome'] = amostra.get('nome') or p['nome_chave']
        p['orgao'] = amostra.get('orgao') or ''
        p['cargo'] = amostra.get('cargo') or ''
    return {'erro': None, 'pessoas': pagina, 'total': total,
            'truncado': total > len(pagina), 'teto': TETO_RESULTADOS}


@login_required
@require_GET
def magistrado_buscar(request):
    """GET /dashboard/magistrado/buscar/?q=…&tribunal=… — localizar a pessoa.

    Existe porque a ficha exige **nome completo e exato** (a busca no texto é
    por frase), e ninguém sabe de cabeça como o diário grafa o nome. Aqui se
    acha; lá se analisa.
    """
    termo = (request.GET.get('q') or '').strip()
    tribunal = (request.GET.get('tribunal') or '').upper()
    ctx = {'q': termo, 'tribunal': tribunal, 'tribunais': tribunais(),
           'cobertura': cobertura(),
           'resultado': None, 'buscou': bool(termo)}
    if termo:
        try:
            ctx['resultado'] = localizar(termo, tribunal)
        except Exception:  # noqa: BLE001
            logger.exception('magistrado_buscar falhou', extra={'q': termo})
            ctx['resultado'] = {'erro': 'A busca não respondeu. Tente de novo.',
                                'pessoas': [], 'total': 0}
    return render(request, 'dashboard/magistrado_buscar.html', ctx)
