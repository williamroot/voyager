from django.urls import path

from . import views

app_name = 'accounts'

urlpatterns = [
    path('dashboard/invites/', views.invites_list, name='invites-list'),
    path('dashboard/invites/create/', views.invites_create, name='invites-create'),
    path('dashboard/invites/<int:pk>/revoke/', views.invites_revoke, name='invites-revoke'),
    path('dashboard/invites/<int:pk>/link/', views.invite_link, name='invites-link'),
    path('invite/<str:token>/', views.accept_invite, name='accept-invite'),
]
