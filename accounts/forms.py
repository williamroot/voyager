from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

User = get_user_model()


class InviteCreateForm(forms.Form):
    email_hint = forms.EmailField(required=False, label='E-mail (opcional)')
    note = forms.CharField(required=False, max_length=255, label='Nota')


class AcceptInviteForm(forms.Form):
    username = forms.CharField(
        max_length=150,
        help_text='Letras, dígitos e @/./+/-/_ apenas.',
    )
    password = forms.CharField(
        widget=forms.PasswordInput,
        min_length=10,
        help_text='Mínimo 10 caracteres. Não use senhas comuns.',
    )
    password_confirm = forms.CharField(
        widget=forms.PasswordInput, label='Confirmar senha',
    )

    def clean_username(self):
        username = self.cleaned_data['username'].strip()
        if User.objects.filter(username__iexact=username).exists():
            raise ValidationError('Username já em uso.')
        return username

    def clean(self):
        cleaned = super().clean()
        pw = cleaned.get('password')
        pw2 = cleaned.get('password_confirm')
        if pw and pw2 and pw != pw2:
            self.add_error('password_confirm', 'Senhas não conferem.')
        if pw:
            try:
                validate_password(pw)
            except ValidationError as exc:
                self.add_error('password', list(exc.messages))
        return cleaned
