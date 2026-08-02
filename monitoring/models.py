from django.conf import settings
from django.db import models

from tribunals.models import ApiClient, Movimentacao


class MonitoredTerm(models.Model):
    """Termo monitorado em diários oficiais (push via webhook)."""
    term = models.CharField(max_length=1024)
    source_ids = models.JSONField(default=list)  # [1, 59, ...] FonteDiario.source_id
    is_active = models.BooleanField(default=True)
    is_reviewed = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    user_creator = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
    )

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.term} ({self.source_ids})'

    def clean(self):
        from django.core.exceptions import ValidationError
        if len(self.term) < 3:
            raise ValidationError({'term': 'Termo deve ter no mínimo 3 caracteres.'})
        for ch in (',', '!', '='):
            if ch in self.term:
                raise ValidationError({'term': f'Caractere "{ch}" não permitido.'})


class MonitoredPerson(models.Model):
    """Pessoa (parte/advogado) monitorada em diários."""
    nome = models.CharField(max_length=255, blank=True)
    documento = models.CharField(max_length=20, blank=True)  # CPF/CNPJ
    oab = models.CharField(max_length=20, blank=True)
    tribunais = models.JSONField(default=list)  # [sigla, ...]
    is_monitored_diario = models.BooleanField(default=True)
    is_monitored_tribunal = models.BooleanField(default=False)
    is_advogado = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.nome or self.documento or self.oab or f'#{self.pk}'


class MonitoredProcess(models.Model):
    """Processo monitorado por CNJ."""
    cnj = models.CharField(max_length=25)
    tribunais = models.JSONField(default=list)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.cnj


class Detection(models.Model):
    """Detecção de uma publicação que casa um monitoramento."""
    TARGET_TERM = 'term'
    TARGET_PERSON = 'person'
    TARGET_PROC = 'proc'
    TARGET_CHOICES = [
        (TARGET_TERM, 'Termo'),
        (TARGET_PERSON, 'Pessoa'),
        (TARGET_PROC, 'Processo'),
    ]

    target_type = models.CharField(max_length=10, choices=TARGET_CHOICES)
    target_id = models.BigIntegerField()  # FK lógico pra MonitoredTerm/Person/Process
    movimentacao = models.ForeignKey(Movimentacao, on_delete=models.CASCADE, related_name='detections')
    snippet = models.TextField()
    detected_at = models.DateTimeField(auto_now_add=True)
    entregue_em = models.DateTimeField(null=True, blank=True)
    erro_entrega = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['target_type', 'target_id', 'movimentacao'],
                name='uniq_detection',
            ),
        ]
        indexes = [
            models.Index(fields=['target_type', 'target_id', '-detected_at']),
            models.Index(fields=['-detected_at']),
        ]
        ordering = ['-detected_at']

    def __str__(self):
        return f'{self.target_type}:{self.target_id} → mov {self.movimentacao_id}'


class WebhookConfig(models.Model):
    """Configuração de entrega webhook por cliente."""
    cliente = models.ForeignKey(ApiClient, on_delete=models.CASCADE, related_name='webhooks')
    url = models.URLField(max_length=2500)
    secret = models.CharField(max_length=64)  # HMAC signing
    evento_types = models.JSONField(default=list)  # ['term','person','proc']
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.cliente.nome} → {self.url[:50]}'