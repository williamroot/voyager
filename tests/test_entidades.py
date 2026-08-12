"""Testes do índice canônico de entidades (`search/entidades.py`).

Puro unit — sem Postgres e sem Elasticsearch. As linhas de entrada são as
MEDIDAS no dado real de prod em 12/08/2026 (`tribunals_parte`, raiz de CNPJ
29.979.036): 610 linhas, 610 CNPJs distintos, 11 grafias distintas.

O que se cobra aqui é exatamente o que quebra o produto se regredir:
  - as grafias + os CNPJs da MESMA raiz viram UMA entidade;
  - o nome canônico é a grafia MAIS FREQUENTE (não a mais longa, não a 1ª);
  - advogado nunca entra (senão vira "devedor" no top-10);
  - CNPJ mascarado NÃO funde por máscara (critério explícito da decisão 2);
  - quem não tem documento cai no nome e é MARCADO como tal (`chave='nome'`);
  - o mapping tem o campo de autocomplete.
"""
import math

import pytest

from search import entidades as ent
from search.mappings import ENTIDADE_MAPPING


# --------------------------------------------------------------------------- #
# Fixtures do dado real (12/08/2026)
# --------------------------------------------------------------------------- #
#: as 8 grafias que o usuário mediu + as demais que apareceram na raiz
GRAFIAS_INSS = [
    'INSTITUTO NACIONAL DO SEGURO SOCIAL',                 # a mais frequente (597 linhas)
    'Instituto Nacional do Seguro Social - INSS',
    'INSTITUTO NACIONAL DO SEGURO SOCIAL - INSS',
    'INSTITUTO NACIONAL DO SEGURO SOCIAL (REQUERIDO(A))',
    'INSTITUTO NACIONAL DO SEGURO SOCIAL (IMPETRADO(A))',
    'Instituto Nacional do Seguro Social',
    'INSTITUTO NACIONAL DE SEGURO SOCIAL',
    'INSS - INSTITUTO NACIONAL DE SEGURIDADE SOCIAL',
]

#: CNPJs da MESMA raiz (matriz + filiais) — o ponto todo da decisão de chave
CNPJS_INSS = [
    '29.979.036/0001-40',   # matriz     — 23.230 processos
    '29.979.036/0002-21',   # filial     —    925
    '29.979.036/0012-01',   # filial     —  2.067
    '29.979.036/0988-76',   # filial     —    158
]


def _linha(nome, documento='', tipo_documento='', tipo='pj', oab='', pid=1):
    """Uma linha de `tribunals_parte` no formato que o Agregador consome."""
    return dict(parte_id=pid, nome=nome, documento=documento,
                tipo_documento=tipo_documento, tipo=tipo, oab=oab)


def _agregar(linhas):
    agg = ent.Agregador()
    for i, ln in enumerate(linhas):
        agg.add(ln.get('parte_id', i + 1), ln['nome'], ln['documento'],
                ln['tipo_documento'], ln['tipo'], ln['oab'])
    return agg


def _docs(agg):
    return {i: d for i, d in agg.docs(agora='2026-08-12T00:00:00+00:00')}


# --------------------------------------------------------------------------- #
# 1. Fusão do INSS — o teste-rei
# --------------------------------------------------------------------------- #
def test_inss_grafias_e_cnpjs_da_mesma_raiz_viram_uma_entidade():
    """8 grafias × 4 CNPJs da raiz 29979036 = 32 linhas de Parte → 1 entidade."""
    linhas = [_linha(nome, doc, 'CNPJ', 'pj', pid=1 + i * 10 + j)
              for i, nome in enumerate(GRAFIAS_INSS)
              for j, doc in enumerate(CNPJS_INSS)]
    agg = _agregar(linhas)
    docs = _docs(agg)

    assert len(docs) == 1, 'o INSS é UMA entidade, não cinco'
    (_id, doc), = docs.items()
    assert _id == 'cnpj:29979036'
    assert doc['chave'] == 'cnpj'
    assert doc['raiz_cnpj'] == '29979036'
    assert doc['n_partes'] == 32
    # o produto: TODAS as grafias sobrevivem à fusão
    assert set(doc['variantes']) == set(GRAFIAS_INSS)
    assert doc['n_variantes'] == 8
    # matriz e filiais listadas — a raiz uniu, os CNPJs completos não sumiram
    assert set(doc['documentos']) == set(CNPJS_INSS)
    assert doc['eh_ente_publico'] is True


def test_nome_canonico_e_a_grafia_mais_frequente():
    """Não a mais longa, não a primeira: a que o dado real mais usa."""
    linhas = (
        # 597 linhas medidas em prod pra esta grafia (usamos 5 no teste)
        [_linha('INSTITUTO NACIONAL DO SEGURO SOCIAL', CNPJS_INSS[0], 'CNPJ')] * 5
        # a mais LONGA aparece 1× — não pode ganhar
        + [_linha('AUTORIDADE COATORA EM MANDADO DE SEGURANCA - GERENTE '
                  'EXECUTIVO(A) DO INSS EM CARUARU/PE', CNPJS_INSS[1], 'CNPJ')]
        # a PRIMEIRA da lista de leitura também não pode ganhar por ordem
        + [_linha('CEAB-DJ INSS', CNPJS_INSS[2], 'CNPJ')]
    )
    # embaralha a ordem de leitura pra provar que não é "a primeira"
    linhas = linhas[-1:] + linhas[:-1]
    doc = next(iter(_docs(_agregar(linhas)).values()))
    assert doc['nome_canonico'] == 'INSTITUTO NACIONAL DO SEGURO SOCIAL'


