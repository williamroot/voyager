from django.contrib import admin

from .models import Invite


@admin.register(Invite)
class InviteAdmin(admin.ModelAdmin):
    list_display = (
        'token_short', 'created_by', 'created_at', 'expires_at',
        'used_at', 'used_by', 'used_ip', 'used_ip_country_code',
    )
    list_filter = ('created_at', 'used_at')
    search_fields = ('token', 'email_hint', 'note', 'used_ip', 'used_by__username')
    readonly_fields = (
        'token', 'created_at', 'used_at', 'used_by',
        'used_ip', 'used_user_agent', 'used_ip_country', 'used_ip_country_code',
        'used_ip_region', 'used_ip_city', 'used_ip_isp', 'used_ip_org',
        'used_ip_asn', 'used_ip_mobile', 'used_ip_hosting', 'used_ip_proxy',
        'used_ip_data',
    )

    def token_short(self, obj):
        return f'{obj.token[:12]}…'
    token_short.short_description = 'Token'
