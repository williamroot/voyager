"""OAuth 2.1 embutido: Authorization Server + Resource Server no mesmo Django.

É o que faz o conector funcionar direto no Claude (web, desktop, mobile e Claude
Code), que só fala OAuth. Mesmo desenho do MCP administrativo do Juriscope
(`juriscope/mcp/src/juriscope_mcp/oauth.py`), com uma simplificação: lá o
login mora em OUTRO processo (o painel do falcon) e volta por um ticket de uso
único trocado servidor→servidor; aqui o dashboard e o MCP são o mesmo Django,
então o `/oauth/authorize` lê a sessão do dashboard direto — sem ticket.

Fluxo:
    Claude ──DCR──► /oauth/register
    Claude ──────► /oauth/authorize ── sem sessão ──► /dashboard/login/?next=…
                   └─ allowlist (MCPAcesso) ── tela de consentimento própria
                         └─ POST /oauth/authorize (CSRF) ──302──► redirect_uri?code&state&iss
    Claude ──────► /oauth/token (PKCE) → access 1h + refresh 30d, opacos

Conformidade (spec MCP / OAuth 2.1):
- RFC 9728 (PRM) com e sem sufixo /mcp; 401 com WWW-Authenticate (server.py).
- RFC 8414 (AS metadata); RFC 9207 (`iss` na resposta de autorização).
- RFC 7591 (DCR) só para clientes públicos e redirect URIs de clientes conhecidos.
- PKCE S256 obrigatório; redirect_uri por comparação exata; code de uso único.
- RFC 8707: `resource`, quando enviado, precisa ser a URI canônica do /mcp.
- Refresh rotativo; refresh inválido/revogado -> invalid_grant; reuso derruba a família.
"""
import base64
import hashlib
import json
import logging
import re
import secrets
import time
from urllib.parse import urlencode, urlparse

from django.conf import settings
from django.contrib.auth import logout
from django.contrib.auth.views import redirect_to_login
from django.http import HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from mcp_server import acesso, tokens

logger = logging.getLogger('voyager.mcp.oauth')

TTL_CLIENT_S = 365 * 24 * 3600  # registro DCR em uso: longo, o Claude reaproveita o client_id
# Registro que nunca trocou um code vive só 24 h: o /oauth/register é público, e
# registro abandonado (ou de abuso) não pode ocupar o Redis por um ano.
TTL_CLIENT_NOVO_S = 24 * 3600
TTL_CONSENTIMENTO_S = 300  # identidade resolvida esperando o clique em "Autorizar"
TTL_CODE_S = 300
TETO_REGISTROS_MIN = 20  # por IP
TETO_REGISTROS_DIA = 500  # no total: uso real é um punhado por pessoa
# Tetos de tamanho (DoS por corpo/URI gigante no endpoint público de DCR)
TETO_CORPO_REGISTRO = 4 * 1024
TETO_CORPO_FORM = 8 * 1024
TETO_REDIRECTS = 5
TETO_URI = 512
TETO_NOME_CLIENTE = 100

# Redirects que um cliente pode registrar. Interno de propósito: só os clientes
# que a equipe usa. Loopback aceita qualquer porta (Claude Code sobe uma efêmera).
_REDIRECTS_PERMITIDOS = (
    re.compile(r'^https://claude\.ai/api/mcp/auth_callback$'),
    re.compile(r'^https://claude\.com/api/mcp/auth_callback$'),
    re.compile(r'^http://localhost(:\d{1,5})?/[\w\-./]*$'),
    re.compile(r'^http://127\.0\.0\.1(:\d{1,5})?/[\w\-./]*$'),
)

_PARAMS_OAUTH = (
    'response_type', 'client_id', 'redirect_uri', 'state',
    'code_challenge', 'code_challenge_method', 'scope', 'resource',
)

