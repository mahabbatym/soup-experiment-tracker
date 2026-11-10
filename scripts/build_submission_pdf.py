"""Render the submission Markdown as a printable, max-2-page PDF note.

This builder is optional; the generated PDF is included in the ZIP. Requires
reportlab and locally available DejaVu fonts (not redistributed).
"""
from html import escape
from pathlib import Path
import re

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, KeepTogether

ROOT=Path(__file__).resolve().parents[1]
font_dir=Path('/usr/share/fonts/truetype/dejavu')
regular=font_dir/'DejaVuSans.ttf'
bold=font_dir/'DejaVuSans-Bold.ttf'
if regular.exists() and bold.exists():
    pdfmetrics.registerFont(TTFont('SoupBody',str(regular)))
    pdfmetrics.registerFont(TTFont('SoupBold',str(bold)))
    pdfmetrics.registerFontFamily('SoupBody',normal='SoupBody',bold='SoupBold',italic='SoupBody',boldItalic='SoupBold')
    FONT='SoupBody'
else:
    FONT='Helvetica'
NAVY=colors.HexColor('#13213D')
GREY=colors.HexColor('#46546B')
MINT=colors.HexColor('#1E6555')
PALE=colors.HexColor('#EDF4F1')
styles={
 'body':ParagraphStyle('body',fontName=FONT,fontSize=8.65,leading=12.65,textColor=GREY,spaceAfter=8),
 'h1':ParagraphStyle('h1',fontName=FONT,fontSize=17.5,leading=24,textColor=NAVY,spaceAfter=9),
 'h2':ParagraphStyle('h2',fontName=FONT,fontSize=11.2,leading=16,textColor=NAVY,spaceBefore=9,spaceAfter=6,keepWithNext=True),
 'bullet':ParagraphStyle('bullet',fontName=FONT,fontSize=8.5,leading=12.4,textColor=GREY,spaceAfter=5,leftIndent=12,firstLineIndent=-8),
 'small':ParagraphStyle('small',fontName=FONT,fontSize=7.6,leading=11.5,textColor=GREY,spaceAfter=4),
 'table':ParagraphStyle('table',fontName=FONT,fontSize=7.2,leading=10.4,textColor=NAVY),
 'table_head':ParagraphStyle('table_head',fontName=FONT,fontSize=7.2,leading=10.5,textColor=NAVY),
}

def rich(text):
    text=escape(text)
    text=re.sub(r'\*\*(.+?)\*\*',r'<b>\1</b>',text)
    text=re.sub(r'`([^`]+)`',r'<font color="#176452">\1</font>',text)
    return text


def render():
    source=(ROOT/'SUBMISSION_NOTE.md').read_text(encoding='utf-8')
    doc=SimpleDocTemplate(str(ROOT/'SUBMISSION_NOTE.pdf'),pagesize=A4,
                          leftMargin=36,rightMargin=36,topMargin=43,bottomMargin=42,
                          title='Soup Internship — Submission Note',author='Soup task implementation')
    story=[];lines=source.splitlines();i=0
    while i < len(lines):
        line=lines[i].strip()
        if not line:
            i+=1;continue
        if line.startswith('|'):
            group=[]
            while i<len(lines) and lines[i].strip().startswith('|'):
                group.append(lines[i].strip());i+=1
            data=[]
            for row_index,row in enumerate(group):
                fields=[v.strip() for v in row.strip('|').split('|')]
                if all(set(v).issubset(set(':- ')) for v in fields):continue
                data.append([Paragraph(rich(f),styles['table_head' if row_index==0 else 'table']) for f in fields])
            if data:
                table=Table(data,colWidths=[113,193,217],repeatRows=1,hAlign='LEFT')
                table.setStyle(TableStyle([
                    ('BACKGROUND',(0,0),(-1,0),PALE),('LINEBELOW',(0,0),(-1,0),.5,colors.HexColor('#C5DAD5')),
                    ('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,colors.HexColor('#F7F9FA')]),
                    ('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),7),
                    ('RIGHTPADDING',(0,0),(-1,-1),7),('TOPPADDING',(0,0),(-1,-1),6),
                    ('BOTTOMPADDING',(0,0),(-1,-1),6),('LINEBELOW',(0,1),(-1,-1),.3,colors.HexColor('#E1E7EB')),
                ]))
                story.append(table);story.append(Spacer(1,7))
            continue
        if line.startswith('# '):
            story.append(Paragraph(rich(line[2:]),styles['h1']));i+=1;continue
        if line.startswith('## '):
            story.append(Paragraph(rich(line[3:]),styles['h2']));i+=1;continue
        if line.startswith('- '):
            story.append(Paragraph('• '+rich(line[2:]),styles['bullet']));i+=1;continue
        paragraph=[line];i+=1
        while i<len(lines) and lines[i].strip() and not lines[i].strip().startswith(('## ','# ','- ','|')):
            paragraph.append(lines[i].strip());i+=1
        story.append(Paragraph(rich(' '.join(paragraph)),styles['body']))
    def draw_page(canvas, doc):
        canvas.setStrokeColor(colors.HexColor('#C9D7DD'))
        canvas.setLineWidth(.7)
        w,h=A4
        canvas.line(36,h-31,w-36,h-31)
        canvas.setFont('Helvetica-Bold',8)
        canvas.setFillColor(MINT)
        canvas.drawString(36,h-24,'SOUP  /  ENGINEERING TAKE-HOME')
        canvas.setFont('Helvetica',7.5)
        canvas.setFillColor(GREY)
        canvas.drawRightString(w-36,h-24,'SUBMISSION NOTE')
        canvas.line(36,30,w-36,30)
        canvas.drawString(36,18,'Evidence is included unmodified in the project archive.')
        canvas.drawRightString(w-36,18,f'{doc.page} / 2 MAX')
    doc.build(story,onFirstPage=draw_page,onLaterPages=draw_page)
    from pypdf import PdfReader
    count=len(PdfReader(str(ROOT/'SUBMISSION_NOTE.pdf')).pages)
    print('PDF pages:',count)
    if count>2:raise RuntimeError('Submission note exceeds 2 pages')

if __name__=='__main__':render()
