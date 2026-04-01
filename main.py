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
from reportlab.lib.pagesizes import landscape
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.platypus import Paragraph, Spacer, Table, TableStyle, Image as RLImage, Frame, KeepInFrame
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet

app = FastAPI()

# --- CONFIGURATION & SECURITY ---
MONGO_URI = os.getenv("MONGO_URI", "your_mongodb_uri_here")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "1234")     
SCANNER_TOKEN = os.getenv("SCANNER_TOKEN", "0000") 

client = MongoClient(MONGO_URI)
db = client["event_database"]
events_collection = db["events"]
tickets_collection = db["tickets"]

# --- BRANDING ASSETS ---
BACKGROUND_PATH = "ticket_bg.png"
SPONSOR_LEFT_PATH = "sponsor_left.png"   # Replaces ABC Brand
SPONSOR_RIGHT_PATH = "sponsor_right.png" # Replaces XYZ Brand

IST = timezone(timedelta(hours=5, minutes=30))
def get_ist_now(): return datetime.now(IST)

# --- ROLE-BASED SECURITY DEPENDENCIES ---
def verify_admin(x_token: str = Header(None)):
    if x_token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized Admin Access!")

def verify_scanner(x_token: str = Header(None)):
    if x_token not in [ADMIN_TOKEN, SCANNER_TOKEN]:
        raise HTTPException(status_code=401, detail="Unauthorized Scanner Access!")

# --- PREMIUM PDF GENERATION ENGINE ---
def _fit_title_font(event_text, max_width, base_size=36, min_size=16, font_name="Times-BoldItalic"):
    size = base_size
    while size >= min_size:
        if stringWidth(event_text, font_name, size) <= max_width: return size
        size -= 2
    return min_size

def generate_qr_for_id(ticket_id):
    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_H, box_size=15, border=2)
    qr.add_data(ticket_id) 
    qr.make(fit=True)
    return qr.make_image(fill_color="black", back_color="white").convert("RGB")

def _pil_to_rlimage(pil_img, max_w=None, max_h=None):
    buf = io.BytesIO()
    pil_img.save(buf, format="PNG")
    buf.seek(0)
    img = RLImage(buf)
    if max_w or max_h: img._restrictSize(max_w or 1e9, max_h or 1e9)
    return img

