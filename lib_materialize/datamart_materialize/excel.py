import csv
from datetime import datetime
import openpyxl

from .utils import SimpleConverter


def xlsx_to_csv(source_filename, dest_fileobj):
    with open(source_filename, 'rb') as fp:
        workbook = openpyxl.load_workbook(fp)
        sheets = workbook.worksheets
        if len(sheets) != 1:
            raise ValueError("Excel workbook has %d sheets" % len(sheets))
        sheet, = sheets

        writer = csv.writer(dest_fileobj)
        for values in sheet.iter_rows(values_only=True):
            values = [
                # Avoid forced decimal point on integers
                '{0:g}'.format(v) if isinstance(v, float)
                # Decode dates into ISO-8601 strings
                else v.isoformat() if isinstance(v, datetime)
                else v
                for v in values
            ]

            writer.writerow(values)

        workbook.close()


class ExcelConverter(SimpleConverter):
    """Adapter converting Excel files to CSV.
    """
    transform = staticmethod(xlsx_to_csv)
