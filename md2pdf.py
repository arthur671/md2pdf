"""md2pdf - turn a Markdown file into a clean, print-ready A4 PDF.

Runs fully offline: no browser, no web service, no LaTeX. Only ReportLab.

Usage:
    python md2pdf.py <input.md> [output.pdf]
"""
import sys
import re
import os
from xml.sax.saxutils import escape
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    PageBreak, HRFlowable
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.enums import TA_LEFT

# --- Colors ---
TEXT_COLOR = colors.HexColor("#333333")
HEADING_COLOR = colors.HexColor("#1a1a1a")
LINK_COLOR = colors.HexColor("#0366d6")
RULE_COLOR = colors.HexColor("#d0d0d0")

# --- Page layout ---
PAGE_SIZE = A4
TOP_MARGIN = 0.75 * inch
BOTTOM_MARGIN = 0.75 * inch
LEFT_MARGIN = 0.9 * inch
RIGHT_MARGIN = 0.9 * inch
FOOTER_Y = 0.4 * inch          # baseline of the footer line, from page bottom
FOOTER_INSET = 5               # footer text sits slightly inside the text margins
FOOTER_FONT = ("Helvetica", 10)

PAGE_BREAK_MARKERS = {
    '<div style="page-break-after: always;"></div>',
    '<div style="page-break-before: always;"></div>',
    '\\pagebreak',
    '\\newpage',
}

LIST_RE = re.compile(r'^(?:[-*+]\s+|(\d+)[.)]\s+)(.*)$')
HR_RE = re.compile(r'^(?:-{3,}|\*{3,}|_{3,})$')


def create_styles():
    """Defines the paragraph styles of the document."""
    styles = getSampleStyleSheet()

    styles.add(ParagraphStyle(
        'MarkTextH1',
        parent=styles['Heading1'],
        fontName='Helvetica-Bold',
        fontSize=20,
        leading=26,
        spaceBefore=0,
        spaceAfter=2,
        textColor=HEADING_COLOR,
        alignment=TA_LEFT,
    ))

    styles.add(ParagraphStyle(
        'MarkTextH2',
        parent=styles['Heading2'],
        fontName='Helvetica-Bold',
        fontSize=16,
        leading=22,
        spaceBefore=18,
        spaceAfter=2,
        textColor=HEADING_COLOR,
        alignment=TA_LEFT,
        keepWithNext=True,
    ))

    styles.add(ParagraphStyle(
        'MarkTextH3',
        parent=styles['Heading3'],
        fontName='Helvetica-Bold',
        fontSize=14,
        leading=20,
        spaceBefore=14,
        spaceAfter=2,
        textColor=HEADING_COLOR,
        alignment=TA_LEFT,
        keepWithNext=True,
    ))

    styles.add(ParagraphStyle(
        'MarkTextBody',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=12,
        leading=18,
        spaceAfter=8,
        textColor=TEXT_COLOR,
    ))

    # List item (tight - no blank line between items)
    styles.add(ParagraphStyle(
        'MarkTextBullet',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=12,
        leading=18,
        leftIndent=24,
        bulletIndent=8,
        spaceBefore=2,
        spaceAfter=2,
        textColor=TEXT_COLOR,
    ))

    # List item (loose - blank line between items)
    styles.add(ParagraphStyle(
        'MarkTextBulletLoose',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=12,
        leading=18,
        leftIndent=24,
        bulletIndent=8,
        spaceBefore=6,
        spaceAfter=6,
        textColor=TEXT_COLOR,
    ))

    return styles


def format_inline(text):
    """Converts Markdown inline formatting to ReportLab paragraph markup."""
    # Escape &, < and > first so plain text like "Smith & Sons" is safe.
    text = escape(text)
    # Keep runs of 2+ spaces (e.g. signature lines "Landlord        Tenant")
    text = re.sub(r' {2,}', lambda m: '&nbsp;' * len(m.group()), text)
    # Bold italic: ***text***
    text = re.sub(r'\*\*\*(.+?)\*\*\*', r'<b><i>\1</i></b>', text)
    # Bold: **text** or __text__
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'__(.*?)__', r'<b>\1</b>', text)
    # Italic: *text* or _text_
    text = re.sub(r'\*(.*?)\*', r'<i>\1</i>', text)
    text = re.sub(r'(?<!\w)_(.*?)_(?!\w)', r'<i>\1</i>', text)
    # Inline code: `text`
    text = re.sub(r'`(.*?)`', r'<font face="Courier">\1</font>', text)
    # Markdown links: [text](url)
    text = re.sub(
        r'\[([^\]]+)\]\(([^)]+)\)',
        r'<a href="\2" color="#0366d6">\1</a>',
        text
    )
    # Autolinks <https://...> (already escaped to &lt;...&gt;)
    text = re.sub(r'&lt;(https?://[^\s&]+)&gt;', r'\1', text)
    # Bare URLs not already inside an <a> tag
    text = re.sub(
        r'(?<!href=")(?<!">)(https?://[^\s<>\)"]+)',
        r'<a href="\1" color="#0366d6">\1</a>',
        text
    )
    return text


