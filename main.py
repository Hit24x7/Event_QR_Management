import os
import io
import csv
import uuid
import qrcode
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, HTTPException, Form, Header, Depends, Request, Response
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

# Twilio Import for WhatsApp
from twilio.twiml.messaging_response import MessagingResponse

app = FastAPI()

# ==========================================
# 1. CONFIGURATION & SECURITY
# ==========================================
MONGO_URI = os.getenv("MONGO_URI", "your_mongodb_uri_here")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "1234")     
SCANNER_TOKEN = os.getenv("SCANNER_TOKEN", "0000") 

# --- YOUR TWO WHATSAPP NUMBERS (Must include country code 91) ---
WA_NUMBER_1 = "919653193636" 
WA_NUMBER_2 = "918169630273"

client = MongoClient(MONGO_URI)
db = client["event_database"]
events_collection = db["events"]
tickets_collection = db["tickets"]
pending_leads = db["pending_leads"] 

IST = timezone(timedelta(hours=5, minutes=30))
def get_ist_now(): return datetime.now(IST)

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
SPONSOR_CENTER_PATH = "sponsor_left.jpg" 
SPONSOR_RIGHT_PATH = "sponsor_right.jpg"
FONT_PATH = "event_font.ttf"  
CUSTOM_FONT_NAME = "CustomEventFont"

if os.path.exists(FONT_PATH):
    try:
        pdfmetrics.registerFont(TTFont(CUSTOM_FONT_NAME, FONT_PATH))
    except Exception:
        CUSTOM_FONT_NAME = "Helvetica-Bold"
else:
    CUSTOM_FONT_NAME = "Helvetica-Bold" 

# ==========================================
# 3. PREMIUM PDF GENERATION ENGINE
# ==========================================
def create_ticket_pdf_buffer(event_name, attendee_name, date, start_time, end_time, venue, tickets, ticket_id):
    
    # --- NEW: DATE FORMATTING (Converts "2026-04-18" to "18 Apr'2026") ---
    try:
        parsed_date = datetime.strptime(date, "%Y-%m-%d")
        display_date = parsed_date.strftime("%d %b'%Y")
    except ValueError:
        display_date = date # Fallback if already formatted

    # --- NEW: TIME FORMATTING (Adds "next day" logic) ---
    try:
        t_start = datetime.strptime(start_time, "%H:%M")
        t_end = datetime.strptime(end_time, "%H:%M")
        
        # Convert 24h to 12h format (e.g., "11:30 pm", "5:00 am")
        start_f = t_start.strftime("%I:%M %p").lstrip("0").lower()
        end_f = t_end.strftime("%I:%M %p").lstrip("0").lower()
        
        # If the end time is numerically smaller than start time, it crossed midnight
        if t_end < t_start:
            time_str = f"{start_f} to {end_f} next day"
        else:
            time_str = f"{start_f} to {end_f}"
    except ValueError:
        time_str = f"{start_time} to {end_time}" # Fallback


    buffer = io.BytesIO()
    PAGE_W = 850
    PAGE_H = 360
    c = canvas.Canvas(buffer, pagesize=(PAGE_W, PAGE_H))

    if os.path.exists(BACKGROUND_PATH):
        c.drawImage(BACKGROUND_PATH, 0, 0, width=PAGE_W, height=PAGE_H, preserveAspectRatio=False)
    else:
        c.setFillColor(colors.darkred)
        c.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)

    main_center_x = 325 
    if os.path.exists(SPONSOR_CENTER_PATH):
        c.drawImage(SPONSOR_CENTER_PATH, main_center_x - 60, 275, width=120, height=45, mask="auto", preserveAspectRatio=True)

    c.setFont("Helvetica-Bold", 10)
    c.setFillColor(colors.white)
    c.drawCentredString(main_center_x, 260, "PRESENTS") 

    c.setFont(CUSTOM_FONT_NAME, 50)
    c.setFillColor(colors.black)
    c.drawCentredString(main_center_x + 2, 200 - 2, event_name)
    c.setFillColor(colors.HexColor("#FFD700"))
    c.drawCentredString(main_center_x, 200, event_name)

    right_center_x = 745 
    c.setFont("Helvetica-Bold", 8)
    c.setFillColor(colors.white)
    c.drawCentredString(right_center_x - 10, 320, "IN ASSOCIATION WITH")

    if os.path.exists(SPONSOR_RIGHT_PATH):
        c.drawImage(SPONSOR_RIGHT_PATH, right_center_x - 60, 270, width=100, height=40, mask="auto", preserveAspectRatio=True)

    c.saveState() 
    c.setFillColor(colors.HexColor("#2b0000"))
    c.setFillAlpha(0.50)
    c.rect(50, 10, 590, 155, fill=1, stroke=0)
    c.restoreState() 

    start_y = 145  
    row_spacing = 28 
    label_x = 55      
    separator_x = 135 
    value_x = 150     

    tickets_count = f"{tickets} Tickets"

    details = [
        ("Attendee:", attendee_name, "white", False),
        ("Tickets:", tickets_count, "gold", True),
        ("Venue:", venue, "white", True),
        ("Date:", display_date, "gold", True), # Now uses your dynamically formatted date
        ("Time:", time_str, "white", True)     # Now uses your dynamically formatted time
    ]

    for i, (label, value, color_theme, use_sep) in enumerate(details):
        current_y = start_y - (i * row_spacing)
        c.setFont("Helvetica-Bold", 14)
        c.setFillColor(colors.white)
        c.drawString(label_x, current_y, label)
        
        if use_sep:
            if i % 2 == 0: c.setFillColor(colors.HexColor("#FFD700"))
            else: c.setFillColor(colors.white)
            c.drawString(separator_x, current_y, "|")
        
        val_color = colors.HexColor("#FFD700") if color_theme == "gold" else colors.white
        
        c.setFillColor(colors.black)
        c.drawString(value_x + 1, current_y - 1, str(value))
        c.setFillColor(val_color)
        c.drawString(value_x, current_y, str(value))
        
        if i < len(details) - 1:
            c.setStrokeColor(colors.HexColor("#FFD700"))
            c.setLineWidth(0.5)
            c.line(label_x, current_y - 8, value_x + 280, current_y - 8)

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

    c.showPage()
    c.save()
    buffer.seek(0)
    return buffer

