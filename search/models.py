"""Estado DURÁVEL da busca — hoje, só a watermark do sync incremental.

Por que uma tabela, e não o cache: em 26/08/2026 às 06:59:02 UTC o Redis de
produção (`192.168.30.100`) reiniciou. Ele roda com `save ""` e
`appendonly no` — **persistência nenhuma** —, então o restart não perdeu
"algumas chaves": ele zerou o keyspace inteiro, as três watermarks do
`search/sync_incremental.py` junto. E o desenho de lá tratava chave ausente
como PRIMEIRO TIQUE DA VIDA DO SISTEMA: ancorava em `agora`/`max(id)` e seguia.
Como o keyset só anda para frente, tudo que foi escrito antes daquele instante
nunca mais seria revisitado.

Não é hipótese: `maxmemory-policy=noeviction`, `evicted_keys=0` e `ttl=-1` nas
três chaves descartam eviction e expiração. O que restou foi o restart, e o
restart é fatal por configuração.

A tabela é a fonte da verdade; o cache continua sendo o caminho rápido. Chave
que some do Redis é RESTAURADA daqui — e a ausência de linha aqui é a única
coisa no mundo que significa "primeiro tique".
"""
from django.db import models


class Watermark(models.Model):
    """Watermark de sync, durável.

    `valor` é JSON com o codec de `search/watermarks.py` (inteiro puro ou o par
    `(atualizado_em, id)`), porque JSON não carrega `datetime` sozinho.

    `ancorada_em` é o carimbo do PRIMEIRO tique — ele nunca é reescrito. É o
    que separa "nunca ancorei" (linha inexistente) de "ancorei e depois perdi
    o valor" (linha existe, `valor` nulo), que é justamente a distinção que
    faltava e que transformava perda de chave em perda de acervo.
    """

    chave = models.CharField(max_length=64, primary_key=True)
    valor = models.JSONField(null=True, blank=True)
    ancorada_em = models.DateTimeField(auto_now_add=True)
    atualizada_em = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'search_watermark'
        verbose_name = 'watermark de sync'
        verbose_name_plural = 'watermarks de sync'

    def __str__(self) -> str:
        return f'{self.chave}={self.valor}'
