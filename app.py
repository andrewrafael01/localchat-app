import json
import requests
import datetime
import os

from functools import wraps

from flask import (
    Flask,
    request,
    jsonify,
    render_template_string,
    redirect,
    url_for,
    session,
)

from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import sessionmaker, declarative_base

from werkzeug.security import generate_password_hash, check_password_hash
from openai import OpenAI


import smtplib
from email.message import EmailMessage

# ---------- Email / SMTP config ----------
# You can configure real SMTP via environment variables, e.g.:
#   export SMTP_HOST="smtp.gmail.com"
#   export SMTP_PORT="587"
#   export SMTP_USER="your_email@gmail.com"
#   export SMTP_PASS="your_app_password"
#   export SMTP_FROM="LocalChat AI <your_email@gmail.com>"
#   export ADMIN_EMAIL="you@yourdomain.com"
#
# If not set, emails won't actually be sent; they will just be printed to the console.
SMTP_HOST = os.environ.get("SMTP_HOST")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASS = os.environ.get("SMTP_PASS")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USER or "localchat@example.com")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL")

client = OpenAI()


def send_email(to_email: str, subject: str, body: str):
    """Send an email if SMTP is configured, otherwise just print to console."""
    if not to_email:
        return

    if not SMTP_HOST or not SMTP_USER or not SMTP_PASS:
        # Fallback: just log the email payload so you can see it works.
        print("[EMAIL MOCK] To:", to_email)
        print("[EMAIL MOCK] Subject:", subject)
        print("[EMAIL MOCK] Body:\n", body)
        return

    try:
        msg = EmailMessage()
        msg["From"] = SMTP_FROM
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.set_content(body)

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        print("[EMAIL SENT] To:", to_email, "Subject:", subject)
    except Exception as e:
        print("[EMAIL ERROR]", repr(e))


# ---------- Files ----------
LEADS_FILE = "leads.json"
CHAT_LOG_FILE = "chat_logs.txt"
DB_URL = "sqlite:///app.db"

# ---------- SQLAlchemy setup ----------
Base = declarative_base()


class Business(Base):
    __tablename__ = "businesses"

    id = Column(Integer, primary_key=True)
    business_id = Column(String(64), unique=True, index=True, nullable=False)
    name = Column(String(200), nullable=False)
    hours = Column(Text, default="")
    services = Column(Text, default="")
    pricing = Column(Text, default="")
    location = Column(Text, default="")
    contact = Column(Text, default="")
    faqs = Column(Text, default="")
    blurb = Column(Text, default="")  # short marketing snippet


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(32), nullable=False, default="business")  # "admin" or "business"
    business_id = Column(String(64), nullable=True)  # link to Business.business_id for owners


engine = create_engine(DB_URL, echo=False, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def create_user(session_db, email, password, role="business", business_id=None):
    hashed = generate_password_hash(password, method="pbkdf2:sha256")
    u = User(
        email=email,
        password_hash=hashed,
        role=role,
        business_id=business_id,
    )
    session_db.add(u)
    session_db.commit()
    return u


def init_db():
    """Create tables, migrate businesses, and create a default admin user if missing."""
    Base.metadata.create_all(engine)
    session_db = SessionLocal()
    try:
        # Seed businesses from business.json if DB empty
        b_count = session_db.query(Business).count()
        if b_count == 0 and os.path.exists("business.json"):
            try:
                with open("business.json", "r") as f:
                    data = json.load(f)

                for bid, fields in data.items():
                    b = Business(
                        business_id=bid,
                        name=fields.get("name", bid),
                        hours=fields.get("hours", ""),
                        services=fields.get("services", ""),
                        pricing=fields.get("pricing", ""),
                        location=fields.get("location", ""),
                        contact=fields.get("contact", ""),
                        faqs=fields.get("faqs", ""),
                        blurb=f"Ask about hours, pricing, or services at {fields.get('name', bid)}.",
                    )
                    session_db.add(b)
                session_db.commit()
                b_count = session_db.query(Business).count()
            except Exception as e:
                print("Error migrating business.json to DB:", repr(e))

        # If still no businesses, create a demo one
        if b_count == 0:
            demo = Business(
                business_id="demo",
                name="Demo Business",
                hours="Mon–Fri 9am–5pm",
                services="Example services",
                pricing="Example pricing",
                location="123 Example St",
                contact="contact@example.com",
                faqs="Q: Is this real? A: Demo only.",
                blurb="Demo business. Ask about hours, pricing, or services.",
            )
            session_db.add(demo)
            session_db.commit()

        # Seed default admin user if none exist
        u_count = session_db.query(User).count()
        if u_count == 0:
            admin_email = "admin@localchat.ai"
            admin_password = "changeme123"

            admin_user = User(
                email=admin_email,
                password_hash=generate_password_hash(
                    admin_password, method="pbkdf2:sha256"
                ),
                role="admin",
                business_id=None,
            )
            session_db.add(admin_user)
            session_db.commit()

            print("Created default admin user:")
            print(f"  Email: {admin_email}")
            print(f"  Password: {admin_password}")

    finally:
        session_db.close()


init_db()

app = Flask(__name__)
app.secret_key = "change-this-secret-key-later"  # change in production

# ---------- Auth helpers ----------


def get_current_user():
    """Return (user, role, business_id_string or None)."""
    uid = session.get("user_id")
    if not uid:
        return None, None, None
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == uid).first()
        if not user:
            return None, None, None
        return user, user.role, user.business_id
    finally:
        db.close()


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        user, role, _ = get_current_user()
        if not user:
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)

    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        user, role, _ = get_current_user()
        if not user or role != "admin":
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)

    return wrapper


def business_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        user, role, business_id = get_current_user()
        if not user or role != "business":
            return redirect(url_for("login", next=request.path))
        if not business_id:
            return redirect(url_for("login"))
        return f(*args, **kwargs)

    return wrapper


def verify_user_password(user, password):
    return check_password_hash(user.password_hash, password)


