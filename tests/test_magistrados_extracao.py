"""O extrator de magistrado — os casos REAIS que ele tem que separar.

Todo texto aqui é recorte de publicação REAL do acervo, colhida em 03/09/2026
(`manage.py medir_magistrados`). Os nomes são públicos: quem assinou o ato num
diário oficial assinou em público.

A régua que estes testes protegem é a do CLAUDE.md nº 6 — **abster > chutar**.
Um nome errado numa ficha de magistrado é pior que uma ficha vazia: ele
atribui a uma pessoa um ato que ela não praticou.
"""
import pytest

from tribunals.services import magistrados as mag

# --------------------------------------------------------------------------- #
# 1. e-SAJ de 2º grau — a jazida (5.631.275 publicações do TJSP)
# --------------------------------------------------------------------------- #
ESAJ = ('PEDRO PAULO MAILLET PREUSS Relator - Magistrado(a) Pedro Paulo Maillet '
        'Preuss - Advs: Julia Spadoni Mahfuz (OAB: 407982/SP)')
ESAJ_COM_CARGO = ('. - Magistrado(a) Roberto Mac Cracken (Pres. Seção de D. '
                  'Privado) - Advs: Fabio Teixeira Ozi (OAB: 172594/SP)')
ESAJ_SEM_NOME = ('codigoNoticia=112920 - Magistrado(a)  - Advs: Camila Costa '
                 'Duarte (OAB: 92737/RS) - 3º andar')
ESAJ_SEM_ADVS = ('. - Magistrado(a) Alvaro Passos - Deram provimento em parte '
                 'ao recurso. V. U.')


def test_esaj_le_o_nome_do_cabecalho():
    assert mag.extrair(ESAJ) == ['Pedro Paulo Maillet Preuss']


def test_esaj_a_mesma_pessoa_em_dois_formatos_conta_UMA_vez():
    """O cabeçalho imprime o nome duas vezes ('… PREUSS Relator - Magistrado(a)
    Pedro Paulo Maillet Preuss'). A unidade é a PESSOA, não a ocorrência."""
    assert len(mag.ler(ESAJ).atribuicoes) == 1


def test_esaj_cargo_entre_parenteses_sai_do_nome_e_nao_some():
    (a,) = mag.ler(ESAJ_COM_CARGO).atribuicoes
    assert a.nome == 'Roberto Mac Cracken'
    assert a.cargo == 'Pres. Seção de D. Privado'


def test_esaj_sem_nome_ABSTEM_e_a_abstencao_e_contada():
    leitura = mag.ler(ESAJ_SEM_NOME)
    assert leitura.atribuicoes == []
    assert leitura.abstencoes >= 1, 'abstenção sem número é descarte mudo'


def test_esaj_nao_depende_do_bloco_de_advogados():
    """`- Advs:` é o terminador mais comum, não o único: quando o acórdão foi
    julgado o que vem depois é o dispositivo."""
    assert mag.extrair(ESAJ_SEM_ADVS) == ['Alvaro Passos']


# --------------------------------------------------------------------------- #
# 2. Assinatura de 1º grau — O CASO QUE ORIGINOU O TRABALHO
# --------------------------------------------------------------------------- #
#: TJSP, `Foro Regional XV - Butantã - Vara Reg.Oeste de Viol. Dom. e
#: Fam.Cont.Mulher`, publicação de 2026-01-30. A juíza do caso concreto **não**
#: aparece no formato `Magistrado(a)` — o 1º grau assina com o cargo colado ao
#: nome. Quem construísse a ficha só sobre a jazida de 5,6 M do e-SAJ entregaria
#: uma tela que não responde à pergunta que a motivou.
PRIMEIRO_GRAU = ('Intime-se. RAFAELA CALDEIRA GONÇALVES Juíza de Direito. - '
                 'ADV: DANILO ANSELMO ZERBATO (OAB 439767/SP)')


