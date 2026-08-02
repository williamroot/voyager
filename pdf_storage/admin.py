from django.contrib import admin

from .models import PdfArquivo


@admin.register(PdfArquivo)
class PdfArquivoAdmin(admin.ModelAdmin):
    list_display = ('movimentacao', 'status', 'tamanho_bytes', 'baixado_em', 'tentativas')
    list_filter = ('status',)
    search_fields = ('movimentacao__id',)
    readonly_fields = ('baixado_em', 'hash_sha256', 'tamanho_bytes')