# ---------- Chat UI HTML ----------
HTML_PAGE = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>AI Business Assistant</title>
  <style>
    :root {
      --bg: #1a1a1a;
      --bg-card: #242424;
      --accent: #007aff;
      --text: #ffffff;
      --text-muted: rgba(255, 255, 255, 0.6);
      --border: rgba(255, 255, 255, 0.08);
      --shadow: 0 2px 8px rgba(0, 0, 0, 0.3);
    }

    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
      -webkit-font-smoothing: antialiased;
    }

    body {
      margin: 0;
      min-height: 100vh;
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
      display: flex;
      flex-direction: column;
      line-height: 1.5;
    }

    .page-shell {
      max-width: 1200px;
      margin: 0 auto;
      padding: 48px 32px;
      display: flex;
      flex-direction: column;
      gap: 32px;
      width: 100%;
    }

    .top-nav {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 0 24px;
      border-bottom: 1px solid var(--border);
    }

    .brand {
      display: flex;
      align-items: center;
      gap: 12px;
    }

    .brand-logo {
      width: 32px;
      height: 32px;
      border-radius: 8px;
      background: var(--accent);
      display: flex;
      align-items: center;
      justify-content: center;
    }

    .brand-logo::before {
      content: "AI";
      color: white;
      font-weight: 600;
      font-size: 12px;
    }

    .brand-text {
      display: flex;
      flex-direction: column;
      gap: 2px;
    }

    .brand-name {
      font-size: 18px;
      font-weight: 600;
      color: var(--text);
    }

    .brand-sub {
      font-size: 12px;
      color: var(--text-muted);
    }

    .nav-right {
      font-size: 13px;
      color: var(--text-muted);
      display: flex;
      align-items: center;
      gap: 16px;
    }

    .badge {
      padding: 4px 12px;
      border-radius: 12px;
      background: rgba(52, 199, 89, 0.1);
      color: #34c759;
      font-size: 11px;
      font-weight: 500;
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }

    .badge-dot {
      width: 5px;
      height: 5px;
      border-radius: 50%;
      background: #34c759;
    }

    @keyframes pulse {
      0%, 100% { opacity: 1; }
      50% { opacity: 0.5; }
    }

    .layout {
      display: grid;
      grid-template-columns: 1.8fr 1fr;
      gap: 24px;
      align-items: start;
    }

    .card {
      border-radius: 16px;
      border: 1px solid var(--border);
      background: var(--bg-card);
      box-shadow: var(--shadow);
      padding: 32px;
      display: flex;
      flex-direction: column;
      min-height: 600px;
    }

    .card.secondary {
      background: var(--bg-card);
    }

    .chat-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding-bottom: 24px;
      border-bottom: 1px solid var(--border);
      margin-bottom: 24px;
    }

    .chat-header-main {
      display: flex;
      flex-direction: column;
      gap: 4px;
    }

    .chat-business-name {
      font-size: 20px;
      font-weight: 600;
      color: var(--text);
    }

    .chat-business-id {
      font-size: 13px;
      color: var(--text-muted);
    }

    .chat-status {
      font-size: 12px;
      color: var(--text-muted);
      display: flex;
      align-items: center;
      gap: 6px;
    }

    .online-dot {
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: #34c759;
    }

    .chat-window {
      flex: 1;
      padding: 16px 0;
      overflow-y: auto;
      display: flex;
      flex-direction: column;
      gap: 16px;
      font-size: 15px;
    }

    .chat-window::-webkit-scrollbar {
      width: 4px;
    }

    .chat-window::-webkit-scrollbar-track {
      background: transparent;
    }

    .chat-window::-webkit-scrollbar-thumb {
      background: var(--border);
      border-radius: 2px;
    }

    .bubble-row {
      display: flex;
    }

    .bubble-row.user {
      justify-content: flex-end;
    }

    .bubble-row.bot {
      justify-content: flex-start;
    }

    .bubble {
      max-width: 75%;
      padding: 12px 16px;
      border-radius: 16px;
      line-height: 1.5;
      word-wrap: break-word;
      white-space: pre-wrap;
      font-size: 15px;
    }

    .bubble.user {
      background: var(--accent);
      color: white;
      border-bottom-right-radius: 4px;
    }

    .bubble.bot {
      background: var(--bg);
      color: var(--text);
      border: 1px solid var(--border);
      border-bottom-left-radius: 4px;
    }

    .bubble.meta {
      background: transparent;
      color: var(--text-muted);
      border: none;
      font-size: 13px;
      max-width: 100%;
      padding: 12px 0;
      text-align: center;
    }

    .input-shell {
      padding-top: 24px;
      border-top: 1px solid var(--border);
      margin-top: 24px;
    }

    .input-row {
      display: flex;
      align-items: center;
      gap: 12px;
    }

    .input-inner {
      flex: 1;
      display: flex;
      align-items: center;
      background: var(--bg);
      border-radius: 12px;
      border: 1px solid var(--border);
      padding: 12px 16px;
      gap: 12px;
    }

    .input-inner:focus-within {
      border-color: var(--accent);
    }

    .input-inner input {
      flex: 1;
      background: transparent;
      border: none;
      outline: none;
      color: var(--text);
      font-size: 15px;
      font-family: inherit;
    }

    .input-inner input::placeholder {
      color: var(--text-muted);
    }

    .send-btn {
      padding: 12px 24px;
      border-radius: 12px;
      border: none;
      background: var(--accent);
      color: white;
      font-size: 15px;
      font-weight: 500;
      cursor: pointer;
      white-space: nowrap;
      font-family: inherit;
    }

    .send-btn:hover {
      opacity: 0.9;
    }

    .send-btn:disabled {
      opacity: 0.5;
      cursor: not-allowed;
    }

    .status-line {
      margin-top: 12px;
      font-size: 12px;
      color: var(--text-muted);
      display: flex;
      align-items: center;
      gap: 8px;
    }

    .status-dot {
      width: 5px;
      height: 5px;
      border-radius: 50%;
      background: #34c759;
    }

    .status-line.loading .status-dot {
      background: #ff9500;
    }

    .status-line.error .status-dot {
      background: #ff3b30;
    }

    .info-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 24px;
      padding-bottom: 20px;
      border-bottom: 1px solid var(--border);
    }

    .info-title {
      font-size: 18px;
      font-weight: 600;
      color: var(--text);
    }

    .info-pill {
      font-size: 11px;
      padding: 4px 10px;
      border-radius: 12px;
      background: rgba(0, 122, 255, 0.1);
      color: var(--accent);
      font-weight: 500;
    }

    .info-section {
      font-size: 14px;
      color: var(--text-muted);
      margin-bottom: 24px;
      line-height: 1.5;
      padding: 20px;
      background: var(--bg);
      border-radius: 12px;
      border: 1px solid var(--border);
    }

    .info-section strong {
      color: var(--text);
      font-weight: 600;
    }

    .info-chip-row {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-bottom: 24px;
    }

    .chip {
      font-size: 12px;
      padding: 6px 12px;
      border-radius: 12px;
      border: 1px solid var(--border);
      background: var(--bg);
      color: var(--text-muted);
      font-weight: 400;
    }

    .lead-card {
      margin-top: 8px;
      padding: 24px;
      border-radius: 12px;
      border: 1px solid var(--border);
      background: var(--bg);
    }

    .lead-title {
      font-size: 16px;
      font-weight: 600;
      margin-bottom: 6px;
      color: var(--text);
    }

    .lead-sub {
      font-size: 13px;
      color: var(--text-muted);
      margin-bottom: 20px;
      line-height: 1.5;
    }

    .lead-card label {
      display: block;
      font-size: 12px;
      margin-bottom: 6px;
      color: var(--text-muted);
      font-weight: 500;
    }

    .lead-card input {
      width: 100%;
      padding: 12px 16px;
      margin-bottom: 16px;
      border-radius: 12px;
      border: 1px solid var(--border);
      background: var(--bg-card);
      color: var(--text);
      font-size: 15px;
      font-family: inherit;
    }

    .lead-card input:focus {
      outline: none;
      border-color: var(--accent);
    }

    .lead-card input::placeholder {
      color: var(--text-muted);
    }

    .lead-btn {
      width: 100%;
      padding: 14px 0;
      border-radius: 12px;
      border: none;
      background: #34c759;
      color: white;
      font-size: 15px;
      font-weight: 500;
      cursor: pointer;
      font-family: inherit;
    }

    .lead-btn:hover {
      opacity: 0.9;
    }

    .lead-btn:disabled {
      opacity: 0.5;
      cursor: not-allowed;
    }

    .lead-note {
      margin-top: 12px;
      font-size: 12px;
      color: var(--text-muted);
      text-align: center;
      line-height: 1.5;
    }

    @media (max-width: 1024px) {
      .layout {
        grid-template-columns: 1fr;
      }
      .card {
        min-height: 500px;
      }
    }

    @media (max-width: 640px) {
      .page-shell {
        padding: 32px 20px;
        gap: 24px;
      }
      .top-nav {
        flex-direction: column;
        align-items: flex-start;
        gap: 12px;
        padding-bottom: 20px;
      }
      .nav-right {
        width: 100%;
        flex-direction: column;
        align-items: flex-start;
        gap: 8px;
      }
      .card {
        padding: 24px;
        min-height: 450px;
      }
      .chat-window {
        max-height: 400px;
      }
      .bubble {
        max-width: 85%;
      }
    }
  </style>
