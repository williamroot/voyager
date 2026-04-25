from django.contrib import admin

from .models import IngestionRun, Movimentacao, Process, SchemaDriftAlert, Tribunal


@admin.register(Tribunal)
class TribunalAdmin(admin.ModelAdmin):
    list_display = ('sigla', 'nome', 'ativo', 'data_inicio_disponivel', 'backfill_concluido_em', 'overlap_dias')
    list_filter = ('ativo',)
    search_fields = ('sigla', 'nome')


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


@admin.register(SchemaDriftAlert)
class SchemaDriftAlertAdmin(admin.ModelAdmin):
    list_display = ('id', 'tribunal', 'tipo', 'detectado_em', 'resolvido')
    list_filter = ('resolvido', 'tribunal', 'tipo')
    search_fields = ('chaves',)
    readonly_fields = ('detectado_em',)
    actions = ['marcar_resolvido']

    @admin.action(description='Marcar como resolvido')
    def marcar_resolvido(self, request, queryset):
        from django.utils import timezone
        queryset.update(resolvido=True, resolvido_em=timezone.now())
