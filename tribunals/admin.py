import secrets

from django.contrib import admin
from django.utils import timezone

from .models import (
    AmostraProcesso,
    AmostraValidacao,
    ApiClient,
    Assunto,
    ClasseJudicial,
    ClassificacaoLog,
    ClassificacaoShadowLog,
    ClassificadorVersao,
    IngestionRun,
    LeadConsumption,
    Movimentacao,
    Parte,
    Process,
    ProcessoParte,
    ProcessoValidacao,
    SchemaDriftAlert,
    ThresholdTribunal,
    Tribunal,
)


@admin.register(ApiClient)
class ApiClientAdmin(admin.ModelAdmin):
    list_display = ('nome', 'api_key_preview', 'ativo', 'criado_em')
    list_filter = ('ativo',)
    search_fields = ('nome',)
    readonly_fields = ('api_key', 'criado_em')

    def api_key_preview(self, obj):
        return f'{obj.api_key[:6]}…{obj.api_key[-4:]}' if obj.api_key else '—'

    def save_model(self, request, obj, form, change):
        if not obj.api_key:
            obj.api_key = secrets.token_urlsafe(32)
        super().save_model(request, obj, form, change)


@admin.register(ClassificadorVersao)
class ClassificadorVersaoAdmin(admin.ModelAdmin):
    list_display = ('versao', 'ativa', 'auc', 'precision_at_5k', 'criada_em')
    list_filter = ('ativa',)
    readonly_fields = ('criada_em',)

    def auc(self, obj):
        return f"{obj.metricas.get('auc', 0):.3f}"

    def precision_at_5k(self, obj):
        return f"{obj.metricas.get('precision_at_5000', 0):.3f}"


@admin.register(LeadConsumption)
class LeadConsumptionAdmin(admin.ModelAdmin):
    list_display = ('id', 'cliente', 'processo', 'resultado', 'consumido_em')
    list_filter = ('cliente', 'resultado')
    search_fields = ('processo__numero_cnj',)
    readonly_fields = ('consumido_em',)


@admin.register(ClassificacaoLog)
class ClassificacaoLogAdmin(admin.ModelAdmin):
    list_display = ('id', 'processo', 'classificacao', 'score', 'versao', 'criada_em')
    list_filter = ('classificacao', 'versao')
    search_fields = ('processo__numero_cnj',)
    readonly_fields = ('criada_em',)


@admin.register(Tribunal)
class TribunalAdmin(admin.ModelAdmin):
    list_display = ('sigla', 'nome', 'ativo', 'data_inicio_disponivel', 'backfill_concluido_em', 'overlap_dias')
    list_filter = ('ativo',)
    search_fields = ('sigla', 'nome')


@admin.register(ClasseJudicial)
class ClasseJudicialAdmin(admin.ModelAdmin):
    list_display = ('codigo', 'nome', 'total_processos')
    search_fields = ('codigo', 'nome')
    ordering = ('-total_processos',)


@admin.register(Assunto)
class AssuntoAdmin(admin.ModelAdmin):
    list_display = ('codigo', 'nome', 'total_processos')
    search_fields = ('codigo', 'nome')
    ordering = ('-total_processos',)


@admin.register(Process)
class ProcessAdmin(admin.ModelAdmin):
    list_display = ('numero_cnj', 'tribunal', 'total_movimentacoes', 'ultima_movimentacao_em', 'inserido_em')
    list_filter = ('tribunal',)
    search_fields = ('numero_cnj',)
    date_hierarchy = 'inserido_em'
    raw_id_fields = ('tribunal',)


@admin.register(Movimentacao)
class MovimentacaoAdmin(admin.ModelAdmin):
    list_display = ('id', 'tribunal', 'external_id', 'data_disponibilizacao', 'tipo_comunicacao', 'nome_orgao')
    list_filter = ('tribunal', 'tipo_comunicacao')
    search_fields = ('external_id', 'processo__numero_cnj', 'texto')
    date_hierarchy = 'data_disponibilizacao'
    raw_id_fields = ('processo', 'tribunal')


@admin.register(IngestionRun)
class IngestionRunAdmin(admin.ModelAdmin):
    list_display = ('id', 'tribunal', 'status', 'janela_inicio', 'janela_fim', 'movimentacoes_novas',
                    'movimentacoes_duplicadas', 'paginas_lidas', 'started_at', 'finished_at')
    list_filter = ('tribunal', 'status')
    date_hierarchy = 'started_at'
    raw_id_fields = ('tribunal',)
    readonly_fields = ('started_at', 'finished_at')