</head>
<body>
  <div class="page-shell">
    <div class="top-nav">
      <div class="brand">
        <div class="brand-logo"></div>
        <div class="brand-text">
          <div class="brand-name">LocalChat AI</div>
          <div class="brand-sub">24/7 assistant for small businesses</div>
        </div>
      </div>
      <div class="nav-right">
        <div class="badge">
          <span class="badge-dot"></span>
          Live demo connected
        </div>
        <span style="font-size: 11px;">Switch business with ?id=your_business_id</span>
      </div>
    </div>

    <div class="layout">
      <!-- Chat side -->
      <div class="card">
        <div class="chat-header">
          <div class="chat-header-main">
            <div class="chat-business-name" id="businessName">Loading business…</div>
            <div class="chat-business-id" id="businessIdLabel"></div>
          </div>
          <div class="chat-status">
            <div class="online-dot"></div>
            <span>AI assistant online</span>
          </div>
        </div>

        <div class="chat-window" id="messages">
          <div class="bubble-row bot">
            <div class="bubble meta">
              Ask a question like "What are your hours?" or "Do you take walk-ins?"
            </div>
          </div>
        </div>

        <div class="input-shell">
          <div class="input-row">
            <div class="input-inner">
              <input
                id="inputMsg"
                type="text"
                placeholder="Type a question and press Enter…"
                autocomplete="off"
              />
            </div>
            <button class="send-btn" id="sendBtn">Send</button>
          </div>
          <div class="status-line" id="status">
            <div class="status-dot"></div>
            <span>Ready.</span>
          </div>
        </div>
      </div>

      <!-- Info + lead side -->
      <div class="card secondary">
        <div class="info-header">
          <div class="info-title">Business Info & Lead Capture</div>
          <div class="info-pill">Per-business config</div>
        </div>

        <div class="info-section" id="bizSnippet">
          <strong>How it works:</strong> This assistant is trained only on the hours,
          pricing, services, and FAQs you enter in the dashboard. It never makes up
          random policies.
        </div>

        <div class="info-chip-row">
          <div class="chip">Answers questions</div>
          <div class="chip">Collects leads</div>
          <div class="chip">24/7 available</div>
          <div class="chip">Auto-updated</div>
        </div>

        <div class="lead-card">
          <div class="lead-title">Send your contact info</div>
          <div class="lead-sub">
            Leave your details and the business can reach back out to you directly.
          </div>

          <label for="leadName">Name</label>
          <input id="leadName" type="text" placeholder="Your name" />

          <label for="leadEmail">Email</label>
          <input id="leadEmail" type="email" placeholder="you@example.com" />

          <label for="leadPhone">Phone (optional)</label>
          <input id="leadPhone" type="tel" placeholder="(Optional)" />

          <button class="lead-btn" id="leadBtn">Send to business</button>
          <div class="lead-note" id="leadNote">
            The business receives your info instantly in their dashboard.
          </div>
        </div>
      </div>
    </div>
  </div>

  <script>
    const input = document.getElementById('inputMsg');
    const sendBtn = document.getElementById('sendBtn');
    const messagesDiv = document.getElementById('messages');
    const statusEl = document.getElementById('status');
    const bizNameEl = document.getElementById('businessName');
    const bizIdEl = document.getElementById('businessIdLabel');
    const bizSnippetEl = document.getElementById('bizSnippet');
    const leadNameEl = document.getElementById('leadName');
    const leadEmailEl = document.getElementById('leadEmail');
    const leadPhoneEl = document.getElementById('leadPhone');
    const leadBtn = document.getElementById('leadBtn');
    const leadNoteEl = document.getElementById('leadNote');

    const urlParams = new URLSearchParams(window.location.search);
    const requestedId = urlParams.get("id") || "";

    let currentBusinessId = null;

    function setStatus(type, text) {
      statusEl.className = "status-line " + (type || "");
      statusEl.innerHTML = '<div class="status-dot"></div><span>' + text + '</span>';
    }

    function appendBubble(who, text) {
      const row = document.createElement("div");
      row.className = "bubble-row " + (who === "You" ? "user" : "bot");

      const bubble = document.createElement("div");
      bubble.className = "bubble " + (who === "You" ? "user" : "bot");
      bubble.textContent = text;

      row.appendChild(bubble);
      messagesDiv.appendChild(row);
      messagesDiv.scrollTop = messagesDiv.scrollHeight;
    }

    async function loadBusiness() {
      try {
        const res = await fetch('/api/business?id=' + encodeURIComponent(requestedId));
        const data = await res.json();

        currentBusinessId = data.business_id;
        bizNameEl.textContent = data.name;
        bizIdEl.textContent = "Business ID: " + data.business_id;
        if (data.blurb) {
          bizSnippetEl.innerHTML = "<strong>About this business:</strong> " + data.blurb;
        }
      } catch (err) {
        console.error(err);
        bizNameEl.textContent = "Business";
        bizIdEl.textContent = "";
      }
    }

    async function sendMessage() {
      const msg = input.value.trim();
      if (!msg) return;
      if (!currentBusinessId) {
        appendBubble("Bot", "Business not ready yet. Please wait a moment and try again.");
        return;
      }

      input.value = "";
      appendBubble("You", msg);
      setStatus("loading", "Thinking…");
      sendBtn.disabled = true;

      try {
        const res = await fetch('/chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            business_id: currentBusinessId,
            message: msg
          })
        });

        const data = await res.json();
        appendBubble("Bot", data.reply || "No reply.");
        setStatus("", "Ready.");
      } catch (err) {
        console.error(err);
        appendBubble("Bot", "Error talking to server.");
        setStatus("error", "Error talking to AI.");
      } finally {
        sendBtn.disabled = false;
      }
    }

    async function sendLead() {
      const name = leadNameEl.value.trim();
      const email = leadEmailEl.value.trim();
      const phone = leadPhoneEl.value.trim();

      if (!currentBusinessId) {
        leadNoteEl.textContent = "Business not ready yet.";
        leadNoteEl.style.color = "#f97316";
        return;
      }

      if (!name || !email) {
        leadNoteEl.textContent = "Name and email are required.";
        leadNoteEl.style.color = "#f97316";
        return;
      }

      leadBtn.disabled = true;
      leadBtn.textContent = "Sending…";
      leadNoteEl.textContent = "Sending your info…";
      leadNoteEl.style.color = "var(--text-tertiary)";

      try {
        const res = await fetch('/lead', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            business_id: currentBusinessId,
            name,
            email,
            phone
          })
        });

        const data = await res.json();
        if (res.ok) {
          leadNoteEl.textContent = "Sent! The business has received your info.";
          leadNoteEl.style.color = "#10b981";
          leadNameEl.value = "";
          leadEmailEl.value = "";
          leadPhoneEl.value = "";
        } else {
          leadNoteEl.textContent = data.message || "Error sending info.";
          leadNoteEl.style.color = "#ef4444";
        }
      } catch (err) {
        console.error(err);
        leadNoteEl.textContent = "Error connecting. Please try again.";
        leadNoteEl.style.color = "#ef4444";
      } finally {
        leadBtn.disabled = false;
        leadBtn.textContent = "Send to business";
      }
    }

    input.addEventListener('keypress', (e) => {
      if (e.key === 'Enter') {
        sendMessage();
      }
    });

    sendBtn.addEventListener('click', sendMessage);
    leadBtn.addEventListener('click', sendLead);

    loadBusiness();
  </script>
