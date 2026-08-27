"""Escrita em lote tem que TOCAR A CAMPAINHA, senão o dado é invisível.

CONTEXTO (medido em 27/08/2026, em produção, processo a processo).

`sync_processos_atualizados` é keyset por `Process.atualizado_em`. Mas
`atualizado_em` é `auto_now`, e **`auto_now` só roda em `Model.save()`** — nem
`.update()`, nem SQL cru, nem `bulk_create` numa tabela relacionada disparam.

Resultado, com o backfill de partes rodando havia dois dias:

    Process 57016866 ... 100 ProcessoParte no Postgres ... 0 no índice
    Process 57011804 ...  79                            ... 0
    Process 57013950 ...  78                            ... 0
    Process 57000736 ...  53                            ... 0
    Process 57003117 ...  51                            ... 0

Não era o teto do tique, nem o dreno, nem a fila: era a campainha. 2,6 milhões
de `ProcessoParte` coletadas de graça, perfeitas no banco, e a tela sem nenhuma.

O que estes testes protegem:
  1. `promover_lote` marca `atualizado_em` dos processos que tocou;
  2. marca DENTRO da mesma transação do `bulk_create` (campainha sem dado é
     reindex à toa; dado sem campainha é o buraco);
  3. `--dry-run` NÃO toca (já criamos 39.303 linhas órfãs com um dry-run que
     escrevia — `.ia/PATTERNS.md`);
  4. o backfill de `grau` também toca, no mesmo UPDATE do `grau`.
"""
import re
from unittest.mock import MagicMock, patch

import pytest


def test_backfill_grau_marca_atualizado_em():
    """O UPDATE do `grau` tem que carregar `atualizado_em = now()` junto."""
    fonte = open('datajud/management/commands/backfill_grau.py').read()
    i = fonte.find('UPDATE tribunals_process p SET grau')
    assert i > 0, 'o UPDATE do grau sumiu — teste desatualizado'
    trecho = fonte[i:i + 260]
    assert 'atualizado_em = now()' in trecho, (
        'o grau é gravado sem tocar a campainha: fica certo no banco e velho na busca')


def test_promover_lote_marca_atualizado_em_no_mesmo_bloco():
    """A campainha tem que estar DENTRO do `with transaction.atomic()` do write."""
    fonte = open('tribunals/services/partes_djen.py').read()
    i = fonte.find('ProcessoParte.objects.bulk_create')
    assert i > 0
    # do bulk_create até o fim do bloco (a primeira linha que volta pra indentação
    # de método, `        res.` com 8 espaços)
    fim = fonte.find('\n        res.processos_tocados', i)
    assert fim > i, 'estrutura do bloco mudou — teste desatualizado'
    bloco = fonte[i:fim]
    assert 'atualizado_em = now()' in bloco, (
        'bulk_create de ProcessoParte sem campainha — 2,6 M de partes invisíveis')
    assert 'UPDATE tribunals_process' in bloco


def test_campainha_nao_toca_em_dry_run():
    """Um `--dry-run` que escreve já custou 39.303 `Parte` órfãs em produção."""
    fonte = open('tribunals/management/commands/tocar_campainha.py').read()
    assert "if o['dry_run']" in fonte
    # no ramo de dry-run a consulta tem que ser SELECT, nunca UPDATE
    i = fonte.find("if o['dry_run']:\n                sql = ")
    assert i > 0, 'o ramo de dry-run mudou — teste desatualizado'
    assert 'SELECT count(*)' in fonte[i:i + 220]
    assert 'UPDATE' not in fonte[i:i + 220]


def test_campainha_grita_quando_para_no_meio():
    """Teto atingido é ERRO com o número real — nunca `return` discreto."""
    fonte = open('tribunals/management/commands/tocar_campainha.py').read()
    assert 'self.stderr.write' in fonte
    assert 'faltam' in fonte and 'topo - lo' in fonte, (
        '"marquei 300 mil" sem dizer que faltam 40 milhões é o corte mudo de novo')


def test_campainha_freia_pela_fila():
    """Empurrar pra fila cheia não aumenta vazão — só esconde o atraso."""
    fonte = open('tribunals/management/commands/tocar_campainha.py').read()
    assert 'FILA_ALTA' in fonte
    assert re.search(r'if fila > FILA_ALTA', fonte), 'sem freio de fila'
