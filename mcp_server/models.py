"""Allowlist do conector do Claude (OAuth).

O MCP autentica pelo PRÓPRIO login do dashboard: o Claude abre
`/oauth/authorize`, a pessoa entra com e-mail e senha de sempre e autoriza.
Ter conta no dashboard NÃO basta: só entra quem tem um `MCPAcesso` ativo aqui.
A allowlist é conferida na tela de autorização, de novo no consentimento, em
cada refresh e (com cache curto) em cada request — revogar no admin corta.

Mesmo desenho do `MCPAcesso` do falcon (mcp_admin/models.py), sem perfil: o
MCP do Voyager não tem dado pessoal de cliente a mascarar por perfil.
"""
from django.conf import settings
from django.db import models
from django.utils import timezone


class MCPAcesso(models.Model):
    # A identidade é o usuário do dashboard: um usuário = um acesso.
    usuario = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='acesso_mcp',
        limit_choices_to={'is_active': True},
        verbose_name='Usuário do Voyager',
    )
    ativo = models.BooleanField(default=True)
    observacao = models.TextField(blank=True, default='', verbose_name='Observação')
    criado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='acessos_mcp_criados',
        editable=False,
    )
    criado_em = models.DateTimeField(auto_now_add=True)
    revogado_em = models.DateTimeField(null=True, blank=True, editable=False)
    ultimo_uso = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        verbose_name = 'Acesso ao MCP (conector do Claude)'
        verbose_name_plural = 'Acessos ao MCP (conector do Claude)'
        ordering = ['usuario__username']

    def __str__(self):
        return self.usuario.get_username()

    @property
    def liberado(self):
        """Vale só com o cadastro ativo E a conta do dashboard ativa:
        desativar o usuário corta o MCP junto, sem passo extra."""
        return self.ativo and self.usuario.is_active

    def save(self, *args, **kwargs):
        if not self.ativo and self.revogado_em is None:
            self.revogado_em = timezone.now()
        elif self.ativo:
            self.revogado_em = None
        super().save(*args, **kwargs)