def create_ticket_pdf_buffer(event, name, date, venue, tickets, ticket_id):
    buffer = io.BytesIO()
    
    # Dimensions matched to the aspect ratio of the provided template
    PAGE_W, PAGE_H = 850, 360
    c = canvas.Canvas(buffer, pagesize=(PAGE_W, PAGE_H))

    # 1. DRAW THE BACKGROUND TEMPLATE
    if os.path.exists(BACKGROUND_PATH):
        c.drawImage(BACKGROUND_PATH, 0, 0, width=PAGE_W, height=PAGE_H, preserveAspectRatio=False)
    else:
        # Fallback dark background if image is missing
        c.setFillColor(colors.HexColor("#3a0a10"))
        c.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)

    # 2. EVENT TITLE (Center-aligned in the left section)
    left_section_center = 330
    title_size = _fit_title_font(event, max_width=500, base_size=42, min_size=20, font_name="Times-BoldItalic")
    c.setFont("Times-BoldItalic", title_size)
    c.setFillColor(colors.HexColor("#FFD700")) # Gold color
    # Add a slight drop shadow effect for readability
    c.drawString(left_section_center - (stringWidth(event, "Times-BoldItalic", title_size)/2) + 2, 228, event)
    c.setFillColor(colors.white)
    c.drawString(left_section_center - (stringWidth(event, "Times-BoldItalic", title_size)/2), 230, event)

    # 3. SPONSOR LOGOS
    # Top Left Sponsor
    if os.path.exists(SPONSOR_LEFT_PATH):
        left_logo = RLImage(SPONSOR_LEFT_PATH)
        left_logo._restrictSize(120, 50)
        lw, lh = left_logo.wrap(0,0)
        c.drawImage(SPONSOR_LEFT_PATH, 40, PAGE_H - lh - 30, width=lw, height=lh, mask="auto")
    
    # Top Right Sponsor
    if os.path.exists(SPONSOR_RIGHT_PATH):
        right_logo = RLImage(SPONSOR_RIGHT_PATH)
        right_logo._restrictSize(140, 50)
        rw, rh = right_logo.wrap(0,0)
        # Center it perfectly in the right perforated section
        c.drawImage(SPONSOR_RIGHT_PATH, 715 - (rw/2), PAGE_H - rh - 40, width=rw, height=rh, mask="auto")

    # 4. ATTENDEE DETAILS TABLE (Bottom Left)
    styles = getSampleStyleSheet()
    # Style for Labels (White)
    lbl_style = ParagraphStyle("lbl", fontName="Helvetica-Bold", fontSize=14, textColor=colors.white)
    # Style for Values (Gold/Yellow)
    val_style = ParagraphStyle("val", fontName="Helvetica-Bold", fontSize=14, textColor=colors.HexColor("#FFD700"))
    
    # Adding a subtle visual separator '|' just like the design
    data = [
        [Paragraph("Attendee:", lbl_style), Paragraph("|", lbl_style), Paragraph(name, val_style)],
        [Paragraph("Tickets:", lbl_style), Paragraph("|", lbl_style), Paragraph(f"{tickets} Tickets", val_style)],
        [Paragraph("Venue:", lbl_style), Paragraph("|", lbl_style), Paragraph(venue, val_style)],
        [Paragraph("Date:", lbl_style), Paragraph("|", lbl_style), Paragraph(date, val_style)]
    ]
    
    t = Table(data, colWidths=[90, 15, 400], rowHeights=25)
    t.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('LEFTPADDING', (0,0), (-1,-1), 0),
        ('BOTTOMPADDING', (0,0), (-1,-1), 0),
    ]))
    
    t.wrapOn(c, 500, 200)
    t.drawOn(c, 50, 50) # Bottom left placement

    # 5. QR CODE (Right Section)
    qr_size = 135
    qr_x = 715 - (qr_size/2) # Center horizontally in right section
    qr_y = 90
    
    # Draw a crisp white rounded box behind the QR code so scanners can easily read it
    c.setFillColor(colors.white)
    c.roundRect(qr_x - 5, qr_y - 5, qr_size + 10, qr_size + 10, radius=8, fill=1, stroke=0)
    
    qr_img_pil = generate_qr_for_id(ticket_id)
    qr_img_flow = _pil_to_rlimage(qr_img_pil, max_w=qr_size, max_h=qr_size)
    qr_img_flow.drawOn(c, qr_x, qr_y)

    # 6. TICKET ID (Small text under QR for manual lookup)
    c.setFont("Helvetica-Bold", 8)
    c.setFillColor(colors.white)
    c.drawCentredString(715, qr_y - 18, f"ID: {ticket_id}")

    c.showPage()
    c.save()
    buffer.seek(0)
    return buffer

# ========================================================
# SECURE API ENDPOINTS
# ========================================================

@app.post("/api/events", dependencies=[Depends(verify_admin)])
def create_event(name: str = Form(...), date: str = Form(...), venue: str = Form(...)):
    event_id = f"EVT-{uuid.uuid4().hex[:6].upper()}"
    events_collection.insert_one({"_id": event_id, "name": name, "date": date, "venue": venue, "created_at": get_ist_now()})
    return {"status": "success", "event_id": event_id}

@app.get("/api/events", dependencies=[Depends(verify_scanner)])
def get_events():
    events = list(events_collection.find().sort("created_at", -1))
    return [{"id": e["_id"], "name": e["name"], "date": e["date"], "venue": e["venue"]} for e in events]

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