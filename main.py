import os
from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pymongo import MongoClient

app = FastAPI()

# Replace with your actual MongoDB URI for local testing
MONGO_URI = os.getenv("MONGO_URI", "mongodb+srv://eventadmin:nfFQDqhnlTYbLHwl@cluster0.u2qj35x.mongodb.net/?appName=Cluster0")
client = MongoClient(MONGO_URI)
db = client["event_database"]
tickets_collection = db["tickets"]

class TicketCreate(BaseModel):
    ticket_id: str
    name: str
    event: str

@app.post("/api/create")
def create_ticket(ticket: TicketCreate):
    if tickets_collection.find_one({"_id": ticket.ticket_id}):
        raise HTTPException(status_code=400, detail="Ticket ID already exists")

    new_ticket = {
        "_id": ticket.ticket_id,
        "name": ticket.name,
        "event": ticket.event,
        "is_scanned": False,
        "scanned_at": None
    }
    tickets_collection.insert_one(new_ticket)
    return {"status": "success", "message": "Ticket stored in DB"}

@app.get("/api/scan/{ticket_id}")
def scan_ticket(ticket_id: str):
    ticket = tickets_collection.find_one({"_id": ticket_id})
    if not ticket:
        return {"status": "error", "message": "Invalid Ticket: Not Found!"}

    if ticket["is_scanned"]:
        return {"status": "warning", "message": f"ALREADY USED! Scanned on {ticket['scanned_at']}", "name": ticket["name"]}

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tickets_collection.update_one({"_id": ticket_id}, {"$set": {"is_scanned": True, "scanned_at": timestamp}})

    return {"status": "success", "message": "ENTRY GRANTED!", "name": ticket["name"]}

app.mount("/", StaticFiles(directory="static", html=True), name="static")