# ========================================================
# 4. PUBLIC CHECKOUT ENDPOINTS
# ========================================================
@app.get("/api/public/events")
def get_public_events():
    events = list(events_collection.find().sort("created_at", -1))
    return [{"id": e["_id"], "name": e["name"], "date": e["date"], "start_time": e.get("start_time", ""), "end_time": e.get("end_time", ""), "venue": e["venue"], "price": e.get("price", 500)} for e in events]

@app.post("/api/public/submit-lead")
def submit_lead(
    event_id: str = Form(...), name: str = Form(..., min_length=2), email: str = Form(...),
    phone: str = Form(..., min_length=10), tickets: int = Form(..., gt=0), total_amount: int = Form(...)
):
    event = events_collection.find_one({"_id": event_id})
    if not event: raise HTTPException(status_code=404, detail="Event not found")

    order_id = f"ORD-{uuid.uuid4().hex[:6].upper()}"
    pending_leads.insert_one({
        "_id": order_id, "event_id": event_id, "event_name": event["name"], "name": name,
        "email": email, "phone": phone, "tickets_requested": tickets, "total_amount": total_amount,
        "status": "awaiting_screenshot", "created_at": get_ist_now()
    })
    return {"status": "success", "order_id": order_id}

# ========================================================
# 5. SECURE ADMIN & SCANNER ENDPOINTS
# ========================================================
@app.post("/api/events", dependencies=[Depends(verify_admin)])
def create_event(name: str = Form(...), date: str = Form(...), start_time: str = Form(...), end_time: str = Form(...), venue: str = Form(...), price: str = Form(...)):
    event_id = f"EVT-{uuid.uuid4().hex[:6].upper()}"
    events_collection.insert_one({
        "_id": event_id, "name": name, "date": date, "start_time": start_time, "end_time": end_time, 
        "venue": venue, "price": int(price), "created_at": get_ist_now()
    })
    return {"status": "success", "event_id": event_id}

@app.post("/api/events/update_price", dependencies=[Depends(verify_admin)])
def update_event_price(event_id: str = Form(...), new_price: int = Form(...)):
    result = events_collection.update_one({"_id": event_id}, {"$set": {"price": new_price}})
    if result.modified_count == 0: return {"status": "error", "message": "Event not found or price unchanged."}
    return {"status": "success", "message": f"Price updated to ₹{new_price}"}

@app.get("/api/events", dependencies=[Depends(verify_scanner)])
def get_events():
    events = list(events_collection.find().sort("created_at", -1))
    return [{"id": e["_id"], "name": e["name"], "date": e["date"], "venue": e["venue"], "price": e.get("price", 500)} for e in events]

@app.post("/api/generate", dependencies=[Depends(verify_admin)])
def generate_ticket(event_id: str = Form(...), name: str = Form(...), email: str = Form(default=""), phone: str = Form(default=""), tickets: str = Form(...)):
    event = events_collection.find_one({"_id": event_id})
    if not event: raise HTTPException(status_code=404, detail="Event not found")

    ticket_id = f"TICKET-{uuid.uuid4().hex[:8].upper()}"
    tickets_collection.insert_one({
        "_id": ticket_id, "event_id": event_id, "attendee_name": name, "attendee_email": email, "attendee_phone": phone,
        "tickets_count": tickets, "is_scanned": False, "scanned_at": None, "created_at": get_ist_now()
    })
    
    start_time = event.get("start_time", "N/A")
    end_time = event.get("end_time", "N/A")
    
    pdf_buffer = create_ticket_pdf_buffer(event["name"], name, event["date"], start_time, end_time, event["venue"], tickets, ticket_id)
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

# ========================================================
# 6. WHATSAPP WEBHOOK (SMART ROUTING)
# ========================================================
@app.post("/api/whatsapp")
async def whatsapp_bot(request: Request):
    form_data = await request.form()
    
    # Check WHICH of your two numbers received the message
    to_number = form_data.get("To", "").replace("whatsapp:", "").replace("+", "")
    
    # Assign agent ID (1 or 2) based on the receiving number
    agent_id = "2" if to_number == WA_NUMBER_2 else "1"
    
    # Pass the agent parameter directly into the checkout link
    # ⚠️ CHANGE THIS TO YOUR ACTUAL RENDER LINK ⚠️
    CHECKOUT_URL = f"https://event-qr-management.onrender.com/checkout.html?agent={agent_id}"
    
    response = MessagingResponse()
    msg = f"Welcome to H&D Events! 🎟️\n\nTo book your tickets securely, please click the link below to select your passes and complete your payment:\n👉 {CHECKOUT_URL}\n\nOnce you have paid, just reply to this chat with your payment screenshot!"
    
    response.message(msg)
    return Response(content=str(response), media_type="application/xml")

app.mount("/", StaticFiles(directory="static", html=True), name="static")
