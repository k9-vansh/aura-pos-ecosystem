"""Background jobs owned by the staff app (discovered by pos_backend.jobs)."""

from pos_backend.jobs import job


@job('staff_api.login_email')
def login_email(payload, job_row):
    from clients import pins
    from notifications import integrations
    from notifications.service import _designed
    from staff.models import StaffMember

    staff = StaffMember.objects.select_related('center').filter(pk=payload.get('staff_id')).first()
    pin = pins.unseal(payload.get('sealed_pin') or '')
    if staff is None or not staff.email or not pin:
        return
    salon = (staff.center.display_name or staff.center.center_name) if staff.center_id else 'the salon'
    url = pins.app_url('staff')
    changed = payload.get('why') == 'changed'
    subject = f'Your new staff app PIN at {salon}' if changed else f'Welcome to the {salon} team: your staff app login'
    opening = ('A new PIN has been set for your staff app. Your old PIN no longer works.' if changed else
               f'Welcome to the team at {salon}. Your job card, appointments and profile are in the staff app.')
    text = (f'{opening}\n\nMobile number: {staff.phone or "the number the salon has for you"}\nYour PIN: {pin}\n\n'
            + (f'Open the staff app: {url}\n\n' if url else '')
            + 'Please do not share your PIN. If you did not expect this e-mail, tell your manager.')
    integrations.send_email(staff.email, subject, text,
                            html=_designed('staff.welcome', subject, text, user=staff, center_id=staff.center_id) or None)