# Sem script; imagem/estilo só daqui. `form-action` vale também para o 302 que
# segue o POST do consentimento: o Chrome bloqueia o redirect para o loopback
# do Claude Code se ele não estiver listado (https: cobre o callback do claude.ai).
CSP_PAGINAS = (
    "default-src 'none'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
    "base-uri 'none'; frame-ancestors 'none'; "
    "form-action 'self' https: http://localhost:* http://127.0.0.1:*"
)

_NO_STORE = {'Cache-Control': 'no-store', 'Pragma': 'no-cache'}


def base():
    return settings.MCP_PUBLIC_BASE_URL


def resource_canonico():
    """URI canônica do recurso protegido (RFC 8707): exatamente a URL do conector."""
    return f'{base()}/mcp'


def desafio():
    """WWW-Authenticate com PRM (RFC 9728): é assim que o Claude descobre o OAuth."""
    return (
        f'Bearer resource_metadata="{base()}/.well-known/oauth-protected-resource/mcp", '
        f'scope="{tokens.ESCOPO}"'
    )


def _sha(valor):
    return hashlib.sha256(valor.encode()).hexdigest()


def _json(dado, status=200):
    return JsonResponse(dado, status=status, headers=_NO_STORE)


def _desligado():
    return _json({'error': 'temporarily_unavailable',
                  'error_description': 'OAuth do MCP não configurado (MCP_PUBLIC_BASE_URL)'}, 503)


def _pagina(request, titulo, texto, *, status=400, etiqueta_ruim=True, **extra):
    resp = render(request, 'mcp_server/oauth.html', {
        'titulo': titulo, 'texto': texto, 'etiqueta_ruim': etiqueta_ruim, **extra,
    }, status=status)
    # Tela de autorização nunca pode ser iframada (clickjacking) nem cacheada.
    resp['X-Frame-Options'] = 'DENY'
    resp['Content-Security-Policy'] = CSP_PAGINAS
    resp['Cache-Control'] = 'no-store'
    # NÃO `no-referrer`: com ele o navegador manda `Origin: null` no POST do
    # formulário e o CSRF do Django recusa o "Autorizar". Nada secreto vai na
    # URL desta tela (o ticket viaja no corpo do form).
    resp['Referrer-Policy'] = 'same-origin'
    return resp


def ip_cliente(request):
    """IP de quem chamou, para rate limit do DCR.

    O único caminho até o web é o túnel do Cloudflare, que SEMPRE sobrescreve
    `CF-Connecting-IP` com o IP real. `X-Forwarded-For` não é usado: o valor
    mais à esquerda dele é do cliente.
    """
    meta = request.META
    return (meta.get('HTTP_CF_CONNECTING_IP') or meta.get('HTTP_X_REAL_IP')
            or meta.get('REMOTE_ADDR') or 'desconhecido').strip()[:64]


def _corpo_grande(request, teto):
    """O WSGI lê no máximo CONTENT_LENGTH bytes (LimitedStream): o declarado
    não tem como mentir pra menos. Não tocar em `request.body` aqui — o CSRF
    já leu o stream nos POSTs de formulário, e o acesso levantaria."""
    declarado = request.META.get('CONTENT_LENGTH', '')
    return declarado.isdigit() and int(declarado) > teto


# ------------------------------------------------------------------ metadata (RFC 9728/8414)

@require_http_methods(['GET'])
def protected_resource_metadata(request):
    if not base():
        return _desligado()
    return JsonResponse({
        'resource': resource_canonico(),
        'authorization_servers': [base()],
        'bearer_methods_supported': ['header'],
        'scopes_supported': [tokens.ESCOPO],
        'resource_name': 'Voyager (acervo judicial, somente leitura)',
    })


