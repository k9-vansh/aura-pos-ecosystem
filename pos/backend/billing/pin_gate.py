"""The PIN check at the till.

Money a client holds (an advance, a value card, a cashback wallet) is spent only when the person at the till has the
client's PIN - like the code of a gift card - or the branch's master PIN for the day. The till first asks the server
(POST /clients/api/pin/verify/), which answers with a short-lived pass, and sends that pass (`pin_token`) with the
payment. The pass is for one client at one branch.

Settings (Admin > Settings): billing.require_client_pin turns the check on or off; billing.pin_for_everyone also holds
back clients who have no PIN yet (otherwise they are let through, and their first purchase gives them a PIN).
"""
from rest_framework import serializers

from billing import payment_methods as pm
from clients import pins
from pos_backend import app_settings


def draws_down(payments_data, quote=None):
    """True when this bill spends money the client holds."""
    if quote is not None and getattr(quote, 'value_card', None) is not None and getattr(quote, 'card_settled', 0) > 0:
        return True
    return any(pm.is_liability_drawdown(pm.canonical(p.get('payment_method'))) for p in (payments_data or []))


def holds_back(client, center):
    """True when spending this client's held money at this branch needs a PIN: the check is on and the client has
    one (or the setting holds back clients who have none too). The till asks this as soon as a card or wallet is
    chosen, so the PIN is asked then and not only when the bill is finished."""
    if client is None:
        return False
    if not app_settings.get('billing.require_client_pin', center):
        return False
    return pins.has_pin(client) or bool(app_settings.get('billing.pin_for_everyone', center))


def enforce(request, client, center, payments_data, quote=None):
    """Refuse (400, pin_required) unless the payment carries a good pass. Nothing happens for a walk-in, a bill that
    spends no held money, or when the check is switched off."""
    if client is None or not draws_down(payments_data, quote):
        return
    if not holds_back(client, center):
        return
    token = (getattr(request, 'data', None) or {}).get('pin_token') if request is not None else None
    if pins.token_via(token, client, center) is None:
        # The key `pin_required` is the signal the till reacts to (it opens the PIN box); the text is for people.
        raise serializers.ValidationError({
            'pin_required': ["Ask the client for their PIN before their card, advance or wallet is used "
                             "(or the branch's master PIN if they have forgotten it)."],
        })