def test_marcador_de_papel_do_pje_nao_vira_entidade_nova():
    """'(REQUERIDO(A))' e '- EXECUTADO' são papel, não nome (fusão por NOME)."""
    linhas = [
        _linha('MUNICIPIO DE SAO PAULO', tipo='desconhecido'),
        _linha('MUNICIPIO DE SAO PAULO (REQUERIDO(A))', tipo='desconhecido'),
        _linha('Município de São Paulo - EXECUTADO', tipo='desconhecido'),
        _linha('MUNICIPIO DE SAO PAULO/SP', tipo='desconhecido'),
    ]
    docs = _docs(_agregar(linhas))
    assert len(docs) == 1
    doc = next(iter(docs.values()))
    assert doc['chave'] == 'nome'
    assert doc['n_variantes'] == 4
    # 4 grafias com frequência 1: o desempate escolhe a de caixa mista (mais
    # legível na UI) e o sufixo de papel é removido do rótulo exibido.
    assert doc['nome_canonico'] == 'Município de São Paulo'
    assert ent.limpar_rotulo('MUNICIPIO DE SAO PAULO (REQUERIDO(A))') \
        == 'MUNICIPIO DE SAO PAULO'
    assert ent.limpar_rotulo('Instituto Nacional do Seguro Social - INSS') \
        == 'Instituto Nacional do Seguro Social - INSS'   # sigla não é papel


# --------------------------------------------------------------------------- #
# 2. Escopo — advogado fora, PF fora
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize('tipo,oab', [
    ('advogado', ''),          # marcado no `tipo`
    ('pj', 'SP123456'),        # `tipo` mente, mas tem OAB
    ('desconhecido', 'MG9999'),
])
def test_advogado_fica_de_fora(tipo, oab):
    """Advogado REPRESENTA o devedor; em SP uma advogada entrou no top-10."""
    dentro, motivo = ent.classificar('FULANA DE TAL SOCIEDADE DE ADVOGADOS',
                                     '', '', tipo, oab)
    assert dentro is False
    assert motivo == ent.FORA_ADVOGADO


def test_advogado_nao_gera_entidade_nem_polui_variantes():
    linhas = [
        _linha('INSTITUTO NACIONAL DO SEGURO SOCIAL', CNPJS_INSS[0], 'CNPJ'),
        _linha('MARIA ADVOGADA', '111.222.333-44', 'CPF', 'advogado', 'SP1'),
        _linha('ESCRITORIO X ADVOGADOS', '11.222.333/0001-44', 'CNPJ',
               'pj', 'SP2'),   # OAB preenchida ⇒ advogado, mesmo com CNPJ
    ]
    agg = _agregar(linhas)
    docs = _docs(agg)
    assert len(docs) == 1
    assert agg.stats['fora_advogado'] == 2


def test_pessoa_fisica_fica_de_fora_mesmo_com_tipo_errado():
    """O CPF PROVA pessoa física — vale mais que o `tipo` do cadastro."""
    dentro, motivo = ent.classificar('JOAO DA SILVA', '123.456.789-00', 'CPF', 'pj', '')
    assert (dentro, motivo) == (False, ent.FORA_PESSOA_FISICA)
    dentro, motivo = ent.classificar('JOAO DA SILVA', '', '', 'pf', '')
    assert (dentro, motivo) == (False, ent.FORA_NAO_PJ_NEM_ENTE)


# --------------------------------------------------------------------------- #
# 3. CNPJ mascarado — decisão 2 (NÃO funde por máscara)
# --------------------------------------------------------------------------- #
def test_cnpj_mascarado_nao_e_chave():
    assert ent.eh_mascarado('29.9**.***/****-**') is True
    assert ent.eh_mascarado('639.XXX.XXX-XX') is True
    assert ent.raiz_cnpj('29.9**.***/****-**') is None
    assert ent.raiz_cnpj('29.979.036/0001-40') == '29979036'


def test_mascarado_cai_no_nome_e_e_contabilizado():
    """656 linhas mascaradas medidas na raiz do INSS: viram grupo por NOME.

    Elas NÃO se juntam à entidade `cnpj:29979036` (a máscara não prova a raiz),
    mas também não se perdem: agrupam pelo nome normalizado e o descarte fica
    contado em `documentos_mascarados`.
    """
    linhas = [
        _linha('INSTITUTO NACIONAL DO SEGURO SOCIAL', CNPJS_INSS[0], 'CNPJ'),
        _linha('INSTITUTO NACIONAL DO SEGURO SOCIAL', '29.9**.***/****-**', 'CNPJ'),
        _linha('INSTITUTO NACIONAL DO SEGURO SOCIAL', '29.9**.***/****-**', 'CNPJ'),
    ]
    agg = _agregar(linhas)
    docs = _docs(agg)

    assert set(docs) == {'cnpj:29979036',
                         ent.entidade_id('nome', 'INSTITUTO NACIONAL SEGURO SOCIAL')}
    assert agg.stats['documentos_mascarados'] == 2

    por_nome = docs[ent.entidade_id('nome', 'INSTITUTO NACIONAL SEGURO SOCIAL')]
    assert por_nome['chave'] == 'nome'
    assert por_nome['n_partes'] == 2
    assert por_nome['documentos_mascarados'] == 2
    # documento mascarado NÃO entra na lista de documentos (seria falso positivo)
    assert por_nome['documentos'] == []
    assert por_nome['raiz_cnpj'] is None


def test_mascaras_diferentes_nao_se_fundem_entre_si():
    """Duas máscaras iguais de entidades diferentes continuam separadas."""
    linhas = [
        _linha('MUNICIPIO DE ARARAS', '29.9**.***/****-**', 'CNPJ', 'desconhecido'),
        _linha('ESTADO DE MINAS GERAIS', '29.9**.***/****-**', 'CNPJ', 'desconhecido'),
    ]
    assert len(_docs(_agregar(linhas))) == 2


