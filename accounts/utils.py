"""Helpers para captura de IP + classificação via ip-api.com."""
import json
import logging
import urllib.error
import urllib.request

from django.conf import settings

logger = logging.getLogger('voyager.accounts')

_FIELDS = (
    'status,message,country,countryCode,regionName,city,isp,org,as,'
    'mobile,proxy,hosting,query'
)


def get_client_ip(request) -> str:
    """Resolve o IP do cliente atrás de Cloudflare Tunnel + nginx.

    Ordem de preferência:
      1. Cf-Connecting-Ip (cloudflared sempre seta)
      2. X-Real-IP (nginx seta)
      3. X-Forwarded-For (primeiro IP da lista, que é o cliente original)
      4. REMOTE_ADDR (último recurso — em prod com tunnel sempre vai ser IP interno)
    """
    cf = request.META.get('HTTP_CF_CONNECTING_IP')
    if cf:
        return cf.strip()
    real_ip = request.META.get('HTTP_X_REAL_IP')
    if real_ip:
        return real_ip.strip()
    xff = request.META.get('HTTP_X_FORWARDED_FOR')
    if xff:
        return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '') or ''


def classify_ip(ip: str) -> dict:
    """Consulta ip-api.com (pro se IP_API_KEY setada, free senão) e devolve
    um dict bruto. Vazio se falhar — não levanta. Timeout curto (5s) pra
    não bloquear o cadastro do usuário.
    """
    if not ip or _is_private(ip):
        return {}
    api_key = getattr(settings, 'IP_API_KEY', '') or ''
    if api_key:
        url = f'https://pro.ip-api.com/json/{ip}?key={api_key}&fields={_FIELDS}'
    else:
        url = f'http://ip-api.com/json/{ip}?fields={_FIELDS}'
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'voyager/1.0'})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        if data.get('status') != 'success':
            logger.warning('ip-api falhou', extra={'ip': ip, 'message': data.get('message')})
            return {}
        return data
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        logger.warning('ip-api request error', extra={'ip': ip, 'erro': str(exc)})
        return {}


def _is_private(ip: str) -> bool:
    """Heurística simples — evita gastar quota com IPs internos da rede docker."""
    if not ip:
        return True
    try:
        import ipaddress
        addr = ipaddress.ip_address(ip)
        return addr.is_private or addr.is_loopback or addr.is_link_local
    except ValueError:
        return True
