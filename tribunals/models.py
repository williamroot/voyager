from django.conf import settings
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.db import models
from django.db.models import Q, UniqueConstraint
from django.utils.translation import gettext_lazy as _


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
    ultima_sinc_djen_em = models.DateTimeField(null=True, blank=True)
    # Timestamps por fonte de enriquecimento — quando cada source rodou
    # com sucesso pra esse processo. Permitem detectar staleness por
    # source independentemente.
    data_enriquecimento_tribunal = models.DateTimeField(null=True, blank=True)
    data_enriquecimento_djen = models.DateTimeField(null=True, blank=True)
    data_enriquecimento_datajud = models.DateTimeField(null=True, blank=True)

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
    enriquecimento_tentativas = models.PositiveSmallIntegerField(default=0)

    inserido_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    # Classificação de lead (modelo LR aplicado após enriquecimento).
    # Categorias hierárquicas: PRECATORIO > PRE_PRECATORIO > DIREITO_CREDITORIO > NAO_LEAD.
    CLASSIF_PRECATORIO = 'PRECATORIO'
    CLASSIF_PRE_PRECATORIO = 'PRE_PRECATORIO'
    CLASSIF_DIREITO_CREDITORIO = 'DIREITO_CREDITORIO'
    CLASSIF_NAO_LEAD = 'NAO_LEAD'
    CLASSIF_CHOICES = [
        (CLASSIF_PRECATORIO, 'Precatório'),
        (CLASSIF_PRE_PRECATORIO, 'Pré-precatório'),
        (CLASSIF_DIREITO_CREDITORIO, 'Direito creditório'),
        (CLASSIF_NAO_LEAD, 'Não-lead'),
    ]
    classificacao = models.CharField(max_length=20, choices=CLASSIF_CHOICES, null=True, blank=True, db_index=True)
    classificacao_score = models.FloatField(null=True, blank=True)
    classificacao_versao = models.CharField(max_length=10, null=True, blank=True)
    classificacao_em = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        constraints = [
            UniqueConstraint(fields=['tribunal', 'numero_cnj'], name='uniq_proc_tribunal_cnj'),
        ]
        indexes = [
            models.Index(fields=['tribunal', 'numero_cnj']),
            models.Index(fields=['tribunal', '-ultima_movimentacao_em']),
            models.Index(fields=['ultima_movimentacao_em', 'classificacao_em'], name='proc_ultmov_classif_idx'),
            models.Index(fields=['inserido_em']),
            models.Index(fields=['enriquecido_em']),
            models.Index(fields=['enriquecimento_status']),
            models.Index(fields=['classe_codigo']),
            models.Index(fields=['classe']),
            models.Index(fields=['assunto']),
            models.Index(fields=['orgao_julgador_codigo']),
            models.Index(fields=['ano_cnj']),
            models.Index(fields=['tribunal', 'ano_cnj']),
            # Cobrem ORDER BY id DESC LIMIT 50 com filtro por tribunal ou enriq_status
            # — evitam bitmap heap scan + sort quando resultado esperado é pequeno.
            models.Index(fields=['tribunal', '-id'], name='proc_tribunal_id_idx'),
            models.Index(fields=['enriquecimento_status', '-id'], name='proc_enriq_id_idx'),
            # Pending-scan do reabastecer: WHERE tribunal_id=X AND status=PENDENTE.
            # Sem o composto, o planner pegava o índice de status (milhões) e
            # filtrava tribunal → scan de minutos (incidente 2026-07-01).
            models.Index(fields=['tribunal', 'enriquecimento_status'],
                         name='proc_trib_enriq_idx'),
            models.Index(fields=['data_enriquecimento_datajud'], name='proc_datajud_em_idx'),
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
            # Sem doc nem OAB (Procuradoria, Defensoria, órgãos públicos):
            # único por (nome, tipo) — sem essa constraint o caminho 4 do
            # _upsert_parte não pode ser idempotente, gerando 64k+ Partes
            # duplicadas pra "Procuradoria Federal" etc.
            UniqueConstraint(
                fields=['nome', 'tipo'],
                condition=Q(documento='') & Q(oab=''),
                name='uniq_parte_sem_doc_nem_oab',
            ),
            UniqueConstraint(fields=['oab'], condition=~Q(oab=''),
                             name='uniq_parte_oab'),
        ]
        indexes = [
            models.Index(fields=['nome']),
            models.Index(fields=['documento']),
            models.Index(fields=['oab']),
            models.Index(fields=['tipo']),
            # Cobre sort default da listagem (/dashboard/partes/):
            # ORDER BY total_processos DESC, nome ASC LIMIT N.
            models.Index(fields=['-total_processos', 'nome'],
                         name='parte_total_procs_nome_idx'),
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


class ParteTribunal(models.Model):
    """Ponte denormalizada parte↔tribunal pra filtro rápido na lista de Partes.

    `Parte` não tem tribunal — ele vive em `ProcessoParte → Process → tribunal`.
    Filtrar a lista por tribunal via EXISTS sobre `tribunals_processoparte`
    (bilhões de linhas) custa ~43s (medido 2026-06-27). Esta ponte (~15-25M
    linhas) com índice por tribunal torna o filtro instantâneo. `total_processos`
    espelha `Parte.total_processos` pra ordenar sem join de volta.

    Eventual-consistente: reconstruída por `manage.py rebuild_parte_bridges`
    (cron diário). Lag de 1 dia é aceitável pra um filtro. Ver dashboard
    `partes` e .ia/DASHBOARD.md."""

    parte = models.ForeignKey(Parte, on_delete=models.CASCADE, related_name='tribunais_ponte')
    tribunal = models.ForeignKey(Tribunal, on_delete=models.CASCADE, related_name='+')
    total_processos = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            UniqueConstraint(fields=['parte', 'tribunal'], name='uniq_parte_tribunal'),
        ]
        indexes = [
            models.Index(fields=['tribunal', '-total_processos'], name='idx_pt_trib_total'),
        ]


