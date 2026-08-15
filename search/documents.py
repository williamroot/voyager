"""Serialização ORM → documento Elasticsearch (formato Jusbrasil/Digesto)."""
from typing import Optional

from tribunals.models import FonteDiario, Movimentacao, Process, ProcessoParte

# Cache em memória das FonteDiario (tabela pequena, ~14 rows, não muda em runtime).
_FONTE_CACHE: dict[str, FonteDiario] = {}


def _get_fonte(tribunal_id: str) -> Optional[FonteDiario]:
    if tribunal_id not in _FONTE_CACHE:
        fd = FonteDiario.objects.filter(tribunal_id=tribunal_id).first()
        _FONTE_CACHE[tribunal_id] = fd
    return _FONTE_CACHE[tribunal_id]


def _so_digitos(numero: Optional[str]) -> str:
    """CNJ só dígitos (20) — busca 'colável': casa com ou sem máscara."""
    return ''.join(ch for ch in (numero or '') if ch.isdigit())


def _serialize_partes(processo: Process) -> tuple[str, str, bool, list[dict]]:
    """Retorna (advs_str, partes_str, tem_ente_publico_passivo, participacoes).

    - advs/partes: strings concatenadas (compat Jusbrasil/Digesto — full-text).
    - participacoes: lista pro campo NESTED do doc processos — busca estruturada
      por polo/papel/documento/OAB ("processos onde X é EXECUTADO").
    - tem_ente_publico_passivo = devedor público no polo passivo (RE_ENTE_PUBLICO,
      a mesma regex do Estágio do Crédito) — derivado aqui porque as participações
      JÁ estão carregadas (zero query extra).
    """
    from tribunals.estagio import RE_ENTE_PUBLICO
    # relação reversa: se o queryset veio com prefetch_related('participacoes'),
    # NÃO dispara query por processo — essencial pro reindex em massa (71M) não cair
    # em N+1. No caminho single-doc (write-through) faz 1 query, igual antes.
    pps = processo.participacoes.all()
    advs = []
    partes = []
    participacoes = []
    tem_ente = False
    for pp in pps:
        eh_advogado = pp.parte.tipo == 'advogado' or 'ADVOGADO' in (pp.papel or '')
        if eh_advogado:
            nome = pp.parte.nome
            if pp.parte.oab:
                nome = f'{nome} (OAB {pp.parte.oab})'
            advs.append(nome)
        partes.append(pp.parte.nome)
        participacoes.append({
            'parte_id': pp.parte_id,
            'nome': pp.parte.nome,
            'documento': pp.parte.documento or None,
            'oab': pp.parte.oab or None,
            'tipo': pp.parte.tipo,
            'polo': pp.polo,
            'papel': pp.papel or '',
            'eh_advogado': eh_advogado,
        })
        if not tem_ente and pp.polo == 'passivo' and RE_ENTE_PUBLICO.search(pp.parte.nome or ''):
            tem_ente = True
    return ', '.join(advs), ', '.join(partes), tem_ente, participacoes


def _source_id_for(tribunal_id: str) -> Optional[int]:
    fd = _get_fonte(tribunal_id)
    return fd.source_id if fd else None


def _periodico_slugs(tribunal_id: str) -> dict:
    fd = _get_fonte(tribunal_id)
    if fd:
        return {
            'periodico_diario_slug': fd.diario_slug,
            'periodico_orgao_slug': fd.orgao_slug,
            'periodico_caderno_slug': fd.caderno_slug,
        }
    return {
        'periodico_diario_slug': tribunal_id.lower(),
        'periodico_orgao_slug': tribunal_id.lower(),
        'periodico_caderno_slug': '',
    }


def _entidades_do_texto(texto, numero_cnj):
    """Entidades extraídas do corpo da publicação (OAB, CPF/CNPJ, CNJ, valores).

    A OAB sempre esteve escrita aqui dentro; a busca é que só olhava o que o
    enricher trazia (0,26% da base). Ver `search/entidades_texto.py`.

    O CNJ do PRÓPRIO processo é removido de `cnjs_citados`: toda publicação cita
    o número dela mesma, e mantê-lo transformaria o campo "processos citados"
    num campo "este processo" — inútil pra achar incidente vinculado, que é
    justamente pra isso que ele serve.
    """
    from search.entidades_texto import extrair
    ent = extrair(texto or '')
    citados = [c for c in ent.get('cnjs_citados', []) if c != numero_cnj]
    if citados:
        ent['cnjs_citados'] = citados
    else:
        ent.pop('cnjs_citados', None)
    return ent


def movimentacao_to_doc(mov: Movimentacao) -> dict:
    """Monta o documento ES no formato Jusbrasil/Digesto."""
    proc = mov.processo
    advs, partes, _, _ = _serialize_partes(proc)
    source_id = _source_id_for(mov.tribunal_id)
    slugs = _periodico_slugs(mov.tribunal_id)
    return {
        'id': mov.id,
        'tribunal': mov.tribunal_id,
        'source': source_id,
        'publish_date': mov.data_disponibilizacao.isoformat() if mov.data_disponibilizacao else None,
        'available_at': mov.inserido_em.isoformat() if mov.inserido_em else None,
        'detected_at': mov.inserido_em.isoformat() if mov.inserido_em else None,
        'body': mov.texto,
        'docurl': mov.link,
        'cached_docurl': None,  # populado por pdf_storage se disponível
        'proc': proc.numero_cnj,
        'proc_digits': _so_digitos(proc.numero_cnj),
        'proc_alt': None,
        'proc_apens': None,
        'advs': advs,
        'partes': partes,
        'assunto': proc.assunto_nome or '',
        'assunto_norm': mov.assunto_norm or [],
        'processo_id': proc.id,
        'classe_nome': mov.nome_classe or proc.classe_nome or '',
        'codigo_classe': mov.codigo_classe or proc.classe_codigo or '',
        'secao_diario': mov.nome_orgao,
        'ativo': mov.ativo,
        'recorte_id': mov.id,
        'tipo_comunicacao': mov.tipo_comunicacao,
        'tipo_documento': mov.tipo_documento,
        'nome_orgao': mov.nome_orgao,
        **slugs,
        **_entidades_do_texto(mov.texto, proc.numero_cnj),
    }


