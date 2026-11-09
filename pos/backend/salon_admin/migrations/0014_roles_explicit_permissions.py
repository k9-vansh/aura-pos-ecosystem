"""Make every existing role's access explicit under the fully role-based model.

Owner decision #6 removed every rule that recognised a role by its NAME. Until
now a role called "owner" (any capitalisation) silently received everything;
from now on only the superuser does. So that nobody loses access on the day
this ships:

  * a role named "owner" is given every module, submodule, verb and action
    tick, and all branches - exactly what its name used to grant;
  * every role keeps Chat (read + send), which needed no permission before.

Running it twice changes nothing. The reverse migration does nothing: the
explicit grants are harmless if the code is rolled back.
"""

from django.db import migrations


def grant(apps, schema_editor):
    from pos_backend.permission_tree import action_map, submodule_map

    Role = apps.get_model('salon_admin', 'Role')
    actions = action_map()
    for role in Role.objects.all():
        perms = role.permissions if isinstance(role.permissions, dict) else {}

        if (role.name or '').strip().lower() == 'owner':
            perms = {'all_centers': True}
            for module, submodules in submodule_map().items():
                perms[module] = {}
                for sub in submodules:
                    entry = {'read': True, 'create': True, 'update': True, 'delete': True}
                    if (module, sub) in actions:
                        entry['actions'] = {a: True for a in actions[(module, sub)]}
                    perms[module][sub] = entry
        else:
            chat = perms.setdefault('chat', {})
            if isinstance(chat, dict):
                messages = chat.setdefault('messages', {})
                if isinstance(messages, dict):
                    messages['read'] = True
                    messages['create'] = True

        role.permissions = perms
        role.save(update_fields=['permissions'])


class Migration(migrations.Migration):

    dependencies = [
        ('salon_admin', '0013_center_sap_branch_name'),
    ]

    operations = [
        migrations.RunPython(grant, migrations.RunPython.noop),
    ]