def plain_text(text):
    """Strips Markdown emphasis markers, for use in the footer."""
    return re.sub(r'(\*\*|__|\*|`)', '', text).strip()


def make_hr():
    """Creates a thin horizontal rule."""
    return HRFlowable(
        width="100%",
        thickness=0.5,
        color=RULE_COLOR,
        spaceBefore=2,
        spaceAfter=8,
    )


def is_list_item(line):
    return LIST_RE.match(line) is not None


def detect_loose_list(lines, start_idx):
    """True if the list starting at start_idx has blank lines between items."""
    i = start_idx
    found_blank = False
    while i < len(lines):
        line = lines[i].strip()
        if is_list_item(line):
            if found_blank:
                return True
        elif line == "":
            found_blank = True
        else:
            break
        i += 1
    return False


def split_front_matter(raw_lines):
    """Splits an optional YAML-style front matter block off the top.

    Only the small subset md2pdf needs is understood:

        ---
        title: Some title
        footer:
          - \\pagenum
          - \\title 'Some text'
        ---

    Returns (settings dict, remaining lines).
    """
    if not raw_lines or raw_lines[0].strip() != '---':
        return {}, raw_lines
    for end in range(1, len(raw_lines)):
        if raw_lines[end].strip() == '---':
            break
    else:
        return {}, raw_lines  # no closing marker: not front matter

    settings, key = {}, None
    for line in raw_lines[1:end]:
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        item = re.match(r'^\s+-\s*(.*)$', line)
        if item and key:
            if not isinstance(settings.get(key), list):
                settings[key] = []
            settings[key].append(unquote(item.group(1)))
            continue
        pair = re.match(r'^([A-Za-z_][\w-]*)\s*:\s*(.*)$', line)
        if pair:
            key = pair.group(1).lower()
            value = pair.group(2).strip()
            settings[key] = unquote(value) if value else []
    return settings, raw_lines[end + 1:]


def unquote(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def resolve_footer(items, doc_title):
    """Turns footer items into a list of callables page -> text."""
    resolved = []
    for raw in items or []:
        item = raw.strip()
        if item == '\\pagenum':
            resolved.append(lambda page, total: f"{page} / {total}")
        elif item.startswith('\\title'):
            text = unquote(item[len('\\title'):]) or doc_title or ''
            resolved.append(lambda page, total, t=text: t)
        elif item == '\\date':
            from datetime import date
            today = date.today().strftime('%d.%m.%Y')
            resolved.append(lambda page, total, t=today: t)
        else:
            resolved.append(lambda page, total, t=item: t)
    return resolved


def make_footer_canvas(footer):
    """Canvas class that draws the footer once the total page count is known.

    1 item  -> centered
    2 items -> left / right, swapped on even pages (mirrored like a book)
    3 items -> left / center / right, outer items swapped on even pages
    """
    class FooterCanvas(rl_canvas.Canvas):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._saved_pages = []

        def showPage(self):
            self._saved_pages.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._saved_pages)
            for state in self._saved_pages:
                self.__dict__.update(state)
                self._draw_footer(self._pageNumber, total)
                super().showPage()
            super().save()

        def _draw_footer(self, page, total):
            if not footer:
                return
            texts = [f(page, total) for f in footer]
            width, _ = self._pagesize
            left_x = LEFT_MARGIN + FOOTER_INSET
            right_x = width - RIGHT_MARGIN - FOOTER_INSET
            self.saveState()
            self.setFont(*FOOTER_FONT)
            self.setFillColor(TEXT_COLOR)
            if len(texts) == 1:
                self.drawCentredString(width / 2, FOOTER_Y, texts[0])
            else:
                left, right = texts[0], texts[-1]
                if page % 2 == 0:
                    left, right = right, left
                self.drawString(left_x, FOOTER_Y, left)
                self.drawRightString(right_x, FOOTER_Y, right)
                if len(texts) >= 3:
                    self.drawCentredString(width / 2, FOOTER_Y, texts[1])
            self.restoreState()

    return FooterCanvas


def read_lines(file_path):
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return [l.rstrip('\n').rstrip('\r') for l in f.readlines()]
    except FileNotFoundError:
        print(f"Error: File '{file_path}' not found.")
        sys.exit(1)