class PartePapel(models.Model):
    """Ponte denormalizada parte↔papel processual pra filtro rápido na lista de
    Partes. Mesmo motivo de `ParteTribunal` (papel vive em `ProcessoParte`).
    Reconstruída por `rebuild_parte_bridges`."""

    parte = models.ForeignKey(Parte, on_delete=models.CASCADE, related_name='papeis_ponte')
    papel = models.CharField(max_length=120)
    total_processos = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            UniqueConstraint(fields=['parte', 'papel'], name='uniq_parte_papel'),
        ]
        indexes = [
            models.Index(fields=['papel', '-total_processos'], name='idx_pp_papel_total'),
        ]


class Movimentacao(models.Model):
    processo = models.ForeignKey(Process, on_delete=models.CASCADE, related_name='movimentacoes')
    tribunal = models.ForeignKey(Tribunal, on_delete=models.PROTECT, related_name='movimentacoes')
    external_id = models.CharField(max_length=64)
    data_disponibilizacao = models.DateTimeField()
    # data_envio: quando o cartório/escrivão liberou a publicação. Geralmente
    # 1-2 dias antes da disponibilização — útil pra detectar atrasos do diário.
    data_envio = models.DateField(null=True, blank=True)
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


# ============== Classificação de leads + API integration ==============

class ClassificadorVersao(models.Model):
    """Versionamento dos modelos de classificação treinados.

    Apenas 1 ativa por vez (constraint partial) — workers carregam essa.
    """
    versao = models.CharField(max_length=10, unique=True)  # 'v5'
    pesos = models.JSONField()                              # {feature_name: weight, _intercept_: ...}
    metricas = models.JSONField(default=dict)               # {auc, prec_at_5k, prec_at_1k, ...}
    ativa = models.BooleanField(default=False, db_index=True)
    # Shadow mode (T5): N versões podem rodar em paralelo registrando
    # ClassificacaoShadowLog sem afetar Process.classificacao oficial.
    # Pode haver N shadow=True simultaneamente; a constraint partial sobre
    # ativa=True garante que só 1 esteja em produção por vez.
    shadow = models.BooleanField(default=False, db_index=True)
    criada_em = models.DateTimeField(auto_now_add=True)
    notas = models.TextField(blank=True)

    class Meta:
        constraints = [
            UniqueConstraint(fields=['ativa'], condition=Q(ativa=True),
                             name='uniq_classificador_versao_ativa'),
        ]

    def __str__(self):
        return f'{self.versao}{" [ativa]" if self.ativa else ""}'


