"""Admin > Settings API.

    GET   /app-settings/api/?center_id=7
          every setting: definition, global value, effective value, and the
          value for `center_id` when the setting can differ per branch.
    PATCH /app-settings/api/
          {"changes": [{"key": "...", "center": null | <id>, "value": ...}]}
          A null value with a center removes that branch's override; a null
          value without a center removes the saved global value (back to the
          server default). All changes are validated first and applied together.

Needs Admin > Settings (read to view, update to change). Changing a global
value needs an all-branches role; a branch value needs access to that branch.
Every change is written to the audit log with the old and new value.
"""

from django.db import transaction
from rest_framework import permissions, status
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from pos_backend import app_settings
from pos_backend.models import AppSetting
from pos_backend.permissions import HasModulePermission
from pos_backend.scoping import can_use_center, coerce_center_id, user_center_ids


def _describe(spec, center_id):
    rows = app_settings._rows()
    stored_global = rows.get((spec.key, 'global'))
    data = {
        'key': spec.key,
        'group': spec.group,
        'label': spec.label,
        'help': spec.help,
        'type': spec.type,
        'unit': spec.unit,
        'choices': [{'value': v, 'label': lbl} for v, lbl in spec.choices],
        'min': app_settings.to_json(spec, spec.min) if spec.min is not None else None,
        'max': app_settings.to_json(spec, spec.max) if spec.max is not None else None,
        'per_branch': 'branch' in spec.scopes,
        'server_default': app_settings.to_json(spec, app_settings.fallback(spec)),
        'global_value': stored_global,
        'global_effective': app_settings.to_json(spec, app_settings.get(spec.key)),
    }
    if center_id is not None and data['per_branch']:
        data['branch_value'] = rows.get((spec.key, app_settings.branch_scope(center_id)))
        data['branch_effective'] = app_settings.to_json(
            spec, app_settings.get(spec.key, center=center_id))
    return data


class AppSettingsView(APIView):
    permission_classes = [permissions.IsAuthenticated, HasModulePermission]
    permission_module = 'admin'
    permission_submodule = 'settings'
    permission_strict_read = True

    def get(self, request):
        center_id = coerce_center_id(request.query_params.get('center_id'))
        if center_id is not None and not can_use_center(request.user, center_id):
            raise PermissionDenied('You are not assigned to this branch.')
        return Response({
            'center_id': center_id,
            'can_change_global': user_center_ids(request.user) is None,
            'settings': [_describe(spec, center_id) for spec in app_settings.SPECS],
        })

    def patch(self, request):
        changes = request.data.get('changes')
        if not isinstance(changes, list) or not changes:
            return Response({'changes': 'Send a non-empty list of changes.'},
                            status=status.HTTP_400_BAD_REQUEST)

        user = request.user
        planned, errors = [], {}
        for index, change in enumerate(changes):
            key = (change or {}).get('key')
            spec = app_settings.BY_KEY.get(key)
            label = key or f'change {index + 1}'
            if spec is None:
                errors[label] = 'Unknown setting.'
                continue
            center_id = coerce_center_id(change.get('center'))
            if center_id is not None:
                if 'branch' not in spec.scopes:
                    errors[label] = 'This setting is the same for every branch.'
                    continue
                if not can_use_center(user, center_id):
                    raise PermissionDenied('You are not assigned to this branch.')
                scope = app_settings.branch_scope(center_id)
            else:
                if user_center_ids(user) is not None:
                    raise PermissionDenied(
                        'Changing a setting for every branch needs an all-branches role. '
                        'Choose your branch to change it there.')
                scope = AppSetting.GLOBAL
            raw = change.get('value')
            if raw is None:
                planned.append((spec, scope, center_id, None))
                continue
            try:
                value = app_settings.coerce(spec, raw)
            except app_settings.SettingError as exc:
                errors[label] = f'{spec.label} {exc}.'
                continue
            planned.append((spec, scope, center_id, app_settings.to_json(spec, value)))

        if errors:
            return Response({'errors': errors}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            for spec, scope, center_id, value in planned:
                row = AppSetting.objects.select_for_update().filter(key=spec.key, scope=scope).first()
                old = row.value if row else None
                if value is None:
                    if row:
                        row.delete()
                elif row:
                    row.value = value
                    row.updated_by = user
                    row.save(update_fields=['value', 'updated_by', 'updated_at'])
                else:
                    AppSetting.objects.create(key=spec.key, scope=scope, center_id=center_id,
                                              value=value, updated_by=user)
                if old != value:
                    _audit(user, spec, scope, center_id, old, value)
        app_settings.invalidate()
        return self.get(request)


def _audit(user, spec, scope, center_id, old, new):
    from audit_logs.models import SystemLog

    is_global = scope == AppSetting.GLOBAL
    where = 'all branches' if is_global else f'branch {center_id}'
    # With no value of its own, a branch follows the all-branches value; only
    # the all-branches value itself falls back to the server default.
    unset = 'server default' if is_global else 'all-branches value'
    text = (f'Setting "{spec.label}" for {where}: '
            f'{unset if old is None else old} -> '
            f'{unset if new is None else new}')
    SystemLog.objects.create(
        module='SETTINGS', action='UPDATE', entity_type='Setting', entity_id=spec.key,
        path='/app-settings/api/', user=user, user_name=getattr(user, 'full_name', '') or '',
        user_email=getattr(user, 'email', '') or '', center_id=center_id,
        human_description=text[:1000], description=text[:2000],
    )
