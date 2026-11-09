"""Spreadsheet formula injection guard for every Excel file the app writes.

openpyxl turns any string that starts with "=" into a live formula. A customer,
vendor or product named `=HYPERLINK("http://...","click")` therefore became a
formula cell in the finance, staff and inventory exports, and ran when someone
opened the file. No export here writes a formula on purpose, so any text that
could be read as one is stored as plain text instead.

Installed once, from PosBackendConfig.ready().
"""

_installed = False


def install():
    global _installed
    if _installed:
        return
    from openpyxl.cell import cell as cell_module
    from openpyxl.cell.cell import Cell

    original = Cell._bind_value

    def _bind_value(self, value):
        original(self, value)
        # Only text that arrived as a string: a number or date keeps its type.
        if self.data_type == 'f' and isinstance(value, str):
            self.data_type = 's'

    Cell._bind_value = _bind_value
    _installed = True
