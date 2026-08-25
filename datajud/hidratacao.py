"""Esqueleto do `voyager-acervo` → processo de verdade no acervo rico.

O Datajud não traz parte, advogado nem valor (medido: o `_source` tem só
numeroProcesso, classe, assuntos, órgão, datas, grau, sistema, formato, sigilo e
movimentos). Então a varredura entrega ESQUELETO, e quem põe carne é isto aqui:

    esqueleto (voyager-acervo)
        │
        ├─ 1. cria o Process no Postgres (com o que o Datajud já sabe)
        ├─ 2. sync_processo() → movimentos do Datajud
        └─ 3. enfileira o enricher do tribunal → PARTES e VALOR

O passo 3 é o caro: o enriquecimento roda a ~113k processos/dia na frota inteira
e só 16 tribunais têm enricher. Por isso a hidratação tem dois gatilhos, e os
dois são deliberadamente ESCOLHIDOS, nunca automáticos sobre tudo:

  - `hidratar_cnj` — alguém abriu aquele processo na busca. É o caminho que
    resolve o caso que originou tudo isso (um CNJ que existe no CNJ, tem zero
    publicação no DJEN e por isso nunca entrou);
  - `hidratar_lote` — recorte de valor (ex.: classe 12078, Cumprimento de
    Sentença contra a Fazenda Pública), que é onde mora o precatório.
"""
from __future__ import annotations

import logging

from django.utils import timezone

from datajud.ingestion import GRAUS_CONHECIDOS, coluna_grau_existe
from search.client import get_es, index_name
from tribunals.cnj import sigla_do_cnj, so_digitos
from tribunals.models import Process, Tribunal

logger = logging.getLogger('voyager.datajud.hidratacao')

INDICE = 'acervo'


def _fmt(digitos: str) -> str:
    d = digitos
    return f'{d[0:7]}-{d[7:9]}.{d[9:13]}.{d[13]}.{d[14:16]}.{d[16:20]}'


def esqueleto(cnj: str) -> dict | None:
    """Busca o esqueleto no `voyager-acervo`. None se nem lá ele existe.

    Um CNJ pode ter mais de um doc (G1 e G2 são documentos distintos no
    Datajud); pegamos o de grau mais alto, que é o que tem o andamento mais
    recente.
    """
    d = so_digitos(cnj)
    if len(d) != 20:
        return None
    r = get_es().search(
        index=index_name(INDICE), size=5,
        query={'term': {'proc': _fmt(d)}},
        sort=[{'atualizado_em': {'order': 'desc'}}],
    )
    hits = r['hits']['hits']
    return hits[0]['_source'] if hits else None