def test_primeiro_grau_e_o_caso_concreto():
    (a,) = mag.ler(PRIMEIRO_GRAU).atribuicoes
    assert a.nome == 'RAFAELA CALDEIRA GONÇALVES'
    assert a.formato == mag.FORMATO_ASSINATURA
    assert a.cargo == 'Juíza de Direito'


def test_menção_ao_nome_nao_e_assinatura():
    """A MESMA juíza citada por outro juízo, sem ter decidido nada ali.

    É por isso que a ficha conta atribuição por marcador e não ocorrência do
    nome: `match_phrase` devolveria esta publicação como se fosse dela."""
    texto = ('reconhece-se a prevenção daquela Magistrada para apreciação do '
             'presente feito, com redistribuição à Vara sob titularidade da '
             'Juíza preventa.')
    assert mag.extrair(texto) == []


def test_nome_com_conectivo_em_caixa_baixa_nao_e_partido():
    assert mag.extrair('conclusos para decisão inicial.   Airton Vargas da '
                       'Silva, Juiz de Direito') == ['Airton Vargas da Silva']


def test_pontuacao_da_fonte_nao_entra_no_nome():
    (a,) = mag.ler('… Airton Vargas da Silva, Juiz de Direito').atribuicoes
    assert not a.nome.endswith(',')


# --------------------------------------------------------------------------- #
# 3. Citação NÃO é assinatura — com controle positivo
# --------------------------------------------------------------------------- #
CITACAO_STJ = ('improvido. (STJ - AgRg no AREsp: 1683006 SC 2020/0070352-5, '
               'Relator: Ministro NEFI CORDEIRO, Data de Julgamento: '
               '04/08/2020, T6 - SEXTA TURMA)')
CITACAO_TJDFT = ('(Acórdão 1792182, 0735554-80.2023.8.07.0000, Relator(a): '
                 'DIAULAS COSTA RIBEIRO, 2ª CÂMARA CÍVEL, data de julgamento: '
                 '27/11/2023.)')


@pytest.mark.parametrize('texto', [CITACAO_STJ, CITACAO_TJDFT])
def test_precedente_citado_entre_parenteses_e_recusado(texto):
    """Sem esta guarda, um ministro do STJ vira autor de um ato de vara."""
    leitura = mag.ler(texto)
    assert leitura.atribuicoes == []
    assert leitura.erros.get('citacao', 0) >= 1


def test_controle_positivo_o_MESMO_rotulo_fora_do_parenteses_e_lido():
    """A guarda não pode ser 'recusar tudo': o cabeçalho usa o mesmo rótulo."""
    assert mag.extrair('1ª TURMA  Relatora: MARIA ROSELI MENDES ALENCAR  '
                       'ROT 0000387-57.2026.5.07.0010') == \
        ['MARIA ROSELI MENDES ALENCAR']


def test_separador_de_campo_nao_e_sobrenome():
    """Espaço duplo separa CAMPO. Colapsar runs fazia `ROT` (a classe do
    processo no TRT) virar o quinto sobrenome da relatora."""
    (a,) = mag.ler('Relatora: MARIA ROSELI MENDES ALENCAR  ROT 0000387-57 '
                   'RECORRENTE: X').atribuicoes
    assert a.nome == 'MARIA ROSELI MENDES ALENCAR'


#: Cabeçalho REAL do 2º grau do TJSP. O rótulo seguinte (`Órgão Julgador:`)
#: vem colado ao nome com UM espaço — o separador de campo não denuncia nada.
CABECALHO_2G = ('Agravo Interno Cível Processo nº 2060275-70.2026.8.26.0000/50000 '
                'Relator(a): PEDRO PAULO MAILLET PREUSS Órgão Julgador: '
                '24ª Câmara de Direito Privado Trata-se de agravo')


