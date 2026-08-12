"""Guarda contra {# comentário #} MULTILINHA — que VAZA LITERAL na tela.

O lexer do Django (`django/template/base.py`) usa
`({%.*?%}|{{.*?}}|{#.*?#})` **sem `re.DOTALL`**: um `{# ... #}` que atravessa
linhas NÃO é reconhecido como comentário e sai como texto pro usuário.

Aconteceu de verdade (12/08/2026): a página do Mapa Comercial exibia 3 desses
em produção, incluindo uma nota técnica sobre a altura medida do gráfico logo
acima do mapa. Comentário de várias linhas em template = `{% comment %}`.
"""
import re
from pathlib import Path

import pytest

# o MESMO regex do lexer do Django — a ausência de DOTALL é o ponto
TAG_RE = re.compile(r'({%.*?%}|{{.*?}}|{#.*?#})')
RAIZ = Path(__file__).resolve().parent.parent
IGNORAR = {'.git', 'node_modules', 'staticfiles', '.venv'}


def _vazamentos(src: str) -> list[int]:
    """Linhas onde um {# ... #} multilinha vaza (o lexer não o reconhece)."""
    out = []
    for m in re.finditer(r'\{#', src):
        fim = src.find('#}', m.end())
        if fim == -1:
            continue
        trecho = src[m.start():fim + 2]
        if '\n' in trecho and not TAG_RE.match(trecho):
            out.append(src[:m.start()].count('\n') + 1)
    return out


def test_detector_pega_o_caso_conhecido():
    """Controle positivo: sem isto, um detector quebrado passaria calado."""
    assert _vazamentos('A{# duas\nlinhas #}B') == [1]
    assert _vazamentos('A{# uma linha #}B') == []          # 1 linha: ok
    assert _vazamentos('{% comment %}\nlivre\n{% endcomment %}') == []


@pytest.mark.parametrize('html', sorted(
    p for p in RAIZ.rglob('*.html') if not IGNORAR & set(p.parts)
), ids=lambda p: str(p.relative_to(RAIZ)))
def test_sem_comentario_multilinha_vazando(html):
    linhas = _vazamentos(html.read_text(encoding='utf-8', errors='replace'))
    assert not linhas, (
        f'{html.relative_to(RAIZ)}: {{# #}} multilinha nas linhas {linhas} '
        f'VAZA literal na tela (lexer do Django não usa DOTALL). '
        f'Use {{% comment %}} … {{% endcomment %}}.'
    )
