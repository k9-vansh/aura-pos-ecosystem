from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response


class OptionalPagination(PageNumberPagination):
    """
    Plain arrays unless the caller asks for a 'page' or 'page_size'.

    That keeps every screen that expects a raw array working, but a caller that
    forgot to ask for a page used to receive the WHOLE table (bills, customers,
    the stock ledger...). Without a page the answer is now capped at
    UNPAGED_LIMIT rows and says so in the `X-Truncated` header; screens that can
    hold more than that already page.
    """
    page_size = 50
    page_size_query_param = 'page_size'
    max_page_size = 1000
    UNPAGED_LIMIT = 10000

    def paginate_queryset(self, queryset, request, view=None):
        if 'page' not in request.query_params and 'page_size' not in request.query_params:
            self._unpaged = True
            self._truncated = False
            # A list that grows by thousands a day (bills, purchase orders, transfers) sets a lower `unpaged_limit`
            # on its view: its screens page, and an unpaged call used to get 18-36 MB at full size.
            limit = min(self.UNPAGED_LIMIT, getattr(view, 'unpaged_limit', None) or self.UNPAGED_LIMIT)
            try:
                rows = list(queryset[: limit + 1])
            except TypeError:                       # already a list
                rows = list(queryset)[: limit + 1]
            if len(rows) > limit:
                rows = rows[: limit]
                self._truncated = True
            return rows
        self._unpaged = False
        return super().paginate_queryset(queryset, request, view)

    def get_paginated_response(self, data):
        if getattr(self, '_unpaged', False):
            response = Response(data)
            if self._truncated:
                response['X-Truncated'] = 'true'
            return response
        return super().get_paginated_response(data)