def test_rotulo_do_campo_seguinte_nao_entra_no_nome():
    """Medido na amostra de 600 publicações do TJSP: **82 de 542 atribuições
    (15,1%)** têm um rótulo de campo logo depois do nome. Sem a lista fechada
    de rótulos, `… PREUSS Órgão` e `… PREUSS` viram DUAS identidades na unique
    — e os nomes distintos da amostra caíam de 210 para 230."""
    assert mag.extrair(CABECALHO_2G) == ['PEDRO PAULO MAILLET PREUSS']


def test_tratamento_impresso_sai_do_nome_e_fica_registrado():
    (a,) = mag.ler('OAB/SP-336353  Relator: DES. FRANCISCO DE ASSIS PESSANHA '
                   'FILHO  TEXTO:').atribuicoes
    assert a.nome == 'FRANCISCO DE ASSIS PESSANHA FILHO'
    assert a.tratamento == 'DES.'


# --------------------------------------------------------------------------- #
# 4. O que NÃO é nome
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize('texto', [
    # substantivo comum — o texto fala DO magistrado, não nomeia nenhum
    'acompanhado por este magistrado, no sentido da relativização do requisito',
    'ao magistrado é defeso decidir novamente sobre questões já decididas',
    # cargo sem nome nenhum: o TJRN publica a assinatura em branco
    'Cumpra-se. Natal/RN, data registrada no sistema. JUIZ(A) DE DIREITO',
    'Teresina-PI, datado eletronicamente. Juiz(a) de Direito do(a) 4º Juizado',
    # o nome vem DEPOIS do cargo, com tratamento no meio — não dá pra provar
    'De ordem do MM. Juiz de Direito desta vara, Dr. MARCELO TADEU',
    # assinatura de SISTEMA, em caixa alta, que não nomeia ninguém
    'DOCUMENTO DATADO E ASSINADO ELETRONICAMENTE PELO(A) MAGISTRADO(A)',
    # texto corrido em caixa baixa
    'julgados sob a presidência desta Magistrada, nos termos regimentais',
])
def test_abstem_onde_a_fonte_nao_nomeia(texto):
    assert mag.extrair(texto) == []


def test_colegiado_no_plural_nao_vira_sobrenome():
    assert mag.extrair(
        'Participaram do julgamento os Senhores Desembargadores Jorge Rachid '
        'Mubárack Maluf, Relator, José de Ribamar Fróz Sobrinho') == \
        ['Jorge Rachid Mubárack Maluf']


# --------------------------------------------------------------------------- #
# 5. HTML — o corpo escapado do TJGO
# --------------------------------------------------------------------------- #
def test_corpo_html_escapado_e_lido_inteiro():
    """Sem `html.unescape` o nome sai truncado em 'RENATO C'."""
    assert mag.extrair('assinado digitalmente)&nbsp; RENATO C&Eacute;SAR DORTA '
                       'PINHEIRO Juiz de Direito') == \
        ['RENATO CÉSAR DORTA PINHEIRO']


def test_tabela_html_do_tjpr():
    assert mag.extrair(
        '<tr><td><strong>Relator(a):</strong></td>'
        '<td>Camila Henning Salmoria</td></tr>') == ['Camila Henning Salmoria']


# --------------------------------------------------------------------------- #
# 6. O GABARITO MECÂNICO — o quádruplo desta missão
# --------------------------------------------------------------------------- #
TODOS_OS_TEXTOS = [
    ESAJ, ESAJ_COM_CARGO, ESAJ_SEM_ADVS, PRIMEIRO_GRAU, CITACAO_STJ,
    CABECALHO_2G,
    'assinado digitalmente)&nbsp; RENATO C&Eacute;SAR DORTA PINHEIRO Juiz de Direito',
    'Brasília, 16 de junho de 2026. Sandra Reves Vasques Tonussi Relatora',
    'Morretes, 15 de julho de 2026   Fernando Andriolli Pereira Magistrado',
]


