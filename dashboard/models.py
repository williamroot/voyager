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


class ShowcaseAnalise(models.Model):
    """Uma análise (extração) SALVA da Showcase do Extrator — compartilhável por UUID.

    Persiste a ficha completa (``resultado``, que ``renderFicha`` consome) + o
    "quem/quando/o quê/quanto tempo/qual modelo". A URL pública usa o ``uuid``
    (não a PK sequencial). Compartilhável entre usuários da plataforma (login).
    """

    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                on_delete=models.SET_NULL, related_name='showcase_analises')
    criado_em = models.DateTimeField(auto_now_add=True)

    arquivo = models.CharField(max_length=255)
    content_type = models.CharField(max_length=120, blank=True, default='')
    tamanho_bytes = models.BigIntegerField(default=0)
    sha256 = models.CharField(max_length=64, blank=True, default='')

    versao = models.CharField(max_length=20, blank=True, default='')       # chave do modelo (v21…)
    modelo_label = models.CharField(max_length=120, blank=True, default='')
    elapsed_ms = models.IntegerField(default=0)          # round-trip web↔pod
    tempos = models.JSONField(default=dict, blank=True)  # tempo REAL do modelo (total_s, n_paginas…)

    n_partes = models.IntegerField(default=0)
    n_docs = models.IntegerField(default=0)
    paginas = models.IntegerField(default=0)
    duracao_s = models.FloatField(default=0)             # tempo REAL do modelo (tempos.total_s) — desnormalizado p/ listar sem tocar o JSON

    # ── facetas desnormalizadas (denorm) — extraídas do ``resultado`` no save.
    # Rodará em MILHÕES de processos: a listagem NUNCA faz parse de JSON por linha;
    # lê estas colunas indexadas. Preenchidas por ``_derivar_facetas`` (showcase_jobs).
    tem_cessao = models.BooleanField(default=False)      # há cessão de crédito na ficha
    oficio_emitido = models.BooleanField(default=False)  # ofício requisitório / precatório expedido
    calculos_homologados = models.BooleanField(null=True)  # cálculos homologados: True/False/None(=não identificado)
    estagio = models.CharField(max_length=32, blank=True, default='')  # código do estágio (PRECATORIO_EMITIDO, PAGO…)
    parte_ativa = models.CharField(max_length=180, blank=True, default='')   # 1 parte do polo ATIVO (quem recebe)
    parte_passiva = models.CharField(max_length=180, blank=True, default='') # 1 parte do polo PASSIVO (quem paga)

    resultado = models.JSONField(default=dict)           # a ficha completa (renderFicha)
    upload_id = models.CharField(max_length=64, blank=True, default='')
    arquivo_path = models.CharField(max_length=255, blank=True, default='')  # cópia persistente (rel. MEDIA_ROOT) p/ reprocessar

    class Meta:
        ordering = ['-criado_em']
        indexes = [
            models.Index(fields=['-criado_em'], name='showanalise_criado_idx'),
            models.Index(fields=['usuario', '-criado_em'], name='showanalise_user_criado_idx'),
            # facetas de filtro — compostas com -criado_em p/ a lista filtrada seguir
            # index-ordered em escala (milhões). Nomes/tempo são display-only → sem índice.
            models.Index(fields=['tem_cessao', '-criado_em'], name='showanalise_cessao_idx'),
            models.Index(fields=['oficio_emitido', '-criado_em'], name='showanalise_oficio_idx'),
            models.Index(fields=['calculos_homologados', '-criado_em'], name='showanalise_homolog_idx'),
            models.Index(fields=['estagio', '-criado_em'], name='showanalise_estagio_idx'),
        ]

    def __str__(self) -> str:
        return f'{self.arquivo} · {self.versao} · {self.criado_em:%Y-%m-%d %H:%M}'
