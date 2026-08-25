"""O `)` que o PJe não fecha, e a hierarquia que virava nome de assunto.

O incidente (auditoria de completude do DADO, 24-25/08/2026; medição própria
em 25/08/2026 sobre 480.000 pks de produção, 12 âncoras, semente 20260825):

  · O detalhe do PJe entrega o assunto hierárquico com o código da folha SEM o
    parêntese de fecho — `… Auxílio-Acidente (Art. 86) (6107`. O regex do
    drainer exigia o fecho, então `assunto_codigo` ia para `''` e
    `assunto_nome` recebia a HIERARQUIA INTEIRA, truncada em 255. Dos 120.755
    processos da amostra com nome e sem código, o regex ANTIGO recuperava
    **zero**; o novo recupera **105.690 (87,5%)** — ≈ 8,2 M no acervo, sem uma
    requisição de rede.

  · A classe passava porque o PJe fecha `PROCEDIMENTO COMUM CÍVEL (7)` direito.
    Daí a assimetria da matriz da auditoria (TRF3: classe cod 70,5% x assunto
    cód 3,6%).

  · Efeito colateral no catálogo: **1.648 de 2.677** linhas de `Assunto` tinham
    a hierarquia inteira no lugar do nome da folha — o dropdown de filtro da
    tela mostrava `DIREITO ADMINISTRATIVO… (9985) - Atos Administrativos (9997)
    - Licenças (9998)` como se fosse o nome do assunto. `ClasseJudicial` está
    limpo (0 de 658).

  · O código recuperado é o certo, conferido contra fonte INDEPENDENTE: 150 de
    150 pares (código → nome da folha) sorteados batem com o `voyager-acervo`,
    o esqueleto nacional lido do Datajud. Controle negativo do mesmo teste: o
    nome hierárquico bate **0 de 150**.

O que cada asserção trava (falha se o defeito voltar):
  1. assunto sem fecho perdendo o código;
  2. hierarquia virando nome de assunto;
  3. dígito solto no MEIO do texto virando código (o pitfall do regex
     permissivo `(.*?)(?:\\s*\\(?\\s*(\\d{2,5})\\s*\\)?)?`, que NÃO pode voltar);
  4. corte por ` - ` de UM espaço, que mutila nome de folha legítimo;
  5. texto truncado em 255 fazendo o código de um ANCESTRAL passar por folha.
"""
from enrichers.drainer import (
    CLASSE_COM_CODIGO_RE,
    normalize_dados,
    split_assunto_folha,
)
from enrichers.management.commands.backfill_assunto import planejar

# Texto REAL de produção (Process id=50152207, TJSP), com o `(` aberto no fim.
REAL_SEM_FECHO = (
    'DIREITO ADMINISTRATIVO E OUTRAS MATÉRIAS DE DIREITO PÚBLICO (9985)'
    '  -  Servidor Público Civil (10219)  -  Pensão (10250)  -  Concessão (10252'
)
# Texto REAL multi-assunto: as hierarquias vêm separadas por LF + 4 espaços.
REAL_MULTI = (
    'DIREITO CIVIL (899)  -  Responsabilidade Civil (10431)  -  Indenização por '
    'Dano Material (10439)  -  Acidente de Trânsito (10441\n'
    '    DIREITO DO CONSUMIDOR (1156)  -  Responsabilidade do Fornecedor (6220)'
)


# ---------- 1. o código que se perdia ----------

def test_folha_sem_parentese_de_fecho_recupera_codigo():
    """O defeito de origem: `(10252` sem `)` ia para código vazio."""
    nome, codigo = split_assunto_folha(REAL_SEM_FECHO)
    assert codigo == '10252', (
        'o código da folha voltou a se perder quando o PJe não fecha o '
        'parêntese — eram 8,2 M de processos recuperáveis sem uma requisição'
    )
    assert nome == 'Concessão'


def test_folha_com_parentese_de_fecho_continua_funcionando():
    """Controle positivo do outro lado: quem fecha o parêntese não regride.

    Nota medida: o regex exige 2 a 5 dígitos, então código de UM dígito (a
    classe 7, `PROCEDIMENTO COMUM CÍVEL`) continua fora — limitação HERDADA,
    não introduzida aqui, e registrada em .ia/ACERVO_CNJ.md.
    """
    assert split_assunto_folha('Aposentadoria por Invalidez (6095)') == (
        'Aposentadoria por Invalidez', '6095')
    assert split_assunto_folha('Procedimento Comum (7)') == (
        'Procedimento Comum (7)', ''), 'código de 1 dígito passou a casar'