# --------------------------------------------------------------------------- #
# 4. Sem documento (18,4% da base) — chave por nome, marcada
# --------------------------------------------------------------------------- #
def test_sem_documento_cai_no_nome_com_procedencia_marcada():
    linhas = [
        _linha('PREFEITURA MUNICIPAL DE ARARAS', tipo='desconhecido'),
        _linha('Prefeitura Municipal de Araras', tipo='desconhecido'),
    ]
    docs = _docs(_agregar(linhas))
    assert len(docs) == 1
    doc = next(iter(docs.values()))
    assert doc['chave'] == 'nome'          # quem consome sabe que é heurística
    assert doc['raiz_cnpj'] is None
    assert doc['nome_normalizado'] == 'PREFEITURA MUNICIPAL ARARAS'
    assert doc['eh_ente_publico'] is True
    assert doc['n_partes'] == 2


def test_ente_publico_sem_documento_entra_mesmo_com_tipo_desconhecido():
    """Medido: numa janela de 200k ids, 704 entes públicos eram `desconhecido`
    SEM documento contra 270 `pj` com documento. Descartá-los seria descartar
    justamente o universo de "quem deve"."""
    for nome in ('MUNICIPIO DE BELO HORIZONTE', 'ESTADO DE MINAS GERAIS',
                 'FAZENDA PUBLICA DO ESTADO DE SAO PAULO', 'UNIAO FEDERAL',
                 'PREFEITURA MUNICIPAL DE ARARAS'):
        dentro, motivo = ent.classificar(nome, '', '', 'desconhecido', '')
        assert dentro is True, nome
        assert motivo == ent.DENTRO_ENTE_PUBLICO


def test_pj_sem_documento_entra_pelo_tipo():
    dentro, motivo = ent.classificar('EMPRESA X COMERCIO LTDA', '', '', 'pj', '')
    assert (dentro, motivo) == (True, ent.DENTRO_TIPO_PJ)


# --------------------------------------------------------------------------- #
# 4b. Consolidação nome → cnpj (a mesma entidade não pode sair 2× na lista)
# --------------------------------------------------------------------------- #
def test_consolidacao_junta_grupo_por_nome_no_grupo_por_cnpj():
    """Medido na fatia de 200k: a DPU aparecia 2× — as linhas com CNPJ e as sem."""
    linhas = [
        _linha('DEFENSORIA PUBLICA DA UNIAO', '00.375.114/0001-05', 'CNPJ'),
        _linha('DEFENSORIA PUBLICA DA UNIAO', '00.375.114/0002-88', 'CNPJ'),
        _linha('DEFENSORIA PÚBLICA DA UNIÃO', tipo='desconhecido'),
        _linha('DEFENSORIA PUBLICA DA UNIAO - DPU', tipo='desconhecido'),
    ]
    agg = _agregar(linhas)
    assert len(agg.grupos) == 2                    # antes: 1 por cnpj + 1 por nome
    assert agg.consolidar()['fundidos'] == 1
    docs = _docs(agg)
    assert set(docs) == {'cnpj:00375114'}
    doc = docs['cnpj:00375114']
    assert doc['n_partes'] == 4
    assert doc['n_variantes'] == 3
    assert doc['grupos_absorvidos'] == 1


def test_consolidacao_abstem_em_homonimo():
    """2 CNPJs de volume parecido com o MESMO nome: a qual pertence? Abstém."""
    linhas = ([_linha('AUTO POSTO SAO JOSE LTDA', '11.111.111/0001-11', 'CNPJ')] * 3
              + [_linha('AUTO POSTO SAO JOSE LTDA', '22.222.222/0001-22', 'CNPJ')] * 2
              + [_linha('AUTO POSTO SAO JOSE LTDA', tipo='pj')])
    agg = _agregar(linhas)
    resultado = agg.consolidar()
    assert resultado == {'fundidos': 0, 'linhas': 0, 'ambiguos': 1}
    assert len(agg.grupos) == 3                    # os 3 seguem separados


def test_consolidacao_funde_quando_um_cnpj_domina():
    """O caso do INSS: 610 linhas na raiz certa contra 5 CNPJs errados de 1
    linha cada (digitação de tribunal). Empate técnico falso não pode barrar."""
    linhas = ([_linha('INSTITUTO NACIONAL DO SEGURO SOCIAL', CNPJS_INSS[0], 'CNPJ')] * 30
              + [_linha('INSTITUTO NACIONAL DO SEGURO SOCIAL',
                        '24.403.442/0001-01', 'CNPJ')]          # CNPJ errado
              + [_linha('INSTITUTO NACIONAL DO SEGURO SOCIAL',
                        '03.500.738/0001-02', 'CNPJ')]          # CNPJ errado
              + [_linha('INSTITUTO NACIONAL DO SEGURO SOCIAL (REQUERIDO(A))',
                        tipo='desconhecido')] * 4)
    agg = _agregar(linhas)
    assert agg.consolidar()['fundidos'] == 1
    docs = _docs(agg)
    assert docs['cnpj:29979036']['n_partes'] == 34    # 30 + 4 sem documento
    assert 'nome:' not in ' '.join(docs)              # nenhum grupo por nome sobrou


def test_consolidacao_nao_usa_variante_generica_como_ponte():
    """'UNIÃO FEDERAL' pendurada no CNPJ da AGU não pode engolir o grupo
    'União Federal' inteiro — a ponte é o nome CANÔNICO, não qualquer grafia."""
    linhas = [
        _linha('ADVOCACIA GERAL DA UNIAO', '26.994.558/0001-23', 'CNPJ'),
        _linha('ADVOCACIA GERAL DA UNIAO', '26.994.558/0002-04', 'CNPJ'),
        _linha('UNIÃO FEDERAL', '26.994.558/0003-95', 'CNPJ'),   # grafia solta
        _linha('UNIAO FEDERAL', tipo='desconhecido'),
        _linha('UNIAO FEDERAL (AGU)', tipo='desconhecido'),
    ]
    agg = _agregar(linhas)
    assert agg.consolidar()['fundidos'] == 0
    docs = _docs(agg)
    assert 'cnpj:26994558' in docs
    assert docs['cnpj:26994558']['nome_canonico'] == 'ADVOCACIA GERAL DA UNIAO'
    assert len(docs) == 2                          # a "União Federal" sobreviveu