@require_http_methods(['GET'])
def authorization_server_metadata(request):
    if not base():
        return _desligado()
    b = base()
    return JsonResponse({
        'issuer': b,
        'authorization_endpoint': f'{b}/oauth/authorize',
        'token_endpoint': f'{b}/oauth/token',
        'registration_endpoint': f'{b}/oauth/register',
        'response_types_supported': ['code'],
        'response_modes_supported': ['query'],
        'grant_types_supported': ['authorization_code', 'refresh_token'],
        'code_challenge_methods_supported': ['S256'],
        'token_endpoint_auth_methods_supported': ['none'],
        'scopes_supported': [tokens.ESCOPO],
        'authorization_response_iss_parameter_supported': True,
    })


# ------------------------------------------------------------------ DCR (RFC 7591)

def redirect_permitido(uri):
    # fullmatch: `$` do re.match aceita um "\n" no fim da URI
    return len(uri) <= TETO_URI and any(p.fullmatch(uri) for p in _REDIRECTS_PERMITIDOS)


def _pode_registrar(ip):
    k = tokens.chave('registro', ip)
    total = tokens.r().incr(k)
    if total == 1:
        tokens.r().expire(k, 60)
    if total > TETO_REGISTROS_MIN:
        return False
    k_dia = tokens.chave('registro', 'dia', time.strftime('%Y%m%d', time.gmtime()))
    no_dia = tokens.r().incr(k_dia)
    if no_dia == 1:
        tokens.r().expire(k_dia, 2 * 24 * 3600)
    if no_dia > TETO_REGISTROS_DIA:
        logger.warning('oauth: teto diário de registros DCR (%s)', no_dia)
        return False
    return True


@csrf_exempt
@require_http_methods(['POST'])
def register(request):
    if not base():
        return _desligado()
    if not _pode_registrar(ip_cliente(request)):
        return _json({'error': 'slow_down'}, 429)
    if _corpo_grande(request, TETO_CORPO_REGISTRO):
        return _json({'error': 'invalid_request', 'error_description': 'corpo da requisição grande demais'}, 413)
    try:
        corpo = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _json({'error': 'invalid_client_metadata'}, 400)
    if not isinstance(corpo, dict):
        return _json({'error': 'invalid_client_metadata'}, 400)
    nome = corpo.get('client_name', '')
    if not isinstance(nome, str) or len(nome) > TETO_NOME_CLIENTE:
        return _json({'error': 'invalid_client_metadata',
                      'error_description': f'client_name precisa ser texto de até {TETO_NOME_CLIENTE} caracteres'}, 400)
    redirect_uris = corpo.get('redirect_uris')
    if (not isinstance(redirect_uris, list) or not redirect_uris or len(redirect_uris) > TETO_REDIRECTS
            or not all(isinstance(u, str) and redirect_permitido(u) for u in redirect_uris)):
        return _json({'error': 'invalid_redirect_uri',
                      'error_description': (
                          f'redirect_uris aceitas (até {TETO_REDIRECTS}, {TETO_URI} caracteres cada): '
                          'callback do Claude (claude.ai/claude.com) ou loopback http://localhost|127.0.0.1')}, 400)
    if corpo.get('token_endpoint_auth_method', 'none') != 'none':
        return _json({'error': 'invalid_client_metadata',
                      'error_description': 'apenas clientes públicos (token_endpoint_auth_method=none)'}, 400)
    client_id = secrets.token_urlsafe(24)
    tokens.r().set(tokens.chave('client', client_id),
                   json.dumps({'redirect_uris': redirect_uris, 'client_name': nome}), ex=TTL_CLIENT_NOVO_S)
    logger.info('oauth: cliente registrado client=%s nome=%r', client_id, nome)
    return _json({
        'client_id': client_id,
        'client_id_issued_at': int(time.time()),
        'redirect_uris': redirect_uris,
        'client_name': nome,
        'token_endpoint_auth_method': 'none',
        'grant_types': ['authorization_code', 'refresh_token'],
        'response_types': ['code'],
    }, 201)


# ------------------------------------------------------------------ validação do pedido

def _carregar_cliente(client_id):
    if not client_id or len(client_id) > 64:
        return None
    cru = tokens.r().get(tokens.chave('client', client_id))
    return json.loads(cru) if cru else None


