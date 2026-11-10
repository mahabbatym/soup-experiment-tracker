"""Build a printable, one-page design overview from the engineering decisions."""
from pathlib import Path
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from pypdf import PdfReader

ROOT=Path(__file__).resolve().parents[1]
font_dir=Path('/usr/share/fonts/truetype/dejavu')
if (font_dir/'DejaVuSans.ttf').exists():
    pdfmetrics.registerFont(TTFont('SB',str(font_dir/'DejaVuSans.ttf')))
    pdfmetrics.registerFont(TTFont('SBB',str(font_dir/'DejaVuSans-Bold.ttf')))
    pdfmetrics.registerFontFamily('SB',normal='SB',bold='SBB')
    font='SB'
else:font='Helvetica'
navy=colors.HexColor('#192842')
mint=colors.HexColor('#137457')
gray=colors.HexColor('#52607A')
body=ParagraphStyle('body',fontName=font,fontSize=8.5,leading=12.1,textColor=gray)
sm=ParagraphStyle('small',parent=body,fontSize=7.5,leading=11)
hdr=ParagraphStyle('hdr',parent=body,fontSize=10.8,leading=15,textColor=navy,spaceAfter=5)
title=ParagraphStyle('title',parent=body,fontSize=18.2,leading=24,textColor=navy,spaceAfter=5)
block=ParagraphStyle('block',parent=body,fontSize=8,leading=11.6,textColor=navy,alignment=TA_CENTER)

def para(x,sty=body):return Paragraph(x,sty)

def section(h,content):
    return [para(h,hdr),para(content,body)]

def build():
    dest=ROOT/'DESIGN_ONE_PAGE.pdf'
    doc=SimpleDocTemplate(str(dest),pagesize=A4,leftMargin=37,rightMargin=37,topMargin=46,bottomMargin=42,
                          title='Soup Tracker — One-Page Design')
    flow=Table([[para('<b>Producer / browser</b><br/>X-API-Key header',block),
                 para('→',block),para('<b>FastAPI</b><br/>Scoped REST + SSE',block),
                 para('→',block),para('<b>SQLite WAL</b><br/>Durable transactions',block)]],
               colWidths=[166,14,159,14,166],hAlign='LEFT')
    flow.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'MIDDLE'),('BACKGROUND',(0,0),(0,0),colors.HexColor('#EAF5ED')),
                              ('BACKGROUND',(2,0),(2,0),colors.HexColor('#EAF1FA')),
                              ('BACKGROUND',(4,0),(4,0),colors.HexColor('#EAF5ED')),
                              ('BOX',(0,0),(0,0),.55,colors.HexColor('#B0C5B4')),
                              ('BOX',(2,0),(2,0),.55,colors.HexColor('#C1CAD5')),
                              ('BOX',(4,0),(4,0),.55,colors.HexColor('#B0C5B4')),
                              ('TOPPADDING',(0,0),(-1,-1),13),('BOTTOMPADDING',(0,0),(-1,-1),13)]))
    left=[];right=[]
    left += section('01 / Event format and ordering',
       '<font color="#137457">POST /runs/{id}/metrics</font>: {batch_id: UUID, events: [{event_id: UUID, name, value: finite float, observed_at: ISO-8601 timezone}]}. Each commit also stores received_at and a per-run integer seq. Writers use SQLite BEGIN IMMEDIATE and update next_seq in the same transaction as inserted metric rows. seq is commit order, never client timestamp order. A late observed_at is not reordered into earlier commits.')
    left.append(Spacer(1,12))
    left += section('02 / Identical and conflicting retries',
       'The pair (run_id, batch_id) stores a canonical request digest plus original response. A bit-for-bit equivalent retry returns its original acknowledgement with zero new rows. A changed batch with the same ID returns 409. In different batches an unchanged event_id is deduped; a changed payload with the same ID returns 409, rolling back the whole transaction. Keys must remain stable across network retries.')
    left.append(Spacer(1,12))
    left += section('03 / Unreliable network and catch-up',
       'On initial load, request paginated metrics after seq=0. On stream connect and reconnect, send Last-Event-ID with the last <b>applied</b> seq; the server emits id: seq and event: metric in order. The UI deduplicates by seq. Durable DB paging of 100 at a time replaces in-memory subscriber queues, so a slow reader gets backpressure rather than silent queue eviction. There is no configured retention pruning.')
    right += section('04 / Project isolation',
       'Each project receives a 256-bit random API key; only its SHA-256 hash is stored. Every authenticated request resolves the project from the key. Every run, history, write and stream query constrains both project_id and run_id. Unknown and wrong-tenant IDs return identical 404 responses; UUIDv4 run IDs are impractical to enumerate. Key rotation revokes the presented key atomically. No response cache; authenticated responses specify no-store. Keys never enter URLs.')
    right.append(Spacer(1,12))
    right += section('05 / Silent failures to probe',
       'Disconnect/replay loss: compare SSE sequences with DB after restoring connectivity. Duplicate deliveries: retry the identical batch and same event in a new batch. Slow readers: paginate 570 records in groups of 17, and test 50 live subscribers. Partial writes: insert then cause a conflicting event, verify rollback. Rotation: assert the old key yields 401 on read/write/SSE. Logs, timing, proxy buffering and power loss are residual risks.')
    right.append(Spacer(1,12))
    right += section('06 / Production decision',
       '<b>DON\'T SHIP</b> for 50 × 10 events/s as a production guarantee. A 4-round local 500 events/s pulse passed with 50 streams and 2,000 DB-matched deliveries. Still needed: unrestricted browser E2E, Docker smoke, rate limits, quota and audit controls, multi-instance/PostgreSQL design, delivery-lag telemetry and long-running proxy-backed soak tests.')
    cols=Table([[left,right]],colWidths=[254,254],hAlign='LEFT')
    cols.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),0),
                              ('RIGHTPADDING',(0,0),(0,0),17),('LEFTPADDING',(1,0),(1,0),16),
                              ('LINEBEFORE',(1,0),(1,0),.7,colors.HexColor('#D7E0E4'))]))
    story=[para('Design before code — event integrity and isolation',title),
           para('One-page architecture | Source and expanded discussion: DESIGN.md',sm),Spacer(1,13),
           flow,Spacer(1,22),cols]
    def decorate(c,doc):
        w,h=A4
        c.setStrokeColor(colors.HexColor('#D0DCE0'))
        c.line(37,h-32,w-37,h-32);c.line(37,31,w-37,31)
        c.setFont('Helvetica-Bold',8);c.setFillColor(mint)
        c.drawString(37,h-24,'SOUP  /  ARCHITECTURE')
        c.setFont('Helvetica',7.5);c.setFillColor(gray)
        c.drawRightString(w-37,h-24,'EXPERIMENT TRACKER')
        c.drawString(37,18,'Evidence: evidence/pytest_final.txt + evidence/load_probe.txt')
        c.drawRightString(w-37,18,'1 / 1')
    doc.build(story,onFirstPage=decorate,onLaterPages=decorate)
    count=len(PdfReader(str(dest)).pages)
    print('Design PDF pages:',count)
    if count!=1:raise RuntimeError('Expected a one-page design')

if __name__=='__main__':build()