# --------------------------------------------------------------------------- #
# 5. Normalização — os casos que quebram na prática
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize('nome', [
    'Instituto Nacional do Seguro Social - INSS',
    'INSTITUTO NACIONAL DO SEGURO SOCIAL',
    'INSTITUTO NACIONAL DO SEGURO SOCIAL (REQUERIDO(A))',
    'instituto nacional do seguro social — REQUERIDO',
    'Instituto Nacional do Seguro Social/INSS',
])
def test_normalizar_nome_colapsa_as_grafias_do_inss(nome):
    assert ent.normalizar_nome(nome) == 'INSTITUTO NACIONAL SEGURO SOCIAL'


def test_normalizar_nome_nao_funde_entidades_diferentes():
    chaves = {ent.normalizar_nome(n) for n in (
        'MUNICIPIO DE ARARAS', 'MUNICIPIO DE ARARAQUARA',
        'ESTADO DE SAO PAULO', 'ESTADO DE MINAS GERAIS',
        'UNIAO FEDERAL', 'UNIVERSIDADE FEDERAL DE MINAS GERAIS')}
    assert len(chaves) == 6


def test_marca_no_inicio_nao_e_tratada_como_sigla():
    """Falso-merge medido no build completo (12/08): 144 empresas distintas
    caíam na chave 'INDUSTRIA COMERCIO' porque a marca ('HENRIMAR', 8 chars)
    era descartada como sigla. Sigla vem DEPOIS do nome, nunca antes."""
    nomes = ['HENRIMAR - INDUSTRIA E COMERCIO LTDA',
             'IMBRAMIL - INDUSTRIA E COMERCIO LTDA',
             'PROFITEC - INDUSTRIA E COMERCIO LTDA - EPP']
    chaves = {ent.normalizar_nome(n) for n in nomes}
    assert len(chaves) == 3
    assert ent.normalizar_nome(nomes[0]) == 'HENRIMAR INDUSTRIA E COMERCIO'
    # e a sigla NO FIM continua sendo descartada (é o que funde o INSS)
    assert (ent.normalizar_nome('INSTITUTO NACIONAL DO SEGURO SOCIAL - INSS')
            == 'INSTITUTO NACIONAL SEGURO SOCIAL')


def test_inicial_de_uma_letra_nao_e_conectivo():
    """'A&E TRANSPORTES' e 'A. O. TRANSPORTES' viravam ambas 'TRANSPORTES'
    (54 empresas fundidas, medido 12/08) quando A/O/E eram stopword."""
    chaves = {ent.normalizar_nome(n) for n in
              ('A&E TRANSPORTES LTDA - ME', 'A. O. TRANSPORTES LTDA',
               'BTT - TRANSPORTES S/A')}
    assert len(chaves) == 3


@pytest.mark.parametrize('nome', [
    'INFORMAÇÃO PROTEGIDA', 'Informação Protegida', 'SEGREDO DE JUSTIÇA',
    'NÃO INFORMADO', 'PARTE SIGILOSA',
])
def test_placeholder_nao_e_entidade(nome):
    """Medido: 'INFORMAÇÃO PROTEGIDA' somava 4.212 linhas e seria a MAIOR
    entidade do índice — um NULL disfarçado no topo do autocomplete."""
    dentro, motivo = ent.classificar(nome, '', '', 'pj', '')
    assert (dentro, motivo) == (False, ent.FORA_PLACEHOLDER)


def test_forma_societaria_nao_quebra_a_chave():
    assert (ent.normalizar_nome('BANCO EXEMPLO S/A')
            == ent.normalizar_nome('BANCO EXEMPLO S.A.')
            == ent.normalizar_nome('BANCO EXEMPLO SA'))


def test_entidade_id_e_deterministico():
    """`_id` estável = reindex idempotente (roda de novo, não duplica)."""
    assert ent.entidade_id('cnpj', '29979036') == 'cnpj:29979036'
    a = ent.entidade_id('nome', 'MUNICIPIO SAO PAULO')
    assert a == ent.entidade_id('nome', 'MUNICIPIO SAO PAULO')
    assert a.startswith('nome:')
    assert a != ent.entidade_id('nome', 'MUNICIPIO SAO PAOLO')


# --------------------------------------------------------------------------- #
# 6. Contrato do índice — mapping e queries
# --------------------------------------------------------------------------- #
def test_mapping_tem_autocomplete_em_nome_e_variantes_de_busca():
    props = ENTIDADE_MAPPING['mappings']['properties']
    for campo in ('nome_canonico', 'variantes_busca'):
        auto = props[campo]['fields']['autocomplete']
        assert auto['type'] == 'search_as_you_type'
        # asciifolding: quem digita "uniao" precisa achar "UNIÃO"
        assert auto['analyzer'] == 'portuguese_asciifolding'
    assert props['nome_canonico']['fields']['raw']['type'] == 'keyword'
    # `variantes` é o campo de RECALL (o OR) — não entra no autocomplete
    assert 'autocomplete' not in props['variantes']['fields']


def test_mapping_cobre_todo_campo_do_doc_builder():
    """Campo no doc sem mapping = dynamic mapping silencioso (tipo errado pra
    sempre). Mesma regra de ouro do tests/test_es_schema_partes.py."""
    doc = next(iter(_docs(_agregar(
        [_linha('INSTITUTO NACIONAL DO SEGURO SOCIAL', CNPJS_INSS[0], 'CNPJ')]
    )).values()))
    faltando = set(doc) - set(ENTIDADE_MAPPING['mappings']['properties'])
    assert not faltando, f'campos sem mapping: {faltando}'


