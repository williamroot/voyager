from django.contrib import admin, messages

from mcp_server import tokens
from mcp_server.models import MCPAcesso


class SoSuperuserMixin:
    """Quem controla a allowlist controla quem lê o acervo pelo Claude — não é
    permissão para delegar por grupo."""

    def has_module_permission(self, request):
        return request.user.is_active and request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_active and request.user.is_superuser

    def has_add_permission(self, request):
        return request.user.is_active and request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_active and request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_active and request.user.is_superuser


@admin.register(MCPAcesso)
class MCPAcessoAdmin(SoSuperuserMixin, admin.ModelAdmin):
    list_display = ('usuario', 'email_display', 'ativo', 'ultimo_uso', 'criado_em')
    list_filter = ('ativo',)
    search_fields = ('usuario__username', 'usuario__email', 'usuario__first_name', 'usuario__last_name')
    autocomplete_fields = ('usuario',)
    list_select_related = ('usuario',)
    actions = ('revogar', 'reativar')
    readonly_fields = ('criado_por', 'criado_em', 'revogado_em', 'ultimo_uso')

    @admin.display(description='E-mail', ordering='usuario__email')
    def email_display(self, obj):
        return obj.usuario.email

    def get_readonly_fields(self, request, obj=None):
        campos = list(super().get_readonly_fields(request, obj))
        # Trocar o usuário de um acesso existente entregaria o histórico de uso
        # a outra pessoa. Para trocar: revogar este e cadastrar outro.
        if obj is not None:
            campos.append('usuario')
        return campos

    def save_model(self, request, obj, form, change):
        if not change and obj.criado_por_id is None:
            obj.criado_por = request.user
        super().save_model(request, obj, form, change)
        if not obj.ativo:
            tokens.revogar_usuario(obj.usuario_id)

    def delete_model(self, request, obj):
        usuario_id = obj.usuario_id
        super().delete_model(request, obj)
        tokens.revogar_usuario(usuario_id)

    def delete_queryset(self, request, queryset):
        ids = list(queryset.values_list('usuario_id', flat=True))
        super().delete_queryset(request, queryset)
        for usuario_id in ids:
            tokens.revogar_usuario(usuario_id)

    @admin.action(description='Revogar acesso (derruba as sessões na hora)')
    def revogar(self, request, queryset):
        n = 0
        for acesso in queryset.filter(ativo=True):
            acesso.ativo = False
            acesso.save()
            tokens.revogar_usuario(acesso.usuario_id)
            n += 1
        self.message_user(request, f'{n} acesso(s) revogado(s).', messages.SUCCESS)

    @admin.action(description='Reativar acesso')
    def reativar(self, request, queryset):
        n = 0
        for acesso in queryset.filter(ativo=False):
            acesso.ativo = True
            acesso.save()
            n += 1
        self.message_user(request, f'{n} acesso(s) reativado(s).', messages.SUCCESS)