class ClassificacaoLog(models.Model):
    """Histórico de classificações — útil pra auditar transições N3→N2→N1."""
    processo = models.ForeignKey(Process, on_delete=models.CASCADE, related_name='classif_logs')
    classificacao = models.CharField(max_length=20)
    score = models.FloatField()
    versao = models.CharField(max_length=10)
    features_snapshot = models.JSONField(default=dict)
    criada_em = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=['processo', '-criada_em']),
        ]


class ApiClient(models.Model):
    """Cliente externo que consome a API de leads (ex: Juriscope)."""
    nome = models.CharField(max_length=64, unique=True)
    api_key = models.CharField(max_length=64, unique=True, db_index=True)
    ativo = models.BooleanField(default=True)
    criado_em = models.DateTimeField(auto_now_add=True)
    notas = models.TextField(blank=True)

    def __str__(self):
        return f'{self.nome}{" [ativo]" if self.ativo else ""}'


class LeadConsumption(models.Model):
    """Registro de processo consumido por um cliente externo. Sem unique
    constraint — re-consumo é permitido (cria novo registro)."""
    RESULTADO_VALIDADO = 'validado'
    RESULTADO_SEM_EXPEDICAO = 'sem_expedicao'
    RESULTADO_ERRO = 'erro'
    RESULTADO_PENDENTE = 'pendente'
    RESULTADO_PAGO = 'pago'
    RESULTADO_ARQUIVADO = 'arquivado'
    RESULTADO_CEDIDO = 'cedido'
    RESULTADO_CHOICES = [
        (RESULTADO_VALIDADO, 'Validado'),
        (RESULTADO_SEM_EXPEDICAO, 'Sem expedição'),
        (RESULTADO_ERRO, 'Erro'),
        (RESULTADO_PENDENTE, 'Pendente'),
        (RESULTADO_PAGO, 'Pago'),
        (RESULTADO_ARQUIVADO, 'Arquivado'),
        (RESULTADO_CEDIDO, 'Cedido'),
    ]

    processo = models.ForeignKey(Process, on_delete=models.CASCADE, related_name='consumos')
    cliente = models.ForeignKey(ApiClient, on_delete=models.CASCADE, related_name='consumos')
    consumido_em = models.DateTimeField(auto_now_add=True, db_index=True)
    resultado = models.CharField(max_length=20, choices=RESULTADO_CHOICES,
                                 default=RESULTADO_PENDENTE, db_index=True)
    lote_id = models.UUIDField(
        null=True, blank=True,
        help_text='UUID do lote de reporte (idempotência). NULL = legado.',
    )

    class Meta:
        indexes = [
            models.Index(fields=['cliente', '-consumido_em']),
            models.Index(fields=['cliente', 'processo']),
            models.Index(fields=['lote_id'], name='trib_lc_lote_id_idx'),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['cliente', 'processo', 'lote_id'],
                name='uniq_consumo_cliente_proc_lote',
                condition=models.Q(lote_id__isnull=False),
            ),
        ]


# ============== Validação humana de classificação (T4/T5) ==============
#
# Pipeline de revisão manual sobre a saída do classificador, para gerar
# ground truth de treino, medir precision real por tribunal e detectar
# drift entre re-treinos. Regras de negócio em
# `.ia/REGRAS_NEGOCIO_VALIDACAO.md` (ADR-018):
#
# - AmostraValidacao: lote sorteado por estratégia (top_score, borderline,
#   low_score, falsos_consumidos, recuperados, on_demand, fn_candidatos,
#   shadow_disagree). Seed persistida para reprodutibilidade.
# - AmostraProcesso: through M2M explícito — preserva ordem, score e
#   motivos de suspeita no momento do sorteio.
# - ProcessoValidacao: DECISÃO HUMANA IMUTÁVEL. Append-only via
#   UniqueConstraint(processo, usuario) — re-anotação proibida; divergência
#   resolvida em `label_final` por revisor sênior. SET_NULL na FK usuario
#   preserva label se o User for deletado.
# - ClassificacaoShadowLog: registra previsões de versões shadow (T5)
#   sem afetar Process.classificacao oficial. Retention 90 dias
#   (job de cleanup é tarefa futura).
# - ThresholdTribunal: thresholds N1/N2/N3 por tribunal, versionados.
#   1 ativo por (tribunal, versao_modelo).