def test_mapping_tem_n_processos_mas_o_build_nao_o_escreve():
    """`n_processos` é campo do índice, mas NÃO sai do build.

    `Parte.total_processos` está preenchido em 39,3% — precomputar dali mostraria
    '0 processos' em 6 de cada 10 entidades (decisão 6). O número vem da segunda
    passada ES→ES (`contar_processos_entidades`), então o doc do build sai SEM o
    campo — e ausência é informação: significa "ainda não contamos".
    """
    props = ENTIDADE_MAPPING['mappings']['properties']
    assert props['n_processos']['type'] == 'long'      # 77M docs e crescendo
    assert props['n_processos_em']['type'] == 'date'   # o número envelhece
    assert 'total_processos' not in props              # nunca veio do Postgres
    assert props['n_partes']['type'] == 'integer'

    doc = next(iter(_docs(_agregar(
        [_linha('INSTITUTO NACIONAL DO SEGURO SOCIAL', CNPJS_INSS[0], 'CNPJ')]
    )).values()))
    assert 'n_processos' not in doc
    assert 'n_processos_em' not in doc


def test_query_variantes_e_or_de_match_phrase_no_campo_partes():
    doc = next(iter(_docs(_agregar(
        [_linha(n, CNPJS_INSS[0], 'CNPJ') for n in GRAFIAS_INSS]
    )).values()))
    body = ent.query_variantes(doc['variantes'])
    should = body['query']['bool']['should']
    assert body['query']['bool']['minimum_should_match'] == 1
    assert len(should) == len(GRAFIAS_INSS)
    assert all(list(c) == ['match_phrase'] and 'partes' in c['match_phrase']
               for c in should)
    frases = {c['match_phrase']['partes'] for c in should}
    assert 'INSTITUTO NACIONAL DO SEGURO SOCIAL' in frases


def test_query_variantes_respeita_o_teto_de_clausulas():
    body = ent.query_variantes([f'ENTIDADE {i}' for i in range(500)])
    assert len(body['query']['bool']['should']) == ent.MAX_CLAUSULAS_VARIANTES


def test_query_variantes_corta_grafia_de_cauda_por_frequencia():
    """Defesa do over-match: grafia genérica de 1 linha fora do OR."""
    body = ent.query_variantes(
        ['INSTITUTO NACIONAL DO SEGURO SOCIAL', 'UNIÃO FEDERAL'],
        ocorrencias=[597, 1], min_ocorrencias=5)
    frases = [c['match_phrase']['partes'] for c in body['query']['bool']['should']]
    assert frases == ['INSTITUTO NACIONAL DO SEGURO SOCIAL']
    # nunca devolve OR vazio (senão a busca some)
    vazio = ent.query_variantes(['SO ESSA'], ocorrencias=[1], min_ocorrencias=99)
    assert len(vazio['query']['bool']['should']) == 1


def test_query_autocomplete_usa_bool_prefix_nos_dois_campos():
    body = ent.query_autocomplete('inss', tamanho=5)
    dis_max = body['query']['function_score']['query']['dis_max']
    campos = {f for c in dis_max['queries'] for f in c['multi_match']['fields']}
    assert all(c['multi_match']['type'] == 'bool_prefix' for c in dis_max['queries'])
    assert 'nome_canonico.autocomplete' in campos
    assert 'variantes_busca.autocomplete' in campos    # "inss" acha o INSS
    assert 'variantes_busca.autocomplete._3gram' in campos
    # prevalência: contagem real quando existe, proxy `n_partes` quando não
    campos_fvf = {f['field_value_factor']['field']
                  for f in body['query']['function_score']['functions']}
    assert campos_fvf == {'n_processos', 'n_partes'}
    assert body['query']['function_score']['boost_mode'] == 'multiply'
    assert body['size'] == 5
    # o consumidor precisa receber o número (e a idade dele) pra exibir
    assert {'n_processos', 'n_processos_em'} <= set(body['_source'])


def test_variantes_de_busca_corta_grafia_de_uma_linha_da_entidade_grande():
    """O INSS tem UMA linha grafada 'Instituto Nacional do Seguro Social
    (UNIÃO)' entre 764 (o cartório digitou as 2 partes no mesmo campo). Com ela
    no campo de busca, procurar "uniao" devolvia o INSS em 1º (medido 12/08)."""
    variantes = [('INSTITUTO NACIONAL DO SEGURO SOCIAL', 610),
                 ('INSTITUTO NACIONAL DO SEGURO SOCIAL - INSS', 17),
                 ('Instituto Nacional do Seguro Social - INSS', 9),
                 ('Instituto Nacional do Seguro Social (UNIÃO)', 1)]
    busca = ent.variantes_de_busca(variantes, n_partes=764)
    assert 'Instituto Nacional do Seguro Social (UNIÃO)' not in busca
    assert len(busca) == 3
    # ...mas a grafia SEGUE em `variantes` — o OR de recall precisa dela
    doc = ent.grupo_to_doc(_grupo(variantes, n_partes=764), '2026-08-12T00:00:00Z')
    assert 'Instituto Nacional do Seguro Social (UNIÃO)' in doc['variantes']


def test_variantes_de_busca_nunca_deixa_entidade_pequena_invisivel():
    """Entidade de 1 linha só tem grafias de 1 linha — não pode sumir da busca."""
    assert ent.variantes_de_busca([('MUNICIPIO DE ARARAS', 1)], n_partes=1) \
        == ['MUNICIPIO DE ARARAS']


