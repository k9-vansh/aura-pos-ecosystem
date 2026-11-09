"""Background jobs owned by notifications (discovered by pos_backend.jobs)."""

import logging

from django.utils import timezone

from pos_backend.jobs import job

logger = logging.getLogger(__name__)


@job('notifications.deliver')
def deliver(payload, job_row):
    """Send one queued e-mail or WhatsApp message.

    A channel with no credentials is *skipped* (WhatsApp ships empty), never
    retried; a provider error is raised so the job is retried with backoff and
    the last error is kept on the log row.
    """
    from notifications.models import DeliveryLog

    log = DeliveryLog.objects.filter(pk=payload.get('log_id')).first()
    if log is None or log.status in (DeliveryLog.SENT, DeliveryLog.SKIPPED):
        return
    try:
        _send(log)
    finally:
        _settle_campaign_row(log, payload)


def _send(log):
    from notifications import integrations
    from notifications.models import DeliveryLog

    try:
        if log.channel == 'email':
            ref = integrations.send_email(log.recipient, log.subject or 'Notification', log.body,
                                          html=log.html or None)
        elif log.channel == 'whatsapp':
            ref = integrations.send_whatsapp(log.recipient, log.body, log.template, log.params)
        else:
            log.status, log.error = DeliveryLog.SKIPPED, f'Unknown channel {log.channel}'
            log.save(update_fields=['status', 'error'])
            return
    except integrations.NotConfigured as exc:
        log.status, log.error = DeliveryLog.SKIPPED, str(exc)[:500]
        log.save(update_fields=['status', 'error'])
        return
    except Exception as exc:                                   # noqa: BLE001 - recorded, then retried
        log.status, log.error = DeliveryLog.FAILED, str(exc)[:500]
        log.save(update_fields=['status', 'error'])
        raise
    log.status, log.error, log.provider_ref = DeliveryLog.SENT, '', str(ref)[:120]
    log.sent_at = timezone.now()
    log.save(update_fields=['status', 'error', 'provider_ref', 'sent_at'])


def _settle_campaign_row(log, payload):
    """A campaign message keeps its status on the Marketing > WhatsApp list."""
    message_id = payload.get('whatsapp_message_id')
    if not message_id:
        return
    from marketing.models import WhatsAppMessage

    text = {'sent': 'Sent', 'skipped': 'Not sent - WhatsApp not set up',
            'failed': 'Failed - will retry'}.get(log.status)
    if text:
        WhatsAppMessage.objects.filter(pk=message_id).update(status=text[:255])
