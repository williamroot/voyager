from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.db import models
from django.db.models import Q, UniqueConstraint


class Tribunal(models.Model):
    sigla = models.CharField(max_length=10, primary_key=True)
    nome = models.CharField(max_length=200)
    sigla_djen = models.CharField(max_length=20)
    ativo = models.BooleanField(default=True)
    overlap_dias = models.PositiveIntegerField(default=3)
    data_inicio_disponivel = models.DateField(null=True, blank=True)
    backfill_concluido_em = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['sigla']

    def __str__(self):
        return self.sigla


class ClasseJudicial(models.Model):
    """Catálogo nacional de classes judiciais (TPU/CNJ).

    PK natural é o código TPU. Nome canônico vem preferencialmente do PJe
    (consulta pública), mais limpo que a string do DJEN.
    """

    codigo = models.CharField(max_length=20, primary_key=True)
    nome = models.CharField(max_length=255)
    total_processos = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['nome']
        indexes = [models.Index(fields=['nome'])]

    def __str__(self):
        return f'{self.codigo} · {self.nome}'


class Assunto(models.Model):
    """Catálogo nacional de assuntos processuais (TPU/CNJ)."""

    codigo = models.CharField(max_length=20, primary_key=True)
    nome = models.CharField(max_length=255)
    total_processos = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['nome']
        indexes = [models.Index(fields=['nome'])]

    def __str__(self):
        return f'{self.codigo} · {self.nome}'


class Process(models.Model):
    numero_cnj = models.CharField(max_length=25)
    # ano_cnj é derivado do numero_cnj (NNNNNNN-DD.AAAA.J.TR.OOOO).
    # Mantido por trigger SQL — não setar manualmente.
    ano_cnj = models.PositiveSmallIntegerField(null=True, blank=True)
    tribunal = models.ForeignKey(Tribunal, on_delete=models.PROTECT, related_name='processos')
    primeira_movimentacao_em = models.DateTimeField(null=True, blank=True)
    ultima_movimentacao_em = models.DateTimeField(null=True, blank=True)
    total_movimentacoes = models.PositiveIntegerField(default=0)

    # Enriquecimento via consulta pública do tribunal (TRF1, etc.).
    # Campos string são fonte de verdade na ingestão; FKs são populadas
    # via data migration / signal pra normalizar e habilitar filtros.
    classe_codigo = models.CharField(max_length=20, blank=True)
    classe_nome = models.CharField(max_length=255, blank=True)
    classe = models.ForeignKey(
        ClasseJudicial, on_delete=models.PROTECT, null=True, blank=True,
        related_name='processos',
    )
    assunto_codigo = models.CharField(max_length=20, blank=True)
    assunto_nome = models.CharField(max_length=255, blank=True)
    assunto = models.ForeignKey(
        Assunto, on_delete=models.PROTECT, null=True, blank=True,
        related_name='processos',
    )
    data_autuacao = models.DateField(null=True, blank=True)
    valor_causa = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    orgao_julgador_codigo = models.CharField(max_length=20, blank=True)
    orgao_julgador_nome = models.CharField(max_length=255, blank=True)
    juizo = models.CharField(max_length=255, blank=True)
    segredo_justica = models.BooleanField(default=False)
    enriquecido_em = models.DateTimeField(null=True, blank=True)

    ENRIQ_PENDENTE = 'pendente'
    ENRIQ_OK = 'ok'
    ENRIQ_NAO_ENCONTRADO = 'nao_encontrado'   # PJe não tem (ex: pré-PJe, físico)
    ENRIQ_ERRO = 'erro'                        # falha transitória, pode retentar
    ENRIQ_CHOICES = [
        (ENRIQ_PENDENTE, 'Pendente'),
        (ENRIQ_OK, 'Enriquecido'),
        (ENRIQ_NAO_ENCONTRADO, 'Não encontrado'),
        (ENRIQ_ERRO, 'Erro'),
    ]
    enriquecimento_status = models.CharField(
        max_length=20, choices=ENRIQ_CHOICES, default=ENRIQ_PENDENTE,
    )
    enriquecimento_erro = models.TextField(blank=True)

    inserido_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            UniqueConstraint(fields=['tribunal', 'numero_cnj'], name='uniq_proc_tribunal_cnj'),
        ]
        indexes = [
            models.Index(fields=['tribunal', 'numero_cnj']),
            models.Index(fields=['tribunal', '-ultima_movimentacao_em']),
            models.Index(fields=['inserido_em']),
            models.Index(fields=['enriquecido_em']),
            models.Index(fields=['enriquecimento_status']),
            models.Index(fields=['classe_codigo']),
            models.Index(fields=['classe']),
            models.Index(fields=['assunto']),
            models.Index(fields=['orgao_julgador_codigo']),
            models.Index(fields=['ano_cnj']),
            models.Index(fields=['tribunal', 'ano_cnj']),
        ]

    def __str__(self):
        return f'{self.tribunal_id}/{self.numero_cnj}'


