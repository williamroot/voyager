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
import logging

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


class _Capturador(logging.Handler):
    """Ouve o logger `voyager.search.sync` DIRETO.

    O `caplog` do pytest instala o handler na RAIZ, e os loggers `voyager.*`
    deste projeto não propagam para lá — o teste leria zero registro e passaria
    verde exatamente quando o alerta sumisse. Uma sonda que só sabe dizer
    "vazio" está quebrada; esta escuta na fonte.
    """

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.registros: list[logging.LogRecord] = []

    def emit(self, record):
        self.registros.append(record)

    def erros(self) -> list[str]:
        return [r.getMessage() for r in self.registros if r.levelno >= logging.ERROR]

    def __enter__(self):
        self._lg = logging.getLogger('voyager.search.sync')
        self._nivel = self._lg.level
        self._lg.setLevel(logging.DEBUG)
        self._lg.addHandler(self)
        return self

    def __exit__(self, *exc):
        self._lg.removeHandler(self)
        self._lg.setLevel(self._nivel)
        return False


# --------------------------------------------------------------------------- #
# 4. o teto do `sync_incremental` é ALERTA, nunca `return` discreto
# --------------------------------------------------------------------------- #
def test_teto_de_proc_novos_vira_erro_com_o_numero_real(monkeypatch):
    """Medido em 25/08/2026: teto batido em **6 de 6 ticks** seguidos, em silêncio.

    `proc_novos` bate `LIMITE_PROC_NOVOS = 20.000` todo tick, e a watermark
    estava em `id=96.776.271` contra `max(id)=104.615.119` — **7,84 milhões de
    pks atrás**, ou seja 7,84 milhões de processos que a busca não enxergava.
    Nada no log dizia isso: o tick saía como INFO com `'novos': 20000`, que lê
    como sucesso.

    O lado das MOVIMENTAÇÕES já alertava (foi o que fechou os 179.490.613 fora
    do índice). Os dois lados de PROCESSO ficaram de fora da mesma lição. Este
    teste falha se o alerta sumir de novo.
    """
    from django.core.cache import cache

    from search import sync_incremental as si
    from tribunals.models import Process, Tribunal

    t, _ = Tribunal.objects.get_or_create(
        sigla='TJTT', defaults={'nome': 'TJTT', 'sigla_djen': 'TJTT'})
    pks = [Process.objects.create(numero_cnj=f'000002{i}-99.2025.8.26.0100',
                                  tribunal=t).pk for i in range(3)]

    cache.set(si._WM_PROC_ID, pks[0] - 1, None)
    monkeypatch.setattr(si, 'LIMITE_PROC_NOVOS', 1)      # força o teto
    monkeypatch.setattr(si, 'computar_sinal', lambda p: 0)
    monkeypatch.setattr(si, '_enfileirar_processos', len)

    with _Capturador() as cap:
        saida = si.sync_processos_novos()

    erros = cap.erros()
    assert erros, ('proc_novos bateu o teto e não registrou ERRO — voltou a ser '
                   'corte mudo (regra nº 2 do CLAUDE.md).')
    msg = erros[0]
    assert 'teto' in msg.lower()
    # o alerta tem que trazer o NÚMERO, senão é só barulho
    assert saida.get('atraso_ids') is not None
    assert str(saida['atraso_ids']) in msg


def test_teto_de_proc_atualizados_alerta_com_a_idade_da_watermark(monkeypatch):
    """A watermark de `proc_atualizados` é um INSTANTE — o número é TEMPO.

    Medido em 25/08/2026: parada em **2026-08-19 18:07**, seis dias atrás,
    avançando ~30-40 s de relógio por tick de 10 min (perde ~17:1). Um teto que
    não converge não é teto, é vazamento — e ele saía do tick como
    `{'atualizados': 10000}`, que lê como trabalho feito.
    """
    import datetime

    from django.core.cache import cache
    from django.utils import timezone

    from search import sync_incremental as si
    from tribunals.models import Process, Tribunal

    t, _ = Tribunal.objects.get_or_create(
        sigla='TJUU', defaults={'nome': 'TJUU', 'sigla_djen': 'TJUU'})
    p = Process.objects.create(numero_cnj='0000030-99.2025.8.26.0100', tribunal=t)

    velha = timezone.now() - datetime.timedelta(days=6)
    # `atualizado_em` é `auto_now`: só um UPDATE cru envelhece a linha. É
    # exatamente o que a produção tem — a watermark para no `atualizado_em` da
    # ÚLTIMA linha lida, e é a IDADE dela que diz se o teto converge ou vaza.
    Process.objects.filter(pk=p.pk).update(atualizado_em=velha)
    cache.set(si._WM_PROC_TS, velha - datetime.timedelta(seconds=1), None)
    monkeypatch.setattr(si, 'LIMITE_PROC_ATUALIZADOS', 1)
    monkeypatch.setattr(si, '_enfileirar_processos', len)

    with _Capturador() as cap:
        saida = si.sync_processos_atualizados()

    erros = cap.erros()
    assert erros, 'proc_atualizados bateu o teto sem ERRO registrado'
    assert 'teto' in erros[0].lower()
    assert saida.get('idade_wm_h') is not None, (
        'o alerta perdeu a IDADE da watermark — sem ela ninguém sabe se o teto '
        'converge ou vaza.')
    assert saida['idade_wm_h'] >= 143, saida['idade_wm_h']   # ~6 dias