# ---------- 2. hierarquia não é nome ----------

def test_nome_e_a_folha_nao_a_hierarquia():
    """1.648 de 2.677 linhas do catálogo mostravam a hierarquia no dropdown."""
    nome, codigo = split_assunto_folha(
        'DIREITO ADMINISTRATIVO E OUTRAS MATÉRIAS DE DIREITO PÚBLICO (9985)'
        '  -  Atos Administrativos (9997)  -  Licenças (9998)'
    )
    assert nome == 'Licenças', f'nome voltou a ser a hierarquia inteira: {nome!r}'
    assert codigo == '9998'


def test_multi_assunto_usa_a_primeira_hierarquia():
    """O PJe empilha assuntos separados por `\\n` (41,6% dos casos sem código).

    Tratar o texto como UMA string casa o fim da ÚLTIMA hierarquia — e quando o
    campo bateu no teto de 255 esse fim é o código de um ANCESTRAL da segunda,
    não a folha da primeira. Era a origem dos "1.515 com código de ancestral".
    """
    nome, codigo = split_assunto_folha(REAL_MULTI)
    assert (nome, codigo) == ('Acidente de Trânsito', '10441')
    assert codigo != '6220', 'pegou o código de um ancestral da 2ª hierarquia'


def test_truncado_em_255_com_multi_assunto_ainda_recupera_a_primeira():
    """O `\\n` prova que a 1ª hierarquia terminou, mesmo com o texto cortado."""
    texto = (REAL_MULTI + ' x' * 300)[:255]
    assert len(texto) == 255 and '\n' in texto
    nome, codigo = split_assunto_folha(texto, truncado=True)
    assert (nome, codigo) == ('Acidente de Trânsito', '10441')


# ---------- 3. dígito solto no meio NÃO é código ----------

def test_digito_no_meio_do_texto_nao_vira_codigo():
    """O pitfall do regex permissivo antigo. Não pode voltar."""
    assert split_assunto_folha('Tributário 12345 algo') == ('Tributário 12345 algo', '')
    assert split_assunto_folha('Procedimento (1234) Comum') == (
        'Procedimento (1234) Comum', ''
    ), 'código só no FIM da string — casar no meio era o bug do regex antigo'
    assert CLASSE_COM_CODIGO_RE.match('Tributário 12345 algo') is None


def test_ano_entre_parenteses_antes_do_codigo_nao_rouba_o_lugar():
    """Nome de folha legítimo com ano: `Plano Verão (1989)`.

    O catálogo tem duas linhas (14196/14197) onde o regex ANTIGO capturou o ANO
    como código porque o parêntese do ano estava fechado e era o último. Com o
    código da folha presente, a folha ganha.
    """
    nome, codigo = split_assunto_folha(
        'DIREITO ADMINISTRATIVO (9985)  -  Servidor Público Civil (10219)'
        '  -  Reajustes de Remuneração / Plano Verão (1989) (14196'
    )
    assert codigo == '14196', f'o ano virou código de novo: {codigo!r}'
    assert nome == 'Reajustes de Remuneração / Plano Verão (1989)'


# ---------- 4. separador é DOIS espaços ----------

def test_hifen_de_um_espaco_nao_corta_o_nome_da_folha():
    """`Crédito Direto ao Consumidor - CDC` é UM nome, não dois níveis.

    O separador do PJe é `  -  ` (dois espaços de cada lado): medido em 100%
    (5.598/5.598) dos assuntos sem código de uma janela de 200 mil pks.
    """
    nome, codigo = split_assunto_folha(
        'DIREITO DO CONSUMIDOR (1156)  -  Contratos de Consumo (7771)'
        '  -  Bancários (7752)  -  Crédito Direto ao Consumidor - CDC (14757'
    )
    assert nome == 'Crédito Direto ao Consumidor - CDC'
    assert codigo == '14757'