class Parte(models.Model):
    """Pessoa física, jurídica ou advogado. Pode aparecer em N processos."""

    TIPO_PF = 'pf'
    TIPO_PJ = 'pj'
    TIPO_ADV = 'advogado'
    TIPO_DESCONHECIDO = 'desconhecido'
    TIPO_CHOICES = [
        (TIPO_PF, 'Pessoa Física'),
        (TIPO_PJ, 'Pessoa Jurídica'),
        (TIPO_ADV, 'Advogado'),
        (TIPO_DESCONHECIDO, 'Desconhecido'),
    ]

    nome = models.CharField(max_length=255)
    documento = models.CharField(max_length=20, blank=True)        # CPF/CNPJ formatado
    tipo_documento = models.CharField(max_length=10, blank=True)    # 'CPF'|'CNPJ'|''
    oab = models.CharField(max_length=20, blank=True)               # 'SP123456' — advogados
    tipo = models.CharField(max_length=20, choices=TIPO_CHOICES, default=TIPO_DESCONHECIDO)

    primeira_aparicao_em = models.DateTimeField(auto_now_add=True)
    ultima_aparicao_em = models.DateTimeField(auto_now=True)
    total_processos = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            # Doc REAL (sem máscara): único globalmente — confiamos no CPF/CNPJ
            # como PK natural mesmo quando nomes divergem por typo.
            UniqueConstraint(
                fields=['documento'],
                condition=(~Q(documento='')
                           & ~Q(documento__contains='X')
                           & ~Q(documento__contains='x')
                           & ~Q(documento__contains='*')),
                name='uniq_parte_documento_real',
            ),
            # Doc MASCARADO (TRF3 esconde dígitos como '639.XXX.XXX-XX'):
            # único por (nome, doc) — máscaras iguais com nomes diferentes
            # são partes distintas; mesmo nome + mesma máscara colapsa.
            UniqueConstraint(
                fields=['nome', 'documento'],
                condition=(Q(documento__contains='X')
                           | Q(documento__contains='x')
                           | Q(documento__contains='*')),
                name='uniq_parte_documento_mascarado',
            ),
            UniqueConstraint(fields=['oab'], condition=~Q(oab=''),
                             name='uniq_parte_oab'),
        ]
        indexes = [
            models.Index(fields=['nome']),
            models.Index(fields=['documento']),
            models.Index(fields=['oab']),
            models.Index(fields=['tipo']),
        ]

    def __str__(self):
        ident = self.documento or self.oab or '—'
        return f'{self.nome} ({ident})'


class ProcessoParte(models.Model):
    """Participação de uma Parte em um Process (com polo/papel)."""

    POLO_ATIVO = 'ativo'
    POLO_PASSIVO = 'passivo'
    POLO_OUTROS = 'outros'
    POLO_CHOICES = [
        (POLO_ATIVO, 'Polo ativo'),
        (POLO_PASSIVO, 'Polo passivo'),
        (POLO_OUTROS, 'Outros'),
    ]

    processo = models.ForeignKey(Process, on_delete=models.CASCADE, related_name='participacoes')
    parte = models.ForeignKey(Parte, on_delete=models.PROTECT, related_name='participacoes')
    polo = models.CharField(max_length=10, choices=POLO_CHOICES)
    papel = models.CharField(max_length=120, blank=True)            # 'autor', 'réu', 'advogado', etc.
    representa = models.ForeignKey('self', on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name='representado_por')
    # ^ Advogado: aponta pra ProcessoParte da pessoa representada no mesmo processo.

    inserido_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            UniqueConstraint(
                fields=['processo', 'parte', 'polo', 'papel'],
                condition=Q(representa__isnull=True),
                name='uniq_processo_parte_polo_papel_principal',
            ),
        ]
        indexes = [
            models.Index(fields=['parte', 'polo']),
            models.Index(fields=['processo', 'polo']),
            models.Index(fields=['papel']),
        ]

    def __str__(self):
        return f'{self.processo.numero_cnj} · {self.parte.nome} ({self.polo}/{self.papel})'