def movimentacao_to_doc_sem_partes(mov: Movimentacao) -> dict:
    """Versão sem query de ProcessoParte — mais rápida pra backfill inicial.

    As partes podem ser reindexadas depois via reindex sem --sem-partes.

    As ENTIDADES do texto entram aqui também: elas não dependem de
    `ProcessoParte` (saem do próprio corpo da publicação), e é justamente esta
    variante que roda no backfill em massa.
    """
    proc = mov.processo
    source_id = _source_id_for(mov.tribunal_id)
    slugs = _periodico_slugs(mov.tribunal_id)
    return {
        'id': mov.id,
        'tribunal': mov.tribunal_id,
        'source': source_id,
        'publish_date': mov.data_disponibilizacao.isoformat() if mov.data_disponibilizacao else None,
        'available_at': mov.inserido_em.isoformat() if mov.inserido_em else None,
        'detected_at': mov.inserido_em.isoformat() if mov.inserido_em else None,
        'body': mov.texto,
        'docurl': mov.link,
        'cached_docurl': None,
        'proc': proc.numero_cnj,
        'proc_digits': _so_digitos(proc.numero_cnj),
        'proc_alt': None,
        'proc_apens': None,
        'advs': '',
        'partes': '',
        'assunto': proc.assunto_nome or '',
        'assunto_norm': mov.assunto_norm or [],
        'processo_id': proc.id,
        'classe_nome': mov.nome_classe or proc.classe_nome or '',
        'codigo_classe': mov.codigo_classe or proc.classe_codigo or '',
        'secao_diario': mov.nome_orgao,
        'ativo': mov.ativo,
        'recorte_id': mov.id,
        'tipo_comunicacao': mov.tipo_comunicacao,
        'tipo_documento': mov.tipo_documento,
        'nome_orgao': mov.nome_orgao,
        **slugs,
        **_entidades_do_texto(mov.texto, proc.numero_cnj),
       
    }


def processo_to_doc(proc: Process) -> dict:
    """Monta o documento ES do processo (index processos).

    Schema de NEGÓCIO completo — todo campo agregável que o comercial/leads/
    jurimetria precisa mora aqui (ver PROC_MAPPING). Não adicionar campo sem
    atualizar o mapping + reindexar.
    """
    from .geo import uf_do_tribunal
    advs, partes, tem_ente, participacoes = _serialize_partes(proc)
    source_id = _source_id_for(proc.tribunal_id)
    # "validado" = passou por QUALQUER enriquecimento (tribunal/djen/datajud).
    # max() das datas — QA: a cadeia de or priorizava datajud e subnotificava
    # freshness (doc enriquecido hoje mostrava data de maio).
    datas = [d for d in (proc.data_enriquecimento_datajud, proc.data_enriquecimento_tribunal,
                         proc.data_enriquecimento_djen, proc.enriquecido_em) if d]
    enriquecido_em = max(datas) if datas else None
    return {
        'id': proc.id,
        'tribunal': proc.tribunal_id,
        'uf': uf_do_tribunal(proc.tribunal_id),            # mapa comercial: agrega por estado
        'tem_sinal_precatorio': proc.tem_sinal_precatorio,  # Fase 0: possível precatório (sinal DJEN)
        'source': source_id,
        'proc': proc.numero_cnj,
        'proc_digits': _so_digitos(proc.numero_cnj),
        'classe_nome': proc.classe_nome or '',
        'codigo_classe': proc.classe_codigo or '',
        'assunto': proc.assunto_nome or '',
        'assunto_codigo': proc.assunto_codigo or '',
        'advs': advs,
        'partes': partes,
        'participacoes': participacoes,
        'orgao_julgador': proc.orgao_julgador_nome or '',
        'juizo': proc.juizo or '',
        'valor_causa': float(proc.valor_causa) if proc.valor_causa else None,
        'ano_cnj': proc.ano_cnj,
        'data_autuacao': proc.data_autuacao.isoformat() if proc.data_autuacao else None,
        'primeira_movimentacao_em': proc.primeira_movimentacao_em.isoformat() if proc.primeira_movimentacao_em else None,
        'total_movimentacoes': proc.total_movimentacoes,
        'ultima_movimentacao_em': proc.ultima_movimentacao_em.isoformat() if proc.ultima_movimentacao_em else None,
        'inserido_em': proc.inserido_em.isoformat() if proc.inserido_em else None,
        'segredo_justica': proc.segredo_justica,
        'classificacao': proc.classificacao or '',
        'classificacao_score': proc.classificacao_score,
        'classificacao_versao': proc.classificacao_versao or '',
        'classificacao_em': proc.classificacao_em.isoformat() if proc.classificacao_em else None,
        'enriquecido': enriquecido_em is not None,
        'enriquecido_em': enriquecido_em.isoformat() if enriquecido_em else None,
        'enriquecimento_status': proc.enriquecimento_status or '',
        'tem_ente_publico_passivo': tem_ente,
    }