@pytest.mark.parametrize('texto', TODOS_OS_TEXTOS)
def test_todo_nome_devolvido_e_verbatim_no_texto_limpo(texto):
    """Se um dia alguém normalizar caixa, acento ou espaço DENTRO do extrator,
    este teste reprova — é o mesmo papel do quádruplo da DEPRE."""
    limpo = mag.limpar(texto)
    for a in mag.ler(texto).atribuicoes:
        assert a.verbatim_ok(limpo), f'{a.nome!r} não é a fatia que diz ser'


def test_o_gabarito_reprova_um_nome_fabricado():
    """Controle negativo: catraca que nunca reprovou não se sabe se trava."""
    limpo = mag.limpar(PRIMEIRO_GRAU)
    falsa = mag.Atribuicao(nome='RAFAELA CALDEIRA GONCALVES',  # sem cedilha
                           formato=mag.FORMATO_ASSINATURA, inicio=11, fim=37)
    assert not falsa.verbatim_ok(limpo)


# --------------------------------------------------------------------------- #
# 7. Identidade — nome sozinho NÃO identifica magistrado
# --------------------------------------------------------------------------- #
def test_grafias_diferentes_colapsam_na_mesma_chave():
    chaves = {mag.normalizar_nome_magistrado(n) for n in (
        'Rafaela Caldeira Gonçalves',
        'RAFAELA CALDEIRA GONÇALVES',
        'Dra. Rafaela Caldeira Gonçalves',
        'Rafaela Caldeira Goncalves')}
    assert chaves == {'RAFAELA CALDEIRA GONCALVES'}


def test_conectivo_fica_fora_da_chave_mas_dentro_do_nome_exibido():
    assert mag.normalizar_nome_magistrado('José de Ribamar Fróz Sobrinho') == \
        'JOSE RIBAMAR FROZ SOBRINHO'


def test_a_chave_do_orgao_normaliza_pontuacao_e_nao_unifica_tribunais():
    a = mag.normalizar_orgao('Foro Regional XV - Butantã - Vara Reg.Oeste de '
                             'Viol. Dom. e Fam.Cont.Mulher')
    b = mag.normalizar_orgao('FORO REGIONAL XV BUTANTA VARA REG OESTE DE VIOL '
                             'DOM E FAM CONT MULHER')
    assert a == b
    # e órgãos de tribunais diferentes continuam DIFERENTES — é a razão de a
    # identidade ser (tribunal, órgão, nome) e nunca só o nome: medido em
    # 03/09/2026, 56 de 195 publicações com "Rafaela Caldeira Gonçalves" são
    # de TJCE/TJRO/TJPE/TJPI/TJMA — outras pessoas.
    assert mag.normalizar_orgao('1ª Vara Cível de Fortaleza') != a


# --------------------------------------------------------------------------- #
# 8. Inventário de marcadores — a perna A, contada FORA do extrator
# --------------------------------------------------------------------------- #
def test_o_marcador_impresso_e_contado_mesmo_quando_nao_vira_nome():
    """`.ia/DIARIOS.md` §18: 'não medido' e 'medido e ok' não podem ter a
    mesma cara. Marcador impresso sem nome é ABSTENÇÃO, não ausência."""
    leitura = mag.ler(ESAJ_SEM_NOME)
    assert leitura.marcadores_vistos.get(mag.FORMATO_ESAJ) == 1
    assert leitura.atribuicoes == []


def test_texto_sem_marcador_nenhum_nao_inventa_abstencao():
    leitura = mag.ler('Fica a parte intimada para, no prazo de 15 dias, '
                      'manifestar-se sobre os documentos juntados.')
    assert leitura.marcadores_vistos == {}
    assert leitura.atribuicoes == []
    assert leitura.abstencoes == 0


def test_texto_vazio_nao_levanta():
    for t in (None, '', '   ', '<p></p>'):
        assert mag.ler(t).atribuicoes == []