def parse_markdown(raw_lines, styles):
    """Converts Markdown lines to ReportLab flowables."""
    story = []
    buffer_table = []
    i = 0

    # Pre-detect which list blocks are loose
    loose_items = set()
    j = 0
    while j < len(raw_lines):
        if is_list_item(raw_lines[j].strip()):
            loose = detect_loose_list(raw_lines, j)
            k = j
            while k < len(raw_lines):
                s = raw_lines[k].strip()
                if is_list_item(s) or (loose and s == ""):
                    if loose and is_list_item(s):
                        loose_items.add(k)
                    k += 1
                else:
                    break
            j = k
        else:
            j += 1

    def flush_table():
        nonlocal buffer_table
        if buffer_table:
            create_table(story, buffer_table)
            buffer_table = []

    while i < len(raw_lines):
        line = raw_lines[i].strip()

        # --- Page Break ---
        if line in PAGE_BREAK_MARKERS:
            flush_table()
            story.append(PageBreak())
            i += 1
            continue

        # --- Table ---
        if line.startswith("|"):
            buffer_table.append([cell.strip() for cell in line.strip('|').split('|')])
            i += 1
            continue
        flush_table()

        if not line:
            i += 1
            continue

        # --- Headings ---
        heading = re.match(r'^(#{1,6})\s+(.*)$', line)
        if heading:
            level = min(len(heading.group(1)), 3)
            story.append(Paragraph(format_inline(heading.group(2)),
                                   styles[f'MarkTextH{level}']))
            story.append(make_hr())
            i += 1
            continue

        # --- Horizontal Rule ---
        if HR_RE.match(line):
            story.append(make_hr())
            i += 1
            continue

        # --- Lists (bullets and numbered) ---
        item = LIST_RE.match(line)
        if item:
            number, text = item.group(1), item.group(2)
            marker = f"{number}." if number else "•"
            style_name = 'MarkTextBulletLoose' if i in loose_items else 'MarkTextBullet'
            story.append(Paragraph(f"{marker}&nbsp; {format_inline(text)}",
                                   styles[style_name]))
            i += 1
            continue

        # --- Normal Paragraph ---
        story.append(Paragraph(format_inline(line), styles['MarkTextBody']))
        i += 1

    flush_table()
    return story


def create_table(story, data):
    """Generates a clean table with a shaded header row."""
    filtered = [
        row for row in data
        if not all(re.match(r'^:?-+:?$', cell) for cell in row if cell)
    ]
    if not filtered:
        return

    col_count = max(len(row) for row in filtered)
    filtered = [row + [''] * (col_count - len(row)) for row in filtered]
    available_width = PAGE_SIZE[0] - LEFT_MARGIN - RIGHT_MARGIN
    col_width = available_width / col_count

    cell_style = ParagraphStyle(
        'MarkTextTableCell',
        fontName='Helvetica',
        fontSize=11,
        leading=14,
        textColor=TEXT_COLOR,
        alignment=TA_LEFT,
        splitLongWords=True,
    )
    header_style = ParagraphStyle(
        'MarkTextTableHeader',
        parent=cell_style,
        fontName='Helvetica-Bold',
    )
    wrapped = [
        [
            Paragraph(format_inline(cell),
                      header_style if row_index == 0 else cell_style)
            for cell in row
        ]
        for row_index, row in enumerate(filtered)
    ]
    t = Table(wrapped, colWidths=[col_width] * col_count, repeatRows=1)
    t.setStyle(TableStyle([
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('TEXTCOLOR', (0, 0), (-1, -1), TEXT_COLOR),
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#f6f8fa")),
        ('GRID', (0, 0), (-1, -1), 0.5, RULE_COLOR),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('PADDING', (0, 0), (-1, -1), 6),
    ]))
    story.append(Spacer(1, 0.1 * inch))
    story.append(t)
    story.append(Spacer(1, 0.1 * inch))


def first_heading(lines):
    for line in lines:
        m = re.match(r'^#\s+(.*)$', line.strip())
        if m:
            return plain_text(m.group(1))
    return None


def convert(input_file, output_file):
    settings, body = split_front_matter(read_lines(input_file))
    doc_title = settings.get('title') or first_heading(body) or \
        os.path.splitext(os.path.basename(input_file))[0]
    footer = resolve_footer(settings.get('footer'), doc_title)

    story = parse_markdown(body, create_styles())
    doc = SimpleDocTemplate(
        output_file,
        pagesize=PAGE_SIZE,
        topMargin=TOP_MARGIN,
        bottomMargin=BOTTOM_MARGIN,
        leftMargin=LEFT_MARGIN,
        rightMargin=RIGHT_MARGIN,
        title=doc_title,
    )
    doc.build(story, canvasmaker=make_footer_canvas(footer))


def main():
    if len(sys.argv) == 2:
        input_file = sys.argv[1]
        output_file = os.path.splitext(input_file)[0] + ".pdf"
        print(f"No output name provided. Defaulting to: {output_file}")
    elif len(sys.argv) == 3:
        input_file, output_file = sys.argv[1], sys.argv[2]
    else:
        print("Usage: python md2pdf.py <input.md> [output.pdf]")
        sys.exit(2)

    try:
        convert(input_file, output_file)
        print(f"Success! PDF generated at: {output_file}")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
