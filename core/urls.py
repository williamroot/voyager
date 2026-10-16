from django.conf import settings
from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView, TemplateView

from mcp_server import oauth
from mcp_server.server import mcp_endpoint


class FaviconRedirect(RedirectView):
    """Redirect lazy pra /static/dashboard/favicon.svg.

    Resolve `static()` no request-time (não no import) — sem isso,
    `CompressedManifestStaticFilesStorage` levanta ValueError se o
    manifest ainda não foi gerado (ex: 1º boot, antes do collectstatic).
    """
    permanent = True

    def get_redirect_url(self, *args, **kwargs):
        from django.templatetags.static import static
        return static('dashboard/favicon.svg')


urlpatterns = [
    path('admin/', admin.site.urls),
    path('django-rq/', include('django_rq.urls')),
    path('', include('django_prometheus.urls')),
    path('api/v1/', include('api.urls')),
    # Sem a barra também: a página de setup ensina `.../mcp`, e o APPEND_SLASH
    # responderia 301 — que o cliente segue trocando POST por GET.
    path('mcp', mcp_endpoint),
    path('mcp/', include('mcp_server.server')),
    # OAuth 2.1 do conector do Claude (mcp_server/oauth.py). PRM e AS metadata
    # na raiz, com e sem o sufixo /mcp: os clientes tentam as duas formas.
    path('.well-known/oauth-protected-resource', oauth.protected_resource_metadata),
    path('.well-known/oauth-protected-resource/mcp', oauth.protected_resource_metadata),
    path('.well-known/oauth-authorization-server', oauth.authorization_server_metadata),
    path('.well-known/oauth-authorization-server/mcp', oauth.authorization_server_metadata),
    path('oauth/register', oauth.register),
    path('oauth/authorize', oauth.authorize),
    path('oauth/token', oauth.token),
    path('oauth/trocar-conta', oauth.trocar_conta),
    path('dashboard/', include('dashboard.urls')),
    path('', include('accounts.urls')),
    path('favicon.ico', FaviconRedirect.as_view()),
    path('', include('dashboard.urls_root')),
]

# Em DEBUG, expõe rotas pra previsualizar templates de erro durante desenvolvimento.
if settings.DEBUG:
    urlpatterns += [
        path('__preview/404/', TemplateView.as_view(template_name='404.html')),
        path('__preview/403/', TemplateView.as_view(template_name='403.html')),
        path('__preview/500/', TemplateView.as_view(template_name='500.html')),
        path('__preview/400/', TemplateView.as_view(template_name='400.html')),
    ]

# Django carrega esses automaticamente quando DEBUG=False; só explicitamos
# os names para deixar claro o contrato.
handler404 = 'django.views.defaults.page_not_found'
handler500 = 'django.views.defaults.server_error'
handler403 = 'django.views.defaults.permission_denied'
handler400 = 'django.views.defaults.bad_request'
