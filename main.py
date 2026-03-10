import os
import io
import csv
import uuid
import qrcode
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, HTTPException, Form, Header, Depends
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

# --- CONFIGURATION & SECURITY ---
MONGO_URI = os.getenv("MONGO_URI", "your_mongodb_uri_here")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "1234")     # Master Password
SCANNER_TOKEN = os.getenv("SCANNER_TOKEN", "0000") # Door Staff Password

client = MongoClient(MONGO_URI)
db = client["event_database"]
events_collection = db["events"]
tickets_collection = db["tickets"]
LOGO_PATH = "logo.png"
IST = timezone(timedelta(hours=5, minutes=30))

def get_ist_now(): return datetime.now(IST)

# --- ROLE-BASED SECURITY DEPENDENCIES ---
def verify_admin(x_token: str = Header(None)):
    if x_token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized Admin Access!")

def verify_scanner(x_token: str = Header(None)):
    # The Admin can also use the scanner, so we accept either token here
    if x_token not in [ADMIN_TOKEN, SCANNER_TOKEN]:
        raise HTTPException(status_code=401, detail="Unauthorized Scanner Access!")

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
    c.drawRightString(card_x + card_w - 16, card_y + 8, f"Issued: {get_ist_now().strftime('%Y-%m-%d %I:%M %p')}")

    c.showPage()
    c.save()
    buffer.seek(0)
    return buffer

# --- SECURE API ENDPOINTS ---

# Admins only can create events
@app.post("/api/events", dependencies=[Depends(verify_admin)])
def create_event(name: str = Form(...), date: str = Form(...), venue: str = Form(...)):
    event_id = f"EVT-{uuid.uuid4().hex[:6].upper()}"
    events_collection.insert_one({"_id": event_id, "name": name, "date": date, "venue": venue, "created_at": get_ist_now()})
    return {"status": "success", "event_id": event_id}

# Both Scanner and Admin need to pull the event list
@app.get("/api/events", dependencies=[Depends(verify_scanner)])
def get_events():
    events = list(events_collection.find().sort("created_at", -1))
    return [{"id": e["_id"], "name": e["name"], "date": e["date"], "venue": e["venue"]} for e in events]

# Admins only can generate tickets
@app.post("/api/generate", dependencies=[Depends(verify_admin)])
def generate_ticket(event_id: str = Form(...), name: str = Form(...), email: str = Form(default=""), phone: str = Form(default=""), tickets: str = Form(...)):
    event = events_collection.find_one({"_id": event_id})
    if not event: raise HTTPException(status_code=404, detail="Event not found")

    ticket_id = f"TICKET-{uuid.uuid4().hex[:8].upper()}"
    
    tickets_collection.insert_one({
        "_id": ticket_id, "event_id": event_id, "attendee_name": name, "attendee_email": email, "attendee_phone": phone,
        "tickets_count": tickets, "is_scanned": False, "scanned_at": None, "created_at": get_ist_now()
    })
    
    pdf_buffer = create_ticket_pdf_buffer(event["name"], name, event["date"], event["venue"], tickets, ticket_id)
    filename = f"Ticket_{name.replace(' ', '_')}.pdf"
    
    pdf_buffer.seek(0)
    headers = {'Content-Disposition': f'attachment; filename="{filename}"'}
    return StreamingResponse(pdf_buffer, media_type="application/pdf", headers=headers)