class AmostraValidacao(models.Model):
    """Lote sorteado por estratégia para anotação humana."""

    ESTRATEGIA_TOP_SCORE = 'top_score'
    ESTRATEGIA_BORDERLINE = 'borderline'
    ESTRATEGIA_LOW_SCORE = 'low_score'
    ESTRATEGIA_FALSOS_CONSUMIDOS = 'falsos_consumidos'
    ESTRATEGIA_RECUPERADOS = 'recuperados'
    ESTRATEGIA_ON_DEMAND = 'on_demand'
    ESTRATEGIA_FN_CANDIDATOS = 'fn_candidatos'
    ESTRATEGIA_SHADOW_DISAGREE = 'shadow_disagree'
    ESTRATEGIA_CHOICES = [
        (ESTRATEGIA_TOP_SCORE, _('Top score (≥ 0.7)')),
        (ESTRATEGIA_BORDERLINE, _('Borderline (0.30-0.70)')),
        (ESTRATEGIA_LOW_SCORE, _('Low score (0.05-0.30)')),
        (ESTRATEGIA_FALSOS_CONSUMIDOS, _('Falsos consumidos')),
        (ESTRATEGIA_RECUPERADOS, _('Recuperados (backfill recente)')),
        (ESTRATEGIA_ON_DEMAND, _('Sob demanda (manual)')),
        (ESTRATEGIA_FN_CANDIDATOS, _('Candidatos a falso negativo')),
        (ESTRATEGIA_SHADOW_DISAGREE, _('Divergência shadow vs ativa')),
    ]

    estrategia = models.CharField(max_length=30, choices=ESTRATEGIA_CHOICES)
    # null = lote multi-tribunal (ex.: análise global)
    tribunal = models.ForeignKey(
        Tribunal, on_delete=models.PROTECT, null=True, blank=True,
        related_name='amostras_validacao',
    )
    versao_modelo = models.CharField(max_length=10)  # snapshot, ex 'v6'
    criada_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='amostras_validacao_criadas',
    )
    criada_em = models.DateTimeField(auto_now_add=True)
    parametros = models.JSONField(default=dict)  # configs do sorteio
    tamanho_alvo = models.PositiveIntegerField()
    # Seed persistida — biz exige reprodutibilidade do sorteio.
    seed = models.BigIntegerField()
    processos = models.ManyToManyField(
        Process, through='AmostraProcesso', related_name='amostras_validacao',
    )

    class Meta:
        verbose_name = _('amostra de validação')
        verbose_name_plural = _('amostras de validação')
        ordering = ['-criada_em']
        indexes = [
            models.Index(fields=['-criada_em']),
            models.Index(fields=['estrategia']),
        ]

    def __str__(self):
        alvo = self.tribunal_id or 'multi'
        return f'Amostra #{self.pk} · {self.estrategia} · {alvo} · {self.tamanho_alvo}'


class AmostraProcesso(models.Model):
    """Through M2M: associação Amostra↔Process com metadados do sorteio."""

    amostra = models.ForeignKey(
        AmostraValidacao, on_delete=models.CASCADE, related_name='itens',
    )
    processo = models.ForeignKey(
        Process, on_delete=models.CASCADE, related_name='amostra_itens',
    )
    ordem = models.PositiveIntegerField()
    score_no_sorteio = models.FloatField()
    classificacao_no_sorteio = models.CharField(max_length=20)
    # Score do "mining" (FN candidates, shadow disagree). NULL = sorteio aleatório.
    suspeita_score = models.FloatField(null=True, blank=True)
    # Lista opcional de estratégias que ativaram o item no mining.
    motivos_suspeita = models.JSONField(null=True, blank=True)

    class Meta:
        verbose_name = _('item de amostra')
        verbose_name_plural = _('itens de amostra')
        constraints = [
            UniqueConstraint(
                fields=['amostra', 'processo'],
                name='uq_amostra_processo',
            ),
        ]
        indexes = [
            models.Index(fields=['amostra', 'ordem']),
        ]
        ordering = ['amostra', 'ordem']


