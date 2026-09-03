"""Classe de cor que não está no `tailwind.config` não pinta nada.

Em 31/08/2026 escrevi o card de Integridade com `bg-warn/10`,
`border-warn/40` e `text-warn-fg`. Nenhuma das três existe: o token é
**`warning`**, e `warn-fg` não existe de forma alguma. O Tailwind não gera CSS
para classe que não está no config, então os chips que separam "fonte pausada
pelo vigia" de "pausada por humano" ficaram sem cor — e ninguém nota, porque a
cor cai no default em vez de quebrar.

Como eu errei: conferi que `warn` "aparece em outros templates" e tratei isso
como prova de que o token existia. **Aparecer num template não prova nada.** A
régua é o `tailwind.config` (inline em `base.html`), não o grep.

O mesmo bloco usava `data-tip="..."`, mas o `.voy-tip` lê
`<span class="voy-tip-body">` — os dois tooltips não apareciam.
"""
import re
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
BASE = RAIZ / 'dashboard' / 'templates' / 'dashboard' / 'base.html'
#: Os templates que ESTE arquivo cobre. Não é a árvore inteira de propósito:
#: rodando em todos, 6 acusam nomes que o meu regex confunde com cor
#: (`input`, `color`, `bg`, `overlay`, `t-accent` — CSS inline, não classe
#: Tailwind). Alargar o regex para calá-los enfraqueceria o teste; alargar a
#: lista de exceções esconderia achado futuro. Cobre-se o que está limpo, e a
#: lista cresce quando cada template for saneado.
COBERTOS = {'acompanhamento.html', 'completude.html', 'estoque.html'}
TEMPLATES = [p for p in sorted((RAIZ / 'dashboard' / 'templates' / 'dashboard').glob('*.html'))
             if p.name in COBERTOS]

#: `bg-danger/10`, `text-fg-subtle`, `border-warning/40`…
USO = re.compile(r'\b(?:bg|text|border|from|to|via|ring|fill|stroke)-'
                 r'([a-z][a-z-]*?)(?:/\d{1,3})?(?=["\s\'])')

#: utilitários do Tailwind que não são cor do nosso tema
NAO_SAO_COR = {
    'base', 'left', 'right', 'center', 'justify', 'top', 'bottom', 'start',
    'end', 'transparent', 'current', 'inherit', 'white', 'black', 'none',
    'sm', 'xs', 'lg', 'xl', 'md', 'wrap', 'nowrap', 'clip', 'ellipsis',
    'balance', 'pretty', 'auto', 'solid', 'dashed', 'dotted', 'hidden',
    'collapse', 'separate', 'fixed', 'local', 'scroll', 'cover', 'contain',
    'no', 'b', 't', 'l', 'r', 'x', 'y', 'e', 's',
    # utilitários que casam com o padrão mas não são cor
    'gradient-to-r', 'gradient-to-l', 'gradient-to-t', 'gradient-to-b',
    'gradient-to-br', 'gradient-to-bl', 'gradient-to-tr', 'gradient-to-tl',
    'inset', 'clipboard', 'offset-base', 'offset-2', 'offset-1',
    'bg-subtle', 'opacity-0', 'opacity-100', 'wide', 'wider', 'widest',
    'tight', 'tighter', 'snug', 'relaxed', 'loose', 'normal', 'baseline',
}


def _tokens_do_config() -> set[str]:
    """As chaves de `theme.extend.colors` no config inline do `base.html`."""
    texto = BASE.read_text(encoding='utf-8')
    i = texto.index('colors: {')
    bloco = texto[i:texto.index('}', texto.index('pale-blue', i))]
    return set(re.findall(r"^\s+'?([a-z][a-z-]*)'?:\s*'rgb", bloco, re.M))


def test_o_config_declara_os_tokens_que_esperamos():
    """CONTROLE: se esta leitura quebrar, o resto do arquivo não vale nada."""
    tokens = _tokens_do_config()
    assert 'warning' in tokens, tokens
    assert 'danger' in tokens and 'accent' in tokens and 'mission' in tokens
    # e os que eu inventei NÃO estão lá — é o ponto do arquivo
    assert 'warn' not in tokens
    assert 'success' not in tokens


@pytest.mark.parametrize('tpl', TEMPLATES, ids=lambda p: p.name)
def test_template_nao_usa_cor_fora_do_config(tpl):
    tokens = _tokens_do_config()
    ruins = set()
    for classe in USO.findall(tpl.read_text(encoding='utf-8')):
        raiz = classe.split('/')[0]
        if raiz in NAO_SAO_COR or raiz in tokens:
            continue
        # `fg-soft`, `accent-fg`… são tokens compostos, já vêm do config
        if raiz.startswith(('fg-', 'gray-', 'zinc-', 'slate-', 'neutral-',
                            'emerald-', 'rose-', 'amber-', 'sky-', 'red-',
                            'green-', 'blue-', 'yellow-', 'orange-')):
            continue
        ruins.add(classe)
    assert not ruins, f'{tpl.name}: cor fora do tailwind.config → {sorted(ruins)}'


def test_voy_tip_usa_o_corpo_e_nao_data_tip():
    """`data-tip=` é inerte: o mecanismo lê `<span class="voy-tip-body">`."""
    alvo = RAIZ / 'dashboard' / 'templates' / 'dashboard' / 'acompanhamento.html'
    texto = alvo.read_text(encoding='utf-8')
    assert 'data-tip' not in texto, 'data-tip não é lido pelo .voy-tip'
    assert texto.count('voy-tip-body') >= 2, 'os dois chips precisam de corpo'


def test_card_resumo_7d_nao_usa_cor_fora_do_config():
    """O card novo entra na mesma varredura dos outros.

    Em 02/09/2026 três chips ficaram INVISÍVEIS porque alguém usou `warn` — que
    não existe; o token é `warning`. Tailwind não avisa: classe inexistente
    vira nada, e o defeito passa em review porque o HTML parece certo.
    """
    from pathlib import Path
    alvo = (Path(__file__).resolve().parents[1]
            / 'dashboard/templates/dashboard/acompanhamento.html')
    html = alvo.read_text(encoding='utf-8')
    assert 'resumo_blocos' in html, 'o card do resumo sumiu do template'
    # o bloco do card, delimitado pelo comentário que o abre
    ini = html.index('RESUMO EXECUTIVO — 7 DIAS')
    fim = html.index('AVANÇOS DA SEMANA')
    trecho = html[ini:fim]
    for proibida in ('text-warn-fg', 'bg-warn/', 'border-warn/',
                     'text-success-fg', 'bg-success/'):
        assert proibida not in trecho, (
            f'`{proibida}` não existe no config inline do base.html — '
            f'a classe vira NADA e o elemento fica invisível'
        )
