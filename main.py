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

# ReportLab Imports
from reportlab.lib import colors
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

app = FastAPI()

# ==========================================
# 1. CONFIGURATION & SECURITY
# ==========================================
MONGO_URI = os.getenv("MONGO_URI", "your_mongodb_uri_here")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "1234")     
SCANNER_TOKEN = os.getenv("SCANNER_TOKEN", "0000") 

client = MongoClient(MONGO_URI)
db = client["event_database"]
events_collection = db["events"]
tickets_collection = db["tickets"]

IST = timezone(timedelta(hours=5, minutes=30))
def get_ist_now(): return datetime.now(IST)

# --- ROLE-BASED SECURITY DEPENDENCIES ---
def verify_admin(x_token: str = Header(None)):
    if x_token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized Admin Access!")

def verify_scanner(x_token: str = Header(None)):
    if x_token not in [ADMIN_TOKEN, SCANNER_TOKEN]:
        raise HTTPException(status_code=401, detail="Unauthorized Scanner Access!")


# ==========================================
# 2. BRANDING ASSETS & FONT REGISTRATION
# ==========================================
BACKGROUND_PATH = "ticket_bg.jpeg"  
SPONSOR_CENTER_PATH = "sponsor_left.jpeg" 
SPONSOR_RIGHT_PATH = "sponsor_right.jpeg"
FONT_PATH = "event_font.ttf"  
CUSTOM_FONT_NAME = "CustomEventFont"

# Register Font Globally on Startup
if os.path.exists(FONT_PATH):
    try:
        pdfmetrics.registerFont(TTFont(CUSTOM_FONT_NAME, FONT_PATH))
        print("✅ Custom Font Loaded Successfully!")
    except Exception as e:
        print(f"⚠️ Font registration error: {e}")
        CUSTOM_FONT_NAME = "Helvetica-Bold"
else:
    CUSTOM_FONT_NAME = "Helvetica-Bold" 


# ==========================================
# 3. PREMIUM PDF GENERATION ENGINE
# ==========================================
def create_ticket_pdf_buffer(event_name, attendee_name, date, venue, tickets, ticket_id):
    buffer = io.BytesIO()
    
    PAGE_W = 850
    PAGE_H = 360
    c = canvas.Canvas(buffer, pagesize=(PAGE_W, PAGE_H))

    # --- A. DRAW BACKGROUND ---
    if os.path.exists(BACKGROUND_PATH):
        c.drawImage(BACKGROUND_PATH, 0, 0, width=PAGE_W, height=PAGE_H, preserveAspectRatio=False)
    else:
        c.setFillColor(colors.darkred)
        c.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)

    # --- B. CENTERED STACK & EVENT NAME SHADOW ---
    main_center_x = 325 

    if os.path.exists(SPONSOR_CENTER_PATH):
        c.drawImage(SPONSOR_CENTER_PATH, main_center_x - 60, 275, width=120, height=45, mask="auto", preserveAspectRatio=True)

    c.setFont("Helvetica-Bold", 10)
    c.setFillColor(colors.white)
    c.drawCentredString(main_center_x, 260, "PRESENTS") 

    # Event Name with Drop Shadow
    c.setFont(CUSTOM_FONT_NAME, 50)
    # Shadow (Black, offset by +2x, -2y)
    c.setFillColor(colors.black)
    c.drawCentredString(main_center_x + 2, 200 - 2, event_name)
    # Main Text (Gold)
    c.setFillColor(colors.HexColor("#FFD700"))
    c.drawCentredString(main_center_x, 200, event_name)

    # --- C. RIGHT SPONSOR ---
    right_center_x = 745 
    c.setFont("Helvetica-Bold", 8)
    c.setFillColor(colors.white)
    c.drawCentredString(right_center_x - 10, 320, "IN ASSOCIATION WITH")

    if os.path.exists(SPONSOR_RIGHT_PATH):
        c.drawImage(SPONSOR_RIGHT_PATH, right_center_x - 60, 270, width=100, height=40, mask="auto", preserveAspectRatio=True)

    # --- D. SEMI-TRANSPARENT DETAILS BOX ---
    c.saveState() 
    c.setFillColor(colors.HexColor("#2b0000"))
    c.setFillAlpha(0.50)
    c.rect(50, 25, 590, 135, fill=1, stroke=0)
    c.restoreState() 

    # --- E. DRAW ATTENDEE DETAILS ---
    start_y = 135  
    row_spacing = 28 

    label_x = 55      
    separator_x = 135 
    value_x = 150     

    tickets_count = f"{tickets} Tickets" # Format dynamic ticket integer

    details = [
        ("Attendee:", attendee_name, "white", False),
        ("Tickets:", tickets_count, "gold", True),
        ("Venue:", venue, "white", True),
        ("Date:", date, "gold", True)
    ]

    for i, (label, value, color_theme, use_sep) in enumerate(details):
        current_y = start_y - (i * row_spacing)
        
        # 1. Label
        c.setFont("Helvetica-Bold", 14)
        c.setFillColor(colors.white)
        c.drawString(label_x, current_y, label)
        
        # 2. Separator
        if use_sep:
            if i % 2 == 0:
                c.setFillColor(colors.HexColor("#FFD700"))
            else:
                c.setFillColor(colors.white)
            c.drawString(separator_x, current_y, "|")
        
        # 3. Determine Color
        val_color = colors.HexColor("#FFD700") if color_theme == "gold" else colors.white
        
        # 4. Value Shadow
        c.setFillColor(colors.black)
        c.drawString(value_x + 1, current_y - 1, str(value))
        
        # 5. Value Main Text
        c.setFillColor(val_color)
        c.drawString(value_x, current_y, str(value))
        
        # 6. Horizontal Gold Line (Except last row)
        if i < len(details) - 1:
            c.setStrokeColor(colors.HexColor("#FFD700"))
            c.setLineWidth(0.5)
            c.line(label_x, current_y - 8, value_x + 280, current_y - 8)

    # --- F. DRAW QR CODE & ID ---
    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_H, box_size=10, border=1)
    qr.add_data(ticket_id)
    qr.make(fit=True)
    qr_pil = qr.make_image(fill_color="black", back_color="white").convert("RGB")

    buf_img = io.BytesIO()
    qr_pil.save(buf_img, format="PNG")
    buf_img.seek(0)
    qr_img_rl = ImageReader(buf_img)

    qr_size = 135
    qr_x = 672   
    qr_y = 100   

    c.setFillColor(colors.white)
    c.roundRect(qr_x - 5, qr_y - 5, qr_size + 10, qr_size + 10, radius=8, fill=1, stroke=0)
    c.drawImage(qr_img_rl, qr_x, qr_y, width=qr_size, height=qr_size)

    c.setFont("Helvetica-Bold", 10)
    c.setFillColor(colors.white)
    c.drawCentredString(right_center_x, qr_y - 25, f"ID: {ticket_id}")

    # --- G. SAVE BUFFER ---
    c.showPage()
    c.save()
    buffer.seek(0)
    return buffer


# ========================================================
# 4. SECURE API ENDPOINTS
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
    
    # Passes database variables directly into your custom drawing function!
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