@admin.register(Parte)
class ParteAdmin(admin.ModelAdmin):
    list_display = ('nome', 'tipo', 'documento', 'oab', 'total_processos', 'primeira_aparicao_em')
    list_filter = ('tipo',)
    search_fields = ('nome', 'documento', 'oab')
    readonly_fields = ('primeira_aparicao_em', 'ultima_aparicao_em', 'total_processos')


@admin.register(ProcessoParte)
class ProcessoParteAdmin(admin.ModelAdmin):
    list_display = ('processo', 'parte', 'polo', 'papel', 'inserido_em')
    list_filter = ('polo',)
    raw_id_fields = ('processo', 'parte', 'representa')
    search_fields = ('parte__nome', 'parte__documento', 'parte__oab')


@admin.register(SchemaDriftAlert)
class SchemaDriftAlertAdmin(admin.ModelAdmin):
    list_display = ('id', 'tribunal', 'tipo', 'detectado_em', 'resolvido')
    list_filter = ('resolvido', 'tribunal', 'tipo')
    search_fields = ('chaves',)
    readonly_fields = ('detectado_em',)
    actions = ['marcar_resolvido']

    @admin.action(description='Marcar como resolvido')
    def marcar_resolvido(self, request, queryset):
        queryset.update(resolvido=True, resolvido_em=timezone.now())


# ============== Validação humana ==============

class AmostraProcessoInline(admin.TabularInline):
    model = AmostraProcesso
    extra = 0
    raw_id_fields = ('processo',)
    readonly_fields = ('ordem', 'score_no_sorteio', 'classificacao_no_sorteio',
                       'suspeita_score', 'motivos_suspeita')
    can_delete = False
    show_change_link = True


@admin.register(AmostraValidacao)
class AmostraValidacaoAdmin(admin.ModelAdmin):
    list_display = ('id', 'estrategia', 'tribunal', 'versao_modelo',
                    'criada_por', 'criada_em', 'tamanho_alvo')
    list_filter = ('estrategia', 'tribunal', 'versao_modelo')
    search_fields = ('id',)
    raw_id_fields = ('criada_por', 'tribunal')
    readonly_fields = ('criada_em',)
    inlines = [AmostraProcessoInline]


@admin.register(AmostraProcesso)
class AmostraProcessoAdmin(admin.ModelAdmin):
    list_display = ('id', 'amostra', 'processo', 'ordem',
                    'score_no_sorteio', 'classificacao_no_sorteio')
    list_filter = ('classificacao_no_sorteio',)
    search_fields = ('processo__numero_cnj',)
    raw_id_fields = ('amostra', 'processo')


@admin.register(ProcessoValidacao)
class ProcessoValidacaoAdmin(admin.ModelAdmin):
    """Admin read-mostly — biz exige imutabilidade.

    Delete bloqueado para qualquer usuário (incluindo superuser via UI);
    snapshots e timestamp são readonly.
    """

    list_display = ('id', 'processo', 'usuario', 'resultado', 'confianca',
                    'versao_modelo', 'criada_em', 'label_final')
    list_filter = ('resultado', 'confianca', 'versao_modelo')
    search_fields = ('processo__numero_cnj',)
    raw_id_fields = ('processo', 'amostra', 'usuario',
                     'label_final_resolvido_por')
    readonly_fields = (
        'processo', 'amostra', 'usuario', 'usuario_hash',
        'resultado', 'confianca', 'motivo', 'tempo_segundos',
        'versao_modelo', 'classificacao_no_momento', 'score_no_momento',
        'features_snapshot', 'criada_em',
    )

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ClassificacaoShadowLog)
class ClassificacaoShadowLogAdmin(admin.ModelAdmin):
    """Read-only — admin é apenas consulta. Insert/Update via job/worker."""

    list_display = ('id', 'processo', 'versao_shadow', 'score', 'categoria', 'criada_em')
    list_filter = ('versao_shadow', 'categoria')
    search_fields = ('processo__numero_cnj',)
    raw_id_fields = ('processo',)
    readonly_fields = ('processo', 'versao_shadow', 'score', 'categoria', 'criada_em')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ThresholdTribunal)
class ThresholdTribunalAdmin(admin.ModelAdmin):
    """Editável apenas por superuser — biz exige dupla aprovação no fluxo
    real (UI dedicada com `can_publish_model`). Admin é fallback operacional.
    """

    list_display = ('id', 'tribunal', 'versao_modelo', 'threshold_precatorio',
                    'threshold_pre', 'threshold_dc', 'ativo', 'atualizado_em')
    list_filter = ('tribunal', 'versao_modelo', 'ativo')
    raw_id_fields = ('tribunal', 'atualizado_por')
    readonly_fields = ('atualizado_em',)

    def _is_super(self, request):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return self._is_super(request)

    def has_change_permission(self, request, obj=None):
        return self._is_super(request)

    def has_delete_permission(self, request, obj=None):
        return self._is_super(request)
