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


# --------------------------------------------------------------------------- #
# 29/08/2026 — a varredura dos OUTROS escritores em lote
# --------------------------------------------------------------------------- #
# A campainha tinha sido posta em dois escritores (`backfill_grau` e
# `promover_lote`). A varredura de `.update()` / `bulk_update` / SQL cru sobre
# `tribunals_process` achou mais SETE, e três deles rodam SOZINHOS, todo dia:
#
#   djen/ingestion.py::_flush_resumo     total_movimentacoes, ultima_movimentacao_em
#   djen/ingestion.py::ingest_processo   data_enriquecimento_djen (→ enriquecido_em)
#   enrichers/jobs.py (3 lugares)        enriquecimento_status, enriquecido_em
#   tribunals/classificador.py           classificacao, score, versao, em
#   backfill_assunto.py                  assunto, assunto_codigo
#   preencher_classe_via_djen.py         classe_nome, codigo_classe
#   backfill_sinal_precatorio.py         tem_sinal_precatorio
#
# Todo campo dessa lista está em `search/documents.py::processo_to_doc`. Sem a
# campainha eles mudam no Postgres e o documento guarda o valor da véspera —
# `sync_processos_atualizados` é keyset por `atualizado_em` e não os enxerga,
# e `sync_processos_novos` só pega pk ACIMA da watermark.
#
# Os testes são por LEITURA DE FONTE, igual aos de cima: o que se quer travar é
# que ninguém apague a linha da campainha, e isso não depende de rodar o job.

def test_flush_resumo_toca_campainha():
    """A ingestão DJEN reescreve o resumo todo dia — e é o maior escritor."""
    fonte = open('djen/ingestion.py').read()
    i = fonte.find('CAMPOS_RESUMO = [')
    assert i > 0, 'CAMPOS_RESUMO sumiu — teste desatualizado'
    assert "'atualizado_em'" in fonte[i:i + 400], (
        'CAMPOS_RESUMO sem `atualizado_em`: total_movimentacoes e '
        'ultima_movimentacao_em mudam no banco e o doc fica da véspera')
    j = fonte.find('p.data_enriquecimento_djen = now_ts')
    assert j > 0
    assert 'p.atualizado_em = now_ts' in fonte[j:j + 300], (
        'o objeto do bulk_update não recebe `atualizado_em` — a coluna entra '
        'no UPDATE com o valor antigo e a campainha não toca')


def test_ingest_processo_toca_campainha():
    fonte = open('djen/ingestion.py').read()
    i = fonte.find('data_enriquecimento_djen=now_ts,')
    assert i > 0
    assert 'atualizado_em=now_ts' in fonte[i:i + 400]


def test_enrichers_jobs_tocam_campainha():
    """`enriquecimento_status`/`enriquecido_em` são campos do doc."""
    fonte = open('enrichers/jobs.py').read()
    updates = [i for i in range(len(fonte))
               if fonte.startswith('Process.objects.filter(pk__in=', i)]
    assert len(updates) >= 3, 'os `.update()` do enricher mudaram — teste desatualizado'
    for i in updates:
        trecho = fonte[i:i + 500]
        assert 'atualizado_em=' in trecho, (
            f'`.update()` de Process sem campainha em enrichers/jobs.py '
            f'perto de {fonte[i:i + 80]!r}')


def test_classificador_toca_campainha():
    """A reclassificação em lote já mediu -26% de defasagem no índice."""
    fonte = open('tribunals/classificador.py').read()
    i = fonte.find('classificacao_versao=versao_em_uso,')
    assert i > 0
    assert 'atualizado_em=now' in fonte[i:i + 400], (
        'classificação gravada sem campainha — foi assim que o QA mediu '
        'ES 35k contra PG 47,6k confirmados')


def test_backfill_assunto_toca_campainha():
    fonte = open('enrichers/management/commands/backfill_assunto.py').read()
    i = fonte.find('objs.append((p,')
    assert i > 0
    assert 'p.atualizado_em = agora' in fonte[max(0, i - 600):i]
    assert "{'atualizado_em'}" in fonte[i:i + 200], (
        '`atualizado_em` fora da lista de campos do bulk_update = coluna não '
        'entra no UPDATE')


def test_preencher_classe_toca_campainha():
    fonte = open('tribunals/management/commands/preencher_classe_via_djen.py').read()
    updates = fonte.count('UPDATE tribunals_process p')
    assert updates == 2, 'os UPDATEs mudaram — teste desatualizado'
    assert fonte.count('atualizado_em') >= 2, (
        'classe preenchida sem campainha: certa no banco, vazia na busca')


def test_backfill_sinal_toca_campainha():
    fonte = open('tribunals/management/commands/backfill_sinal_precatorio.py').read()
    i = fonte.find('sql_upd = (')
    assert i > 0
    assert 'atualizado_em = now()' in fonte[i:i + 500], (
        'tem_sinal_precatorio é campo do doc — sem campainha o mapa comercial '
        'continua lendo o valor velho')
