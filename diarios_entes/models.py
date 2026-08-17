"""Publicação de diário oficial de ENTE DEVEDOR (Executivo estadual/municipal).

POR QUE UM MODEL PRÓPRIO, E NÃO `Movimentacao`
==============================================
Três motivos, todos medidos no recon de 16/08/2026 — nenhum é preferência:

1. `Movimentacao.tribunal` é FK NOT NULL e o unique é (tribunal, external_id).
   Publicação do Executivo NÃO tem tribunal. Forçar um tribunal sintético
   contamina o heatmap de saúde da ingestão, o lag por tribunal e o
   `mv_pipeline_diario` — métricas que hoje significam "o DJEN está em dia".

2. Isto não é ato processual. De 30 publicações ALEATÓRIAS do DOE-SP de
   14/08/2026, **0** continham CNJ; o Diário do Executivo de MG do mesmo dia
   (958.880 chars extraídos) tem ZERO menção a precatório e ZERO CNJ. Quem
   esperava "mais um DJEN" vai se frustrar: esta fonte é SINAL DE DESFECHO
   (o ente pagou / convocou para acordo direto / abriu crédito), não porta de
   acervo. Amarrar isso a `Process` como se fosse movimentação mentiria sobre
   o que o dado é.

3. A chave natural aqui é o ENTE (município IBGE / UF), não o juízo. E ela já
   existe no repo: o `territory_id` de 7 dígitos do Querido Diário é o mesmo
   `id_ente` que `dashboard/fontes_publicas.py::ente_fiscal` usa no SICONFI.
   `FonteDiario` também não serve — é OneToOne com `Tribunal` e mapeia diário
   de TRIBUNAL (as 14 linhas de lá são dje-tjsp, dje-trf1..5, dje-stf...).

O VÍNCULO COM O PROCESSO É OPORTUNISTA, NUNCA CHUTADO
-----------------------------------------------------
`cnjs` guarda os números que o TEXTO cita, verbatim. `processos` só é
preenchido com `Process` que JÁ EXISTE no acervo. Não criamos processo a partir
daqui: para criar seria preciso inventar o `tribunal` (FK obrigatória) a partir
dos dígitos J.TR do CNJ, e a regra da casa é abster > chutar. Publicação sem
CNJ entra assim mesmo, solta — é evidência de que o ente publicou algo, e o
extrator ainda pode tirar nome de credor e valor dali.
"""

from django.db import models
from django.db.models import UniqueConstraint

ESFERA_MUNICIPAL = 'municipal'
ESFERA_ESTADUAL = 'estadual'
ESFERA_FEDERAL = 'federal'
ESFERA_CHOICES = [
    (ESFERA_MUNICIPAL, 'Municipal'),
    (ESFERA_ESTADUAL, 'Estadual'),
    (ESFERA_FEDERAL, 'Federal'),
]

#: Por que a publicação entrou no acervo. Não é enfeite: o recon mediu que
#: `precatório` como termo solto casa sobretudo linha de RREO/RGF orçamentário
#: ("31.5- RECEITA DE PRECATÓRIOS - FUNDEF E FUNDEB 1.296.000,00"), enquanto a
#: FRASE "câmara de conciliação de precatórios" deu 64 acertos em 2,5 anos com
#: precisão altíssima. Guardar a confiança na linha permite que a fila de
#: extração/triagem consuma a alta primeiro e trate a baixa como rede.
CONFIANCA_ALTA = 'alta'      # casou frase de alta precisão
CONFIANCA_BAIXA = 'baixa'    # casou só termo solto (ruído orçamentário provável)
CONFIANCA_CHOICES = [(CONFIANCA_ALTA, 'Alta (frase)'), (CONFIANCA_BAIXA, 'Baixa (termo)')]


