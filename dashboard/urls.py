from django.contrib.auth.views import LoginView, LogoutView
from django.urls import path
from django.views.generic.base import RedirectView

from . import views, completude_views
from . import acompanhamento_views
from . import busca_views
from . import estoque_views
from . import overview_views
from . import showcase_analise
from . import showcase_chunks
from . import showcase_export
from . import showcase_proxy

app_name = 'dashboard'

urlpatterns = [
    path('', views.overview, name='overview'),
    path('kpis/', views.overview_kpis, name='overview-kpis'),
    path('login/', LoginView.as_view(template_name='dashboard/login.html'), name='login'),
    path('logout/', LogoutView.as_view(), name='logout'),
    path('tribunais/', views.tribunais, name='tribunais'),
    path('tribunais/status/', views.tribunal_status, name='tribunal-status'),
    path('tribunais/cobertura/', views.cobertura_enriquecimento, name='tribunal-cobertura'),
    path('tribunais/<str:sigla>/', views.tribunal_detail, name='tribunal-detail'),
    path('processos/', views.processos, name='processos'),
    path('processos/<int:pk>/', views.processo_detail, name='processo-detail'),
    path('processos/<int:pk>/movs/', views.processo_movs, name='processo-movs'),
    path('processos/<int:pk>/metadados/', views.processo_metadados, name='processo-metadados'),
    path('processos/<int:pk>/enriquecer/', views.processo_enriquecer, name='processo-enriquecer'),
    path('processos/<int:pk>/sincronizar/', views.processo_sincronizar, name='processo-sincronizar'),
    path('jurimetria/', views.jurimetria, name='jurimetria'),
    path('jurimetria/dossie/', views.jurimetria_dossie, name='jurimetria-dossie'),
    path('jurimetria/dossie/narrativa/', views.jurimetria_dossie_narrativa, name='jurimetria-dossie-narrativa'),
    path('jurimetria/prompt/', views.jurimetria_prompt, name='jurimetria-prompt'),
    path('jurimetria/dossie/narrativa/stream/', views.jurimetria_dossie_narrativa_stream, name='jurimetria-dossie-narrativa-stream'),
    path('jurimetria/chat/', views.jurimetria_chat, name='jurimetria-chat'),
    path('jurimetria/chat/stream/', views.jurimetria_chat_stream, name='jurimetria-chat-stream'),
    path('jurimetria/chat/sessoes/', views.jurimetria_chat_sessoes, name='jurimetria-chat-sessoes'),
    path('jurimetria/chat/sessoes/<uuid:sess_uuid>/', views.jurimetria_chat_sessao, name='jurimetria-chat-sessao'),
    path('jurimetria/chat/prompt/', views.jurimetria_chat_prompt, name='jurimetria-chat-prompt'),
    path('jurimetria/chat/upload/', views.jurimetria_chat_upload, name='jurimetria-chat-upload'),
    path('jurimetria/chat/enviar/', views.jurimetria_chat_enviar, name='jurimetria-chat-enviar'),
    path('jurimetria/chat/eventos/', views.jurimetria_chat_eventos, name='jurimetria-chat-eventos'),
    path('movimentacoes/', views.movimentacoes, name='movimentacoes'),
    path('partes/', views.partes, name='partes'),
    path('partes/<int:pk>/', views.parte_detail, name='parte-detail'),
    path('ingestao/', views.ingestao, name='ingestao'),
    path('ingestao/saude/', views.ingestao_saude, name='ingestao-saude'),
    path('ingestao/enriquecimento/', views.enriquecimento_saude, name='enriquecimento-saude'),
    path('api/enriquecimento/<str:key>/', views.enriquecimento_chart, name='api-enriquecimento'),
    path('workers/', views.workers, name='workers'),
    path('consulta-rapida/', views.consulta_rapida, name='consulta-rapida'),
    path('consulta-rapida/api/', views.consulta_rapida_api, name='consulta-rapida-api'),
    path('consulta-rapida/hidratar/', views.consulta_rapida_hidratar,
         name='consulta-rapida-hidratar'),
    # Acompanhamento — diário de bordo do produto (descobertas medidas,
    # decisões, incidentes, entregas). Login-gated: tem número de acervo e
    # relato de incidente lá dentro.
    # Completude do acervo — a única tela que compara os DOIS lados (o nosso
    # número contra o que a fonte declara). Ver dashboard/completude_views.py.
    path('completude/', completude_views.completude, name='completude'),
    # Estoque — quanto marcamos x quanto o cliente ja consumiu, por tribunal.
    # Le SO cache (a agregacao custa ~52 s). Ver dashboard/estoque_views.py.
    path('estoque/', estoque_views.estoque, name='estoque'),
    path('acompanhamento/', acompanhamento_views.acompanhamento, name='acompanhamento'),
    path('acompanhamento/<int:pk>/', acompanhamento_views.acompanhamento_nota,
         name='acompanhamento-nota'),
    path('api/', views.api_docs, name='api-docs'),
    path('mcp/', views.mcp_setup, name='mcp-setup'),
    # IA LABS — Centro de Inteligência Voyager (landing hub das ferramentas de IA)
    path('ia/', views.ia_hub, name='ia-hub'),
    # Estágio do Crédito — vitrine investidor (CNJ → DC/PRÉ/EMITIDO/MORTO ancorado)
    path('ia/estagio/', views.ia_estagio, name='ia-estagio'),
    path('ia/estagio/analisar/', views.ia_estagio_analisar, name='ia-estagio-analisar'),
    path('ia/estagio/status/', views.ia_estagio_status, name='ia-estagio-status'),
    # Showcase do Extrator — sobe PDF → extração 100% on-device (SDK no pod), ficha rica + comparar versões
    path('ia/showcase/', showcase_proxy.showcase, name='ia-showcase'),
    # Análises SALVAS (compartilháveis por UUID entre usuários)
    path('ia/showcase/analises/', showcase_analise.analise_lista, name='showcase-analises'),
    path('ia/showcase/a/<uuid:aid>/', showcase_analise.analise_detalhe, name='showcase-analise'),
    path('api/showcase/reprocessar/<uuid:aid>/', showcase_analise.analise_reprocessar, name='showcase-reprocessar'),
    path('api/showcase/extrair/<str:versao>/', showcase_proxy.showcase_extrair, name='showcase-extrair'),
    path('api/showcase/explicar/<str:versao>/', showcase_proxy.showcase_explicar, name='showcase-explicar'),
    # Upload em chunks (aguenta ~1GB via Cloudflare) → extração assíncrona → polling.
    # O transporte é chunk+async; o CONTRATO do resultado é o mesmo do extrair síncrono.
    path('api/showcase/upload/init/', showcase_chunks.upload_init, name='showcase-upload-init'),
    path('api/showcase/upload/chunk/<str:upload_id>/<int:index>/', showcase_chunks.upload_chunk, name='showcase-upload-chunk'),
    path('api/showcase/upload/finish/<str:upload_id>/', showcase_chunks.upload_finish, name='showcase-upload-finish'),
    path('api/showcase/job/<str:job_id>/', showcase_chunks.job_status, name='showcase-job'),
    # Exportar a análise da showcase (o front POSTa o payload que já tem em mãos)
    path('api/showcase/export/json', showcase_export.export_json, name='showcase-export-json'),
    path('api/showcase/export/md', showcase_export.export_md, name='showcase-export-md'),
    path('api/showcase/export/pdf', showcase_export.export_pdf, name='showcase-export-pdf'),
    path('modelos/extrator/', views.modelo_extrator, name='modelo-extrator'),
    path('modelos/extrator/vitrine/', views.modelo_extrator_vitrine, name='modelo-extrator-vitrine'),
    path('modelos/extrator/kappa/', views.kappa_amostra, name='kappa-amostra'),
    path('modelos/treinos/', views.treinos_dashboard, name='treinos-dashboard'),
    path('modelos/treinos/data/', views.treinos_dashboard_data, name='treinos-dashboard-data'),
    path('leads/', views.leads_overview, name='leads'),
    path('leads/lista/', views.leads_lista, name='leads-lista'),
    path('leads/export/', views.leads_export_csv, name='leads-export'),
    path('api/leads/<str:key>/', views.leads_chart_data, name='leads-chart'),
    path('api/chart/<str:key>/', views.chart_data, name='api-chart'),

    # Mapa Comercial de Precatórios — agregações ES (choropleth + drill-down + ranking)
    path('overview/mapa/', overview_views.comercial_mapa_page, name='overview-mapa-page'),
    path('api/overview/mapa', overview_views.comercial_mapa, name='overview-mapa'),
    path('api/overview/tribunais', overview_views.comercial_tribunais, name='overview-tribunais'),
    path('api/overview/top', overview_views.comercial_top, name='overview-top'),
    # página dedicada por ESTADO (aggs "explodidas" da UF) — contrato em search/agg_estado.py
    path('overview/estado/<str:uf>/', overview_views.comercial_estado_page,
         name='overview-estado-page'),
    path('api/overview/estado/<str:uf>/', overview_views.comercial_estado,
         name='overview-estado'),
    # autocomplete de ENTIDADE canônica (índice `voyager-entidades*`) — alimenta
    # o filtro "Parte / entidade" do mapa. Fora de `api/overview/` de propósito:
    # o cadastro de entidades não é do mapa, é do produto (listagem e busca vão
    # consumir o mesmo endpoint).
    path('api/entidades/autocomplete', overview_views.entidades_autocomplete,
         name='entidades-autocomplete'),
    # BUSCA DE PROCESSOS — contrato JSON completo em search/busca_ui.py.
    # Reusa o mesmo serviço da API externa /api/v1/busca/* (search/busca_api.py);
    # o que muda aqui é o envelope de cobertura ("CPF varre 0,14% da base").
    path('busca/', busca_views.busca_page, name='busca'),
    path('api/busca/processos/', busca_views.busca_processos,
         name='busca-processos'),
    path('api/busca/varas/', busca_views.busca_varas, name='busca-varas'),
    # busca NO TEXTO das publicações — o acervo de conteúdo que a tela não via
    path('api/busca/conteudo/', busca_views.busca_conteudo, name='busca-conteudo'),
    # TELAS DE ENTIDADES ("quem deve") — contrato em search/agg_entidade.py.
    # A rota do ranking vem DEPOIS do autocomplete de propósito: `<str:...>`
    # casaria "autocomplete" como se fosse um entidade_id (o Django resolve na
    # ordem, então o autocomplete continua ganhando).
    # ---- REDIRECTS das URLs antigas (`comercial/*` → `overview/*`) ----------
    # O módulo se chamava "comercial" até 13/08/2026. Links salvos, a showcase e
    # o /extrair apontam pros paths velhos; rename sem redirect quebra em
    # silêncio — quem clica vê 404 e não sabe por quê. `permanent=False` de
    # propósito: 301 fica no cache do browser PRA SEMPRE e, se um dia
    # revertermos, não há como desfazer na máquina de quem já acessou.
    path('comercial/mapa/', RedirectView.as_view(pattern_name='dashboard:overview-mapa-page',
                                                 permanent=False, query_string=True)),
    path('comercial/estado/<str:uf>/', RedirectView.as_view(
        pattern_name='dashboard:overview-estado-page', permanent=False, query_string=True)),
    path('comercial/entidades/', RedirectView.as_view(
        pattern_name='dashboard:entidades', permanent=False, query_string=True)),
    path('comercial/entidade/<str:entidade_id>/', RedirectView.as_view(
        pattern_name='dashboard:entidade', permanent=False, query_string=True)),
    path('api/comercial/mapa', RedirectView.as_view(
        pattern_name='dashboard:overview-mapa', permanent=False, query_string=True)),
    path('api/comercial/tribunais', RedirectView.as_view(
        pattern_name='dashboard:overview-tribunais', permanent=False, query_string=True)),
    path('api/comercial/top', RedirectView.as_view(
        pattern_name='dashboard:overview-top', permanent=False, query_string=True)),
    path('api/comercial/estado/<str:uf>/', RedirectView.as_view(
        pattern_name='dashboard:overview-estado', permanent=False, query_string=True)),

    # TELAS (shell HTML) — vêm antes dos endpoints por legibilidade; não há
    # colisão: estas são `comercial/entidade*`, aquelas `api/entidades/*`.
    path('overview/entidades/', overview_views.entidades_page, name='entidades'),
    path('overview/entidade/<str:entidade_id>/', overview_views.entidade_page,
         name='entidade'),
    path('api/entidades/', overview_views.entidades_ranking,
         name='entidades-ranking'),
    path('api/entidades/<str:entidade_id>/', overview_views.entidades_ficha,
         name='entidades-ficha'),
    path('jobs/<str:job_id>/status/', views.job_status, name='job-status'),
    path('wizard/', views.WizardView.as_view(), name='wizard'),
    path('wizard/count/', views.WizardCountView.as_view(), name='wizard-count'),
    path('wizard/export/', views.WizardExportView.as_view(), name='wizard-export'),

    # Validação humana / observabilidade de leads (T8)
    # Página didática "como o robô classifica" — advogado-friendly + sandbox CNJ
    path('leads/algoritmo/', views.algoritmo, name='algoritmo'),
    path('leads/algoritmo/explicar/', views.algoritmo_explicar, name='algoritmo_explicar'),

    path('leads/visibilidade/', views.leads_visibilidade, name='leads_visibilidade'),
    path('leads/visibilidade/chart/histograma-score/',
         views.chart_histograma_score, name='chart_histograma_score'),
    path('leads/visibilidade/chart/calibracao/',
         views.chart_calibracao_por_tribunal, name='chart_calibracao_por_tribunal'),
    path('leads/visibilidade/chart/heatmap/',
         views.chart_heatmap_tribunal_ano, name='chart_heatmap_tribunal_ano'),
    path('leads/visibilidade/chart/funil/',
         views.chart_funil_ampliado, name='chart_funil_ampliado'),
    path('leads/visibilidade/chart/top-fn/',
         views.chart_top_fn_semana, name='chart_top_fn_semana'),
    path('leads/visibilidade/chart/shadow-status/',
         views.chart_shadow_status, name='chart_shadow_status'),

    path('leads/validacao/', views.leads_validacao_overview,
         name='leads_validacao_overview'),
    path('leads/validacao/criar-lote/', views.leads_validacao_criar_lote,
         name='leads_validacao_criar_lote'),
    path('leads/validacao/salvar/', views.leads_validacao_salvar,
         name='leads_validacao_salvar'),
    path('leads/validacao/<int:lote_id>/', views.leads_validacao_lote,
         name='leads_validacao_lote'),
    path('leads/validacao/<int:lote_id>/concluido/',
         views.leads_validacao_lote_concluido,
         name='leads_validacao_lote_concluido'),
    path('leads/validacao/<int:lote_id>/item/<int:posicao>/',
         views.leads_validacao_item, name='leads_validacao_item'),

    # Command Center — dashboard única premium (pipeline + frota + custo)
    path('command/', views.command_center, name='command'),
    path('command/data/', views.command_data, name='command-data'),

    # Vetorização — velocidade de processamento da frota (Zordon)
    path('vetorizacao/', views.vetorizacao, name='vetorizacao'),

    # Indexação Elasticsearch — cobertura por conteúdo
    path('indexacao/', views.indexacao, name='indexacao'),

    # Acervo — busca semântica (Zordon)
    path('acervo/busca/', views.acervo_busca, name='acervo-busca'),
    path('acervo/teor/<str:cnj>/', views.acervo_teor, name='acervo-teor'),
]
