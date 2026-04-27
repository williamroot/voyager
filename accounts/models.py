"""Sistema de convites: superuser cria token de uso único válido por
7 dias; convidado escolhe username/senha numa página pública e o link
expira no momento do uso. Captura IP + User-Agent + classificação
ip-api.com pra auditoria.
"""
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone


def _gerar_token() -> str:
    """Token URL-safe de 32 bytes (256 bits de entropia)."""
    return secrets.token_urlsafe(32)


def _expiracao_default():
    return timezone.now() + timedelta(days=7)


class Invite(models.Model):
    token = models.CharField(max_length=64, unique=True, default=_gerar_token)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name='invites_created',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(default=_expiracao_default)

    # Hint opcional pra quem é o convite (não restringe — token é o que vale).
    email_hint = models.EmailField(blank=True)
    note = models.CharField(max_length=255, blank=True)

    used_at = models.DateTimeField(null=True, blank=True)
    used_by = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='invite_used',
    )

    # Captura no momento do uso — pra auditoria.
    used_ip = models.GenericIPAddressField(null=True, blank=True)
    used_user_agent = models.CharField(max_length=500, blank=True)

    # Classificação ip-api.com (se IP_API_KEY configurada ou free tier OK).
    used_ip_country = models.CharField(max_length=80, blank=True)
    used_ip_country_code = models.CharField(max_length=4, blank=True)
    used_ip_region = models.CharField(max_length=80, blank=True)
    used_ip_city = models.CharField(max_length=120, blank=True)
    used_ip_isp = models.CharField(max_length=180, blank=True)
    used_ip_org = models.CharField(max_length=180, blank=True)
    used_ip_asn = models.CharField(max_length=80, blank=True)
    used_ip_mobile = models.BooleanField(null=True, blank=True)
    used_ip_hosting = models.BooleanField(null=True, blank=True)
    used_ip_proxy = models.BooleanField(null=True, blank=True)
    used_ip_data = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['token']),
            models.Index(fields=['used_at']),
        ]

    def __str__(self):
        return f'Invite({self.token[:12]}…) by {self.created_by_id}'

    @property
    def is_used(self) -> bool:
        return self.used_at is not None

    @property
    def is_expired(self) -> bool:
        return timezone.now() > self.expires_at

    @property
    def is_valid(self) -> bool:
        return not self.is_used and not self.is_expired