</body>
</html>
"""

# ---------- Routes ----------


@app.route("/")
def index():
    return render_template_string(HTML_PAGE)


@app.route("/widget.js")
def widget_js():
    """
    Embeddable widget script.
    Usage:
    <script src="https://YOUR-SERVER/widget.js" data-business-id="campuscuts1"></script>
    """
    base_url = request.host_url.rstrip("/")
    js = f"""(function() {{
  try {{
    var script = document.currentScript;
    var businessId = script && script.getAttribute('data-business-id') || '';
    var iframe = document.createElement('iframe');
    iframe.src = '{base_url}/?id=' + encodeURIComponent(businessId);
    iframe.style.position = 'fixed';
    iframe.style.bottom = '20px';
    iframe.style.right = '20px';
    iframe.style.width = '360px';
    iframe.style.height = '520px';
    iframe.style.border = 'none';
    iframe.style.borderRadius = '12px';
    iframe.style.boxShadow = '0 4px 16px rgba(0, 0, 0, 0.3)';
    iframe.style.zIndex = '999999';
    iframe.setAttribute('title', 'AI Assistant');
    document.body.appendChild(iframe);
  }} catch (e) {{
    console && console.error && console.error('Widget load error', e);
  }}
}})();"""
    return js, 200, {"Content-Type": "application/javascript"}


@app.route("/api/business")
def api_business():
    raw_id = (request.args.get("id") or "").strip()
    db = SessionLocal()
    try:
        biz = None
        if raw_id:
            biz = db.query(Business).filter(Business.business_id == raw_id).first()
        if not biz:
            biz = db.query(Business).order_by(Business.id).first()

        if not biz:
            biz = Business(
                business_id="demo",
                name="Demo Business",
                hours="Mon–Fri 9am–5pm",
                services="Example services",
                pricing="Example pricing",
                location="123 Example St",
                contact="contact@example.com",
                faqs="Q: Is this real? A: Demo only.",
                blurb="Demo business. Ask about hours, pricing, or services.",
            )
            db.add(biz)
            db.commit()
            db.refresh(biz)

        return jsonify(
            {
                "business_id": biz.business_id,
                "name": biz.name,
                "blurb": biz.blurb or "",
            }
        )
    finally:
        db.close()


@app.route("/chat", methods=["POST"])
def chat():
    try:
        data = request.get_json(force=True)
        business_id = (data.get("business_id") or "").strip()
        user_message = (data.get("message") or "").strip()

        if not business_id or not user_message:
            return jsonify({"reply": "Missing business_id or message."}), 400

        db = SessionLocal()
        try:
            biz = db.query(Business).filter(Business.business_id == business_id).first()
        finally:
            db.close()

        if not biz:
            return jsonify({"reply": "Business not found."}), 404

        system_prompt = f"""
You are a helpful, concise AI assistant for the business \"{biz.name}\".

You MUST follow these rules:
- Answer ONLY using the data provided below (Hours, Services, Pricing, Location, Contact, FAQs).
- If the answer is not clearly supported by this data, say you are not sure and ask the user to contact the business directly.
- Do NOT invent prices, policies, availability, discounts, or guarantees.
- Keep answers brief and clear (usually under 5 sentences) unless the user asks for more detail.
- If the user asks about booking or making an appointment, direct them to the contact information.

BUSINESS PROFILE
----------------
Name: {biz.name}
Location: {biz.location}
Contact: {biz.contact}

Hours:
{biz.hours}

Services:
{biz.services}

Pricing:
{biz.pricing}

FAQs (may be free text with Q&A pairs):
{biz.faqs}

