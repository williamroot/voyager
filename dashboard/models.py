"""Models do dashboard — sessões do chat de jurimetria.

Espelha o padrão do Horizon/smart-mail (ChatSession/ChatMessage com content_json
em blocks), adaptado: role só user|assistant; tool_use/tool_result ficam como
blocks DENTRO da mensagem assistant (reidratam os chips na UI ao reabrir).
"""
from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models


class ChatSession(models.Model):
    """Uma conversa do chat de jurimetria — sempre de UM usuário."""

    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name='chat_sessions')
    title = models.CharField(max_length=255, default='Nova conversa')
    # CNJ que originou a conversa (botão "Conversar" no dossiê) — entra no system prompt
    cnj_contexto = models.CharField(max_length=30, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    last_message_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-last_message_at', '-created_at']
        indexes = [models.Index(fields=['user', '-last_message_at'])]

    def __str__(self) -> str:
        return f'{self.title} ({self.user_id})'


class ChatFile(models.Model):
    """Arquivo anexado numa conversa do chat. Guardamos o TEXTO extraído (é o que
    o agente lê via tool `ler_arquivo`) — o binário original não é persistido."""

    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name='chat_files')
    filename = models.CharField(max_length=255)
    mime = models.CharField(max_length=100, blank=True, default='')
    texto = models.TextField(blank=True, default='')
    chars = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f'{self.filename} ({self.chars} chars)'


class ChatMessage(models.Model):
    """Uma mensagem da conversa. content_json = {'blocks': [...]} — blocks de tipo
    'text' (sempre) e, na assistant, 'tool_use'/'tool_result' intercalados."""

    session = models.ForeignKey(ChatSession, on_delete=models.CASCADE,
                                related_name='messages')
    role = models.CharField(max_length=16)  # user | assistant
    content_json = models.JSONField(default=dict)
    model = models.CharField(max_length=64, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['id']
        indexes = [models.Index(fields=['session', 'id'])]

    def __str__(self) -> str:
        return f'{self.role}@{self.session_id}#{self.pk}'

    def texto(self) -> str:
        """Concatena os blocks de texto (pro contexto do LLM e pra busca)."""
        blocks = (self.content_json or {}).get('blocks') or []
        return '\n'.join(b.get('text', '') for b in blocks if b.get('type') == 'text').strip()
