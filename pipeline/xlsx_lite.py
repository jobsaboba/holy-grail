"""xlsx_lite — minimal .xlsx writer with no third-party dependencies.

Writes a single-sheet workbook with a bold frozen header row, optional
column widths, inline strings (no sharedStrings table) and native numbers.
Enough for LibreOffice Calc and Excel to open cleanly on double-click.
"""
from __future__ import annotations

import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _col(i: int) -> str:
    """1-based column index -> letters (1 -> A, 27 -> AA)."""
    s = ""
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _cell(ref: str, v, bold: bool) -> str:
    s = ' s="1"' if bold else ""
    if v is None or v == "":
        return f'<c r="{ref}"{s}/>'
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return f'<c r="{ref}"{s}><v>{v}</v></c>'
    return (f'<c r="{ref}" t="inlineStr"{s}>'
            f'<is><t xml:space="preserve">{escape(str(v))}</t></is></c>')


def write_xlsx(path: Path, header: list, rows: list[list],
               widths: list[float] | None = None, sheet: str = "Sheet1") -> None:
    cols = ""
    if widths:
        cols = "<cols>" + "".join(
            f'<col min="{i}" max="{i}" width="{w}" customWidth="1"/>'
            for i, w in enumerate(widths, 1)
        ) + "</cols>"

    xml_rows = ['<row r="1">' + "".join(
        _cell(f"{_col(i)}1", h, bold=True) for i, h in enumerate(header, 1)
    ) + "</row>"]
    for rn, row in enumerate(rows, 2):
        cells = "".join(
            _cell(f"{_col(i)}{rn}", v, bold=False)
            for i, v in enumerate(row, 1)
        )
        xml_rows.append(f'<row r="{rn}">{cells}</row>')

    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<worksheet xmlns="{_NS}">'
        '<sheetViews><sheetView workbookViewId="0">'
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        '<selection pane="bottomLeft" activeCell="A2" sqref="A2"/>'
        "</sheetView></sheetViews>"
        f"{cols}<sheetData>{''.join(xml_rows)}</sheetData></worksheet>"
    )
    # sheet name needs manual attribute escaping
    name = sheet.replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;")
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<workbook xmlns="{_NS}" xmlns:r="{_R}">'
        f'<sheets><sheet name="{name}" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        "</Types>"
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    wb_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        "</Relationships>"
    )
    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<styleSheet xmlns="{_NS}">'
        '<fonts count="2">'
        '<font><sz val="10"/><name val="Liberation Sans"/></font>'
        '<font><b/><sz val="10"/><name val="Liberation Sans"/></font>'
        "</fonts>"
        '<fills count="2">'
        '<fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill>'
        "</fills>"
        '<borders count="1"><border/></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="2">'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
        "</cellXfs>"
        "</styleSheet>"
    )

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", root_rels)
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        z.writestr("xl/styles.xml", styles)
        z.writestr("xl/worksheets/sheet1.xml", sheet_xml)


def numify(v):
    """Turn numeric-looking strings into real numbers for spreadsheet cells."""
    if isinstance(v, (int, float)) or v is None or v == "":
        return v
    s = str(v)
    try:
        return int(s)
    except ValueError:
        try:
            return float(s)
        except ValueError:
            return s
