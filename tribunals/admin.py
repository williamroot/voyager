import secrets

from django.contrib import admin
from django.utils import timezone

from .models import (
    ApiClient,
    Assunto,
    ClasseJudicial,
    ClassificacaoLog,
    ClassificadorVersao,
    IngestionRun,
    LeadConsumption,
    Movimentacao,
    Parte,
    Process,
    ProcessoParte,
    SchemaDriftAlert,
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
