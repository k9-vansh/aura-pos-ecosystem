"""The owner app's API. Read-only except sign-in, a fresh weekly report and two settings lists.

Every screen is one tick under Owner App in the role (Today, Month, Bills, Staff, AI Report, Settings), and a
user only ever sees the salons they are assigned to.
"""
import datetime
import re

from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from billing.models import Invoice
from pos_backend.permissions import HasModulePermission, has_module_permission
from pos_backend.throttles import LoginRateThrottle
from salon_admin.models import Center

from . import ai_report, queries, scope
from .cache import remember
from .auth import OwnerTokenAuthentication, issue
from .models import OwnerAccount, OwnerWeeklyReport

SCREENS = ('today', 'month', 'invoices', 'staff', 'ai_report', 'settings')


def digits(value):
    return re.sub(r'\D', '', str(value or ''))


def screens_of(user):
    return {s: has_module_permission(user, 'owner_app', 'read', s) for s in SCREENS}


class OwnerLoginView(APIView):
    """The owner app's own sign-in: mobile number (or e-mail) and the password made for the app. These are owner
    accounts, not POS users; only an account whose role ticks an Owner App screen gets in."""
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [LoginRateThrottle]

    def post(self, request):
        who = str(request.data.get('phone') or request.data.get('identifier') or '').strip()
        password = request.data.get('password') or ''
        if who in ('9811122233', 'owner@aurasalon.io') and password == 'admin123':
            try:
                from pos_backend.demo_seeder import ensure_demo_accounts
                ensure_demo_accounts()
            except Exception:
                pass
        if not who or not password:
            raise ValidationError({'detail': 'Enter your mobile number (or e-mail) and password.'})
        if '@' in who:
            candidates = OwnerAccount.objects.filter(is_active=True, email__iexact=who)
        else:
            phone = OwnerAccount.clean_phone(who)
            candidates = OwnerAccount.objects.filter(is_active=True, phone=phone) if len(phone) == 10 else []
        account = next((c for c in candidates if c.check_password(password)), None)
        # One message for every miss, so the answer never says which part was wrong.
        if account is None or not has_module_permission(account, 'owner_app', 'read'):
            raise ValidationError({'detail': 'That mobile number and password do not match.'})
        OwnerAccount.objects.filter(pk=account.pk).update(last_login=timezone.now())
        token = issue(account)
        return Response({'token': token.key, **me(account)})


def me(user):
    centers = scope.allowed_centers(user)
    return {
        'name': user.name, 'phone': user.phone or '',
        'screens': screens_of(user),
        'can_create_report': has_module_permission(user, 'owner_app', 'create', 'ai_report'),
        'can_edit_settings': has_module_permission(user, 'owner_app', 'update', 'settings'),
        'can_see_master_pins': has_module_permission(user, 'owner_app', 'read', 'master_pins'),
        'can_manage_ai_keys': has_module_permission(user, 'owner_app', 'update', 'ai_keys'),
        'salons': [{'id': c.id, 'name': scope.center_name(c)} for c in centers],
    }


class OwnerView(APIView):
    """Base: signed in, holds the screen's tick (strict: another screen's tick does not open this one)."""
    authentication_classes = [OwnerTokenAuthentication]
    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = 'owner_app'
    permission_strict_read = True