class ProcessoValidacao(models.Model):
    """Decisão humana sobre a classificação de um processo.

    APPEND-ONLY: UniqueConstraint(processo, usuario) impede re-anotação.
    Divergências entre anotadores são resolvidas em `label_final` por
    usuário com permission `can_resolve_disagreement`.

    `usuario` é SET_NULL no delete do User (preserva label órfã).
    Anonimização LGPD via `usuario_hash` está descomissionada nesta versão
    (campo permanece no schema mas não é populado).
    """

    RESULTADO_EH_LEAD = 'eh_lead'
    RESULTADO_EH_PRECATORIO = 'eh_precatorio'
    RESULTADO_EH_PRE = 'eh_pre'
    RESULTADO_EH_DC = 'eh_dc'
    RESULTADO_NAO_LEAD = 'nao_lead'
    RESULTADO_INCERTO = 'incerto'
    RESULTADO_PRECISA_ENRIQUECER = 'precisa_enriquecer'
    RESULTADO_SKIP = 'skip'
    RESULTADO_CHOICES = [
        (RESULTADO_EH_LEAD, _('É lead (genérico)')),
        (RESULTADO_EH_PRECATORIO, _('É precatório (N1)')),
        (RESULTADO_EH_PRE, _('É pré-precatório (N2)')),
        (RESULTADO_EH_DC, _('É direito creditório (N3)')),
        (RESULTADO_NAO_LEAD, _('Não é lead')),
        (RESULTADO_INCERTO, _('Incerto')),
        (RESULTADO_PRECISA_ENRIQUECER, _('Precisa enriquecer')),
        (RESULTADO_SKIP, _('Pulou (skip)')),
    ]

    CONFIANCA_ALTA = 'alta'
    CONFIANCA_MEDIA = 'media'
    CONFIANCA_BAIXA = 'baixa'
    CONFIANCA_CHOICES = [
        (CONFIANCA_ALTA, _('Alta')),
        (CONFIANCA_MEDIA, _('Média')),
        (CONFIANCA_BAIXA, _('Baixa')),
    ]

    processo = models.ForeignKey(
        Process, on_delete=models.CASCADE, related_name='validacoes',
    )
    amostra = models.ForeignKey(
        AmostraValidacao, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='validacoes',
    )
    # SET_NULL preserva label se User for deletado (turnover, conta apagada).
    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='validacoes',
    )
    # Reservado pra anonimização futura (LGPD). Não populado nesta versão.
    usuario_hash = models.CharField(max_length=64, blank=True)

    resultado = models.CharField(max_length=20, choices=RESULTADO_CHOICES)
    confianca = models.CharField(
        max_length=10, choices=CONFIANCA_CHOICES, default=CONFIANCA_ALTA,
    )
    motivo = models.TextField(blank=True)
    tempo_segundos = models.PositiveIntegerField(null=True, blank=True)

    # Snapshot do estado do modelo no momento da anotação.
    versao_modelo = models.CharField(max_length=10)
    classificacao_no_momento = models.CharField(max_length=20)
    score_no_momento = models.FloatField()
    features_snapshot = models.JSONField(default=dict)

    criada_em = models.DateTimeField(auto_now_add=True)

    # Resolução de divergência em dupla-anotação (10% dos lotes).
    # Preenchido por revisor sênior — campos editáveis pós-criação.
    label_final = models.CharField(
        max_length=20, choices=RESULTADO_CHOICES, null=True, blank=True,
    )
    label_final_resolvido_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='validacoes_resolvidas',
    )
    label_final_resolvido_em = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _('validação de processo')
        verbose_name_plural = _('validações de processo')
        constraints = [
            # Imutabilidade — biz exige sem UPDATE. Garantido também por
            # trigger Postgres em migration futura (T5+).
            UniqueConstraint(
                fields=['processo', 'usuario'],
                name='uq_processo_usuario_validacao',
            ),
        ]
        indexes = [
            models.Index(fields=['processo', 'criada_em']),
            models.Index(fields=['usuario', 'criada_em']),
            models.Index(fields=['resultado']),
            models.Index(fields=['amostra']),
            models.Index(fields=['label_final']),
        ]
        permissions = [
            ('can_validate_lead',
             'Pode anotar validações de leads'),
            ('can_publish_model',
             'Pode promover ClassificadorVersao e editar thresholds'),
            ('can_view_validacao_dashboard',
             'Pode ver dashboard de validação'),
            ('can_resolve_disagreement',
             'Pode resolver divergências preenchendo label_final'),
            ('can_view_motivo',
             'Pode ler o campo motivo de validações de outros usuários'),
        ]

    def __str__(self):
        return f'Validação #{self.pk} · {self.processo_id} · {self.resultado}'

    def motivo_visivel_para(self, user) -> str:
        """Retorna o motivo se `user` tem direito; senão string vazia.

        Regra (REGRAS_NEGOCIO_VALIDACAO):
        - Autor da anotação sempre vê o próprio motivo
        - Outros precisam de `tribunals.can_view_motivo`
        Use SEMPRE este helper em templates/serializers que mostram
        motivos de outros validadores.
        """
        if user is None or not getattr(user, 'is_authenticated', False):
            return ''
        if self.usuario_id and self.usuario_id == getattr(user, 'pk', None):
            return self.motivo
        if user.has_perm('tribunals.can_view_motivo'):
            return self.motivo
        return ''


