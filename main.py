import os
import io
import uuid
import qrcode
from datetime import datetime
from fastapi import FastAPI, HTTPException, Form
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pymongo import MongoClient
from PIL import Image

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.platypus import Paragraph, Spacer, Table, TableStyle, Image as RLImage, Frame, KeepInFrame
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet

app = FastAPI()

# Database Connection
MONGO_URI = os.getenv("MONGO_URI", "mongodb+srv://eventadmin:nfFQDqhnlTYbLHwl@cluster0.u2qj35x.mongodb.net/?appName=Cluster0")
client = MongoClient(MONGO_URI)
db = client["event_database"]
tickets_collection = db["tickets"]

LOGO_PATH = "logo.png"

# --- PDF GENERATION HELPERS ---
def _fit_title_font(event_text, max_width, base_size=17, min_size=11, font_name="Helvetica-Bold"):
    size = base_size
    while size >= min_size:
        if stringWidth(event_text, font_name, size) <= max_width: return size
        size -= 1
    return min_size

def _pil_to_rlimage(pil_img, max_w=None, max_h=None):
    buf = io.BytesIO()
    pil_img.save(buf, format="PNG")
    buf.seek(0)
    img = RLImage(buf)
    if max_w or max_h: img._restrictSize(max_w or 1e9, max_h or 1e9)
    return img

def generate_qr_for_id(ticket_id):
    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_H, box_size=12, border=4)
    qr.add_data(ticket_id) 
    qr.make(fit=True)
    return qr.make_image(fill_color="black", back_color="white").convert("RGB")

def create_ticket_pdf_buffer(event, name, date, venue, tickets, ticket_id):
    buffer = io.BytesIO()
    PAGE_W, PAGE_H = 650, 340
    card_x, card_y = 20, 20
    card_w, card_h = PAGE_W - 40, PAGE_H - 40

    c = canvas.Canvas(buffer, pagesize=(PAGE_W, PAGE_H))
    c.setFillColor(colors.whitesmoke)
    c.roundRect(card_x, card_y, card_w, card_h, radius=12, fill=1, stroke=0)

    header_h = 52
    c.setFillColor(colors.HexColor("#1F4F82"))
    c.roundRect(card_x, card_y + card_h - header_h, card_w, header_h, radius=12, fill=1, stroke=0)
    c.rect(card_x, card_y + card_h - header_h, card_w, header_h - 12, fill=1, stroke=0)

    left_head_x = card_x + 16
    if os.path.exists(LOGO_PATH):
        logo_img = RLImage(LOGO_PATH)
        logo_img._restrictSize(40, 40)
        logo_w, logo_h = logo_img.wrap(0,0)
        logo_y = card_y + card_h - header_h + (header_h - logo_h) / 2
        c.drawImage(LOGO_PATH, left_head_x, logo_y, width=logo_w, height=logo_h, mask="auto")
        left_head_x += logo_w + 12

    right_head_x = card_x + card_w - 16
    event_max_w = right_head_x - left_head_x - 80
    title_size = _fit_title_font(event[:120], event_max_w, base_size=17, min_size=11)
    
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", title_size)
    c.drawString(left_head_x, card_y + card_h - header_h + (header_h - title_size) / 2 + 1, event[:120])
    c.setFont("Helvetica", 11)
    c.drawRightString(right_head_x, card_y + card_h - header_h + (header_h - 11) / 2 + 1, f"Date: {date}")

    content_x, content_y = card_x + 16, card_y + 36 
    content_w, content_h = card_w - 32, card_h - header_h - 52
    left_w = content_w * 0.70
    right_w = content_w - left_w

    styles = getSampleStyleSheet()
    label_style = ParagraphStyle("label", parent=styles["Normal"], fontName="Helvetica", fontSize=8.5, leading=10.5, textColor=colors.grey)
    value_style = ParagraphStyle("value", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=12, leading=14, textColor=colors.black, spaceAfter=6)

    details_table = Table([
        [Paragraph("NAME", label_style), Paragraph(name, value_style)],
        [Paragraph("VENUE", label_style), Paragraph(venue, value_style)],
        [Paragraph("TICKETS", label_style), Paragraph(str(tickets), value_style)],
    ], colWidths=[34*mm, left_w - 36*mm], hAlign="LEFT")
    details_table.setStyle(TableStyle([("VALIGN", (0,0), (-1,-1), "TOP"), ("BOTTOMPADDING", (0,0), (-1,-1), 6)]))

    qr_img_pil = generate_qr_for_id(ticket_id)
    qr_max_size = min(right_w - 8, content_h - 10)
    qr_img_flow = _pil_to_rlimage(qr_img_pil, max_w=qr_max_size, max_h=qr_max_size)

    Frame(content_x, content_y, left_w, content_h, showBoundary=0).addFromList([KeepInFrame(left_w, content_h, [details_table], hAlign="LEFT", vAlign="TOP")], c)
    Frame(content_x + left_w, content_y, right_w, content_h, showBoundary=0).addFromList([KeepInFrame(right_w, content_h, [Spacer(1,2), qr_img_flow], hAlign="CENTER", vAlign="TOP")], c)

    c.setFont("Helvetica", 8)
    c.setFillColor(colors.grey)
    c.drawString(card_x + 16, card_y + 8, f"Ticket ID: {ticket_id}")
    c.drawRightString(card_x + card_w - 16, card_y + 8, f"Issued: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    c.showPage()
    c.save()
    buffer.seek(0)
    return buffer

# --- API ENDPOINTS ---

@app.post("/api/generate")
def generate_ticket(event: str = Form(...), name: str = Form(...), date: str = Form(...), venue: str = Form(...), tickets: str = Form(...)):
    # 1. Create DB Entry
    ticket_id = f"TICKET-{uuid.uuid4().hex[:8].upper()}"
    tickets_collection.insert_one({
        "_id": ticket_id, "name": name, "event": event,
        "is_scanned": False, "scanned_at": None
    })
    
    # 2. Generate PDF and send it straight to the user's phone
    pdf_buffer = create_ticket_pdf_buffer(event, name, date, venue, tickets, ticket_id)
    headers = {'Content-Disposition': f'attachment; filename="Ticket_{name.replace(" ", "_")}.pdf"'}
    return StreamingResponse(pdf_buffer, media_type="application/pdf", headers=headers)

@app.get("/api/scan/{ticket_id}")
def scan_ticket(ticket_id: str):
    ticket = tickets_collection.find_one({"_id": ticket_id})
    if not ticket: return {"status": "error", "message": "Invalid Ticket!"}
    if ticket["is_scanned"]: return {"status": "warning", "message": f"ALREADY USED! ({ticket['scanned_at']})", "name": ticket["name"]}
    
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tickets_collection.update_one({"_id": ticket_id}, {"$set": {"is_scanned": True, "scanned_at": timestamp}})
    return {"status": "success", "message": "ENTRY GRANTED!", "name": ticket["name"]}

@app.get("/api/stats")
def get_stats():
    total = tickets_collection.count_documents({})
    scanned = tickets_collection.count_documents({"is_scanned": True})
    return {"total": total, "scanned": scanned}

app.mount("/", StaticFiles(directory="static", html=True), name="static")