"""M12: a list asked for without a page is capped, and says so."""
from django.test import SimpleTestCase
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from pos_backend.pagination import OptionalPagination


class CappedPagination(OptionalPagination):
    UNPAGED_LIMIT = 5


def ask(rows, query='', view=None, paginator=None):
    request = Request(APIRequestFactory().get('/x/' + query))
    paginator = paginator or CappedPagination()
    page = paginator.paginate_queryset(rows, request, view)
    return paginator.get_paginated_response(page)


class UnpagedCapTests(SimpleTestCase):
    def test_a_short_list_comes_back_whole_and_unmarked(self):
        response = ask(list(range(5)))
        self.assertEqual(response.data, [0, 1, 2, 3, 4])
        self.assertNotIn('X-Truncated', response)

    def test_a_long_list_is_cut_and_marked(self):
        response = ask(list(range(9)))
        self.assertEqual(response.data, [0, 1, 2, 3, 4])
        self.assertEqual(response['X-Truncated'], 'true')

    def test_asking_for_a_page_still_pages(self):
        response = ask(list(range(9)), '?page=1&page_size=4')
        self.assertEqual(response.data['count'], 9)
        self.assertEqual(response.data['results'], [0, 1, 2, 3])

    def test_a_list_that_grows_fast_can_set_a_lower_cap_on_its_view(self):
        class FastGrowing:
            unpaged_limit = 3
        response = ask(list(range(9)), view=FastGrowing())
        self.assertEqual(response.data, [0, 1, 2])
        self.assertEqual(response['X-Truncated'], 'true')

    def test_a_view_cannot_raise_the_cap_above_the_global_one(self):
        class Greedy:
            unpaged_limit = 10 ** 6
        response = ask(list(range(9)), view=Greedy())
        self.assertEqual(response.data, [0, 1, 2, 3, 4])

    def test_a_page_ignores_the_unpaged_cap(self):
        class FastGrowing:
            unpaged_limit = 3
        response = ask(list(range(9)), '?page=1&page_size=4', view=FastGrowing())
        self.assertEqual(response.data['results'], [0, 1, 2, 3])
