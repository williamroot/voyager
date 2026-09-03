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

#: Tribunais oferecidos. Começa no TJSP porque é onde o marcador do e-SAJ
#: nomeia o magistrado no cabeçalho — 5,6 M publicações medidas. Em outros
#: tribunais o formato muda, e oferecer sem medir seria prometer cobertura
#: que não existe.
TRIBUNAIS = ['TJSP']

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
    if tribunal not in TRIBUNAIS:
        return f'Tribunal fora da cobertura medida. Disponível: {", ".join(TRIBUNAIS)}.'
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
    tribunal = (request.GET.get('tribunal') or 'TJSP').upper()

    contexto = {'nome': nome, 'tribunal': tribunal, 'tribunais': TRIBUNAIS,
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
        contexto['erro'] = (f'Nenhuma publicação de "{nome}" no {tribunal}. '
                            f'Confira a grafia completa do nome — a busca é por '
                            f'frase exata, e abreviações não casam.')
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
    tribunal = (request.GET.get('tribunal') or 'TJSP').upper()
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
