from django.contrib.auth.views import LoginView, LogoutView
from django.urls import path

from . import views

app_name = 'dashboard'

urlpatterns = [
    path('', views.overview, name='overview'),
    path('login/', LoginView.as_view(template_name='dashboard/login.html'), name='login'),
    path('logout/', LogoutView.as_view(), name='logout'),
    path('tribunais/<str:sigla>/', views.tribunal_detail, name='tribunal-detail'),
    path('processos/', views.processos, name='processos'),
    path('processos/<int:pk>/', views.processo_detail, name='processo-detail'),
    path('movimentacoes/', views.movimentacoes, name='movimentacoes'),
    path('ingestao/', views.ingestao, name='ingestao'),
]