def _grupo(variantes, n_partes):
    g = ent.Grupo('cnpj', '29979036')
    g.variantes = dict(variantes)
    g.n_partes = n_partes
    g.tipos = {'pj': n_partes}
    return g


def test_query_autocomplete_estreita_quando_o_usuario_digita_mais():
    """`bool_prefix` casa por OR: 'fazenda sao paulo' trazia 'FAZENDA SÃO
    MARCELO LTDA' (2 de 3 termos) no topo e a Fazenda do Estado de SP ficava
    fora do top-5 (medido 12/08). A cláusula `and` vem primeiro e mais forte."""
    q = ent.query_autocomplete('fazenda sao paulo')
    clausulas = q['query']['function_score']['query']['dis_max']['queries']
    ands = [c for c in clausulas if c['multi_match']['operator'] == 'and']
    ors = [c for c in clausulas if c['multi_match']['operator'] == 'or']
    assert len(ands) == 2 and len(ors) == 2          # 2 campos × 2 operadores
    # o `or` continua existindo (rede pra typo/palavra a mais), mas mais fraco
    assert max(c['multi_match']['boost'] for c in ors) \
        < min(c['multi_match']['boost'] for c in ands)


def test_query_autocomplete_nao_soma_os_dois_campos():
    """`dis_max` (melhor campo), não `bool.should`: casar nos dois campos NÃO
    pode valer o dobro — senão '…INSS Manaus' (7 linhas) passa na frente do
    INSS (610), que casa 'inss' só pela variante. Medido no ES de prod."""
    q = ent.query_autocomplete('inss')['query']['function_score']['query']
    assert 'dis_max' in q and 'bool' not in q
    assert q['dis_max']['tie_breaker'] < 0.5


def test_query_autocomplete_filtra_ente_publico_sem_perder_o_dis_max():
    q = ent.query_autocomplete('prefeitura', somente_ente_publico=True)
    interna = q['query']['function_score']['query']
    assert interna['bool']['filter'] == [{'term': {'eh_ente_publico': True}}]
    assert 'dis_max' in interna['bool']['must'][0]


# --------------------------------------------------------------------------- #
# 6b. `n_processos` — a contagem REAL e o ranking que ela conserta
# --------------------------------------------------------------------------- #
#: medido no índice real (12/08) — os dois lados do bug do autocomplete de "inss".
#: `score` é o score que o ES devolvia ANTES (relevância textual × log2p(n_partes)),
#: e é dele que se extrai o componente TEXTUAL, que esta mudança não altera.
INSS_REAL = {'n_partes': 764, 'n_processos': 4_402_239, 'score': 9.062}
#: 109 linhas de cadastro ("o cartório redigitou o nome da gerência 109 vezes")
#: contra 21 processos de verdade. O proxy errava por 5 ordens de grandeza.
GERENTE_SP_REAL = {'n_partes': 109, 'n_processos': 21, 'score': 9.166}


def _prevalencia(doc):
    """Reproduz o `function_score` do ES sobre um `_source`: qual fator sai.

    `score_mode: multiply` das funções cujo FILTRO casa — o ES simplesmente
    ignora as outras. `log2p` do ES é `log10(2 + fator·valor)`.
    """
    tem = doc.get('n_processos') is not None
    fator = None
    for funcao in ent.funcoes_prevalencia():
        filtro = funcao['filter']
        casa = ('exists' in filtro) if tem else ('bool' in filtro)
        if not casa:
            continue
        fvf = funcao['field_value_factor']
        valor = doc.get(fvf['field'], fvf['missing'])
        if valor is None:
            valor = fvf['missing']
        parcela = math.log10(2 + fvf['factor'] * valor)
        fator = parcela if fator is None else fator * parcela
    assert fator is not None, f'nenhuma função de prevalência casou com {doc}'
    return fator


def _componente_textual(real):
    """O pedaço do score que vem do TEXTO — o que esta mudança NÃO toca.

    `score_antigo = texto × log2p(n_partes)`, então `texto = score / log2p(...)`.
    Reconstruir daqui é o que torna o teste um teste do dado real e não da
    minha aritmética: os dois `score` foram copiados do ES de prod.
    """
    return real['score'] / math.log10(2 + 4 * real['n_partes'])


def test_inss_ganha_de_quem_so_tem_linhas_de_cadastro():
    """O TESTE-REI DESTA MUDANÇA. Buscar "inss" devolvia, no índice real:

        1º  Gerente Executivo do INSS de São Paulo/Centro   109 partes  score 9,166
        2º  INSTITUTO NACIONAL DO SEGURO SOCIAL             764 partes  score 9,062

    O proxy `n_partes` conta CADASTRO, não litígio: 109 redigitações do nome de
    uma gerência (21 processos de verdade) contra a autarquia com 4.402.239.
    Como `log2p` achata (3,49 contra 2,64), quem decidia era o texto — e o texto
    prefere o nome curto e específico. Com a contagem real não há o que decidir.
    """
    texto_inss = _componente_textual(INSS_REAL)
    texto_gerente = _componente_textual(GERENTE_SP_REAL)
    # o bug: o texto favorece a gerência e o proxy não dá conta de reverter
    assert texto_gerente > texto_inss
    assert INSS_REAL['score'] < GERENTE_SP_REAL['score']

    depois_inss = texto_inss * _prevalencia(INSS_REAL)
    depois_gerente = texto_gerente * _prevalencia(GERENTE_SP_REAL)
    assert depois_inss > depois_gerente, 'digitar "inss" tem que trazer o INSS'
    # e não por pouco: a distância tem que aguentar variação de score textual
    assert depois_inss > 2 * depois_gerente