class Movimentacao(models.Model):
    processo = models.ForeignKey(Process, on_delete=models.CASCADE, related_name='movimentacoes')
    tribunal = models.ForeignKey(Tribunal, on_delete=models.PROTECT, related_name='movimentacoes')
    external_id = models.CharField(max_length=64)
    data_disponibilizacao = models.DateTimeField()
    inserido_em = models.DateTimeField(auto_now_add=True)

    tipo_comunicacao = models.CharField(max_length=120, blank=True)
    tipo_documento = models.CharField(max_length=120, blank=True)
    nome_orgao = models.CharField(max_length=255, blank=True)
    id_orgao = models.IntegerField(null=True, blank=True)
    nome_classe = models.CharField(max_length=255, blank=True)
    codigo_classe = models.CharField(max_length=20, blank=True)
    classe = models.ForeignKey(
        ClasseJudicial, on_delete=models.PROTECT, null=True, blank=True,
        related_name='movimentacoes',
    )
    link = models.URLField(max_length=500, blank=True)
    destinatarios = models.JSONField(default=list)
    destinatario_advogados = models.JSONField(default=list)
    texto = models.TextField(blank=True)

    numero_comunicacao = models.CharField(max_length=120, blank=True)
    hash = models.CharField(max_length=128, blank=True)
    meio = models.CharField(max_length=20, blank=True)
    meio_completo = models.CharField(max_length=120, blank=True)
    status = models.CharField(max_length=40, blank=True)

    ativo = models.BooleanField(default=True)
    data_cancelamento = models.DateTimeField(null=True, blank=True)
    motivo_cancelamento = models.TextField(blank=True)

    search_vector = SearchVectorField(null=True)

    class Meta:
        constraints = [
            UniqueConstraint(fields=['tribunal', 'external_id'], name='uniq_mov_tribunal_extid'),
        ]
        indexes = [
            models.Index(fields=['processo', '-data_disponibilizacao']),
            models.Index(fields=['tribunal', '-data_disponibilizacao']),
            models.Index(fields=['inserido_em']),
            models.Index(fields=['tribunal', 'ativo']),
            models.Index(fields=['hash']),
            models.Index(fields=['classe']),
            GinIndex(fields=['search_vector'], name='mov_search_vector_gin'),
            GinIndex(name='mov_texto_trgm', fields=['texto'], opclasses=['gin_trgm_ops']),
        ]


class IngestionRun(models.Model):
    STATUS_RUNNING = 'running'
    STATUS_SUCCESS = 'success'
    STATUS_FAILED = 'failed'
    STATUS_CHOICES = [
        (STATUS_RUNNING, 'Em execução'),
        (STATUS_SUCCESS, 'Sucesso'),
        (STATUS_FAILED, 'Falha'),
    ]

    tribunal = models.ForeignKey(Tribunal, on_delete=models.PROTECT, related_name='runs')
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_RUNNING)
    janela_inicio = models.DateField()
    janela_fim = models.DateField()
    paginas_lidas = models.PositiveIntegerField(default=0)
    movimentacoes_novas = models.PositiveIntegerField(default=0)
    movimentacoes_duplicadas = models.PositiveIntegerField(default=0)
    processos_novos = models.PositiveIntegerField(default=0)
    erros = models.JSONField(default=list)

    class Meta:
        ordering = ['-started_at']
        indexes = [
            models.Index(fields=['tribunal', '-started_at']),
            models.Index(fields=['status', '-started_at']),
            models.Index(fields=['tribunal', 'janela_inicio', 'janela_fim']),
        ]


class SchemaDriftAlert(models.Model):
    TIPO_EXTRA = 'extra_keys'
    TIPO_MISSING = 'missing_keys'
    TIPO_TYPE_MISMATCH = 'type_mismatch'
    TIPO_CHOICES = [
        (TIPO_EXTRA, 'Chaves extras'),
        (TIPO_MISSING, 'Chaves faltantes'),
        (TIPO_TYPE_MISMATCH, 'Tipo divergente'),
    ]

    tribunal = models.ForeignKey(Tribunal, on_delete=models.PROTECT, related_name='drift_alerts')
    detectado_em = models.DateTimeField(auto_now_add=True)
    tipo = models.CharField(max_length=20, choices=TIPO_CHOICES)
    chaves = models.JSONField()
    chaves_hash = models.CharField(max_length=64, db_index=True)
    exemplo = models.JSONField()
    ingestion_run = models.ForeignKey(IngestionRun, on_delete=models.SET_NULL, null=True, blank=True)
    resolvido = models.BooleanField(default=False)
    resolvido_em = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            UniqueConstraint(
                fields=['tribunal', 'tipo', 'chaves_hash'],
                condition=Q(resolvido=False),
                name='uniq_alerta_aberto_tribunal_tipo_chaves',
            ),
        ]
        indexes = [
            models.Index(fields=['resolvido', 'tribunal']),
        ]
