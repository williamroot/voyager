from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .forms import AcceptInviteForm, InviteCreateForm
from .models import Invite
from .utils import classify_ip, get_client_ip

User = get_user_model()


def _is_superuser(u):
    return u.is_authenticated and u.is_superuser


@login_required
@user_passes_test(_is_superuser)
@require_GET
def invites_list(request):
    invites = Invite.objects.select_related('created_by', 'used_by')
    form = InviteCreateForm()
    return render(request, 'accounts/invites_list.html', {
        'invites': invites,
        'form': form,
    })


@login_required
@user_passes_test(_is_superuser)
@require_POST
def invites_create(request):
    form = InviteCreateForm(request.POST)
    if form.is_valid():
        inv = Invite.objects.create(
            created_by=request.user,
            email_hint=form.cleaned_data.get('email_hint') or '',
            note=form.cleaned_data.get('note') or '',
        )
        messages.success(request, f'Convite criado. Token: {inv.token[:12]}…')
    else:
        messages.error(request, 'Falha ao criar convite — verifique o formulário.')
    return redirect('accounts:invites-list')


@login_required
@user_passes_test(_is_superuser)
@require_POST
def invites_revoke(request, pk):
    inv = get_object_or_404(Invite, pk=pk)
    if inv.used_at:
        messages.error(request, 'Convite já foi usado — não pode revogar.')
    else:
        inv.expires_at = timezone.now()
        inv.save(update_fields=['expires_at'])
        messages.success(request, 'Convite revogado.')
    return redirect('accounts:invites-list')


def accept_invite(request, token):
    """Página pública: convidado escolhe username/senha pelo link."""
    inv = Invite.objects.filter(token=token).first()
    if inv is None:
        raise Http404('Convite não encontrado.')

    if inv.is_used:
        return render(request, 'accounts/invite_invalid.html', {
            'reason': 'usado',
            'invite': inv,
        }, status=410)

    if inv.is_expired:
        return render(request, 'accounts/invite_invalid.html', {
            'reason': 'expirado',
            'invite': inv,
        }, status=410)

    if request.method == 'POST':
        form = AcceptInviteForm(request.POST)
        if form.is_valid():
            ip = get_client_ip(request)
            ua = request.META.get('HTTP_USER_AGENT', '')[:500]
            ip_data = classify_ip(ip)

            with transaction.atomic():
                # Re-checa dentro da transação pra evitar race entre 2 abas
                # do mesmo convite — select_for_update serializa.
                inv = Invite.objects.select_for_update().get(pk=inv.pk)
                if not inv.is_valid:
                    return render(request, 'accounts/invite_invalid.html', {
                        'reason': 'usado' if inv.is_used else 'expirado',
                        'invite': inv,
                    }, status=410)

                user = User.objects.create_user(
                    username=form.cleaned_data['username'],
                    password=form.cleaned_data['password'],
                    email=inv.email_hint or '',
                )
                inv.used_at = timezone.now()
                inv.used_by = user
                inv.used_ip = ip or None
                inv.used_user_agent = ua
                inv.used_ip_country = ip_data.get('country', '')[:80]
                inv.used_ip_country_code = ip_data.get('countryCode', '')[:4]
                inv.used_ip_region = ip_data.get('regionName', '')[:80]
                inv.used_ip_city = ip_data.get('city', '')[:120]
                inv.used_ip_isp = ip_data.get('isp', '')[:180]
                inv.used_ip_org = ip_data.get('org', '')[:180]
                inv.used_ip_asn = ip_data.get('as', '')[:80]
                inv.used_ip_mobile = ip_data.get('mobile')
                inv.used_ip_hosting = ip_data.get('hosting')
                inv.used_ip_proxy = ip_data.get('proxy')
                inv.used_ip_data = ip_data
                inv.save()

            login(request, user)
            messages.success(request, f'Bem-vindo, {user.username}!')
            return redirect('dashboard:overview')
    else:
        form = AcceptInviteForm()

    return render(request, 'accounts/accept_invite.html', {
        'form': form, 'invite': inv,
    })


@login_required
@user_passes_test(_is_superuser)
@require_GET
def invite_link(request, pk):
    """Devolve o link absoluto pra copiar — endpoint de conveniência."""
    inv = get_object_or_404(Invite, pk=pk)
    url = request.build_absolute_uri(
        reverse('accounts:accept-invite', kwargs={'token': inv.token})
    )
    return render(request, 'accounts/invite_link.html', {'invite': inv, 'url': url})