def test_ranking_cai_no_n_partes_quando_n_processos_e_null():
    """Fallback: fora do escopo da contagem, o comportamento é o de antes."""
    sem = {'n_partes': 44, 'n_processos': None}
    assert _prevalencia(sem) == pytest.approx(math.log10(2 + 4 * 44))
    # e quem foi contado passa na frente de quem só tem cadastro
    assert _prevalencia({'n_partes': 44, 'n_processos': 500_000}) > _prevalencia(sem)


def test_null_nao_e_zero_no_ranking():
    """"Não contamos" ≠ "contamos e deu zero".

    Se o ranking usasse `max` entre os dois campos, os dois casos cairiam no
    `n_partes` e virariam a mesma coisa. Zero é MEDIÇÃO e tem que ranquear
    abaixo do desconhecido, que ainda merece o benefício da dúvida do proxy.
    """
    desconhecido = {'n_partes': 50, 'n_processos': None}
    medido_zero = {'n_partes': 50, 'n_processos': 0}
    assert _prevalencia(medido_zero) < _prevalencia(desconhecido)
    # contado: log2p(0) × atestação(50); desconhecido: só log2p(50)
    assert _prevalencia(medido_zero) == pytest.approx(
        math.log10(2) * math.log10(2 + 4 * 50))
    assert _prevalencia(desconhecido) == pytest.approx(math.log10(2 + 4 * 50))
    # e nenhuma das duas zera o score (log2p, não log) — a entidade não some
    assert _prevalencia(medido_zero) > 0

    funcoes = ent.funcoes_prevalencia()
    assert funcoes[0]['filter'] == {'exists': {'field': 'n_processos'}}
    assert funcoes[1]['filter']['bool']['must_not'] == [
        {'exists': {'field': 'n_processos'}}]
    corpo = ent.query_autocomplete('inss')['query']['function_score']
    assert corpo['score_mode'] == 'multiply'


def test_atestacao_so_vale_para_quem_foi_contado():
    """O fallback tem que ser EXATAMENTE o comportamento anterior.

    Se a atestação valesse pra todo mundo, `n_partes` entraria duas vezes no
    score de quem não foi contado — e aí a mudança mexeria também em quem ela
    não mediu. Fora do escopo da contagem, nada muda.
    """
    funcoes = ent.funcoes_prevalencia()
    atestacao = [f for f in funcoes
                 if f['field_value_factor']['field'] == 'n_partes'
                 and f['filter'] == {'exists': {'field': 'n_processos'}}]
    assert len(atestacao) == 1
    assert _prevalencia({'n_partes': 764, 'n_processos': None}) \
        == pytest.approx(math.log10(2 + 4 * 764))


def test_atestacao_separa_a_entidade_das_suas_facetas():
    """Achado da 1ª contagem: contando só processos, buscar "inss" devolvia

        1º  INSS                                  53 linhas · 4.255.175
       11º  INSTITUTO NACIONAL DO SEGURO SOCIAL  764 linhas · 4.402.239

    `n_processos` é propriedade da FRASE: as facetas do INSS casam quase os
    mesmos processos, o log achata 4,40 MI e 4,25 MI em 3,5% e o texto (que
    prefere o nome curto "INSS") volta a decidir. Quem separa a autarquia das
    suas facetas é o CADASTRO: 764 linhas contra 53.
    """
    autarquia = {'n_partes': 764, 'n_processos': 4_402_239}
    faceta = {'n_partes': 53, 'n_processos': 4_255_175}
    # a contagem sozinha não separa: 0,2% de diferença
    so_contagem = (math.log10(2 + 4 * autarquia['n_processos'])
                   / math.log10(2 + 4 * faceta['n_processos']))
    assert so_contagem < 1.01
    # ...e o texto dá 1,33× de vantagem pro nome curto (medido no ES de prod)
    vantagem_textual = _componente_textual(GERENTE_SP_REAL) / _componente_textual(INSS_REAL)
    assert _prevalencia(autarquia) / _prevalencia(faceta) > vantagem_textual


def test_poda_a_grafia_truncada_que_sequestra_a_contagem():
    """Achado da 1ª contagem completa (12/08): o top-10 de "quem mais litiga no
    Brasil" saiu com `Procuradoria - Allianz` (4 linhas de cadastro) em 1º com
    **7.222.852** processos — porque uma das grafias da entidade era só
    "PROCURADORIA", e `match_phrase` de frase curta casa toda frase longa que a
    contém. `INSTITUTO NACIONAL LTDA` (2 linhas) marcou 4.469.999 pelo mesmo
    motivo: a grafia "INSTITUTO NACIONAL" pega o INSS inteiro."""
    assert ent.grafias_para_contagem(
        ['PROCURADORIA', 'Procuradoria - Allianz'],
        {'PROCURADORIA': 1, 'Procuradoria - Allianz': 3}) == ['Procuradoria - Allianz']
    # empate de frequência também poda: sem prova de que a curta é o nome, a
    # longa é a aposta segura (abster > chutar)
    assert ent.grafias_para_contagem(
        ['INSTITUTO NACIONAL', 'INSTITUTO NACIONAL LTDA'],
        {'INSTITUTO NACIONAL': 1, 'INSTITUTO NACIONAL LTDA': 1}) \
        == ['INSTITUTO NACIONAL LTDA']


def test_poda_preserva_o_inss_porque_a_grafia_curta_e_o_nome():
    """O contra-exemplo que a regra NÃO pode quebrar: a grafia curta do INSS é
    sub-frase da longa, mas aparece 610× contra 17× — ela é o NOME, não um
    truncamento. Podá-la derrubaria a contagem validada de 4.402.239."""
    grafias = ['INSTITUTO NACIONAL DO SEGURO SOCIAL',
               'INSTITUTO NACIONAL DO SEGURO SOCIAL - INSS',
               'Instituto Nacional do Seguro Social - INSS']
    ocorrencias = dict(zip(grafias, (610, 17, 9)))
    assert ent.grafias_para_contagem(grafias, ocorrencias) == grafias
    # e é essa lista que vai pro OR da contagem
    corpo = ent.query_contagem(grafias, ocorrencias=ocorrencias)
    assert len(corpo['query']['bool']['should']) == 3


