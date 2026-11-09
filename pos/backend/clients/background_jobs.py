"""Background jobs owned by clients (discovered by pos_backend.jobs)."""

from pos_backend.jobs import job


@job('clients.welcome_email')
def welcome_email(payload, job_row):
    """Welcome a new client: one message with their PIN and the address of the client app.

    It carries the salon's logo and goes by e-mail (and WhatsApp once that is linked). Switched in Admin > Settings.
    Counter sign-ups send the same message at once from the form; the message is only ever sent once per client
    (`send_to_client` remembers), so whichever gets there first sends it.
    """
    from clients.models import Client
    from clients import pins

    client = Client.objects.select_related('center').filter(pk=payload.get('client_id')).first()
    if client is None or not client.email:
        return
    pins.on_create(client)
