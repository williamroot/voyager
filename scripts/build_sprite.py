"""One-shot: gera dashboard/static/dashboard/voyager-icons.svg do Lucide.

Mantém os IDs voy-{alias} pra não precisar mudar nenhum template.
"""
from __future__ import annotations
import re
import sys
import urllib.request
import urllib.error
import concurrent.futures as cf

LUCIDE_VERSION = '0.460.0'
CDN = f'https://cdn.jsdelivr.net/npm/lucide-static@{LUCIDE_VERSION}/icons'

# alias_voyager -> nome_lucide_principal (+ fallbacks se algum não existir)
MAPPING = [
    # 1:1 / quase 1:1
    ('telescope',     ['telescope']),
    ('moon',          ['moon']),
    ('sun',           ['sun']),
    ('radar',         ['radar']),
    # Espaciais (semânticos)
    ('probe',         ['satellite']),
    ('pulsar',        ['radio-tower']),
    ('constellation', ['share-2']),
    ('trajectory',    ['route']),
    ('transmission',  ['radio']),
    # Painel / mission
    ('calibrate',     ['sliders-horizontal']),
    ('dossier',       ['file-text']),
    ('mission-tag',   ['tag']),
    ('retrograde',    ['rotate-ccw']),
    ('anomaly',       ['triangle-alert', 'alert-triangle']),
    # Sinais
    ('signal-ok',     ['wifi']),
    ('signal-lost',   ['wifi-off']),
    ('uplink',        ['arrow-up-to-line']),
    ('downlink',      ['arrow-down-to-line']),
    # Utilitários
    ('eject',         ['log-out']),
    ('arrow',         ['arrow-right']),
    ('clear',         ['x']),
    # ── Substituição de emojis (badges de nível + famílias + features) ──
    # Badges dos 4 níveis
    ('gem',           ['gem']),                      # 💎 PRECATÓRIO
    ('hourglass',     ['hourglass']),                # ⏳ PRÉ-PRECATÓRIO
    ('sprout',        ['sprout']),                   # 🌱 DIREITO CREDITÓRIO
    ('ban',           ['ban']),                      # 🚫 NÃO LEAD
    # Famílias de features
    ('scale',         ['scale']),                    # ⚖️ classe
    ('scroll-text',   ['scroll-text']),              # 📜 texto
    ('trending-up',   ['trending-up']),              # 📈 volume
    ('history',       ['history']),                  # 🕰️ recência
    ('link-2',        ['link-2', 'link']),           # 🔗 interação
    ('flask',         ['flask-conical']),            # 🧪 v7
    # Features individuais
    ('send',          ['send']),                     # 📤 envTrib
    ('search',        ['search']),                   # 🔎 texto/regex
    ('tornado',       ['tornado']),                  # 🌀 variedade
    ('target',        ['target']),                   # 🎯 N1count
    ('calendar',      ['calendar']),                 # 📅 ano
    ('circle-x',      ['circle-x']),                 # ❌ cancelado
    ('circle-check',  ['circle-check']),             # ✅ juriscope
    ('users',         ['users']),                    # 👥 partes
    ('sparkles',      ['sparkles']),                 # 🆕 v7 features
]


def fetch_one(alias: str, candidates: list[str]) -> tuple[str, str, str]:
    """Retorna (alias, lucide_usado, inner_svg). Tenta cada candidato até 200."""
    last_err = ''
    for name in candidates:
        url = f'{CDN}/{name}.svg'
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                if resp.status != 200:
                    last_err = f'HTTP {resp.status} em {url}'
                    continue
                raw = resp.read().decode('utf-8')
                # Extrai só o conteúdo interno do <svg>...</svg>
                m = re.search(r'<svg[^>]*>(.*)</svg>', raw, re.DOTALL)
                if not m:
                    last_err = f'sem <svg> em {url}'
                    continue
                inner = m.group(1).strip()
                # Limpa whitespace excessivo entre tags
                inner = re.sub(r'>\s+<', '><', inner)
                return alias, name, inner
        except urllib.error.HTTPError as e:
            last_err = f'HTTP {e.code} em {url}'
        except Exception as e:
            last_err = f'{type(e).__name__}: {e}'
    raise RuntimeError(f'Falha em {alias}: {last_err}')


def main() -> int:
    results: dict[str, tuple[str, str]] = {}
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(fetch_one, a, c): a for a, c in MAPPING}
        for fut in cf.as_completed(futures):
            try:
                alias, lucide_name, inner = fut.result()
                results[alias] = (lucide_name, inner)
                print(f'  ✓ {alias:14s} ← lucide:{lucide_name}', file=sys.stderr)
            except Exception as e:
                print(f'  ✗ {futures[fut]}: {e}', file=sys.stderr)
                return 1

    # Monta sprite na mesma ordem do MAPPING (estável)
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!--',
        f'  voyager-icons.svg — sprite gerado a partir do Lucide v{LUCIDE_VERSION} (ISC).',
        '  Cada <symbol id="voy-{alias}"> mapeia 1 ícone do Voyager pra 1 ícone do Lucide.',
        '  Gerado por scripts/build_sprite.py. NÃO editar à mão — re-rodar o script.',
        '',
        '  Estilo base: stroke=currentColor, stroke-width=2, fill=none, linecap/join=round, viewBox=24×24.',
        '  Tailwind: cor via text-* (currentColor), tamanho via w-*/h-*.',
        '',
        '  Mapping alias → Lucide:',
    ]
    for alias, _ in MAPPING:
        parts.append(f'    voy-{alias:<14s} = lucide:{results[alias][0]}')
    parts += [
        '-->',
        '<svg xmlns="http://www.w3.org/2000/svg" style="display:none">',
        '  <defs>',
    ]
    for alias, _ in MAPPING:
        _, inner = results[alias]
        parts.append(
            f'    <symbol id="voy-{alias}" viewBox="0 0 24 24" fill="none" '
            f'stroke="currentColor" stroke-width="1.6" '
            f'stroke-linecap="round" stroke-linejoin="round">{inner}</symbol>'
        )
    parts.append('  </defs>')
    parts.append('</svg>')
    parts.append('')

    out_path = '/home/will/projetos/voyager/dashboard/static/dashboard/voyager-icons.svg'
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(parts))
    print(f'\nEscrito: {out_path} ({len(results)} símbolos)', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