def test_poda_ignora_o_papel_do_pje_na_comparacao():
    """"X" e "X (REQUERIDO(A))" são a MESMA identidade — a decorada não pode
    engolir a limpa, senão toda entidade carimbada pelo cartório passa a ser
    contada só nos processos onde o carimbo aparece (subregistro sistemático)."""
    grafias = ['MUNICIPIO DE ARARAS', 'MUNICIPIO DE ARARAS (REQUERIDO(A))']
    assert ent.grafias_para_contagem(grafias, dict.fromkeys(grafias, 1)) == grafias


def test_poda_nunca_esvazia_o_or():
    """Sem grafia não há contagem — a mais longa nunca é sub-frase de ninguém."""
    assert ent.grafias_para_contagem(['ÚNICA'], {'ÚNICA': 1}) == ['ÚNICA']
    assert ent.grafias_para_contagem([]) == []
    # sem `ocorrencias` a regra vira "empate em 0": a longa fica
    assert ent.grafias_para_contagem(['S.', 'S.A. VARIG FALIDA']) == ['S.A. VARIG FALIDA']


def test_query_contagem_pede_total_exato_e_zero_documentos():
    """`track_total_hits` default do ES 8 é 10.000: sem isso o INSS voltaria
    10.000 e as 50 maiores entidades do país empatariam num número redondo."""
    grafias = ['INSTITUTO NACIONAL DO SEGURO SOCIAL',
               'INSTITUTO NACIONAL DO SEGURO SOCIAL - INSS']
    corpo = ent.query_contagem(grafias, ocorrencias=dict(zip(grafias, (610, 17))))
    assert corpo['track_total_hits'] is True
    assert corpo['size'] == 0
    should = corpo['query']['bool']['should']
    assert len(should) == 2 and all('match_phrase' in c for c in should)
    assert corpo['query']['bool']['minimum_should_match'] == 1
    # a poda de over-match é o DEFAULT, não um opcional: sem `ocorrencias` a
    # grafia curta não tem como se provar nome e sai do OR
    sem_prova = ent.query_contagem(grafias)['query']['bool']['should']
    assert sem_prova == [{'match_phrase': {
        'partes': 'INSTITUTO NACIONAL DO SEGURO SOCIAL - INSS'}}]


def test_total_exato_recusa_contagem_truncada_ou_com_erro():
    """Abster > chutar: total truncado/erro vira `None` e o campo não é gravado."""
    assert ent.total_exato({'hits': {'total': {'value': 4402239, 'relation': 'eq'}}}) \
        == 4402239
    assert ent.total_exato({'hits': {'total': {'value': 0, 'relation': 'eq'}}}) == 0
    assert ent.total_exato({'hits': {'total': {'value': 10000, 'relation': 'gte'}}}) is None
    assert ent.total_exato({'error': {'type': 'search_phase_execution_exception'}}) is None
    assert ent.total_exato({}) is None
    assert ent.total_exato(None) is None


def test_escopo_contagem_e_o_corte_declarado():
    """Escopo = quem disputa o autocomplete (n_partes>=2 OU ente público)."""
    escopo = ent.escopo_contagem()
    assert escopo['bool']['minimum_should_match'] == 1
    assert {'range': {'n_partes': {'gte': ent.CONTAGEM_MIN_PARTES}}} in escopo['bool']['should']
    assert {'term': {'eh_ente_publico': True}} in escopo['bool']['should']
    assert 'must_not' not in escopo['bool']
    # ente público entra com QUALQUER n_partes: é o universo do produto e quase
    # nunca tem CNPJ no dado do tribunal (decisão 3)
    so_partes = ent.escopo_contagem(incluir_ente_publico=False)
    assert so_partes['bool']['should'] == [{'range': {'n_partes': {'gte': 2}}}]


def test_contagem_e_idempotente():
    """Rodar 2× não corrompe: `--somente-faltantes` nem revisita quem já tem
    número, e o `doc` da escrita é parcial e determinístico."""
    faltantes = ent.escopo_contagem(somente_faltantes=True)
    assert faltantes['bool']['must_not'] == [{'exists': {'field': 'n_processos'}}]

    agora = '2026-08-12T00:00:00+00:00'
    doc = ent.doc_contagem(4402239, agora)
    assert doc == ent.doc_contagem(4402239, agora)          # mesma entrada, mesma saída
    assert doc == {'n_processos': 4402239, 'n_processos_em': agora}
    # escrita PARCIAL: não pode carregar nada do build junto (senão o `update`
    # sobrescreveria variantes/documentos com o que a contagem não sabe)
    assert set(doc) == {'n_processos', 'n_processos_em'}


# --------------------------------------------------------------------------- #
# 7. Estatística do build (o relatório precisa ser honesto)
# --------------------------------------------------------------------------- #
def test_resumo_reporta_taxa_de_fusao_e_procedencia():
    linhas = ([_linha(n, d, 'CNPJ') for n in GRAFIAS_INSS[:4] for d in CNPJS_INSS]
              + [_linha('MUNICIPIO DE ARARAS', tipo='desconhecido')] * 3
              + [_linha('JOAO', '123.456.789-00', 'CPF', 'pf')]
              + [_linha('ADV', '', '', 'advogado', 'SP1')])
    r = _agregar(linhas).resumo()
    assert r['lidas'] == 21
    assert r['dentro'] == 19 and r['fora'] == 2
    assert r['entidades'] == 2
    assert r['entidades_por_chave'] == {'cnpj': 1, 'nome': 1}
    assert r['taxa_fusao_pct'] == pytest.approx(100 * (1 - 2 / 19), abs=0.01)
