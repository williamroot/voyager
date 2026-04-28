"""Dump DJEN dia-a-dia, paralelo, com proxies, fallback orgaoId — garantia
de cobertura 100% via probe + paginação + subdivisão.

Limites confirmados da API DJEN (empíricos):
- Cap absoluto: 10.000 itens por query (pagina × itensPorPagina ≤ 10000),
  acima HTTP 500
- `count` satura em 10000
- `itensPorPagina` máx útil = 1000 (acima: 0 itens silenciosamente)
- `orgaoId` (numérico) é filtro real; `nomeOrgao` (string) é IGNORADO
- Rate limit por IP → backoff + rotação de proxy

Algoritmo:
1. Pra cada dia, probe (1 request, itensPorPagina=1) → conta total
2. Se count < 10000: pagina com itensPorPagina=1000 até completar
3. Se count == 10000 (cap): descobre orgaoId únicos via 1ª página,
   re-consulta cada (dia, orgaoId) — granularidade fina dentro do dia
4. Salva items em JSONL (1 arquivo por janela final), append em _index.jsonl
5. Idempotente: pula arquivos existentes; index permite auditar gap

Uso:
  python manage.py djen_dump_v2 TRF1 --ultimos 30 --threads 8 --data-dir data/djen
"""
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

from django.core.management.base import BaseCommand

from djen.client import DJENClient, DjenClientError
from tribunals.models import Tribunal

logger = logging.getLogger('voyager.djen.dump')

CAP = 10_000
PAGE_SIZE = 1000
MAX_PAGINAS = 10  # 10 × 1000 = cap