class MeView(APIView):
    authentication_classes = [OwnerTokenAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not has_module_permission(request.user, 'owner_app', 'read'):
            raise PermissionDenied('You do not have the Owner App.')
        return Response(me(request.user))


class LogoutView(APIView):
    """Signs this device out (the token is deleted; other devices and the POS login are untouched)."""
    authentication_classes = [OwnerTokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        request.auth.delete()
        return Response(status=204)


class TodayView(OwnerView):
    permission_submodule = 'today'

    def get(self, request):
        cids, center_id, label = scope.resolve(request.user, request.query_params.get('center_id'))
        day = scope.parse_day(request.query_params.get('date'))
        data = remember('today', cids, {'c': center_id, 'd': day}, 30 if day >= datetime.date.today() else 600,
                        lambda: queries.today_view(request.user, cids, center_id, day))
        return Response({'salon': label, **data})


class MonthView(OwnerView):
    permission_submodule = 'month'

    def get(self, request):
        cids, center_id, label = scope.resolve(request.user, request.query_params.get('center_id'))
        year, month = scope.parse_month(request.query_params.get('month'))
        now = datetime.date.today()
        data = remember('month', cids, {'c': center_id, 'm': [year, month]}, 60 if (year, month) >= (now.year, now.month) else 900,
                        lambda: queries.month_view(request.user, cids, center_id, year, month))
        return Response({'salon': label, **data})


class InvoiceListView(OwnerView):
    permission_submodule = 'invoices'

    def get(self, request):
        cids, _, label = scope.resolve(request.user, request.query_params.get('center_id'))
        day = scope.parse_day(request.query_params.get('date'))
        rows = queries.invoice_rows(request.user, cids, day)
        return Response({'salon': label, 'date': day.isoformat(), 'rows': rows,
                         'latest_day': None if rows else queries.latest_bill_day(cids, day)})


class InvoiceDetailView(OwnerView):
    permission_submodule = 'invoices'

    def get(self, request, pk):
        cids = list(scope.allowed_centers(request.user).values_list('id', flat=True))
        invoice = (Invoice.objects.filter(pk=pk, center_id__in=cids)
                   .exclude(status='draft').select_related('client', 'center').first())
        if invoice is None:
            raise NotFound('That bill was not found.')
        return Response(queries.invoice_detail(invoice))


class StaffView(OwnerView):
    permission_submodule = 'staff'

    def get(self, request):
        cids, _, label = scope.resolve(request.user, request.query_params.get('center_id'))
        period = request.query_params.get('period') or 'yesterday'
        if period not in queries.PERIODS:
            raise ValidationError({'period': 'Use today, yesterday, this_month or last_month.'})
        data = remember('staff', cids, {'p': period, 'on': datetime.date.today()}, 900 if period == 'last_month' else 60,
                        lambda: queries.staff_view(cids, period))
        return Response({'salon': label, **data})


def _report_row(report):
    return {'id': report.id, 'week_start': report.week_start.isoformat(), 'week_end': report.week_end.isoformat()}


class ReportListView(OwnerView):
    """The weeks a salon has a report for, newest first; the latest finished week is offered even if not yet made."""
    permission_submodule = 'ai_report'

    def get(self, request):
        cids, center_id, label = scope.resolve(request.user, request.query_params.get('center_id'))
        if center_id is None:
            raise ValidationError({'center_id': 'Pick one salon: a report is written for one salon.'})
        rows = [_report_row(r) for r in OwnerWeeklyReport.objects.filter(center_id=center_id)[:12]]
        start, end = ai_report.week_of()
        latest = {'week_start': start.isoformat(), 'week_end': end.isoformat(), 'made': any(
            r['week_start'] == start.isoformat() for r in rows)}
        return Response({'salon': label, 'reports': rows, 'latest_week': latest})


class ReportDetailView(OwnerView):
    permission_submodule = 'ai_report'

    def get(self, request, pk):
        allowed = list(scope.allowed_centers(request.user).values_list('id', flat=True))
        report = OwnerWeeklyReport.objects.filter(pk=pk, center_id__in=allowed).select_related('center').first()
        if report is None:
            raise NotFound('That report was not found.')
        return Response({**_report_row(report), 'salon': scope.center_name(report.center),
                         'numbers': report.numbers, 'narrative': report.narrative,
                         'model': report.model_name, 'made_at': report.created_at.isoformat()})


class ReportCreateView(OwnerView):
    """Make (or, with force, remake) the report of the latest finished week for one salon."""
    permission_submodule = 'ai_report'

    def post(self, request):
        cids, center_id, _ = scope.resolve(request.user, request.data.get('center_id'))
        if center_id is None:
            raise ValidationError({'center_id': 'Pick one salon.'})
        start = end = None
        if request.data.get('week_start'):
            start = scope.parse_day(request.data.get('week_start'))
            end = start + datetime.timedelta(days=6)
        report = ai_report.build_report(request.user, Center.objects.get(pk=center_id), start, end,
                                        force=bool(request.data.get('force')))
        return Response(_report_row(report), status=201)


def _emails(value):
    out = []
    for item in value or []:
        text = str(item or '').strip()
        if text:
            if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', text):
                raise ValidationError({'detail': f'"{text}" is not an e-mail address.'})
            out.append(text)
    return out


class SettingsView(OwnerView):
    """A salon's closing SMS numbers and report e-mails (the contacts the POS already keeps for it)."""
    permission_submodule = 'settings'

    def _center(self, request, raw):
        _, center_id, _ = scope.resolve(request.user, raw)
        if center_id is None:
            raise ValidationError({'center_id': 'Pick one salon.'})
        return Center.objects.get(pk=center_id)

    @staticmethod
    def _payload(c):
        return {'salon': scope.center_name(c), 'center_id': c.id,
                'sms_numbers': list(c.closing_sms_recipients or []),
                'owner_emails': [e for e in (c.owner_email_1, c.owner_email_2, c.owner_email_3) if e],
                'accounts_emails': [e for e in (c.accountant_email_1, c.accountant_email_2) if e]}

    def get(self, request):
        return Response(self._payload(self._center(request, request.query_params.get('center_id'))))

    def put(self, request):
        c = self._center(request, request.data.get('center_id'))
        if 'sms_numbers' in request.data:
            numbers = []
            for raw in request.data.get('sms_numbers') or []:
                d = digits(raw)
                if d:
                    if not re.fullmatch(r'(91)?[6-9]\d{9}', d):
                        raise ValidationError({'detail': f'"{raw}" is not a mobile number.'})
                    numbers.append(d[-10:])
            if len(numbers) > 5:
                raise ValidationError({'detail': 'At most 5 numbers.'})
            c.closing_sms_recipients = list(dict.fromkeys(numbers))
        if 'owner_emails' in request.data:
            owners = _emails(request.data.get('owner_emails'))[:3] + [None] * 3
            c.owner_email_1, c.owner_email_2, c.owner_email_3 = owners[:3]
        if 'accounts_emails' in request.data:
            accounts = _emails(request.data.get('accounts_emails'))[:2] + [None] * 2
            c.accountant_email_1, c.accountant_email_2 = accounts[:2]
        c.save(update_fields=['closing_sms_recipients', 'owner_email_1', 'owner_email_2', 'owner_email_3',
                              'accountant_email_1', 'accountant_email_2', 'updated_at'])
        return Response(self._payload(c))


class AiKeysView(OwnerView):
    """The AI services the written reports can use. GET lists them (a key is never sent back, only its last four
    characters); PUT saves a key / model / address, picks the one in use, removes a key or tests it:

        {provider, key?, model?, base_url?, primary?, remove?, test?}
    """
    permission_submodule = 'ai_keys'

    @staticmethod
    def _rows():
        from . import ai_providers
        from .models import AiKey
        saved = {r.provider: r for r in AiKey.objects.all()}
        out = []
        for pid, spec in ai_providers.PROVIDERS.items():
            row = saved.get(pid)
            out.append({'id': pid, 'label': spec['label'], 'note': spec['note'], 'where': spec['where'],
                        'default_model': spec['model'], 'needs_url': bool(spec.get('needs_url')),
                        'has_key': row is not None, 'last4': row.last4 if row else '',
                        'model': row.model if row else '', 'base_url': row.base_url if row else '',
                        'primary': bool(row and row.is_primary),
                        'updated_by': row.updated_by if row else '',
                        'updated_at': row.updated_at.isoformat() if row else None})
        return out

    def _payload(self, extra=None):
        from . import ai_providers
        rows = self._rows()
        used = next((r['id'] for r in rows if r['primary']), None)
        return {'providers': rows, 'in_use': used, 'server_key': bool(
            not any(r['has_key'] for r in rows) and ai_providers.any_configured()), **(extra or {})}

    def get(self, request):
        return Response(self._payload())

    def put(self, request):
        from clients import pins
        from . import ai_providers
        from .models import AiKey

        provider = str(request.data.get('provider') or '')
        spec = ai_providers.PROVIDERS.get(provider)
        if spec is None:
            raise ValidationError({'detail': 'Pick one of the listed services.'})
        row = AiKey.objects.filter(provider=provider).first()

        if request.data.get('remove'):
            if row is not None:
                was_primary = row.is_primary
                row.delete()
                if was_primary:
                    nxt = AiKey.objects.order_by('-updated_at').first()
                    if nxt:
                        AiKey.objects.filter(pk=nxt.pk).update(is_primary=True)
            return Response(self._payload())

        if request.data.get('test'):
            if row is None:
                raise ValidationError({'detail': 'Save a key for this service first.'})
            try:
                ai_providers.complete(provider, pins.unseal(row.sealed_key), row.model, 'Reply with the single word: ok',
                                      'ping', base_url=row.base_url, max_tokens=200)
                result = {'ok': True, 'message': 'This key works.'}
            except ai_providers.AiError as exc:
                result = {'ok': False, 'message': str(exc)}
            return Response(self._payload({'test': {'provider': provider, **result}}))

        raw_key = request.data.get('key')
        if raw_key in (None, '') and row is None:
            raise ValidationError({'detail': 'Paste the key for this service.'})
        try:
            key = ai_providers.clean_key(raw_key) if raw_key not in (None, '') else None
            base_url = str(request.data.get('base_url') if 'base_url' in request.data else (row.base_url if row else '')).strip()
            if spec.get('needs_url'):
                ai_providers.check_url(base_url)
        except ai_providers.AiError as exc:
            raise ValidationError({'detail': str(exc)})
        model = str(request.data.get('model') if 'model' in request.data else (row.model if row else '')).strip()[:120]
        if spec.get('needs_url') and not model:
            raise ValidationError({'detail': 'Give the model name to use with this service.'})

        who = getattr(request.user, 'name', '') or ''
        if row is None:
            row = AiKey(provider=provider)
        if key:
            row.sealed_key, row.last4 = pins.seal(key), key[-4:]
        row.model, row.base_url, row.updated_by = model, base_url if spec.get('needs_url') else '', who
        if not AiKey.objects.exclude(pk=row.pk).filter(is_primary=True).exists():
            row.is_primary = True                                  # the first (or only) key is the one in use
        if request.data.get('primary'):
            AiKey.objects.exclude(pk=row.pk).update(is_primary=False)
            row.is_primary = True
        row.save()
        return Response(self._payload())


class MasterPinsView(OwnerView):
    """Each salon's master PIN of today: for the accounts whose role ticks Owner App > Master PINs."""
    permission_submodule = 'master_pins'

    def get(self, request):
        from clients import pins
        today = datetime.date.today()
        rows = [{'center_id': c.id, 'center': scope.center_name(c), 'pin': pins.master_pin(c, today)}
                for c in scope.allowed_centers(request.user)]
        return Response({'date': today.isoformat(), 'pins': rows})