def hidratar_cnj(cnj: str, com_enricher: bool = True) -> dict:
    """Faz o esqueleto virar processo: cria, puxa movimentos, pede partes.

    Idempotente: se o processo já existe no acervo rico, não recria — só
    completa o que faltar. Devolve sempre um dict com `estado`, pra tela poder
    dizer o que aconteceu em vez de só girar um spinner.
    """
    digitos = so_digitos(cnj)
    if len(digitos) != 20:
        return {'cnj': cnj, 'estado': 'cnj_invalido'}
    numero = _fmt(digitos)

    proc = Process.objects.filter(numero_cnj=numero).first()
    if proc:
        estado = 'ja_no_acervo'
    else:
        esq = esqueleto(numero)
        sigla = (esq or {}).get('tribunal') or sigla_do_cnj(digitos)
        trib = Tribunal.objects.filter(sigla=sigla).first()
        if not trib:
            # sem tribunal conhecido não dá pra criar: PROTECT na FK, e chutar
            # tribunal faria o enricher bater na porta errada
            return {'cnj': numero, 'estado': 'tribunal_desconhecido', 'sigla': sigla}
        campos = {
            'numero_cnj': numero, 'tribunal': trib,
            'classe_codigo': (esq or {}).get('classe_codigo') or '',
            'classe_nome': (esq or {}).get('classe_nome') or '',
            'assunto_codigo': ((esq or {}).get('assunto_codigos') or [''])[0],
            'assunto_nome': ((esq or {}).get('assunto_nomes') or [''])[0],
            'orgao_julgador_codigo': (esq or {}).get('orgao_codigo') or '',
            'orgao_julgador_nome': (esq or {}).get('orgao_nome') or '',
        }
        # O esqueleto já traz `grau` — 342.046.902 de 342.046.902 docs do
        # `voyager-acervo` têm o campo, e 21,6% deles são `JE` (Juizado
        # Especial paga por RPV, não por precatório). Só entra no INSERT se a
        # coluna já existir no BANCO: o ALTER da 0052 é sobre 102 M de linhas
        # sob escrita e pode não ter passado quando este código subir.
        grau = str((esq or {}).get('grau') or '').strip().upper()
        if grau in GRAUS_CONHECIDOS and coluna_grau_existe():
            campos['grau'] = grau
        proc = Process.objects.create(**campos)
        estado = 'criado'

    # movimentos do Datajud (1 request; o array inteiro vem no mesmo hit)
    from datajud.ingestion import sync_processo
    sinc = sync_processo(proc)

    enfileirado = False
    if com_enricher:
        from djen.ingestion import TRIBUNAIS_COM_ENRICHER
        from enrichers.jobs import enqueue_enriquecimento_manual
        if proc.tribunal_id in TRIBUNAIS_COM_ENRICHER:
            # fila MANUAL: quem hidrata está esperando resposta, e a fila
            # per-tribunal tem centenas de milhares de itens de backlog
            enqueue_enriquecimento_manual(proc.pk)
            enfileirado = True

    _marcar_no_acervo(numero)
    return {
        'cnj': numero, 'estado': estado, 'process_id': proc.pk,
        'tribunal': proc.tribunal_id,
        'movimentos_novos': sinc.get('novos', 0),
        'datajud_encontrado': sinc.get('encontrado', False),
        'enricher_enfileirado': enfileirado,
        # honestidade: sem enricher no tribunal, partes/valor NÃO virão
        'tera_partes': enfileirado,
        'em': timezone.now().isoformat(),
    }


def _marcar_no_acervo(numero_cnj: str) -> None:
    """Marca o esqueleto como presente no acervo rico (a tela lê isso)."""
    try:
        get_es().update_by_query(
            index=index_name(INDICE), conflicts='proceed', refresh=False,
            query={'term': {'proc': numero_cnj}},
            script={'source': "ctx._source.no_acervo = true", 'lang': 'painless'},
        )
    except Exception as e:  # noqa: BLE001 — marcação é cosmética, não pode derrubar a hidratação
        logger.warning('falha ao marcar no_acervo de %s: %s', numero_cnj, e)


def hidratar_lote(sigla: str, classe: str | int = 12078, limite: int = 1000,
                  com_enricher: bool = True) -> dict:
    """Hidrata um recorte do esqueleto — o caminho do nicho.

    Puxa do `voyager-acervo` quem ainda não está no acervo rico, na ordem do
    mais recentemente movimentado (crédito vivo antes de crédito dormente).
    """
    r = get_es().search(
        index=index_name(INDICE), size=limite,
        query={'bool': {'filter': [
            {'term': {'tribunal': sigla.upper()}},
            {'term': {'classe_codigo': str(classe)}},
        ], 'must_not': [{'term': {'no_acervo': True}}]}},
        sort=[{'atualizado_em': {'order': 'desc'}}],
        source=['proc'],
    )
    resultados = {'criados': 0, 'ja_tinha': 0, 'falhas': 0, 'enfileirados': 0}
    for h in r['hits']['hits']:
        try:
            out = hidratar_cnj(h['_source']['proc'], com_enricher=com_enricher)
        except Exception as e:  # noqa: BLE001 — um CNJ ruim não pode matar o lote
            logger.warning('hidratação falhou em %s: %s', h['_source']['proc'], e)
            resultados['falhas'] += 1
            continue
        if out['estado'] == 'criado':
            resultados['criados'] += 1
        elif out['estado'] == 'ja_no_acervo':
            resultados['ja_tinha'] += 1
        resultados['enfileirados'] += 1 if out.get('enricher_enfileirado') else 0
    resultados.update({'tribunal': sigla.upper(), 'classe': str(classe),
                       'candidatos': len(r['hits']['hits'])})
    return resultados
