# Contas e convites

App `accounts/`. Sem auto-cadastro público — acesso é por **convite** gerado por superuser.

## Modelo (`accounts.Invite`)

```python
token              char(64)    unique     secrets.token_urlsafe(32) — 256 bits
created_by         FK User                superuser que gerou
created_at         datetime
expires_at         datetime    default now+7d
email_hint         email                  hint visual, não restringe
note               char(255)              texto livre
used_at            datetime    NULL       null = ainda válido
used_by            OneToOne User NULL     usuário criado ao aceitar

# Captura no momento do uso (auditoria)
used_ip            inet
used_user_agent    char(500)

# Classificação ip-api.com
used_ip_country         char(80)
used_ip_country_code    char(4)
used_ip_region          char(80)
used_ip_city            char(120)
used_ip_isp             char(180)
used_ip_org             char(180)
used_ip_asn             char(80)
used_ip_mobile          bool NULL
used_ip_hosting         bool NULL
used_ip_proxy           bool NULL
used_ip_data            jsonb              payload bruto pra debug
```

Properties: `is_used`, `is_expired`, `is_valid`.

## Fluxo

```
Superuser:
  GET  /dashboard/invites/         lista + form
  POST /dashboard/invites/create/  gera Invite (token + 7d)
  GET  /dashboard/invites/<pk>/link/   página com URL pra copiar
  POST /dashboard/invites/<pk>/revoke/ marca expires_at=now (se ainda não usado)

Convidado (público):
  GET  /invite/<token>/            formulário (username + senha + confirm)
  POST /invite/<token>/            valida, cria User, marca invite usado, login()
                                     ↓
                                   redireciona /dashboard/
```

## Captura de IP (`accounts/utils.py::get_client_ip`)

Ordem de preferência (cloudflared → nginx → padrão):

1. `Cf-Connecting-Ip` (cloudflared sempre seta)
2. `X-Real-IP` (nginx seta)
3. `X-Forwarded-For` (primeiro IP — cliente original)
4. `REMOTE_ADDR` (último recurso)

## Classificação (`accounts/utils.py::classify_ip`)

Usa `ip-api.com`:
- Com `IP_API_KEY` setada → `https://pro.ip-api.com/json/<ip>?key=...&fields=...`
- Sem chave → `http://ip-api.com/json/<ip>?fields=...` (free, 45req/min, HTTP only)
- IP privado/loopback é skipado
- Timeout 5s — não bloqueia o cadastro se ip-api estiver fora

Campos retornados: country, countryCode, regionName, city, isp, org, as, mobile, proxy, hosting, query.

## Race-safety

`accept_invite` usa `select_for_update` na transação:
```python
with transaction.atomic():
    inv = Invite.objects.select_for_update().get(pk=inv.pk)
    if not inv.is_valid:
        return invalid_response  # outra aba já usou
    user = User.objects.create_user(...)
    inv.used_at = now
    inv.used_by = user
    inv.save()
```

Garante que duas abas abertas no mesmo link não criam 2 usuários.

## Settings

```python
# core/settings.py
IP_API_KEY = env('IP_API_KEY', default='')
```

`.env.example` documenta. Vazio é OK em dev.

## URLs

| Path | Auth | Descrição |
|---|---|---|
| `/dashboard/invites/` | superuser | Lista + form de criar |
| `/dashboard/invites/<pk>/link/` | superuser | Página com URL pra copiar |
| `/dashboard/invites/<pk>/revoke/` | superuser, POST | Revoga (set expires=now) |
| `/invite/<token>/` | público | Aceitar convite |

## Templates

`accounts/templates/accounts/`:
- `invites_list.html` — tabela de invites + form, mostra IP/geo/ISP/flags na coluna IP
- `invite_link.html` — input com URL absoluta + click-to-copy
- `accept_invite.html` — login-like screen com brand + form
- `invite_invalid.html` — error 410 ("usado" / "expirado")

## Sidebar

Link "Convites" aparece apenas pra `user.is_superuser` (decorator `@user_passes_test(_is_superuser)` nas views).

## Segurança

- Token com 256 bits de entropia (`secrets.token_urlsafe(32)`)
- Validade 7 dias (default no field)
- Uso único garantido por `select_for_update`
- Auditoria completa: IP + UA + classificação ip-api persistidas
- Senha mínima 10 chars + `validate_password` (rejeita senhas comuns, inteiramente numéricas, similar ao username)
- CSRF cobre POSTs
