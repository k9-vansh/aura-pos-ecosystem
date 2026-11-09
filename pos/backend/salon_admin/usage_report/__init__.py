"""Dashboard > Usage Report: marketing performance for the marketing team.

One endpoint, `/salon_admin/api/usage-report/<section>/`, one section at a time so a page
loads only what is open. The Memberships and Value cards sections are the dashboard's
existing reports (`/analytics/memberships/`, `/analytics/value-cards/`), which accept this
tab's tick too, so their numbers are the same wherever they are read.
"""
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from pos_backend.permissions import module_permission
from salon_admin.dashboard_endpoints import dashboard_tab

from .base import Ctx, cached
from .cashback import cashback
from .coupons import coupons
from .customers import customers
from .forecast import forecast
from .offers import offers
from .overview import overview
from .packages import packages
from .verify import verify
from .whatsapp import whatsapp

SECTIONS = {
    'overview': overview, 'offers': offers, 'coupons': coupons, 'packages': packages, 'cashback': cashback,
    'customers': customers, 'whatsapp': whatsapp, 'forecast': forecast, 'verify': verify,
}


@module_permission(**dashboard_tab('usage_report'))
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def usage_report(request, section):
    build = SECTIONS.get(section)
    if build is None:
        raise NotFound('There is no such section in the Usage Report.')
    ctx = Ctx(request)
    # Verify is the page that checks the numbers against the books: it is always worked out afresh.
    return Response(build(ctx) if section == 'verify' else cached(ctx, section, build))
