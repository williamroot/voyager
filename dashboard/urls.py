from django.contrib.auth.views import LoginView, LogoutView
from django.urls import path

from . import views

app_name = 'dashboard'

urlpatterns = [
    path('', views.overview, name='overview'),
    path('kpis/', views.overview_kpis, name='overview-kpis'),
    path('login/', LoginView.as_view(template_name='dashboard/login.html'), name='login'),
    path('logout/', LogoutView.as_view(), name='logout'),
    path('tribunais/', views.tribunais, name='tribunais'),
    path('tribunais/status/', views.tribunal_status, name='tribunal-status'),
    path('tribunais/<str:sigla>/', views.tribunal_detail, name='tribunal-detail'),
    path('processos/', views.processos, name='processos'),
    path('processos/<int:pk>/', views.processo_detail, name='processo-detail'),
    path('processos/<int:pk>/movs/', views.processo_movs, name='processo-movs'),
    path('processos/<int:pk>/enriquecer/', views.processo_enriquecer, name='processo-enriquecer'),
    path('processos/<int:pk>/sincronizar/', views.processo_sincronizar, name='processo-sincronizar'),
    path('jurimetria/', views.jurimetria, name='jurimetria'),
    path('jurimetria/dossie/', views.jurimetria_dossie, name='jurimetria-dossie'),
    path('jurimetria/dossie/narrativa/', views.jurimetria_dossie_narrativa, name='jurimetria-dossie-narrativa'),
    path('jurimetria/prompt/', views.jurimetria_prompt, name='jurimetria-prompt'),
    path('jurimetria/dossie/narrativa/stream/', views.jurimetria_dossie_narrativa_stream, name='jurimetria-dossie-narrativa-stream'),
    path('movimentacoes/', views.movimentacoes, name='movimentacoes'),
    path('partes/', views.partes, name='partes'),
    path('partes/<int:pk>/', views.parte_detail, name='parte-detail'),
    path('ingestao/', views.ingestao, name='ingestao'),
    path('ingestao/saude/', views.ingestao_saude, name='ingestao-saude'),
    path('workers/', views.workers, name='workers'),
    path('consulta-rapida/', views.consulta_rapida, name='consulta-rapida'),
    path('consulta-rapida/api/', views.consulta_rapida_api, name='consulta-rapida-api'),
    path('api/', views.api_docs, name='api-docs'),
    path('leads/', views.leads_overview, name='leads'),
    path('leads/lista/', views.leads_lista, name='leads-lista'),
    path('leads/export/', views.leads_export_csv, name='leads-export'),
    path('api/leads/<str:key>/', views.leads_chart_data, name='leads-chart'),
    path('api/chart/<str:key>/', views.chart_data, name='api-chart'),
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

    # Acervo — busca semântica (Zordon)
    path('acervo/busca/', views.acervo_busca, name='acervo-busca'),
    path('acervo/teor/<str:cnj>/', views.acervo_teor, name='acervo-teor'),
]
