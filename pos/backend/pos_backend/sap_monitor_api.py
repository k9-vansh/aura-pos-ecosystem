"""Admin > SAP Sync: every POS document on its way to SAP, and what went wrong.

    GET  /sap-sync/api/?status=attention|retrying|waiting|done|all&center_id=&q=&page=
         summary counts plus one page of postings, newest first
    POST /sap-sync/api/<id>/retry/     run it again now, with fresh attempts
    POST /sap-sync/api/<id>/resolve/   {"note": "..."} - the SAP team posted or
                                       corrected it by hand; stop trying

Read needs Admin > SAP Sync; Retry and Resolve are action ticks on it. A
branch user sees only their branches' postings; master data (items, partners,
warehouses) belongs to no branch and is shown to all-branch users only.
Retry and Resolve are written to the activity log.
"""

from datetime import timedelta

from django.core.paginator import EmptyPage, Paginator
from django.db.models import Q
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from pos_backend import jobs, sap_jobs
from pos_backend.models import BackgroundJob
from pos_backend.permissions import HasModulePermission, require_action
from pos_backend.scoping import can_use_center, coerce_center_id, user_center_ids

PAGE_SIZE = 50

# What a person sees, from the job's state.
STATUS_FILTERS = {
    'attention': Q(status=BackgroundJob.DEAD),
    'retrying': Q(status=BackgroundJob.FAILED),
    'waiting': Q(status__in=[BackgroundJob.QUEUED, BackgroundJob.RUNNING]),
    'done': Q(status=BackgroundJob.DONE),
    'all': Q(),
}


def _clean_error(text):
    """'SapPostingFailed: HTTP 400 - -5002: ...' -> 'HTTP 400 - -5002: ...'."""
    text = text or ''
    for prefix in ('SapPostingFailed: ', 'RuntimeError: '):
        if text.startswith(prefix):
            return text[len(prefix):]
    return text


def _state(job):
    if job.status == BackgroundJob.DEAD:
        return 'attention'
    if job.status == BackgroundJob.FAILED:
        # A RetryLater (waiting on SAP or on the invoice) is not a failure.
        return 'waiting' if job.attempts == 0 else 'retrying'
    if job.status == BackgroundJob.DONE:
        return 'done'
    return 'waiting'


class _Scoped:
    permission_classes = [permissions.IsAuthenticated, HasModulePermission]
    permission_module = 'admin'
    permission_submodule = 'sap_sync'
    permission_strict_read = True

    def scoped(self, request):
        qs = BackgroundJob.objects.filter(kind=sap_jobs.JOB_KIND)
        centers = user_center_ids(request.user)
        if centers is not None:
            qs = qs.filter(payload__center_id__in=list(centers))
        return qs

    def get_job(self, request, pk):
        job = self.scoped(request).filter(pk=pk).first()
        if job is None:
            raise PermissionDenied('That posting is not one you can see.')
        return job


def _serialize(job, centers):
    payload = job.payload or {}
    center_id = payload.get('center_id')
    return {
        'id': job.pk,
        'label': payload.get('label') or f"{payload.get('object_type')} #{payload.get('object_id')}",
        'object_type': payload.get('object_type'),
        'object_id': payload.get('object_id'),
        'center_id': center_id,
        'center_name': centers.get(center_id, ''),
        'state': _state(job),
        'attempts': job.attempts,
        'max_attempts': job.max_attempts,
        'error': _clean_error(job.last_error),
        'created_at': job.created_at,
        'next_try_at': job.run_after if job.status in (BackgroundJob.QUEUED, BackgroundJob.FAILED) else None,
        'finished_at': job.finished_at,
        'resolved_note': payload.get('resolved_note', ''),
        'sap_ref': payload.get('sap_ref', ''),
    }