def test_formato_sem_codigo_nenhum_abstem():
    """Visto em produção (TJDFT): hierarquia com ` - ` e nenhum código.

    Sem código não dá para provar onde termina o nível — abster > chutar.
    """
    texto = ('DIREITO ADMINISTRATIVO E OUTRAS MATÉRIAS DE DIREITO PÚBLICO'
             ' - Serviços - Saúde - Ressarcimento ao SUS')
    assert split_assunto_folha(texto) == (texto, '')


# ---------- 5. truncamento sem prova: abstém ----------

def test_truncado_em_255_sem_quebra_de_linha_abstem():
    """Texto de exatos 255 sem `\\n`: o `(1234` do fim pode ser um ancestral
    cortado. Abstém (regra nº 6) em vez de gravar o código errado."""
    texto = ('DIREITO PREVIDENCIÁRIO (195)  -  RMI - Renda Mensal Inicial (6119'
             + 'x' * 300)[:255]
    assert len(texto) == 255 and '\n' not in texto
    nome, codigo = split_assunto_folha(texto, truncado=True)
    assert codigo == '', 'gravou código a partir de texto que pode estar cortado'
    assert nome == texto


def test_texto_fresco_do_enricher_nao_e_tratado_como_truncado():
    """`truncado` só vale para texto vindo de coluna varchar(255).

    O caminho ao vivo recebe o texto inteiro do PJe; tratá-lo como truncado
    jogaria fora código bom por coincidência de comprimento.
    """
    texto = 'DIREITO CIVIL (899)  -  Obrigações (7681)  -  ' + 'A' * 255 + ' (9607'
    assert len(texto) > 255
    assert split_assunto_folha(texto)[1] == '9607'


# ---------- normalize_dados: o caminho ao vivo ----------

def test_normalize_dados_assunto_hierarquico_grava_folha_e_codigo():
    out = normalize_dados({'assunto': REAL_SEM_FECHO})
    assert out['assunto_codigo'] == '10252'
    assert out['assunto_nome'] == 'Concessão'


def test_normalize_dados_classe_nao_passa_pelo_split_de_folha():
    """Classe não tem hierarquia (0 de 658 linhas do catálogo têm `(código)`).
    O nome tem que sobreviver inteiro, com hífen e tudo."""
    out = normalize_dados({'classe': 'Cumprimento de Sentença - Fazenda (12078)'})
    assert out['classe_nome'] == 'Cumprimento de Sentença - Fazenda'
    assert out['classe_codigo'] == '12078'


def test_normalize_dados_assunto_sem_codigo_preserva_o_codigo_existente():
    """Guard de 2026-07-06: e-SAJ dá só o nome, e apagar o código herdado do
    DJEN revertia leads a NAO_LEAD."""
    out = normalize_dados({'assunto': 'DIREITO TRIBUTÁRIO'})
    assert out == {'assunto_nome': 'DIREITO TRIBUTÁRIO'}
    assert 'assunto_codigo' not in out


# ---------- o plano do backfill ----------

def test_planejar_recupera_quando_nao_havia_codigo():
    veredito, campos = planejar(1, REAL_SEM_FECHO, '', None)
    assert veredito == 'recupera'
    assert campos == {'assunto_codigo': '10252', 'assunto_nome': 'Concessão',
                      'assunto_id': '10252'}


def test_planejar_corrige_so_o_nome_quando_o_codigo_ja_existe():
    """Caso do catálogo: o regex antigo tirou o `(10252)` e deixou a hierarquia
    como nome. O código veio DESTE texto, então o par é consistente."""
    sem_cod = REAL_SEM_FECHO.rsplit(' (', 1)[0]
    veredito, campos = planejar(1, sem_cod, '10252', '10252')
    assert veredito == 'folha'
    assert campos == {'assunto_nome': 'Concessão'}


def test_planejar_abstem_quando_codigo_guardado_diverge_do_texto():
    """418 de 88.825 (0,47%) da amostra: dois writers discordam sobre QUAL
    assunto é o do processo. Escolher no chute é pior que deixar como está."""
    veredito, campos = planejar(1, REAL_MULTI, '7780', '7780')
    assert veredito == 'conflito'
    assert campos == {}, 'escreveu por cima de um conflito não resolvido'


def test_planejar_abstem_no_truncado_sem_prova():
    texto = ('DIREITO PREVIDENCIÁRIO (195)  -  RMI (6119' + 'x' * 300)[:255]
    veredito, campos = planejar(1, texto, '', None)
    assert veredito == 'abstem'
    assert campos == {}