def resource_valido(resource):
    """RFC 8707: aceita a URI canônica do /mcp (tolerando barra final e a raiz)."""
    if not resource:
        return True  # opcional para o cliente; quando vem, precisa ser nosso
    return resource.rstrip('/') in {resource_canonico(), base()}


def _validar_pedido(params, cliente):
    """Mensagem de erro (sem redirect: pedido inválido nunca redireciona)."""
    if cliente is None:
        return 'client_id desconhecido — o aplicativo precisa se registrar de novo (DCR).'
    if params.get('response_type') != 'code':
        return 'response_type não suportado (apenas code).'
    if params.get('redirect_uri') not in cliente['redirect_uris']:
        return 'redirect_uri não registrado para este cliente.'
    if params.get('code_challenge_method') != 'S256' or not params.get('code_challenge'):
        return 'PKCE S256 é obrigatório.'
    if not resource_valido(params.get('resource', '')):
        return 'resource não corresponde a este servidor MCP.'
    return None


def _redirect_cliente(params, extra):
    """Volta ao cliente com `state` e `iss` (RFC 9207) além do que vier em `extra`."""
    query = {**extra, 'iss': base()}
    if params.get('state'):
        query['state'] = params['state']
    separador = '&' if '?' in params['redirect_uri'] else '?'
    resp = HttpResponseRedirect(params['redirect_uri'] + separador + urlencode(query))
    resp['Cache-Control'] = 'no-store'
    return resp


def destino_legivel(redirect_uri):
    """Para onde o navegador volta depois do "Autorizar", em linguagem de gente.

    O nome do app (`client_name`) é escolhido por quem registrou o cliente e
    pode mentir ("Claude"); o host do redirect não — é ele que recebe o code.
    """
    url = urlparse(redirect_uri)
    host = url.hostname or '?'
    if host in {'localhost', '127.0.0.1'}:
        return f'{host}:{url.port or 80} (aplicativo neste computador, como o Claude Code)'
    return host


def _url_authorize(params):
    return '/oauth/authorize?' + urlencode({k: v for k, v in params.items() if v})


# ------------------------------------------------------------------ authorize

@require_http_methods(['GET', 'POST'])
def authorize(request):
    if not base():
        return _pagina(request, 'MCP desligado',
                       'A conexão do Claude com o Voyager não está configurada neste servidor.', status=503)
    if request.method == 'POST':
        return _authorize_post(request)

    params = {k: request.GET.get(k, '') for k in _PARAMS_OAUTH}
    erro = _validar_pedido(params, _carregar_cliente(params['client_id']))
    if erro:
        return _pagina(request, 'Pedido inválido', erro)
    if not request.user.is_authenticated:
        # O login do dashboard devolve para cá (mesma URL, mesmos parâmetros).
        return redirect_to_login(request.get_full_path())

    trocar = _url_authorize(params)
    if acesso.acesso_liberado(request.user.id) is None:
        logger.info('oauth: acesso negado usuario=%s', request.user.id)
        return _pagina(request, 'Sem acesso ao MCP', acesso.MENSAGEM_SEM_ACESSO,
                       status=403, trocar=trocar)

    ticket = secrets.token_urlsafe(32)
    tokens.r().set(tokens.chave('consentimento', _sha(ticket)),
                   json.dumps({'params': params, 'usuario_id': request.user.id}), ex=TTL_CONSENTIMENTO_S)
    cliente = _carregar_cliente(params['client_id']) or {}
    user = request.user
    logger.info('oauth: consentimento client=%s usuario=%s', params['client_id'], user.id)
    return _pagina(
        request, 'Autorizar acesso', '', status=200, etiqueta_ruim=False, consentimento=True,
        ticket=ticket, trocar=trocar,
        cliente_nome=cliente.get('client_name') or params['client_id'][:12],
        destino=destino_legivel(params['redirect_uri']),
        nome=user.get_full_name() or user.get_username(),
        email=user.email if user.get_full_name() else '',
    )


