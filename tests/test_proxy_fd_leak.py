"""O cache de proxies da `requests` não pode crescer sem fim.

Por que existe um teste pra isto: em 17/08/2026 os workers `.102` começaram a
morrer com `ProxyError(... [Errno 24] Too many open files)` no meio da auditoria
do TJSP, e o watchdog marcou 2.981 runs como `failed` na mesma janela. A causa
não era proxy ruim nem rede: `session.get(proxies=...)` guarda um ProxyManager
por URL de proxy num dict que nunca encolhe, e a gente rotaciona sobre centenas
de IPs. O `Errno 24` derruba TODO request seguinte do worker — inclusive os que
iriam pra proxies saudáveis — o que é perda de cobertura silenciosa.

O teste não mede descritor (não dá, de forma portátil): mede o que os
descritores seguem, que é o tamanho do cache.
"""
import threading

from djen.proxies import MAX_PROXY_MANAGERS, sessao_rotativa


def _gerentes(sessao):
    return sessao.adapters['https://'].proxy_manager


def test_cache_de_proxies_para_de_crescer():
    s = sessao_rotativa()
    for i in range(MAX_PROXY_MANAGERS * 10):
        s.get_adapter('https://x/').proxy_manager_for(f'http://10.0.0.{i % 250}:{3000 + i}')
    assert len(_gerentes(s)) <= MAX_PROXY_MANAGERS, 'cache de proxies cresceu sem teto'


def test_proxy_repetido_nao_ocupa_duas_vagas():
    """Reusar o mesmo IP tem que reaproveitar o pool, não abrir outro."""
    s = sessao_rotativa()
    a = s.get_adapter('https://x/').proxy_manager_for('http://10.0.0.1:3128')
    b = s.get_adapter('https://x/').proxy_manager_for('http://10.0.0.1:3128')
    assert a is b
    assert len(_gerentes(s)) == 1


def test_lru_mantem_o_proxy_em_uso_e_descarta_o_antigo():
    """Quem acabou de ser usado é o que tem request em voo — não pode ser a
    vítima do despejo."""
    s = sessao_rotativa()
    ad = s.get_adapter('https://x/')
    quente = 'http://10.0.0.99:3128'
    ad.proxy_manager_for(quente)
    for i in range(MAX_PROXY_MANAGERS - 1):
        ad.proxy_manager_for(f'http://10.0.1.{i}:3128')
        ad.proxy_manager_for(quente)          # segue em uso
    ad.proxy_manager_for('http://10.0.2.1:3128')   # estoura o teto
    assert quente in _gerentes(s), 'o LRU despejou justo o proxy em uso'


def test_http_e_https_dividem_o_mesmo_orcamento():
    """Um adaptador só pros dois esquemas: senão o teto vira 2×MAX na prática."""
    s = sessao_rotativa()
    assert s.adapters['http://'] is s.adapters['https://']


def test_seguro_sob_threads():
    """`_ingest_day_por_uf` compartilha UMA sessão entre 8 fetchers de UF; o
    despejo roda no meio disso."""
    s = sessao_rotativa()
    ad = s.get_adapter('https://x/')

    def girar(base):
        for i in range(200):
            ad.proxy_manager_for(f'http://10.{base}.0.{i % 250}:3128')

    ts = [threading.Thread(target=girar, args=(n,)) for n in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(_gerentes(s)) <= MAX_PROXY_MANAGERS


def test_quem_manda_proxies_nao_usa_sessao_crua():
    """Regressão de cobertura, checada no código-fonte.

    Um cliente novo que faça `requests.Session()` cru e mande `proxies=` reabre
    o vazamento — e o sintoma só aparece horas depois, num worker que para de
    coletar. Vale mais barrar o padrão do que instanciar cada cliente (vários
    exigem tribunal/config e alguns são classes-base abstratas).
    """
    import pathlib

    raiz = pathlib.Path(__file__).resolve().parent.parent
    culpados = []
    for py in raiz.rglob('*.py'):
        if any(p in py.parts for p in ('tests', '.venv', 'venv', 'node_modules', 'migrations')):
            continue
        if py.match('djen/proxies.py'):
            continue                     # é quem DEFINE a sessão limitada
        src = py.read_text(errors='ignore')
        if 'proxies=' in src and 'requests.Session()' in src:
            culpados.append(str(py.relative_to(raiz)))
    assert not culpados, (
        'gira proxy com Session() crua (use sessao_rotativa): ' + ', '.join(culpados))
