"""One branch's products, found by code, by ID or by name, without asking the database for each row.

The product import used to look every file row up with up to three case-insensitive queries per branch. On
a branch with a couple of thousand products each of those reads (and sorts) all of them, so an import took
minutes and a file for "all branches" took hours with the database locked for writing the whole time.
This holds the branch's (id, code, ID, name) in memory once and answers the same question from there.

The answer is the one the queries gave: a case-insensitive exact match, tried by code, then by ID, then by
name; when several products match, the first by name (then oldest). Products the import creates or renames
are kept up to date here, so a later row of the same file finds them, as it did through the database.
"""
from .models import Product


def _key(value):
    return str(value).lower()


class ProductIndex:
    def __init__(self, center):
        self.by_code, self.by_id, self.by_name = {}, {}, {}
        for pk, code, id_str, name in Product.objects.filter(center=center).values_list(
                'id', 'product_code', 'product_id_str', 'name'):
            self.add(pk, code, id_str, name)

    def add(self, pk, code, id_str, name):
        order = (_key(name), pk)
        for table, value in ((self.by_code, code), (self.by_id, id_str), (self.by_name, name)):
            if value:
                table.setdefault(_key(value), []).append((order, pk))

    def remove(self, pk, code, id_str, name):
        for table, value in ((self.by_code, code), (self.by_id, id_str), (self.by_name, name)):
            if value:
                kept = [entry for entry in table.get(_key(value), []) if entry[1] != pk]
                if kept:
                    table[_key(value)] = kept
                else:
                    table.pop(_key(value), None)

    def find(self, code, id_str, name):
        """The id of the product the file row means, or None."""
        for table, value in ((self.by_code, code), (self.by_id, id_str), (self.by_name, name)):
            if value:
                found = table.get(_key(value))
                if found:
                    return min(found)[1]
        return None
