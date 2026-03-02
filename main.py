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
events_collection = db["events"]       # NEW: Collection for Events
tickets_collection = db["tickets"]     # EXISTING: Collection for Tickets

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

# 1. Create a New Event
@app.post("/api/events")
def create_event(name: str = Form(...), date: str = Form(...), venue: str = Form(...)):
    event_id = f"EVT-{uuid.uuid4().hex[:6].upper()}"
    events_collection.insert_one({
        "_id": event_id,
        "name": name,
        "date": date,
        "venue": venue,
        "created_at": datetime.now()
    })
    return {"status": "success", "event_id": event_id}

# 2. Get All Events (For the Dropdown)
@app.get("/api/events")
def get_events():
    events = list(events_collection.find().sort("created_at", -1))
    return [{"id": e["_id"], "name": e["name"], "date": e["date"], "venue": e["venue"]} for e in events]

# 3. Generate Ticket (Linked to Event)
@app.post("/api/generate")
def generate_ticket(
    event_id: str = Form(...), 
    name: str = Form(...), 
    email: str = Form(default=""), 
    phone: str = Form(default=""), 
    tickets: str = Form(...)
):
    # Fetch the parent event details
    event = events_collection.find_one({"_id": event_id})
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    # Create DB Entry linking to the event_id and storing all attendee details
    ticket_id = f"TICKET-{uuid.uuid4().hex[:8].upper()}"
    tickets_collection.insert_one({
        "_id": ticket_id, 
        "event_id": event_id,
        "attendee_name": name, 
        "attendee_email": email,
        "attendee_phone": phone,
        "tickets_count": tickets,
        "is_scanned": False, 
        "scanned_at": None
    })
    
    # Generate PDF using the fetched event details
    pdf_buffer = create_ticket_pdf_buffer(event["name"], name, event["date"], event["venue"], tickets, ticket_id)
    headers = {'Content-Disposition': f'attachment; filename="Ticket_{name.replace(" ", "_")}.pdf"'}
    return StreamingResponse(pdf_buffer, media_type="application/pdf", headers=headers)

# 4. Scan Ticket
@app.get("/api/scan/{ticket_id}")
def scan_ticket(ticket_id: str):
    ticket = tickets_collection.find_one({"_id": ticket_id})
    if not ticket: return {"status": "error", "message": "Invalid Ticket!"}
    
    event = events_collection.find_one({"_id": ticket["event_id"]})
    event_name = event["name"] if event else "Unknown Event"

    if ticket["is_scanned"]: 
        return {"status": "warning", "message": f"ALREADY USED! ({ticket['scanned_at']})", "name": f"{ticket['attendee_name']} - {event_name}"}
    
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tickets_collection.update_one({"_id": ticket_id}, {"$set": {"is_scanned": True, "scanned_at": timestamp}})
    return {"status": "success", "message": "ENTRY GRANTED!", "name": f"{ticket['attendee_name']} - {event_name}"}

# 5. Stats
@app.get("/api/stats")
def get_stats():
    total_events = events_collection.count_documents({})
    total_tickets = tickets_collection.count_documents({})
    scanned_tickets = tickets_collection.count_documents({"is_scanned": True})
    return {"events": total_events, "tickets": total_tickets, "scanned": scanned_tickets}

app.mount("/", StaticFiles(directory="static", html=True), name="static")