class SapSyncListView(_Scoped, APIView):
    def get(self, request):
        from django.conf import settings
        from salon_admin.models import Center

        qs = self.scoped(request)
        center_id = coerce_center_id(request.query_params.get('center_id'))
        if center_id is not None:
            if not can_use_center(request.user, center_id):
                raise PermissionDenied('You are not assigned to this branch.')
            qs = qs.filter(payload__center_id=center_id)
        else:
            from pos_backend.scoping import selected_center_ids
            picked = selected_center_ids()
            if picked:
                condition = Q()
                for pk in picked:
                    condition |= Q(payload__center_id=pk)
                qs = qs.filter(condition)

        since = timezone.now() - timedelta(hours=24)
        summary = {
            'attention': qs.filter(STATUS_FILTERS['attention']).count(),
            'retrying': qs.filter(status=BackgroundJob.FAILED, attempts__gt=0).count(),
            'waiting': qs.filter(Q(status__in=[BackgroundJob.QUEUED, BackgroundJob.RUNNING])
                                 | Q(status=BackgroundJob.FAILED, attempts=0)).count(),
            'done_24h': qs.filter(status=BackgroundJob.DONE, finished_at__gte=since).count(),
            'sap_enabled': bool(getattr(settings, 'SAP_ENABLED', False)),
            'last_posted_at': (qs.filter(status=BackgroundJob.DONE).order_by('-finished_at')
                               .values_list('finished_at', flat=True).first()),
        }

        state = request.query_params.get('status') or 'attention'
        if state == 'retrying':
            listed = qs.filter(status=BackgroundJob.FAILED, attempts__gt=0)
        elif state == 'waiting':
            listed = qs.filter(Q(status__in=[BackgroundJob.QUEUED, BackgroundJob.RUNNING])
                               | Q(status=BackgroundJob.FAILED, attempts=0))
        else:
            listed = qs.filter(STATUS_FILTERS.get(state, STATUS_FILTERS['attention']))

        term = (request.query_params.get('q') or '').strip()
        if term:
            listed = listed.filter(Q(payload__label__icontains=term) | Q(last_error__icontains=term))

        paginator = Paginator(listed.order_by('-created_at', '-id'), PAGE_SIZE)
        try:
            page = paginator.page(int(request.query_params.get('page') or 1))
        except (EmptyPage, ValueError):
            page = paginator.page(paginator.num_pages) if paginator.num_pages else None

        rows = list(page.object_list) if page else []
        center_ids = {(r.payload or {}).get('center_id') for r in rows} - {None}
        centers = dict(Center.all_objects.filter(pk__in=center_ids).values_list('pk', 'center_name'))
        return Response({
            'summary': summary,
            'status': state,
            'count': paginator.count,
            'page': page.number if page else 1,
            'pages': paginator.num_pages,
            'results': [_serialize(r, centers) for r in rows],
        })


class SapSyncRetryView(_Scoped, APIView):
    # A plain APIView has no action name: the tick governs the whole view.
    permission_action_ticks = {None: ('sap_sync', 'retry')}

    def post(self, request, pk):
        require_action(request.user, 'admin', 'sap_sync', 'retry')
        job = self.get_job(request, pk)
        if job.status not in (BackgroundJob.FAILED, BackgroundJob.DEAD):
            return Response({'detail': 'This posting is not waiting on a retry.'},
                            status=status.HTTP_400_BAD_REQUEST)
        jobs.revive(job.pk)
        _audit(request.user, job, 'RETRY', 'Retried SAP posting')
        job.refresh_from_db()
        return Response(_serialize(job, {}))


class SapSyncResolveView(_Scoped, APIView):
    permission_action_ticks = {None: ('sap_sync', 'resolve')}

    def post(self, request, pk):
        require_action(request.user, 'admin', 'sap_sync', 'resolve')
        job = self.get_job(request, pk)
        note = str(request.data.get('note') or '').strip()
        if not note:
            return Response({'note': 'Say what was done in SAP (for the record).'},
                            status=status.HTTP_400_BAD_REQUEST)
        if job.status not in (BackgroundJob.FAILED, BackgroundJob.DEAD):
            return Response({'detail': 'Only a failed posting can be marked as done by hand.'},
                            status=status.HTTP_400_BAD_REQUEST)
        payload = dict(job.payload or {})
        who = getattr(request.user, 'full_name', '') or request.user.email
        payload['resolved_note'] = f'{who}: {note}'[:500]
        BackgroundJob.objects.filter(pk=job.pk).update(
            status=BackgroundJob.DONE, payload=payload, finished_at=timezone.now(),
            locked_until=None, dedupe_key=None)
        _audit(request.user, job, 'RESOLVE', f'Marked SAP posting as done by hand: {note}')
        job.refresh_from_db()
        return Response(_serialize(job, {}))


def _audit(user, job, action, text):
    from audit_logs.models import SystemLog

    payload = job.payload or {}
    description = f"{text} - {payload.get('label') or job.pk}"
    SystemLog.objects.create(
        module='SAP', action=action, entity_type='SAP posting', entity_id=str(job.pk),
        path=f'/sap-sync/api/{job.pk}/', user=user,
        user_name=getattr(user, 'full_name', '') or '', user_email=getattr(user, 'email', '') or '',
        center_id=payload.get('center_id'),
        human_description=description[:1000], description=description[:2000],
    )
