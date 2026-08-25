"""Regressão da LINHA DE BASE de 25/08/2026 — o índice de processos e as partes.

Cada teste aqui trava um número medido em produção e o defeito que o produziu.
Se um deles quebrar, o defeito voltou.

## Os três incidentes que estes testes guardam

1. **`exists` do ES conta string vazia como valor presente.** Medido no índice
   de produção em 25/08/2026, no MESMO instante:

       exists no campo `partes` ............ 92.707.849 = 100,0%
       `partes` com CONTEÚDO (n=26.809) ....             18,1%
       `participacoes` nested com >=1 filho .  3.645.848 =  3,93%

   Quem medisse cobertura de partes por `exists` publicaria 100% de um campo
   que vale 18%. Já aconteceu nesta casa (`partes`/`advs` servidos como 100%
   valendo 20%) e é a regra nº 4 do CLAUDE.md.

2. **`ProcessoParte` escrita em massa não chega ao índice sozinha.** Ela não
   tem signal (de propósito — ver `search/signals.py`), não muda `Process.id`
   (então `sync_processos_novos` não a vê) e não muda `Process.atualizado_em`
   (então `sync_processos_atualizados` também não). Só chega ao ES se quem
   escreveu enfileirar `search.jobs.indexar_processos_bulk`.

   Medido na mesma amostra de 30.000 processos: dos 21.956 docs sem `partes`,
   **21.911 (99,8%) estão assim porque o Postgres não tem parte nenhuma** e só
   45 (0,2%) são atraso de índice. O buraco é de DADO; mas no dia em que o dado
   existir, ele PRECISA de reindex — não pega carona.

3. **`_bulk` dimensionado por CONTAGEM de documentos.** 83 jobs mortos com
   `ApiError(413)` e **45.313 publicações fora do índice** (21/08/2026). O doc
   do processo tem o mesmo formato de risco: `partes`/`advs` concatenam TODAS
   as participações, e medido em produção o doc COM parte é **+1.416 B** maior
   que o doc sem (2.369 B contra 953 B de média).
"""
import pytest

pytestmark = pytest.mark.django_db


# --------------------------------------------------------------------------- #
# 1. `exists` mente: o doc SEM partes tem o campo `partes` presente e VAZIO
# --------------------------------------------------------------------------- #
def test_doc_sem_partes_ainda_tem_a_chave_partes():
    """Por isso `exists` mede 100% e o conteúdo mede 18%.

    O builder emite `partes: ''` sempre. Um teste que medisse "o campo veio?"
    passaria em 100% dos documentos do índice — que é exatamente o número
    falso. A medição honesta é pelo CONTEÚDO.
    """
    from search.documents import processo_to_doc
    from tribunals.models import Process, Tribunal

    t, _ = Tribunal.objects.get_or_create(
        sigla='TJZZ', defaults={'nome': 'TJZZ', 'sigla_djen': 'TJZZ'})
    p = Process.objects.create(numero_cnj='0000001-11.2025.8.26.0100', tribunal=t)

    doc = processo_to_doc(p)
    # a chave EXISTE (é isto que o `exists` do ES enxerga) ...
    assert 'partes' in doc and 'advs' in doc
    # ... e está VAZIA. Medir por presença de chave é medir 100%.
    assert doc['partes'] == ''
    assert doc['advs'] == ''
    assert doc['participacoes'] == []


def test_sonda_de_partes_mede_conteudo_e_recusa_vazio():
    """Controle positivo da sonda: verificação com input vazio reporta SUCESSO.

    `scripts/sonda_es_partes.py` só vale se souber dizer SIM e NÃO. Sem este
    controle, "0,0% com partes" não distingue "índice vazio" de "sonda
    quebrada" — e a sonda quebrada é a que encerra a investigação.
    """
    import pathlib

    caminho = (pathlib.Path(__file__).resolve().parent.parent
               / 'scripts' / 'sonda_es_partes.py')
    fonte = caminho.read_text(encoding='utf-8')
    # o módulo faz django.setup() no import; aqui só se quer a função pura.
    escopo: dict = {}
    inicio = fonte.index('def _tem_partes(')
    fim = fonte.index('def _tem(')
    exec(fonte[inicio:fim], escopo)
    tem_partes = escopo['_tem_partes']

    assert tem_partes({'partes': 'FULANO DE TAL, INSS'}), 'sonda recusou parte real'
    assert not tem_partes({'partes': ''}), 'sonda contou string VAZIA como parte'
    assert not tem_partes({'partes': '   '}), 'sonda contou espaço como parte'
    assert not tem_partes({}), 'sonda contou campo AUSENTE como parte'


# --------------------------------------------------------------------------- #
# 2. ProcessoParte em bulk não chega ao índice sozinha
# --------------------------------------------------------------------------- #
def test_processoparte_em_bulk_nao_dispara_indexacao():
    """`bulk_create` de `ProcessoParte` NÃO enfileira nada na `es_index`.

    Não é bug — é decisão registrada em `search/signals.py` (signal por linha
    multiplicaria a fila ~2N por processo). É o CONTRATO: quem escreve parte em
    massa tem que reindexar o processo explicitamente.

    Este teste falha se alguém "consertar" isso pondo um signal em
    `ProcessoParte` sem passar pela decisão — e falha também se o contrato for
    esquecido do outro lado (o teste seguinte cobre isso).
    """
    import django_rq

    from tribunals.models import Parte, Process, ProcessoParte, Tribunal

    t, _ = Tribunal.objects.get_or_create(
        sigla='TJYY', defaults={'nome': 'TJYY', 'sigla_djen': 'TJYY'})
    p = Process.objects.create(numero_cnj='0000002-11.2025.8.26.0100', tribunal=t)
    parte = Parte.objects.create(nome='INSS', tipo='pj')

    fila = django_rq.get_queue('es_index')
    antes = fila.count
    ProcessoParte.objects.bulk_create([
        ProcessoParte(processo=p, parte=parte, polo='passivo', papel='EXECUTADO')])
    assert fila.count == antes, (
        'ProcessoParte passou a disparar indexação por linha — a decisão de '
        'search/signals.py mudou sem passar por ADR. Ver .ia/SEARCH_SCHEMA.md.')