# Scanners and Admins can scan tickets
@app.get("/api/scan/{ticket_id}", dependencies=[Depends(verify_scanner)])
def scan_ticket(ticket_id: str):
    ticket = tickets_collection.find_one({"_id": ticket_id})
    if not ticket: return {"status": "error", "message": "Invalid Ticket!"}
    
    event = events_collection.find_one({"_id": ticket["event_id"]})
    event_name = event["name"] if event else "Unknown Event"

    if ticket["is_scanned"]: 
        return {"status": "warning", "message": "ALREADY SCANNED!", "name": ticket['attendee_name'], "event": event_name, "tickets_count": ticket['tickets_count'], "scanned_time": ticket['scanned_at']}
    
    timestamp = get_ist_now().strftime("%d-%b-%Y %I:%M %p")
    tickets_collection.update_one({"_id": ticket_id}, {"$set": {"is_scanned": True, "scanned_at": timestamp}})
    return {"status": "success", "message": "ENTRY GRANTED", "name": ticket['attendee_name'], "event": event_name, "tickets_count": ticket['tickets_count'], "scanned_time": timestamp}

# Admins only can download CSV
@app.get("/api/export/{event_id}", dependencies=[Depends(verify_admin)])
def export_guests(event_id: str):
    event = events_collection.find_one({"_id": event_id})
    if not event: raise HTTPException(status_code=404, detail="Event not found")

    tickets = tickets_collection.find({"event_id": event_id}).sort("created_at", 1)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Ticket ID", "Attendee Name", "Email", "Phone", "Tickets Bought", "Has Scanned In?", "Scan Time"])

    for t in tickets:
        writer.writerow([
            t["_id"], t.get("attendee_name", ""), t.get("attendee_email", ""), 
            t.get("attendee_phone", ""), t.get("tickets_count", ""), 
            "Yes" if t.get("is_scanned") else "No", t.get("scanned_at", "")
        ])

    buffer.seek(0)
    return StreamingResponse(buffer, media_type="text/csv", headers={"Content-Disposition": f"attachment; filename=GuestList_{event['name']}.csv"})

# Admins only can view dashboards
@app.get("/api/dashboard/global", dependencies=[Depends(verify_admin)])
def get_global_stats():
    total_events = events_collection.count_documents({})
    pipeline_total = [{"$addFields": {"tickets_int": {"$toInt": "$tickets_count"}}}, {"$group": {"_id": None, "total": {"$sum": "$tickets_int"}}}]
    expected = list(tickets_collection.aggregate(pipeline_total))[0]["total"] if list(tickets_collection.aggregate(pipeline_total)) else 0
    pipeline_scanned = [{"$match": {"is_scanned": True}}, {"$addFields": {"tickets_int": {"$toInt": "$tickets_count"}}}, {"$group": {"_id": None, "total": {"$sum": "$tickets_int"}}}]
    arrived = list(tickets_collection.aggregate(pipeline_scanned))[0]["total"] if list(tickets_collection.aggregate(pipeline_scanned)) else 0
    return {"events": total_events, "expected": expected, "arrived": arrived}

@app.get("/api/dashboard/event/{event_id}", dependencies=[Depends(verify_admin)])
def get_event_stats(event_id: str):
    pipeline_total = [{"$match": {"event_id": event_id}}, {"$addFields": {"tickets_int": {"$toInt": "$tickets_count"}}}, {"$group": {"_id": None, "total": {"$sum": "$tickets_int"}}}]
    expected = list(tickets_collection.aggregate(pipeline_total))[0]["total"] if list(tickets_collection.aggregate(pipeline_total)) else 0
    pipeline_scanned = [{"$match": {"event_id": event_id, "is_scanned": True}}, {"$addFields": {"tickets_int": {"$toInt": "$tickets_count"}}}, {"$group": {"_id": None, "total": {"$sum": "$tickets_int"}}}]
    arrived = list(tickets_collection.aggregate(pipeline_scanned))[0]["total"] if list(tickets_collection.aggregate(pipeline_scanned)) else 0
    recent = list(tickets_collection.find({"event_id": event_id, "is_scanned": True}, {"_id": 0, "attendee_name": 1, "tickets_count": 1, "scanned_at": 1}).sort("scanned_at", -1).limit(5))
    return {"expected": expected, "arrived": arrived, "recent": recent}

app.mount("/", StaticFiles(directory="static", html=True), name="static")