def _authorize_post(request):
    # CSRF do Django protege este POST (o form sai da nossa tela, com a sessão).
    if _corpo_grande(request, TETO_CORPO_FORM):
        return _pagina(request, 'Pedido inválido', 'Formulário grande demais.', status=413)
    ticket = request.POST.get('ticket', '')
    cru = tokens.r().getdel(tokens.chave('consentimento', _sha(ticket))) if ticket else None
    if not cru:
        return _pagina(request, 'Autorização expirada',
                       'Este aceite não está mais válido. Comece a conexão de novo pelo Claude.')
    guardado = json.loads(cru)
    params = guardado['params']
    # O aceite vale só para quem o recebeu: sessão trocada no meio não herda.
    if not request.user.is_authenticated or request.user.id != guardado['usuario_id']:
        return _pagina(request, 'Autorização expirada',
                       'A sessão mudou desde a tela de autorização. Comece de novo pelo Claude.')
    invalido = _validar_pedido(params, _carregar_cliente(params.get('client_id', '')))
    if invalido:
        return _pagina(request, 'Pedido inválido', invalido)
    if request.POST.get('decisao') != 'autorizar':
        logger.info('oauth: cancelado client=%s usuario=%s', params['client_id'], request.user.id)
        return _redirect_cliente(params, {'error': 'access_denied'})
    # Revalida: o acesso pode ter sido revogado entre a tela e o clique.
    if acesso.acesso_liberado(request.user.id) is None:
        return _pagina(request, 'Sem acesso ao MCP', acesso.MENSAGEM_SEM_ACESSO, status=403)

    code = secrets.token_urlsafe(32)
    tokens.r().set(tokens.chave('code', _sha(code)), json.dumps({
        'client_id': params['client_id'],
        'redirect_uri': params['redirect_uri'],
        'challenge': params['code_challenge'],
        'resource': params.get('resource', ''),
        'usuario_id': request.user.id,
    }), ex=TTL_CODE_S)
    logger.info('oauth: autorizado client=%s usuario=%s', params['client_id'], request.user.id)
    return _redirect_cliente(params, {'code': code})


@require_http_methods(['POST'])
def trocar_conta(request):
    """"Entrar com outra conta": sem sair, a sessão do dashboard devolveria o
    MESMO usuário. Só por POST com o CSRF do próprio dashboard — um link GET
    deslogaria a pessoa a partir de qualquer página de terceiro."""
    volta = request.POST.get('volta', '')
    if not volta.startswith('/oauth/authorize?') or len(volta) > 4096:
        return _pagina(request, 'Pedido inválido',
                       'Este link de conexão está incompleto. Volte ao Claude e conecte de novo.')
    logout(request)
    return redirect_to_login(volta)


# ------------------------------------------------------------------ token