def test_reindex_apos_bulk_de_partes_leva_a_parte_ao_doc():
    """O outro lado do contrato: reindexado, o doc TEM a parte, com polo e OAB.

    É a prova de produto que a régua de 25/08/2026 pede: 21.911 dos 21.956 docs
    sem partes estão assim porque o PG não tem parte. Quando o PG tiver, este é
    o caminho que faz a parte chegar à tela — e ele passa por
    `indexar_processos_bulk`, não por signal.
    """
    from search.documents import processo_to_doc
    from tribunals.models import Parte, Process, ProcessoParte, Tribunal

    t, _ = Tribunal.objects.get_or_create(
        sigla='TJXX', defaults={'nome': 'TJXX', 'sigla_djen': 'TJXX'})
    p = Process.objects.create(numero_cnj='0000003-11.2025.8.26.0100', tribunal=t)
    autor = Parte.objects.create(nome='João da Silva', tipo='pf',
                                 documento='111.222.333-44')
    adv = Parte.objects.create(nome='Maria Advogada', tipo='advogado', oab='SP123456')
    ProcessoParte.objects.bulk_create([
        ProcessoParte(processo=p, parte=autor, polo='ativo', papel='EXEQUENTE'),
        ProcessoParte(processo=p, parte=adv, polo='ativo', papel='ADVOGADO'),
    ])

    doc = processo_to_doc(Process.objects.get(pk=p.pk))
    assert doc['partes'].strip(), 'doc reindexado continuou sem `partes`'
    nomes = {x['nome'] for x in doc['participacoes']}
    assert nomes == {'João da Silva', 'Maria Advogada'}
    polos = {x['nome']: x['polo'] for x in doc['participacoes']}
    assert polos['João da Silva'] == 'ativo'
    # a OAB é o que a tela precisa mostrar para o advogado
    assert 'OAB SP123456' in doc['advs']
    oabs = {x['nome']: x['oab'] for x in doc['participacoes']}
    assert oabs['Maria Advogada'] == 'SP123456'


# --------------------------------------------------------------------------- #
# 3. o `_bulk` fecha por BYTES, não por contagem de documentos
# --------------------------------------------------------------------------- #
def test_indexar_processos_bulk_fecha_por_bytes_e_nao_por_contagem(monkeypatch):
    """Um único processo gigante tem que fechar o lote sozinho.

    Contando DOCUMENTOS, 500 processos de ente público (centenas de partes
    concatenadas em `partes`/`advs`) viram um corpo acima do
    `http.max_content_length` do ES e o job morre com 413 — foi assim que
    45.313 publicações ficaram fora do índice do lado das movimentações.

    O teste força `BULK_MAX_BYTES` pequeno e exige MAIS DE UM `_bulk` para
    3 processos: se alguém voltar a fechar por contagem, sai 1 só e o teste
    quebra.
    """
    from search import jobs
    from tribunals.models import Parte, Process, ProcessoParte, Tribunal

    t, _ = Tribunal.objects.get_or_create(
        sigla='TJWW', defaults={'nome': 'TJWW', 'sigla_djen': 'TJWW'})
    pks = []
    for i in range(3):
        p = Process.objects.create(numero_cnj=f'000000{i}-99.2025.8.26.0100',
                                   tribunal=t)
        parte = Parte.objects.create(nome=f'ENTE {i} ' + 'F' * 200, tipo='pj')
        ProcessoParte.objects.create(processo=p, parte=parte, polo='passivo',
                                     papel='EXECUTADO')
        pks.append(p.pk)

    envios: list[int] = []

    def falso_enviar(ops, rotulo='x'):
        envios.append(len(ops) // 2)
        return len(ops) // 2

    monkeypatch.setattr(jobs, '_enviar_bulk', falso_enviar)
    monkeypatch.setattr(jobs, 'BULK_MAX_BYTES', 6_000)

    aceitos = jobs.indexar_processos_bulk(pks)

    assert len(envios) > 1, (
        'os 3 documentos couberam num `_bulk` só com teto de 6 KB — o lote '
        'voltou a fechar por CONTAGEM de documentos. Ver o 413 de 21/08/2026.')
    assert sum(envios) == 3
    assert aceitos == 3, (
        'indexar_processos_bulk voltou a esconder quantos documentos entraram; '
        'devolver None é a diferença entre "mandei 500" e "entraram 400".')


def test_indexar_processos_bulk_devolve_o_que_o_es_aceitou(monkeypatch):
    """Devolver `None` escondia a perda. O chamador tem que poder comparar."""
    from search import jobs
    from tribunals.models import Process, Tribunal

    t, _ = Tribunal.objects.get_or_create(
        sigla='TJVV', defaults={'nome': 'TJVV', 'sigla_djen': 'TJVV'})
    pks = [Process.objects.create(numero_cnj=f'000001{i}-99.2025.8.26.0100',
                                  tribunal=t).pk for i in range(4)]

    monkeypatch.setattr(jobs, '_enviar_bulk',
                        lambda ops, rotulo='x': (len(ops) // 2) - 1)  # 1 recusado
    assert jobs.indexar_processos_bulk(pks) == 3
    assert jobs.indexar_processos_bulk([]) == 0