When you respond, write in a natural, friendly tone and reference the business by name when helpful.
"""

        completion = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
            ],
            max_tokens=300,
            temperature=0.3,
        )

        reply_text = (completion.choices[0].message.content or "").strip() or "Sorry, I couldn't generate a reply."

        ts = datetime.datetime.now().isoformat()
        log_line = f"{ts} | {business_id} | USER: {user_message} | BOT: {reply_text}\n"
        with open(CHAT_LOG_FILE, "a") as logf:
            logf.write(log_line)

        return jsonify({"reply": reply_text})

    except Exception as e:
        print("ERROR in /chat:", repr(e))
        return jsonify({"reply": f"Server error: {e}"}), 500


@app.route("/lead", methods=["POST"])
def lead():
    try:
        data = request.get_json(force=True)
        business_id = (data.get("business_id") or "").strip()
        name = (data.get("name") or "").strip()
        email = (data.get("email") or "").strip()
        phone = (data.get("phone") or "").strip()

        if not business_id or not name or not email:
            return jsonify({"message": "business_id, name, and email are required."}), 400

        lead_entry = {
            "timestamp": datetime.datetime.now().isoformat(),
            "business_id": business_id,
            "name": name,
            "email": email,
            "phone": phone,
        }

        if os.path.exists(LEADS_FILE):
            with open(LEADS_FILE, "r") as f:
                existing = json.load(f)
        else:
            existing = []

        existing.append(lead_entry)

        with open(LEADS_FILE, "w") as f:
            json.dump(existing, f, indent=2)

        print("NEW LEAD:", lead_entry)

        # --- Email notifications ---
        # Look up business + owner so we know who to email
        db = SessionLocal()
        try:
            biz = db.query(Business).filter(Business.business_id == business_id).first()
            owner_user = None
            if biz:
                owner_user = (
                    db.query(User)
                    .filter(User.business_id == business_id, User.role == "business")
                    .first()
                )
        finally:
            db.close()

        business_name = biz.name if biz else business_id
        owner_email = owner_user.email if owner_user else None

        subject = f"New lead for {business_name}"
        body = (
            f"You have a new lead for {business_name}\n\n"
            f"Name: {name}\n"
            f"Email: {email}\n"
            f"Phone: {phone or '-'}\n"
            f"Business ID: {business_id}\n"
            f"Time: {lead_entry['timestamp']}\n"
        )

        # Email business owner (if exists)
        if owner_email:
            send_email(owner_email, subject, body)

        # Optional: email admin copy
        if ADMIN_EMAIL:
            admin_body = body + "\n(This is an admin copy.)"
            send_email(ADMIN_EMAIL, subject, admin_body)

        return jsonify({"message": "Lead saved."})

    except Exception as e:
        print("ERROR in /lead:", repr(e))
        return jsonify({"message": f"Server error: {e}"}), 500


# ---------- Auth pages ----------

LOGIN_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Login · LocalChat AI</title>
  <style>
    :root {
      --bg: #1a1a1a;
      --bg-card: #242424;
      --accent: #007aff;
      --text: #ffffff;
      --text-muted: rgba(255, 255, 255, 0.6);
      --border: rgba(255, 255, 255, 0.08);
      --shadow: 0 2px 8px rgba(0, 0, 0, 0.3);
    }

    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
      -webkit-font-smoothing: antialiased;
      -moz-osx-font-smoothing: grayscale;
    }

    body {
      margin: 0;
      min-height: 100vh;
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 24px;
    }

    .card {
      width: 100%;
      max-width: 400px;
      background: var(--bg-card);
      border-radius: 16px;
      border: 1px solid var(--border);
      box-shadow: var(--shadow);
      padding: 48px 40px;
    }

    .logo {
      width: 48px;
      height: 48px;
      border-radius: 12px;
      background: var(--accent);
      display: flex;
      align-items: center;
      justify-content: center;
      margin: 0 auto 32px;
    }

    .logo::before {
      content: "AI";
      color: white;
      font-weight: 600;
      font-size: 18px;
    }

    h1 {
      margin: 0 0 12px;
      font-size: 28px;
      font-weight: 600;
      text-align: center;
      color: var(--text);
    }

    p {
      margin: 0 0 32px;
      font-size: 14px;
      color: var(--text-muted);
      text-align: center;
      line-height: 1.5;
    }

    label {
      display: block;
      font-size: 13px;
      color: var(--text-muted);
      margin-bottom: 8px;
      font-weight: 500;
    }

    input {
      width: 100%;
      padding: 12px 16px;
      margin-bottom: 20px;
      border-radius: 12px;
      border: 1px solid var(--border);
      background: var(--bg);
      color: var(--text);
      font-size: 15px;
      font-family: inherit;
      box-sizing: border-box;
    }

    input:focus {
      outline: none;
      border-color: var(--accent);
    }

    input::placeholder {
      color: var(--text-muted);
    }

    button {
      width: 100%;
      padding: 14px 0;
      border-radius: 12px;
      border: none;
      background: var(--accent);
      color: white;
      font-size: 15px;
      font-weight: 500;
      cursor: pointer;
      font-family: inherit;
      margin-top: 8px;
    }

    button:hover {
      opacity: 0.9;
    }

    .error {
      margin-top: 16px;
      padding: 12px 16px;
      font-size: 13px;
      color: #ffcccc;
      background: rgba(255, 59, 48, 0.1);
      border: 1px solid rgba(255, 59, 48, 0.2);
      border-radius: 12px;
      text-align: center;
    }

    .hint {
      margin-top: 24px;
      padding-top: 24px;
      border-top: 1px solid var(--border);
      font-size: 12px;
      color: var(--text-muted);
      text-align: center;
      line-height: 1.5;
    }

    a {
      color: var(--accent);
      text-decoration: none;
    }

    a:hover {
      text-decoration: underline;
    }
  </style>
</head>
<body>
  <div class="card">
    <div class="logo"></div>
    <h1>Sign in</h1>
    <p>Use your admin or business owner account to access the dashboard.</p>
    <form method="post">
      <label>Email</label>
      <input type="email" name="email" placeholder="you@example.com" required autocomplete="email" />
      <label>Password</label>
      <input type="password" name="password" placeholder="••••••••" required autocomplete="current-password" />
      <button type="submit">Sign in</button>
    </form>
    {% if error %}
      <div class="error">{{ error }}</div>
    {% endif %}
    <div class="hint">
      Default admin (dev only): admin@localchat.ai / changeme123<br/>
      <a href="/">← Back to chat</a>
    </div>
  </div>
</body>
</html>
"""


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""

        db = SessionLocal()
        try:
            user = db.query(User).filter(User.email == email).first()
        finally:
            db.close()

        if not user or not verify_user_password(user, password):
            error = "Invalid email or password."
        else:
            session["user_id"] = user.id
            session["role"] = user.role
            session["business_id"] = user.business_id
            next_url = request.args.get("next") or url_for("admin" if user.role == "admin" else "business_dashboard")
            return redirect(next_url)

    return render_template_string(LOGIN_HTML, error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/change-password", methods=["GET", "POST"])
@business_required
def change_password():
    user, role, business_id = get_current_user()
    error = None
    success = None

    if request.method == "POST":
        current_pw = request.form.get("current_password") or ""
        new_pw = request.form.get("new_password") or ""
        confirm_pw = request.form.get("confirm_password") or ""

        if not current_pw or not new_pw or not confirm_pw:
            error = "All fields are required."
        elif new_pw != confirm_pw:
            error = "New passwords do not match."
        else:
            db = SessionLocal()
            try:
                db_user = db.query(User).filter(User.id == user.id).first()
                if not db_user or not verify_user_password(db_user, current_pw):
                    error = "Current password is incorrect."
                else:
                    db_user.password_hash = generate_password_hash(new_pw, method="pbkdf2:sha256")
                    db.commit()
                    success = "Password updated successfully."
            finally:
                db.close()

    html = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Change Password</title>
  <style>
    :root {
      --bg: #1a1a1a;
      --bg-card: #242424;
      --accent: #007aff;
      --text: #ffffff;
      --text-muted: rgba(255, 255, 255, 0.6);
      --border: rgba(255, 255, 255, 0.08);
      --shadow: 0 2px 8px rgba(0, 0, 0, 0.3);
    }

    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }

    body {
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
      margin: 0;
      padding: 24px;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
    }

    .card {
      max-width: 440px;
      width: 100%;
      background: var(--bg-card);
      border-radius: 16px;
      border: 1px solid var(--border);
      box-shadow: var(--shadow);
      padding: 40px 32px;
    }

    .top-link {
      margin-bottom: 24px;
      font-size: 13px;
      color: var(--text-muted);
    }

    .top-link a {
      color: var(--accent);
      text-decoration: none;
    }

    .top-link a:hover {
      text-decoration: underline;
    }

    h1 {
      margin: 0 0 32px;
      font-size: 24px;
      font-weight: 600;
      color: var(--text);
    }

    label {
      display: block;
      font-size: 13px;
      color: var(--text-muted);
      margin-bottom: 8px;
      font-weight: 500;
    }

    input {
      width: 100%;
      padding: 12px 16px;
      margin-bottom: 20px;
      border-radius: 12px;
      border: 1px solid var(--border);
      background: var(--bg);
      color: var(--text);
      font-size: 15px;
      font-family: inherit;
      box-sizing: border-box;
    }

    input:focus {
      outline: none;
      border-color: var(--accent);
    }

    button {
      width: 100%;
      padding: 14px 0;
      border-radius: 12px;
      border: none;
      background: var(--accent);
      color: white;
      font-size: 15px;
      font-weight: 500;
      cursor: pointer;
      font-family: inherit;
      margin-top: 8px;
    }

    button:hover {
      opacity: 0.9;
    }

    .error {
      margin-top: 16px;
      padding: 12px 16px;
      font-size: 13px;
      color: #ffcccc;
      background: rgba(255, 59, 48, 0.1);
      border: 1px solid rgba(255, 59, 48, 0.2);
      border-radius: 12px;
      text-align: center;
    }

    .success {
      margin-top: 16px;
      padding: 12px 16px;
      font-size: 13px;
      color: #d1fae5;
      background: rgba(52, 199, 89, 0.1);
      border: 1px solid rgba(52, 199, 89, 0.2);
      border-radius: 12px;
      text-align: center;
    }
  </style>
</head>
<body>
  <div class="card">
    <div class="top-link"><a href="/dashboard">← Back to dashboard</a></div>
    <h1>Change Password</h1>
    <form method="post">
      <label>Current password</label>
      <input type="password" name="current_password" required autocomplete="current-password" />
      <label>New password</label>
      <input type="password" name="new_password" required autocomplete="new-password" />
      <label>Confirm new password</label>
      <input type="password" name="confirm_password" required autocomplete="new-password" />
      <button type="submit">Update password</button>
    </form>
    {% if error %}
      <div class="error">{{ error }}</div>
    {% endif %}
    {% if success %}
      <div class="success">{{ success }}</div>
    {% endif %}
  </div>