class ClassificacaoShadowLog(models.Model):
    """Predições de versões shadow do classificador (T5).

    Registra previsões paralelas de candidatas a próxima versão sem
    afetar `Process.classificacao` oficial. Permite comparar AUC/precision
    contra a versão ativa antes de promover.

    Retention: 90 dias. Job de cleanup é tarefa futura — não há trigger
    de purge neste model.
    """

    processo = models.ForeignKey(
        Process, on_delete=models.CASCADE, related_name='shadow_logs',
    )
    versao_shadow = models.CharField(max_length=10)
    score = models.FloatField()
    categoria = models.CharField(
        max_length=20, choices=Process.CLASSIF_CHOICES,
    )
    criada_em = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _('log de classificação shadow')
        verbose_name_plural = _('logs de classificação shadow')
        indexes = [
            models.Index(fields=['versao_shadow', 'criada_em'],
                         name='shadow_log_versao_em_idx'),
        ]


class ThresholdTribunal(models.Model):
    """Thresholds de classificação (N1/N2/N3) por tribunal e versão.

    Biz exige DB-driven (não hardcoded em código). Fallback para defaults
    em código se row não existir. Apenas 1 ativo por (tribunal, versao_modelo).
    """

    tribunal = models.ForeignKey(
        Tribunal, on_delete=models.CASCADE, related_name='thresholds',
    )
    versao_modelo = models.CharField(max_length=10)
    threshold_precatorio = models.FloatField()
    threshold_pre = models.FloatField()
    threshold_dc = models.FloatField()
    ativo = models.BooleanField(default=True)
    atualizado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='thresholds_atualizados',
    )
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _('threshold por tribunal')
        verbose_name_plural = _('thresholds por tribunal')
        constraints = [
            UniqueConstraint(
                fields=['tribunal', 'versao_modelo'],
                name='uq_threshold_tribunal_versao',
            ),
            # Apenas 1 ativo por (tribunal, versao_modelo).
            UniqueConstraint(
                fields=['tribunal', 'versao_modelo'],
                condition=Q(ativo=True),
                name='uq_threshold_ativo_por_tribunal_versao',
            ),
        ]

    def __str__(self):
        return f'{self.tribunal_id}/{self.versao_modelo}'
