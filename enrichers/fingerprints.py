"""Fingerprints anti-bot exigidos por tribunal. Puro: sem Django, sem I/O.

Mora fora do módulo do enricher porque é usado por DOIS caminhos — o
enriquecimento (`enrichers/tjmt.py`) e a busca por parte
(`enrichers/busca/rest.py`) — e porque precisa rodar fora do container, no
validador da matriz. Um algoritmo, um lugar.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time

#: Chave do `environment.fingerPrint`, hardcoded no bundle da SPA do TJMT.
CHAVE_TJMT = 'A_mesma_mao_que_aplaude_e_a_que_vaia!'

_UA = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) '
       'Chrome/120.0.0.0 Safari/537.36')
_TELA = '1920x1080'
_LINGUA = 'pt-BR'


def tjmt(ts_ms: int | None = None) -> str:
    """Valor do header `X-Fingerprint` do TJMT (uma string JSON).

    Reproduz a função `Bd()` do bundle Angular:

        mensagem  = "{userAgent}-{screenResolution}-{language}-{timestamp_ms}"
        signature = base64(HMAC_SHA256(mensagem, CHAVE))
        header    = json({signature, timestamp, userAgent, screenResolution, language})

    `ts_ms` é injetável só para teste: em produção usa o relógio atual, porque o
    servidor valida uma janela de timestamp — por isso é gerado por requisição,
    nunca cacheado.
    """
    if ts_ms is None:
        ts_ms = int(time.time() * 1000)
    msg = f'{_UA}-{_TELA}-{_LINGUA}-{ts_ms}'
    assinatura = base64.b64encode(
        hmac.new(CHAVE_TJMT.encode('utf-8'), msg.encode('utf-8'),
                 hashlib.sha256).digest()).decode('ascii')
    # Sem espaços: espelha o `JSON.stringify` do JS.
    return json.dumps({
        'signature': assinatura,
        'timestamp': ts_ms,
        'userAgent': _UA,
        'screenResolution': _TELA,
        'language': _LINGUA,
    }, ensure_ascii=False, separators=(',', ':'))


# ── TJRS: ALTCHA (prova de trabalho) + credencial anônima ────────────────────
#
# A consulta processual pública do TJRS (`consulta.tjrs.jus.br/consulta-processual`,
# SPA Angular) fala com `consulta-processual-service.tjrs.jus.br`. Não há
# captcha visual: cada credencial anônima custa uma prova de trabalho ALTCHA e
# vale 7 consultas (a 8ª devolve **429 "Limite de chamadas atingido"**, medido
# em 15/10/2026). Tudo abaixo foi lido do bundle `main.*.js` da SPA (serviço
# `zt`, `generate()`/`obfuscation()`) e do web worker `334.*.js`.

def resolver_altcha(desafio: dict) -> dict:
    """Acha `n` tal que `sha256(salt + str(n)) == challenge` (hex).

    É o laço do web worker da SPA. `maxnumber` vem do servidor (1e6 hoje): o
    custo medido é de 0,2 s a 1 s por credencial em Python puro. Devolve o
    payload que o `/auth/submit` espera — ou levanta `ValueError` se o desafio
    não tem solução no intervalo (resposta adulterada, nunca visto ao vivo).
    """
    salt = desafio['salt']
    alvo = desafio['challenge']
    algoritmo = (desafio.get('algorithm') or 'SHA-256').upper()
    if algoritmo != 'SHA-256':
        raise ValueError(f'ALTCHA com algoritmo inesperado: {algoritmo}')
    teto = int(desafio.get('maxnumber') or 1_000_000)
    for n in range(teto + 1):
        if hashlib.sha256(f'{salt}{n}'.encode()).hexdigest() == alvo:
            return {'algorithm': desafio.get('algorithm') or 'SHA-256',
                    'challenge': alvo, 'number': n, 'salt': salt,
                    'signature': desafio['signature']}
    raise ValueError('ALTCHA sem solução no intervalo declarado')


def altcha_formulario(solucao: dict) -> str:
    """O campo `altcha` do POST: `btoa(JSON.stringify(solucao))`."""
    return base64.b64encode(
        json.dumps(solucao, separators=(',', ':')).encode()).decode('ascii')


#: Constantes de `obfuscation()` — `x = n % MODULO + SOMA`. **Mudam com o
#: deploy da SPA**: até 15/10/2026 eram (60029, 92120) no `main.3ce0ff771a3adcf2.js`;
#: em 16/10/2026 ~00:00 UTC o `main.667e4d4a4b94df67.js` trocou a soma para
#: 92125 e TODA credencial calculada com a antiga passou a levar 401. Quem usa
#: em produção lê do bundle vivo (`enrichers.tjrs.chaves_tjrs`); estas são só o
#: último par conhecido, para teste e para quando o bundle não responde.
CHAVES_TJRS_CONHECIDAS = (60029, 92125)

_RE_OBFUSCATION = re.compile(r'%BigInt\((\d+)\)\+BigInt\((\d+)\)')
#: O resto da fórmula — `"@"+x+"@"+(n*x)+"!"` com SHA-256. Se ISTO mudar, não
#: adianta trocar número: o formato é outro e a função tem de ser relida.
_RE_FORMATO_VT = re.compile(
    r'"@"\+\w+\.toString\(\)\+"@"\+\w+\.toString\(\)\+"!"')
_RE_BUNDLE_MAIN = re.compile(r'src="(main\.[0-9a-f]+\.js)"')


class BundleTjrsMudouError(ValueError):
    """O bundle da SPA não tem a `obfuscation()` no formato que sabemos ler."""


def bundle_principal_tjrs(html: str) -> str:
    """Nome do `main.<hash>.js` no `index.html` da SPA."""
    m = _RE_BUNDLE_MAIN.search(html or '')
    if not m:
        raise BundleTjrsMudouError('index da SPA sem main.<hash>.js')
    return m.group(1)


def chaves_do_bundle_tjrs(js: str) -> tuple[int, int]:
    """`(MODULO, SOMA)` lidos da `obfuscation()` do bundle — ou `BundleTjrsMudouError`.

    Exige UMA ocorrência do par e o formato do `vt` intacto: abster > chutar —
    duas ocorrências ou fórmula diferente é a SPA reescrita, não número trocado.
    """
    pares = _RE_OBFUSCATION.findall(js or '')
    if len(set(pares)) != 1:
        raise BundleTjrsMudouError(f'{len(pares)} ocorrências de %BigInt(m)+BigInt(s) no bundle')
    if not _RE_FORMATO_VT.search(js):
        raise BundleTjrsMudouError('formato "@x@n·x!" não encontrado na obfuscation()')
    modulo, soma = (int(v) for v in pares[0])
    return modulo, soma


def credencial_tjrs(username: str, chaves: tuple[int, int] = CHAVES_TJRS_CONHECIDAS) -> str:
    """Valor do `Authorization: Basic …` a partir do `username` do submit.

    Reproduz `obfuscation()` do bundle (com `chaves = (MODULO, SOMA)` lidos dele):

        n  = BigInt(username sem "ChaAnon_")
        x  = n % MODULO + SOMA
        vt = "@" + x + "@" + (n * x) + "!"
        cred = btoa(username + ":" + SHA256(vt).hex)
    """
    modulo, soma = chaves
    n = int(username.replace('ChaAnon_', ''))
    x = n % modulo + soma
    vt = f'@{x}@{n * x}!'
    senha = hashlib.sha256(vt.encode()).hexdigest()
    return base64.b64encode(f'{username}:{senha}'.encode()).decode('ascii')


def comarca_tjrs(cnj: str) -> str:
    """`codComarca` que a API exige junto do número — `getCodComarcaPeloNumeroProcesso`.

    O órgão de origem do CNJ (`OOOO`) é o código da comarca no TJRS, salvo o
    2º grau (`7000` → 700) e as Turmas Recursais (`9000` → 710). Sem o código
    certo a API devolve 404 para um processo que existe.
    """
    d = ''.join(c for c in (cnj or '') if c.isdigit())
    if len(d) != 20:
        return ''
    if d.endswith('7000'):
        return '700'
    if d.endswith('9000'):
        return '710'
    return str(int(d[17:20]))