class Command(BaseCommand):
    help = 'Dump DJEN paralelo com proxies — garantia 100% via probe + fallback orgaoId.'

    def add_arguments(self, parser):
        parser.add_argument('sigla')
        parser.add_argument('--ultimos', type=int, default=None,
                            help='Últimos N dias (default: usa --inicio/--fim).')
        parser.add_argument('--inicio', default=None, help='YYYY-MM-DD')
        parser.add_argument('--fim', default=None, help='YYYY-MM-DD; default: hoje.')
        parser.add_argument('--data-dir', default='data/djen')
        parser.add_argument('--threads', type=int, default=8,
                            help='Dias processados em paralelo (default 8).')
        parser.add_argument('--force', action='store_true', help='Re-baixa mesmo se exists.')

    def handle(self, *args, sigla, ultimos, inicio, fim, data_dir, threads, force, **opts):        
        t = Tribunal.objects.get(sigla=sigla)
        end = date.fromisoformat(fim) if fim else date.today()
        if ultimos:
            ini = end - timedelta(days=ultimos - 1)
        elif inicio:
            ini = date.fromisoformat(inicio)
        else:
            ini = t.data_inicio_disponivel
        if not ini:
            self.stderr.write(f'{sigla}: passe --ultimos N ou --inicio YYYY-MM-DD')
            return

        out_dir = Path(data_dir) / sigla
        out_dir.mkdir(parents=True, exist_ok=True)
        index_path = out_dir / '_index.jsonl'

        dias = [(ini + timedelta(days=i)) for i in range((end - ini).days + 1)]
        self.stdout.write(self.style.HTTP_INFO(
            f'\n=== {sigla}: {len(dias)} dias [{ini} → {end}] · '
            f'{threads} threads · output={out_dir} ===\n'
        ))

        client = DJENClient()
        t0_total = time.monotonic()
        total_dias = len(dias)
        resultados = {'ok': 0, 'skip': 0, 'erro': 0, 'items': 0, 'subdividido': 0}

        def _processar(i, dia):
            t0_dia = time.monotonic()
            concluidos = resultados['ok'] + resultados['skip'] + resultados['erro']
            restantes = total_dias - concluidos
            elapsed = time.monotonic() - t0_total
            velocidade = concluidos / elapsed if elapsed > 0 and concluidos > 0 else None
            eta = f'{restantes / velocidade:.0f}s' if velocidade else '?'
            prefix = f'[{i+1}/{total_dias}]'

            try:
                res = self._processar_dia(client, t, dia, out_dir, index_path, force)
                tempo_dia = time.monotonic() - t0_dia
                if res.get('skip'):
                    resultados['skip'] += 1
                    logger.info('%s ⏭  %s skip', prefix, dia)
                else:
                    resultados['ok'] += 1
                    resultados['items'] += res.get('items', 0)
                    if res.get('subdividido'):
                        resultados['subdividido'] += 1
                    flag = ' 🔀 subdividido' if res.get('subdividido') else ''
                    logger.info(
                        '%s ✅ %s → %s items · %.0fs · eta %s%s',
                        prefix, dia, f'{res.get("items", 0):,}', tempo_dia, eta, flag,
                    )
            except Exception as exc:
                resultados['erro'] += 1
                logger.error('%s ❌ %s → %s: %s', prefix, dia, type(exc).__name__, str(exc)[:120])

        if threads == 1:
            for i, dia in enumerate(dias):
                _processar(i, dia)
        else:
            with ThreadPoolExecutor(max_workers=threads) as pool:
                futs = {pool.submit(_processar, i, d): d for i, d in enumerate(dias)}
                for fut in as_completed(futs):
                    fut.result()  # exceções já tratadas dentro de _processar

        elapsed = time.monotonic() - t0_total
        self.stdout.write(self.style.SUCCESS(
            f'\nResumo {sigla}: ok={resultados["ok"]} skip={resultados["skip"]} '
            f'erro={resultados["erro"]} subdividido={resultados["subdividido"]} '
            f'items={resultados["items"]:,} tempo={elapsed:.1f}s '
            f'({resultados["items"]/(elapsed or 1):.0f} items/s)'
        ))

    def _processar_dia(self, client: DJENClient, tribunal: Tribunal, dia: date,
                       out_dir: Path, index_path: Path, force: bool) -> dict:
        arquivo = out_dir / f'{dia.isoformat()}.jsonl'
        if arquivo.exists() and not force:
            return {'skip': True}

        sigla_djen = tribunal.sigla_djen
        t0 = time.monotonic()

        # 1) Probe count via 1 request com itensPorPagina=1 (barato + count real)
        
        probe = client._fetch(sigla_djen, dia, dia, pagina=1, itens_por_pagina=1)
        count = int(probe.get('count') or 0)
        cap_flag = ' 🔥 CAP' if count >= CAP else ''
        logger.debug('🔍 probe %s %s → %d docs%s', tribunal.sigla, dia, count, cap_flag)

        if count == 0:
            self._salvar([], arquivo)
            self._append_index(index_path, {
                'data': dia.isoformat(), 'count': 0, 'baixados': 0,
                'ok': True, 'tempo_s': round(time.monotonic() - t0, 2),
            })
            return {'items': 0}

        if count < CAP:
            # 2) Caminho normal — pagina com itensPorPagina=1000
            items = self._coletar_paginado(
                client, sigla_djen, {'siglaTribunal': sigla_djen,
                                     'dataDisponibilizacaoInicio': dia.isoformat(),
                                     'dataDisponibilizacaoFim': dia.isoformat()},
                count, dia,
            )
            self._salvar(items, arquivo)
            self._append_index(index_path, {
                'data': dia.isoformat(), 'count': count, 'baixados': len(items),
                'ok': len(items) == count, 'tempo_s': round(time.monotonic() - t0, 2),
            })
            return {'items': len(items)}

        # 3) Cap — subdividir por orgaoId
        return self._subdividir_por_orgao(
            client, tribunal, dia, out_dir, index_path, t0,
        )

    def _coletar_paginado(self, client: DJENClient, sigla_djen: str,
                          filtros: dict, count_esperado: int, dia: date,
                          extra_params: dict = None) -> list:
        """Pagina com itensPorPagina=1000 até esgotar. Loga cada request."""
        items = []
        for pagina in range(1, MAX_PAGINAS + 1):
            payload = client._fetch(
                sigla_djen,
                date.fromisoformat(filtros['dataDisponibilizacaoInicio']),
                date.fromisoformat(filtros['dataDisponibilizacaoFim']),
                pagina=pagina,
                itens_por_pagina=PAGE_SIZE,
                extra_params=extra_params,
            )
            page_items = payload.get('items') or []
            items.extend(page_items)
            logger.debug(
                '📄 %s pg=%d +%d → %d/%d',
                dia, pagina, len(page_items), len(items), count_esperado,
            )
            if len(page_items) < PAGE_SIZE or len(items) >= count_esperado:
                break
        return items

    def _subdividir_por_orgao(self, client: DJENClient, tribunal: Tribunal,
                              dia: date, out_dir: Path, index_path: Path,
                              t0: float) -> dict:
        """Cap atingido — descobre orgaoIds via 1ª página, re-consulta cada um."""
        sigla_djen = tribunal.sigla_djen
        # Coleta inicial (10k items via paginação) pra extrair orgaoIds
        primeira_passada = self._coletar_paginado(
            client, sigla_djen,
            {'siglaTribunal': sigla_djen,
             'dataDisponibilizacaoInicio': dia.isoformat(),
             'dataDisponibilizacaoFim': dia.isoformat()},
            CAP, dia,
        )
        orgao_ids = sorted({item.get('idOrgao') for item in primeira_passada
                            if item.get('idOrgao')})
        logger.info(
            '⚠️  %s %s CAP → subdividindo %d órgãos',
            tribunal.sigla, dia, len(orgao_ids),
        )

        all_items = list(primeira_passada)
        seen_ids = {it.get('id') for it in primeira_passada}
        total_orgaos = len(orgao_ids)
        for idx, oid in enumerate(orgao_ids, 1):
            logger.debug('⚙️  %s órgão %d/%d (id=%s)', dia, idx, total_orgaos, oid)
            payload = self._fetch_orgao(client, sigla_djen, dia, oid, pagina=1)
            count_o = int(payload.get('count') or 0)
            if count_o == 0:
                continue
            if count_o >= CAP:
                logger.warning('🔥 %s órgão %s também atingiu CAP — cobertura pode ser incompleta', dia, oid)
            # Pagina o órgão se >1000
            items_o = list(payload.get('items') or [])
            for pg in range(2, min(MAX_PAGINAS, (count_o // PAGE_SIZE) + 2)):
                p = self._fetch_orgao(client, sigla_djen, dia, oid, pagina=pg)
                page = p.get('items') or []
                items_o.extend(page)
                if len(page) < PAGE_SIZE:
                    break
            for it in items_o:
                if it.get('id') not in seen_ids:
                    all_items.append(it)
                    seen_ids.add(it.get('id'))

        novos = len(all_items) - len(primeira_passada)
        cobertura = len(all_items) / len(primeira_passada) if primeira_passada else 1
        if novos == 0:
            logger.warning(
                '⚠️  %s %s subdivisão não adicionou itens novos — '
                'possível perda de cobertura (primera passada = %d, órgãos = %d)',
                tribunal.sigla, dia, len(primeira_passada), len(orgao_ids),
            )
        else:
            logger.info(
                '📊 %s %s cobertura: %d itens via %d órgãos (+%d vs primera passada, %.1fx)',
                tribunal.sigla, dia, len(all_items), len(orgao_ids), novos, cobertura,
            )

        arquivo = out_dir / f'{dia.isoformat()}.jsonl'
        self._salvar(all_items, arquivo)
        self._append_index(index_path, {
            'data': dia.isoformat(), 'count': '>= 10000', 'baixados': len(all_items),
            'subdividido': True, 'orgaos': len(orgao_ids), 'primera_passada': len(primeira_passada),
            'ok': True, 'tempo_s': round(time.monotonic() - t0, 2),
        })
        return {'items': len(all_items), 'subdividido': True}

    def _fetch_orgao(self, client: DJENClient, sigla_djen: str,
                     dia: date, orgao_id: int, pagina: int) -> dict:
        return client._fetch(
            sigla_djen, dia, dia, pagina=pagina,
            itens_por_pagina=PAGE_SIZE,
            extra_params={'orgaoId': orgao_id},
        )

    def _salvar(self, items: list, arquivo: Path) -> None:
        tmp = arquivo.with_suffix('.jsonl.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            for item in items:
                f.write(json.dumps(item, ensure_ascii=False, default=str))
                f.write('\n')
        os.replace(tmp, arquivo)

    def _append_index(self, index_path: Path, registro: dict) -> None:
        with open(index_path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(registro, ensure_ascii=False, default=str))
            f.write('\n')