</body>
</html>
"""
    return render_template_string(html, error=error, success=success)



# ---------- Admin dashboard ----------


@app.route("/admin")
@admin_required
def admin():
    # Load all leads
    if os.path.exists(LEADS_FILE):
        with open(LEADS_FILE, "r") as f:
            leads = json.load(f)
    else:
        leads = []

    # Load last 100 chat log lines
    chats = []
    if os.path.exists(CHAT_LOG_FILE):
        with open(CHAT_LOG_FILE, "r") as f:
            lines = f.readlines()
            chats = lines[-100:]

    user, role, _ = get_current_user()
    email = user.email if user else "Unknown"

    html = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Admin Dashboard</title>
  <style>
    :root {
      --bg: #1a1a1a;
      --bg-card: #242424;
      --accent: #007aff;
      --text: #ffffff;
      --text-muted: rgba(255, 255, 255, 0.6);
      --border: rgba(255, 255, 255, 0.08);
      --shadow: 0 2px 8px rgba(0, 0, 0, 0.3);
    }

    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
      -webkit-font-smoothing: antialiased;
    }

    body {
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
      margin: 0;
      padding: 40px 32px;
      min-height: 100vh;
      line-height: 1.5;
    }

    .container {
      max-width: 1400px;
      margin: 0 auto;
    }

    .top-bar {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      margin-bottom: 32px;
      padding-bottom: 24px;
      border-bottom: 1px solid var(--border-primary);
      flex-wrap: wrap;
      gap: 16px;
    }

    .top-left h1 {
      margin: 0 0 8px;
      font-size: 32px;
      font-weight: 700;
      letter-spacing: -0.02em;
      background: linear-gradient(135deg, var(--text-primary) 0%, var(--text-secondary) 100%);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      background-clip: text;
    }

    .top-left p {
      font-size: 14px;
      color: var(--text-tertiary);
      margin: 0;
    }

    .top-right {
      font-size: 13px;
      color: var(--text-tertiary);
      display: flex;
      align-items: center;
      gap: 16px;
      flex-wrap: wrap;
    }

    .top-right span {
      padding: 8px 16px;
      background: rgba(15, 22, 41, 0.6);
      border-radius: 12px;
      border: 1px solid var(--border-primary);
    }

    a {
      color: var(--accent-secondary);
      text-decoration: none;
      font-weight: 500;
      padding: 8px 16px;
      border-radius: 12px;
      background: rgba(99, 102, 241, 0.1);
      border: 1px solid rgba(99, 102, 241, 0.2);
      transition: all 0.2s ease;
      display: inline-block;
    }

    a:hover {
      background: rgba(99, 102, 241, 0.2);
      border-color: rgba(99, 102, 241, 0.4);
      transform: translateY(-1px);
    }

    .section {
      margin-bottom: 32px;
      border-radius: 20px;
      padding: 24px;
      background: rgba(15, 22, 41, 0.6);
      backdrop-filter: blur(20px);
      border: 1px solid var(--border-primary);
      box-shadow: var(--shadow-xl);
    }

    .section h2 {
      margin: 0 0 20px;
      font-size: 20px;
      font-weight: 600;
      color: var(--text-primary);
      letter-spacing: -0.01em;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
      overflow-x: auto;
      display: block;
    }

    thead {
      display: table-header-group;
    }

    tbody {
      display: table-row-group;
    }

    tr {
      display: table-row;
    }

    th, td {
      padding: 12px 16px;
      text-align: left;
      border-bottom: 1px solid var(--border);
      vertical-align: middle;
    }

    th {
      background: var(--bg);
      color: var(--text-muted);
      font-weight: 600;
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }

    td {
      color: var(--text);
    }

    tr:hover td {
      background: rgba(0, 122, 255, 0.05);
    }

    tr:last-child td {
      border-bottom: none;
    }

    pre {
      background: var(--bg);
      padding: 20px;
      border-radius: 12px;
      overflow-x: auto;
      font-size: 12px;
      font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
      color: var(--text-muted);
      line-height: 1.6;
      border: 1px solid var(--border);
    }

    pre::-webkit-scrollbar {
      height: 6px;
    }

    pre::-webkit-scrollbar-track {
      background: transparent;
    }

    pre::-webkit-scrollbar-thumb {
      background: var(--border);
      border-radius: 3px;
    }

    .empty-state {
      text-align: center;
      padding: 48px 20px;
      color: var(--text-muted);
      font-size: 14px;
    }

    @media (max-width: 768px) {
      body {
        padding: 20px 16px;
      }

      .top-bar {
        flex-direction: column;
      }

      .section {
        padding: 20px;
        overflow-x: auto;
      }

      table {
        font-size: 12px;
      }

      th, td {
        padding: 8px 12px;
      }
    }
  </style>
</head>
<body>
  <div class="container">
  <div class="top-bar">
      <div class="top-left">
      <h1>Admin Dashboard</h1>
        <p>Global view of all leads and chats</p>
    </div>
    <div class="top-right">
      <span>Signed in as {{ email }} (admin)</span>
      <a href="/admin/businesses">Manage businesses</a>
      <a href="/logout">Log out</a>
    </div>
  </div>

  <div class="section">
    <h2>Leads</h2>
"""
    if not leads:
        html += '<div class="empty-state">No leads yet.</div>'
    else:
        html += '<table><thead><tr><th>Time</th><th>Business ID</th><th>Name</th><th>Email</th><th>Phone</th></tr></thead><tbody>'
        for lead in reversed(leads[-200:]):
            html += (
                "<tr>"
                f"<td>{lead.get('timestamp','')}</td>"
                f"<td>{lead.get('business_id','')}</td>"
                f"<td>{lead.get('name','')}</td>"
                f"<td>{lead.get('email','')}</td>"
                f"<td>{lead.get('phone','')}</td>"
                "</tr>"
            )
        html += "</tbody></table>"

    html += """
  </div>

  <div class="section">
    <h2>Recent Chats (last 100 lines)</h2>
"""
    if not chats:
        html += '<div class="empty-state">No chats logged yet.</div>'
    else:
        html += "<pre>"
        for line in chats:
            html += line.replace("<", "&lt;").replace(">", "&gt;")
        html += "</pre>"

    html += """
    </div>
  </div>
</body>
</html>
"""
    return render_template_string(html, email=email)


