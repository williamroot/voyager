from django.contrib import admin

from .models import (
    Detection, MonitoredPerson, MonitoredProcess, MonitoredTerm, WebhookConfig,
)


@admin.register(MonitoredTerm)
class MonitoredTermAdmin(admin.ModelAdmin):
    list_display = ('term', 'is_active', 'is_reviewed', 'created_at')
    list_filter = ('is_active',)
    search_fields = ('term',)


@admin.register(MonitoredPerson)
class MonitoredPersonAdmin(admin.ModelAdmin):
    list_display = ('nome', 'documento', 'oab', 'is_active', 'created_at')
    list_filter = ('is_active',)
    search_fields = ('nome', 'documento', 'oab')


@admin.register(MonitoredProcess)
class MonitoredProcessAdmin(admin.ModelAdmin):
    list_display = ('cnj', 'is_active', 'created_at')
    list_filter = ('is_active',)
    search_fields = ('cnj',)


@admin.register(Detection)
class DetectionAdmin(admin.ModelAdmin):
    list_display = ('target_type', 'target_id', 'movimentacao', 'detected_at', 'entregue_em')
    list_filter = ('target_type',)
    readonly_fields = ('detected_at',)


@admin.register(WebhookConfig)
class WebhookConfigAdmin(admin.ModelAdmin):
    list_display = ('cliente', 'url', 'is_active', 'created_at')
    list_filter = ('is_active',)