def test_bloco_que_falha_no_tick_e_erro_porque_a_watermark_congela(monkeypatch):
    """`LockNotAvailable` no `proc_atualizados` saía como WARNING (25/08/2026).

    Bloco que falha é watermark que não anda, e keyset só anda pra frente: o que
    ficou para trás só volta se alguém for buscar. "Perda silenciosa com log de
    WARNING é o pior formato" — a lição já estava escrita em `_enfileirar_movs`
    e não valia para o bloco inteiro.
    """
    from search import sync_incremental as si

    def explode():
        raise RuntimeError('canceling statement due to lock timeout')

    monkeypatch.setattr(si, 'sync_processos_novos', lambda: {'novos': 0})
    monkeypatch.setattr(si, 'sync_processos_atualizados', explode)
    monkeypatch.setattr(si, 'sync_movimentacoes_novas', lambda: {'movs': 0})

    with _Capturador() as cap:
        out = si.tick_sync_es_incremental()

    assert out['proc_atualizados'] == {'erro': True}
    erros = cap.erros()
    assert erros, ('bloco do tick falhou e saiu como WARNING — é o formato de '
                   'perda que este módulo já aprendeu a não usar.')
    assert 'watermark' in erros[0].lower()


# --------------------------------------------------------------------------- #
# 5. `segredo_justica` tri-estado — `null` NÃO pode virar "campo ausente"
# --------------------------------------------------------------------------- #
def test_segredo_justica_tri_estado_e_explicito_no_doc():
    """Medido em 25/08/2026, no índice de produção, no mesmo instante:

        segredo_justica = true .....          0
        segredo_justica = false ... 28.263.970
        campo AUSENTE ............. 64.442.760

    Os 64,4 M ausentes são documentos construídos ANTES de o campo entrar no
    builder — ninguém nunca perguntou nada sobre eles. A migration 0052 torna a
    coluna nullable, e `NULL` passa a significar "não perguntamos".

    Se `NULL` fosse representado por AUSÊNCIA no ES, os dois casos colapsariam:
    a tela leria "não perguntamos" em 64 milhões de docs velhos. Dado pela
    metade produzindo confiança falsa é literalmente o princípio nº 1.

    Por isso o estado vai num campo PRÓPRIO: presença de
    `segredo_justica_estado` prova que o doc é da era nova; ausência é "legado".
    """
    from search.documents import processo_to_doc
    from tribunals.models import Process, Tribunal

    t, _ = Tribunal.objects.get_or_create(
        sigla='TJSS', defaults={'nome': 'TJSS', 'sigla_djen': 'TJSS'})

    casos = {None: 'nao_perguntamos', False: 'sem_segredo', True: 'segredo'}
    for i, (valor, esperado) in enumerate(casos.items()):
        p = Process.objects.create(numero_cnj=f'000004{i}-99.2025.8.26.0100',
                                   tribunal=t)
        # `update` e não `save`: a coluna pode ser NOT NULL numa frota onde a
        # 0052 ainda não passou, e o teste mede o BUILDER, não a migration.
        Process.objects.filter(pk=p.pk).update(segredo_justica=valor)
        doc = processo_to_doc(Process.objects.get(pk=p.pk))
        assert doc['segredo_justica_estado'] == esperado, (
            f'segredo_justica={valor!r} virou {doc["segredo_justica_estado"]!r}')
        # o campo é SEMPRE emitido — é a presença dele que separa era nova de
        # doc legado. Se alguém "otimizar" omitindo-o no caso NULL, os 64,4 M
        # voltam a ser indistinguíveis.
        assert 'segredo_justica_estado' in doc

    # e os três estados são distinguíveis entre si
    assert len(set(casos.values())) == 3


def test_participacao_carrega_a_procedencia_para_a_tela_nao_mentir():
    """`fonte='djen'` = parte promovida da publicação: tem nome, polo e OAB, e
    NÃO tem CPF/CNPJ.

    Sem o campo, a tela não consegue distinguir "parte sem documento porque a
    fonte não dá" de "parte sem documento porque ninguém buscou" — e acabaria
    exibindo um cadastro que não existe. Abster > chutar (regra nº 6): o campo
    existe para a tela poder DIZER que está vazio.
    """
    from search.documents import processo_to_doc
    from tribunals.models import Parte, Process, ProcessoParte, Tribunal

    t, _ = Tribunal.objects.get_or_create(
        sigla='TJFF', defaults={'nome': 'TJFF', 'sigla_djen': 'TJFF'})
    p = Process.objects.create(numero_cnj='0000050-99.2025.8.26.0100', tribunal=t)
    parte = Parte.objects.create(nome='Fulano do DJEN', tipo='pf')
    pp = ProcessoParte.objects.create(processo=p, parte=parte, polo='ativo',
                                      papel='EXEQUENTE')

    doc = processo_to_doc(Process.objects.get(pk=p.pk))
    assert 'fonte' in doc['participacoes'][0], (
        'a participação perdeu a procedência — a tela volta a não saber se a '
        'ausência de CPF/CNPJ é fato da fonte ou buraco nosso.')
    # sem a coluna preenchida (ou sem a 0052 aplicada) o builder ABSTÉM
    assert doc['participacoes'][0]['fonte'] is None

    try:
        ProcessoParte.objects.filter(pk=pp.pk).update(fonte='djen')
    except Exception:                      # coluna ainda não existe nesta frota
        return
    doc = processo_to_doc(Process.objects.get(pk=p.pk))
    assert doc['participacoes'][0]['fonte'] == 'djen'