@app.route("/admin/businesses", methods=["GET", "POST"])
@admin_required
def admin_businesses():
    db = SessionLocal()
    try:
        if request.method == "POST":
            db_id = (request.form.get("db_id") or "").strip()
            business_id = (request.form.get("business_id") or "").strip()
            name = (request.form.get("name") or "").strip()
            hours = (request.form.get("hours") or "").strip()
            services = (request.form.get("services") or "").strip()
            pricing = (request.form.get("pricing") or "").strip()
            location = (request.form.get("location") or "").strip()
            contact = (request.form.get("contact") or "").strip()
            faqs = (request.form.get("faqs") or "").strip()
            blurb = (request.form.get("blurb") or "").strip()

            if not business_id or not name:
                return redirect(url_for("admin_businesses"))

            if db_id:
                biz = db.query(Business).filter(Business.id == int(db_id)).first()
                if biz:
                    biz.business_id = business_id
                    biz.name = name
                    biz.hours = hours
                    biz.services = services
                    biz.pricing = pricing
                    biz.location = location
                    biz.contact = contact
                    biz.faqs = faqs
                    biz.blurb = blurb
            else:
                biz = Business(
                    business_id=business_id,
                    name=name,
                    hours=hours,
                    services=services,
                    pricing=pricing,
                    location=location,
                    contact=contact,
                    faqs=faqs,
                    blurb=blurb,
                )
                db.add(biz)

            db.commit()
            return redirect(url_for("admin_businesses"))

        edit_id = (request.args.get("edit_id") or "").strip()
        edit_biz = None
        if edit_id:
            edit_biz = db.query(Business).filter(Business.id == int(edit_id)).first()

        all_biz = db.query(Business).order_by(Business.id).all()
        user, _, _ = get_current_user()
        email = user.email if user else "Unknown"

        html = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Manage Businesses</title>
  <style>
    :root {
      --bg: #1a1a1a;
      --bg-card: #242424;
      --accent: #007aff;
      --text: #ffffff;
      --text-muted: rgba(255, 255, 255, 0.6);
      --border: rgba(255, 255, 255, 0.08);
      --shadow: 0 2px 8px rgba(0, 0, 0, 0.3);
    }

    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
      -webkit-font-smoothing: antialiased;
    }

    body {
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
      margin: 0;
      padding: 40px 32px;
      min-height: 100vh;
      line-height: 1.5;
    }

    .container {
      max-width: 1400px;
      margin: 0 auto;
    }

    .top-bar {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      margin-bottom: 32px;
      padding-bottom: 24px;
      border-bottom: 1px solid var(--border-primary);
      flex-wrap: wrap;
      gap: 16px;
    }

    .top-left h1 {
      margin: 0 0 8px;
      font-size: 32px;
      font-weight: 700;
      letter-spacing: -0.02em;
      background: linear-gradient(135deg, var(--text-primary) 0%, var(--text-secondary) 100%);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      background-clip: text;
    }

    .top-left p {
      font-size: 14px;
      color: var(--text-tertiary);
      margin: 0;
    }

    .top-right {
      font-size: 13px;
      color: var(--text-tertiary);
      display: flex;
      align-items: center;
      gap: 16px;
      flex-wrap: wrap;
    }

    .top-right span {
      padding: 8px 16px;
      background: rgba(15, 22, 41, 0.6);
      border-radius: 12px;
      border: 1px solid var(--border-primary);
    }

    a {
      color: var(--accent-secondary);
      text-decoration: none;
      font-weight: 500;
      padding: 8px 16px;
      border-radius: 12px;
      background: rgba(99, 102, 241, 0.1);
      border: 1px solid rgba(99, 102, 241, 0.2);
      transition: all 0.2s ease;
      display: inline-block;
    }

    a:hover {
      background: rgba(99, 102, 241, 0.2);
      border-color: rgba(99, 102, 241, 0.4);
      transform: translateY(-1px);
    }

    .section {
      margin-bottom: 32px;
      border-radius: 20px;
      padding: 24px;
      background: rgba(15, 22, 41, 0.6);
      backdrop-filter: blur(20px);
      border: 1px solid var(--border-primary);
      box-shadow: var(--shadow-xl);
    }

    .section h2 {
      margin: 0 0 20px;
      font-size: 20px;
      font-weight: 600;
      color: var(--text-primary);
      letter-spacing: -0.01em;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }

    th, td {
      padding: 12px 16px;
      text-align: left;
      border-bottom: 1px solid var(--border);
      vertical-align: middle;
    }

    th {
      background: var(--bg);
      color: var(--text-muted);
      font-weight: 600;
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }

    td {
      color: var(--text);
    }

    tr:hover td {
      background: rgba(0, 122, 255, 0.05);
    }

    tr:last-child td {
      border-bottom: none;
    }

    input, textarea {
      width: 100%;
      padding: 12px 16px;
      margin-bottom: 16px;
      border-radius: 12px;
      border: 1px solid var(--border);
      background: var(--bg);
      color: var(--text);
      font-size: 15px;
      font-family: inherit;
      box-sizing: border-box;
    }

    input:focus, textarea:focus {
      outline: none;
      border-color: var(--accent);
    }

    textarea {
      resize: vertical;
      min-height: 100px;
    }

    label {
      display: block;
      font-size: 13px;
      color: var(--text-muted);
      margin-bottom: 8px;
      font-weight: 500;
    }

    button {
      border-radius: 12px;
      border: none;
      padding: 14px 24px;
      background: var(--accent);
      color: white;
      font-size: 15px;
      font-weight: 500;
      cursor: pointer;
      font-family: inherit;
    }

    button:hover {
      opacity: 0.9;
    }

    .snippet-box {
      font-size: 12px;
      padding: 16px;
      border-radius: 12px;
      border: 1px dashed var(--border);
      background: var(--bg);
      margin-top: 16px;
      font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
    }

    .snippet-box strong {
      display: block;
      margin-bottom: 8px;
      color: var(--text-muted);
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }

    code {
      font-size: 12px;
      color: var(--accent);
      word-break: break-all;
    }

    .empty-state {
      text-align: center;
      padding: 48px 20px;
      color: var(--text-muted);
      font-size: 14px;
    }

    @media (max-width: 768px) {
      body {
        padding: 20px 16px;
      }

      .top-bar {
        flex-direction: column;
      }

      .section {
        padding: 20px;
        overflow-x: auto;
      }

      table {
        font-size: 12px;
      }

      th, td {
        padding: 8px 12px;
      }
    }
  </style>
</head>
<body>
  <div class="container">
  <div class="top-bar">
      <div class="top-left">
      <h1>Manage Businesses</h1>
        <p>Create and update businesses and copy their embed code</p>
    </div>
    <div class="top-right">
      <span>Signed in as {{ email }} (admin)</span>
      <a href="/admin">Admin dashboard</a>
      <a href="/logout">Log out</a>
    </div>
  </div>

  <div class="section">
    <h2>Existing Businesses</h2>
"""
        if not all_biz:
            html += '<div class="empty-state">No businesses yet.</div>'
        else:
            html += '<table><thead><tr><th>ID</th><th>Business ID</th><th>Name</th><th>Preview</th><th>Actions</th></tr></thead><tbody>'
            for b in all_biz:
                preview_url = f"/?id={b.business_id}"
                snippet = (
                    f'&lt;script src="{request.host_url.rstrip("/")}/widget.js" '
                    f'data-business-id="{b.business_id}"&gt;&lt;/script&gt;'
                )
                html += (
                    "<tr>"
                    f"<td>{b.id}</td>"
                    f"<td>{b.business_id}</td>"
                    f"<td>{b.name}</td>"
                    f'<td><a href="{preview_url}" target="_blank">Open</a></td>'
                    f'<td><a href="/admin/businesses?edit_id={b.id}">Edit</a></td>'
                    "</tr>"
                    "<tr><td colspan='5'>"
                    "<div class='snippet-box'>"
                    "<strong>Embed snippet:</strong>"
                    f"<code>{snippet}</code>"
                    "</div>"
                    "</td></tr>"
                )
            html += "</tbody></table>"

        html += """
  </div>

  <div class="section">
    <h2>"""
        html += "Edit Business" if edit_biz else "Add New Business"
        html += """</h2>
    <form method="post">
"""
        if edit_biz:
            html += f'<input type="hidden" name="db_id" value="{edit_biz.id}"/>'
            val_bid = edit_biz.business_id or ""
            val_name = edit_biz.name or ""
            val_hours = edit_biz.hours or ""
            val_services = edit_biz.services or ""
            val_pricing = edit_biz.pricing or ""
            val_location = edit_biz.location or ""
            val_contact = edit_biz.contact or ""
            val_faqs = edit_biz.faqs or ""
            val_blurb = edit_biz.blurb or ""
        else:
            val_bid = ""
            val_name = ""
            val_hours = ""
            val_services = ""
            val_pricing = ""
            val_location = ""
            val_contact = ""
            val_faqs = ""
            val_blurb = ""

        html += f"""
      <label>Business ID (short string, used in URL ?id=...)</label>
      <input name="business_id" value="{val_bid}" required />

      <label>Name</label>
      <input name="name" value="{val_name}" required />

      <label>Blurb (short description shown in UI)</label>
      <input name="blurb" value="{val_blurb}" />

      <label>Hours</label>
      <textarea name="hours">{val_hours}</textarea>

      <label>Services</label>
      <textarea name="services">{val_services}</textarea>

      <label>Pricing</label>
      <textarea name="pricing">{val_pricing}</textarea>

      <label>Location</label>
      <input name="location" value="{val_location}" />

      <label>Contact</label>
      <input name="contact" value="{val_contact}" />

      <label>FAQs (free text)</label>
      <textarea name="faqs">{val_faqs}</textarea>

      <button type="submit">Save Business</button>
    </form>
    </div>
  </div>
