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


class Process(models.Model):
    numero_cnj = models.CharField(max_length=25)
    tribunal = models.ForeignKey(Tribunal, on_delete=models.PROTECT, related_name='processos')
    primeira_movimentacao_em = models.DateTimeField(null=True, blank=True)
    ultima_movimentacao_em = models.DateTimeField(null=True, blank=True)
    total_movimentacoes = models.PositiveIntegerField(default=0)
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
        ]

    def __str__(self):
        return f'{self.tribunal_id}/{self.numero_cnj}'


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