def _s256(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()


def _erro_token(codigo, descricao='', status=400):
    corpo = {'error': codigo}
    if descricao:
        corpo['error_description'] = descricao
    return _json(corpo, status)


def _resposta_tokens(par):
    return _json({
        'access_token': par.access,
        'token_type': 'Bearer',
        'expires_in': par.expira_em_s,
        'refresh_token': par.refresh,
        'scope': tokens.ESCOPO,
    })


@csrf_exempt
@require_http_methods(['POST'])
def token(request):
    if not base():
        return _desligado()
    if not request.META.get('CONTENT_TYPE', '').startswith('application/x-www-form-urlencoded'):
        return _erro_token('invalid_request', 'use application/x-www-form-urlencoded')
    if _corpo_grande(request, TETO_CORPO_FORM):
        return _erro_token('invalid_request', 'corpo da requisição grande demais', 413)
    form = request.POST
    if not resource_valido(form.get('resource', '')):
        return _erro_token('invalid_target', 'resource não corresponde a este servidor MCP')
    grant = form.get('grant_type', '')
    if grant == 'authorization_code':
        return _trocar_code(form)
    if grant == 'refresh_token':
        return _renovar(form)
    return _erro_token('unsupported_grant_type')


def _trocar_code(form):
    code = form.get('code', '')
    verifier = form.get('code_verifier', '')
    redirect_uri = form.get('redirect_uri', '')
    client_id = form.get('client_id', '')
    if not code or not verifier:
        return _erro_token('invalid_request', 'code e code_verifier são obrigatórios')
    # GETDEL: code de uso único — replay morre aqui
    cru = tokens.r().getdel(tokens.chave('code', _sha(code)))
    if cru is None:
        familia = tokens.familia_de_code_usado(code)
        if familia:
            # Code reapresentado: quem tem a cópia pode ter levado os tokens
            # da primeira troca. Derruba o que ela emitiu.
            apagados = tokens.revogar_familia(familia)
            logger.warning('oauth: code reusado client=%s revogados=%s', client_id, apagados)
        return _erro_token('invalid_grant', 'code inválido, expirado ou já usado')
    dados = json.loads(cru)
    if dados['client_id'] != client_id or dados['redirect_uri'] != redirect_uri:
        return _erro_token('invalid_grant', 'client_id/redirect_uri não conferem')
    if not secrets.compare_digest(_s256(verifier), dados['challenge']):
        logger.warning('oauth: PKCE falhou client=%s', client_id)
        return _erro_token('invalid_grant', 'verificação PKCE falhou')
    resource = form.get('resource', '') or dados.get('resource', '')
    par = tokens.emitir(dados['usuario_id'], client_id, resource or resource_canonico())
    tokens.marcar_code_usado(code, par.familia)
    # Cliente que chegou até aqui é de verdade: o registro passa a durar um ano.
    tokens.r().expire(tokens.chave('client', client_id), TTL_CLIENT_S)
    return _resposta_tokens(par)


def _renovar(form):
    refresh = form.get('refresh_token', '')
    if not refresh:
        return _erro_token('invalid_request', 'refresh_token obrigatório')
    sessao = tokens.ler_refresh(refresh)
    if sessao is None:
        _derrubar_se_reusado(refresh)
        return _erro_token('invalid_grant', 'refresh token inválido, expirado ou já usado')
    client_id = form.get('client_id', '')
    if client_id and client_id != sessao.client_id:
        return _erro_token('invalid_grant', 'refresh token de outro cliente')
    # Renovar é o momento de conferir a allowlist SEM cache: acesso revogado não
    # pode ganhar mais 30 dias. A checagem vem ANTES de consumir o refresh:
    # banco fora do ar é transitório e não pode derrubar a sessão.
    veredito = acesso.verificar(sessao.usuario_id, usar_cache=False)
    if veredito == acesso.INDISPONIVEL:
        return _erro_token('temporarily_unavailable', 'não foi possível confirmar o acesso agora', 503)
    if veredito != acesso.PERMITIDO:
        return _erro_token('invalid_grant', 'acesso ao MCP revogado; entre de novo')
    # GETDEL: rotação — se duas renovações correrem juntas, só uma leva o par
    # novo; a outra é tratada como reuso (uma cópia do refresh está por aí).
    if tokens.consumir_refresh(refresh) is None:
        _derrubar_se_reusado(refresh)
        return _erro_token('invalid_grant', 'refresh token já usado')
    par = tokens.emitir(sessao.usuario_id, sessao.client_id, sessao.resource, familia=sessao.familia or None)
    return _resposta_tokens(par)


def _derrubar_se_reusado(refresh):
    """Refresh já consumido reapresentado = roubo provável (RFC 9700 §4.14.2):
    derruba a família inteira, inclusive o par que o legítimo tem agora."""
    familia = tokens.familia_de_refresh_usado(refresh)
    if familia:
        apagados = tokens.revogar_familia(familia)
        logger.warning('oauth: refresh reusado revogados=%s', apagados)