</body>
</html>
"""
        return render_template_string(html, email=email)
    finally:
        db.close()


# ---------- Business owner dashboard ----------


@app.route("/dashboard", methods=["GET", "POST"])
@business_required
def business_dashboard():
    user, role, business_id = get_current_user()
    db = SessionLocal()
    try:
        biz = None
        if business_id:
            biz = db.query(Business).filter(Business.business_id == business_id).first()
            if request.method == "POST" and biz:
                # Allow the business owner to update their own info
                biz.name = (request.form.get("name") or "").strip()
                biz.blurb = (request.form.get("blurb") or "").strip()
                biz.hours = (request.form.get("hours") or "").strip()
                biz.services = (request.form.get("services") or "").strip()
                biz.pricing = (request.form.get("pricing") or "").strip()
                biz.location = (request.form.get("location") or "").strip()
                biz.contact = (request.form.get("contact") or "").strip()
                biz.faqs = (request.form.get("faqs") or "").strip()
                db.commit()
    finally:
        db.close()

    # Load leads for this business
    if os.path.exists(LEADS_FILE):
        with open(LEADS_FILE, "r") as f:
            leads = json.load(f)
    else:
        leads = []

    my_leads = [l for l in leads if l.get("business_id") == business_id]

    embed_snippet = (
        f'<script src="{request.host_url.rstrip("/")}/widget.js" '
        f'data-business-id="{business_id}"></script>'
        if business_id
        else ""
    )

    html = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Your Business Dashboard</title>
  <style>
    :root {
      --bg: #1a1a1a;
      --bg-card: #242424;
      --accent: #007aff;
      --text: #ffffff;
      --text-muted: rgba(255, 255, 255, 0.6);
      --border: rgba(255, 255, 255, 0.08);
      --shadow: 0 2px 8px rgba(0, 0, 0, 0.3);
    }

    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
      -webkit-font-smoothing: antialiased;
    }

    body {
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
      margin: 0;
      padding: 40px 32px;
      min-height: 100vh;
      line-height: 1.5;
    }

    .container {
      max-width: 1400px;
      margin: 0 auto;
    }

    .top-bar {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      margin-bottom: 32px;
      padding-bottom: 24px;
      border-bottom: 1px solid var(--border-primary);
      flex-wrap: wrap;
      gap: 16px;
    }

    .top-left h1 {
      margin: 0 0 8px;
      font-size: 28px;
      font-weight: 600;
      color: var(--text);
    }

    .top-left p {
      font-size: 14px;
      color: var(--text-muted);
      margin: 0;
    }

    .top-right {
      font-size: 13px;
      color: var(--text-muted);
      display: flex;
      align-items: center;
      gap: 16px;
      flex-wrap: wrap;
    }

    .top-right span {
      padding: 6px 12px;
      background: var(--bg-card);
      border-radius: 8px;
      border: 1px solid var(--border);
    }

    a {
      color: var(--accent);
      text-decoration: none;
      font-weight: 500;
      padding: 8px 16px;
      border-radius: 8px;
      background: rgba(0, 122, 255, 0.1);
      border: 1px solid rgba(0, 122, 255, 0.2);
      display: inline-block;
    }

    a:hover {
      background: rgba(0, 122, 255, 0.15);
    }

    .section {
      margin-bottom: 32px;
      border-radius: 16px;
      padding: 32px;
      background: var(--bg-card);
      border: 1px solid var(--border);
      box-shadow: var(--shadow);
    }

    .section h2 {
      margin: 0 0 24px;
      font-size: 20px;
      font-weight: 600;
      color: var(--text);
    }

    .business-header {
      padding: 20px;
      background: rgba(0, 122, 255, 0.1);
      border: 1px solid rgba(0, 122, 255, 0.2);
      border-radius: 12px;
      margin-bottom: 24px;
    }

    .business-header strong {
      display: block;
      font-size: 18px;
      color: var(--text);
      margin-bottom: 4px;
    }

    .business-header span {
      font-size: 13px;
      color: var(--text-muted);
    }

    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }

    th, td {
      padding: 12px 16px;
      text-align: left;
      border-bottom: 1px solid var(--border);
      vertical-align: middle;
    }

    th {
      background: var(--bg);
      color: var(--text-muted);
      font-weight: 600;
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }

    td {
      color: var(--text);
    }

    tr:hover td {
      background: rgba(0, 122, 255, 0.05);
    }

    tr:last-child td {
      border-bottom: none;
    }

    .snippet-box {
      font-size: 12px;
      padding: 16px;
      border-radius: 12px;
      border: 1px dashed var(--border);
      background: var(--bg);
      margin-top: 16px;
      font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
    }

    .snippet-box strong {
      display: block;
      margin-bottom: 8px;
      color: var(--text-muted);
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      font-family: inherit;
    }

    code {
      font-size: 12px;
      color: var(--accent);
      word-break: break-all;
    }

    input, textarea {
      width: 100%;
      padding: 12px 16px;
      margin-bottom: 16px;
      border-radius: 12px;
      border: 1px solid var(--border);
      background: var(--bg);
      color: var(--text);
      font-size: 15px;
      font-family: inherit;
      box-sizing: border-box;
    }

    input:focus, textarea:focus {
      outline: none;
      border-color: var(--accent);
    }

    textarea {
      resize: vertical;
      min-height: 100px;
    }

    label {
      display: block;
      font-size: 13px;
      color: var(--text-muted);
      margin-bottom: 8px;
      font-weight: 500;
    }

    button {
      border-radius: 12px;
      border: none;
      padding: 14px 24px;
      background: var(--accent);
      color: white;
      font-size: 15px;
      font-weight: 500;
      cursor: pointer;
      font-family: inherit;
    }

    button:hover {
      opacity: 0.9;
    }

    .empty-state {
      text-align: center;
      padding: 48px 20px;
      color: var(--text-muted);
      font-size: 14px;
    }

    @media (max-width: 768px) {
      body {
        padding: 20px 16px;
      }

      .top-bar {
        flex-direction: column;
      }

      .section {
        padding: 20px;
        overflow-x: auto;
      }

      table {
        font-size: 12px;
      }

      th, td {
        padding: 8px 12px;
      }
    }
  </style>
</head>
<body>
  <div class="container">
  <div class="top-bar">
      <div class="top-left">
      <h1>Your Business Dashboard</h1>
        <p>Update your info and view your leads</p>
    </div>
    <div class="top-right">
      <span>Signed in as {{ email }} (business)</span>
      <a href="/change-password">Change password</a>
      <a href="/logout">Log out</a>
    </div>
  </div>

  <div class="section">
    <h2>Business Info</h2>
    {% if biz %}
        <div class="business-header">
          <strong>{{ biz.name }}</strong>
          <span>ID: {{ biz.business_id }}</span>
        </div>
      <form method="post">
        <label>Display Name</label>
        <input name="name" value="{{ biz.name }}" />

        <label>Blurb (short description)</label>
        <input name="blurb" value="{{ biz.blurb or '' }}" />

        <label>Hours</label>
        <textarea name="hours">{{ biz.hours or "" }}</textarea>

        <label>Services</label>
        <textarea name="services">{{ biz.services or "" }}</textarea>

        <label>Pricing</label>
        <textarea name="pricing">{{ biz.pricing or "" }}</textarea>

        <label>Location</label>
        <input name="location" value="{{ biz.location or '' }}" />

        <label>Contact</label>
        <input name="contact" value="{{ biz.contact or '' }}" />

        <label>FAQs</label>
        <textarea name="faqs">{{ biz.faqs or "" }}</textarea>

        <button type="submit">Save Changes</button>
      </form>

      <div class="snippet-box">
          <strong>Embed this on your website:</strong>
        <code>{{ snippet }}</code>
      </div>
    {% else %}
        <div class="empty-state">No business linked to this account yet.</div>
    {% endif %}
  </div>

  <div class="section">
    <h2>Your Leads</h2>
"""
    if not my_leads:
        html += '<div class="empty-state">No leads yet.</div>'
    else:
        html += '<table><thead><tr><th>Time</th><th>Name</th><th>Email</th><th>Phone</th></tr></thead><tbody>'
        for lead in reversed(my_leads[-200:]):
            html += (
                "<tr>"
                f"<td>{lead.get('timestamp','')}</td>"
                f"<td>{lead.get('name','')}</td>"
                f"<td>{lead.get('email','')}</td>"
                f"<td>{lead.get('phone','')}</td>"
                "</tr>"
            )
        html += "</tbody></table>"

    html += """
    </div>
  </div>
</body>
</html>
"""
    return render_template_string(
        html,
        email=user.email,
        biz=biz,
        snippet=embed_snippet,
    )
# ---------- Run ----------
if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