class PublicacaoOficial(models.Model):
    """Um ato publicado no diário oficial de um ente devedor."""

    #: slug do coletor que trouxe ('qd-municipal', 'doe-sp'). Mesmo namespace
    #: do `external_id` de `diarios/base.py` — é o discriminador de origem.
    fonte = models.CharField(max_length=16)
    #: `<slug>:<coordenada determinística na fonte>`. Nunca ordinal de laço,
    #: nunca timestamp: re-coletar o mesmo dia tem que produzir o mesmo id,
    #: senão o backfill duplica em vez de deduplicar.
    external_id = models.CharField(max_length=120)

    esfera = models.CharField(max_length=12, choices=ESFERA_CHOICES)
    #: nome do ente devedor como a fonte o chama ('Maceió', 'Estado de São Paulo').
    ente = models.CharField(max_length=160)
    uf = models.CharField(max_length=2, blank=True)
    #: código IBGE de 7 dígitos (só municipal). É a chave que casa com o
    #: `id_ente` do SICONFI já usado em `dashboard/fontes_publicas.py`.
    territory_id = models.CharField(max_length=7, blank=True)

    #: data de publicação NO DIÁRIO DO ENTE. Semanticamente é publicação
    #: administrativa — não é intimação judicial, não vale como marco de prazo.
    data_publicacao = models.DateField()
    titulo = models.CharField(max_length=300, blank=True)
    #: órgão do EXECUTIVO (não juízo): 'Procuradoria-Geral do Estado',
    #: 'Secretaria da Segurança Pública > Diretoria de Pessoal'.
    orgao = models.CharField(max_length=255, blank=True)
    tipo_documento = models.CharField(max_length=120, blank=True)
    edicao = models.CharField(max_length=40, blank=True)

    link = models.URLField(max_length=500, blank=True)         # página humana
    link_texto = models.URLField(max_length=500, blank=True)   # .txt/.pdf integral

    #: VERBATIM. Para a gazeta municipal (média de 772.500 chars, uma delas com
    #: 6,6 MB) guardamos a JANELA em torno das ocorrências, não o diário inteiro
    #: — o resto é licitação, folha e decreto de trânsito, e são ~10 documentos
    #: por dia no país todo contra um Postgres que a doc já classifica como
    #: disk-I/O-bound. `link_texto` mantém o integral sempre recuperável e
    #: `texto_integral_chars` diz de quanto foi recortado.
    texto = models.TextField()
    texto_integral_chars = models.IntegerField(default=0)
    texto_completo = models.BooleanField(default=False)

    #: quais consultas casaram (a evidência de POR QUE isto foi coletado).
    consultas = models.JSONField(default=list, blank=True)
    confianca = models.CharField(max_length=8, choices=CONFIANCA_CHOICES, default=CONFIANCA_BAIXA)

    #: CNJs citados no texto, na ordem de aparição. Vazio é resposta legítima e
    #: FREQUENTE (0/30 em publicação aleatória do DOE-SP).
    cnjs = models.JSONField(default=list, blank=True)
    #: só a CONTAGEM de CPFs NO TRECHO GUARDADO (não na gazeta inteira, que tem
    #: 19 no caso de Maceió e é quase toda assunto de outro órgão). Guardar o
    #: número em si seria replicar PII de graça num banco que não precisa dela —
    #: o CPF continua no `texto` verbatim quando está no ato, mas não vira
    #: coluna indexável.
    cpfs_no_texto = models.PositiveIntegerField(default=0)

    #: vínculo OPORTUNISTA: só processos que já existem no acervo. Nunca cria.
    processos = models.ManyToManyField(
        'tribunals.Process', blank=True, related_name='publicacoes_oficiais',
    )

    coletado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            UniqueConstraint(fields=['fonte', 'external_id'], name='uniq_pubof_fonte_extid'),
        ]
        indexes = [
            models.Index(fields=['fonte', '-data_publicacao']),
            models.Index(fields=['territory_id', '-data_publicacao']),
            models.Index(fields=['uf', '-data_publicacao']),
            models.Index(fields=['confianca', '-data_publicacao']),
        ]
        ordering = ['-data_publicacao', 'ente']
        verbose_name = 'Publicação oficial de ente'
        verbose_name_plural = 'Publicações oficiais de entes'

    def __str__(self):
        return f'{self.ente}/{self.data_publicacao} · {self.titulo[:60]}'
