import os
import json
import datetime
import secrets
import re
from functools import wraps

import requests
from flask import (
    Flask,
    request,
    jsonify,
    render_template_string,
    redirect,
    url_for,
    session,
    Response,
)
from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    String,
    Text,
    DateTime,
    Boolean,
)
from sqlalchemy.orm import sessionmaker, declarative_base
from werkzeug.security import generate_password_hash, check_password_hash


# ============================================================
# Config
# ============================================================

DB_URL = "sqlite:///app.db"
LEADS_FILE = "leads.json"
CHAT_LOG_FILE = "chat_logs.txt"

OPENAI_MODEL = "gpt-4.1-mini"

# Email / Resend
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
EMAIL_FROM_ADDRESS = os.environ.get("EMAIL_FROM_ADDRESS", "no-reply@example.com")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", None)

# Flask secret
FLASK_SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")


# ============================================================
# Database models
# ============================================================

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
    blurb = Column(Text, default="")  # marketing blurb
    booking_url = Column(Text, default="")  # Calendly or similar
    address = Column(Text, default="")
    category = Column(String(100), default="")


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(32), nullable=False, default="business")  # "admin" or "business"
    business_id = Column(String(64), nullable=True)  # links to Business.business_id
    is_active = Column(Boolean, nullable=False, default=False)  # must be approved
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class Lead(Base):
    __tablename__ = "leads"

    id = Column(Integer, primary_key=True)
    business_id = Column(String(64), index=True, nullable=False)
    name = Column(String(255), nullable=False)
    email = Column(String(255), nullable=False)
    phone = Column(String(64), nullable=True)
    message = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    source = Column(String(64), default="widget")


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False)
    token = Column(String(128), unique=True, index=True, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    used_at = Column(DateTime, nullable=True)


engine = create_engine(DB_URL, echo=False, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


# ============================================================
# Email via Resend
# ============================================================

def send_email(to_email: str, subject: str, body: str) -> None:
    """
    Send an email using the Resend HTTP API.
    Falls back to console logging if not configured.
    """
    if not to_email:
        return

    api_key = RESEND_API_KEY
    from_addr = EMAIL_FROM_ADDRESS

    if not api_key or not from_addr:
        print("=== EMAIL (mock) ===")
        print("To:", to_email)
        print("Subject:", subject)
        print("Body:\n", body)
        print("====================")
        return

    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": from_addr,
                "to": [to_email],
                "subject": subject,
                "text": body,
            },
            timeout=15,
        )
        resp.raise_for_status()
        print(f"[EMAIL SENT] to={to_email} subject={subject}")
    except Exception as e:
        print("[EMAIL ERROR]", repr(e))
        print("=== EMAIL (fallback log) ===")
        print("To:", to_email)
        print("Subject:", subject)
        print("Body:\n", body)
        print("============================")


# ============================================================
# DB init / migrations
# ============================================================

def ensure_user_is_active_column():
    """If upgrading existing DB, ensure 'is_active' and 'created_at' exist on users."""
    with engine.connect() as conn:
        res = conn.execute("PRAGMA table_info(users)")
        cols = [row[1] for row in res.fetchall()]
        if "is_active" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN is_active INTEGER DEFAULT 0")
            print("Added is_active column to users.")
        if "created_at" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN created_at TEXT")
            print("Added created_at column to users.")


def init_db():
    Base.metadata.create_all(engine)
    # try to backfill new cols on existing DB
    try:
        ensure_user_is_active_column()
    except Exception as e:
        print("ensure_user_is_active_column error:", repr(e))

    db = SessionLocal()
    try:
        # Seed demo business if none
        if db.query(Business).count() == 0:
            demo = Business(
                business_id="demo",
                name="Demo Business",
                hours="Mon–Fri 9am–5pm",
                services="Demo services (haircuts, fades, etc.)",
                pricing="Haircut $25, Student $20",
                location="123 College Ave",
                contact="(555) 123-4567 · demo@example.com",
                faqs="Q: Do you take walk-ins?\nA: Yes, but appointments get priority.",
                blurb="Campus barber specializing in clean fades and student-friendly pricing.",
                category="barbershop",
            )
            db.add(demo)
            db.commit()
            print("Seeded demo business.")

        # Seed admin if none
        admin = db.query(User).filter(User.role == "admin").first()
        if not admin:
            admin_email = os.environ.get("ADMIN_EMAIL", "admin@localchat.ai")
            admin_password = os.environ.get("ADMIN_PASSWORD", "changeme123")
            admin_user = User(
                email=admin_email.lower(),
                password_hash=generate_password_hash(admin_password, method="pbkdf2:sha256"),
                role="admin",
                business_id=None,
                is_active=True,
            )
            db.add(admin_user)
            db.commit()
            print("Created default admin:")
            print(f"  Email: {admin_email}")
            print(f"  Password: {admin_password}")

        # Make sure all existing admins are active
        admins = db.query(User).filter(User.role == "admin").all()
        for a in admins:
            if a.is_active is None or a.is_active is False:
                a.is_active = True
        db.commit()
    finally:
        db.close()


init_db()


# ============================================================
# Helpers
# ============================================================

def slugify_business_id(name: str) -> str:
    base = re.sub(r"[^a-zA-Z0-9]+", "-", name.strip().lower()).strip("-")
    base = base or "business"
    db = SessionLocal()
    try:
        candidate = base
        i = 1
        while db.query(Business).filter(Business.business_id == candidate).first():
            i += 1
            candidate = f"{base}{i}"
        return candidate
    finally:
        db.close()


def get_business_by_id(business_id: str):
    db = SessionLocal()
    try:
        return db.query(Business).filter(Business.business_id == business_id).first()
    finally:
        db.close()


def append_chat_log(business_id: str, role: str, message: str) -> None:
    line = f"{datetime.datetime.utcnow().isoformat()}Z\t{business_id}\t{role}\t{message}\n"
    try:
        with open(CHAT_LOG_FILE, "a") as f:
            f.write(line)
    except Exception:
        pass


def call_openai_chat(system_prompt: str, user_message: str) -> str:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set")

    payload = {
        "model": OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "temperature": 0.3,
    }

    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"].strip()


def system_prompt_for_business(biz: Business) -> str:
    return f"""
You are a friendly, concise AI assistant for the local business "{biz.name}".

Here is the business information:

Location:
{biz.location}

Address:
{biz.address}

Hours:
{biz.hours}

Services:
{biz.services}

Pricing:
{biz.pricing}

Contact:
{biz.contact}

Booking / scheduling link (if any):
{biz.booking_url}

FAQs:
{biz.faqs}

Short blurb:
{biz.blurb}

Rules:
- Answer ONLY questions that you can infer from this business information.
- If the user asks for pricing, hours, services, or location, answer clearly using the data above.
- If the user asks to book an appointment, clearly provide the booking_link above if it is non-empty.
- If the user asks something not covered by this data, say you are not sure and suggest they contact the business directly.
- DO NOT invent prices, discounts, guarantees, or availability.
- Keep responses under 5 sentences unless the user asks for more detail.
    """.strip()


# ============================================================
# Auth helpers / decorators
# ============================================================

def get_current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == uid).first()
        return user
    finally:
        db.close()


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = get_current_user()
        if not user:
            return redirect(url_for("login", next=request.path))
        if not user.is_active:
            return "Your account is not active yet. Please wait for admin approval.", 403
        return fn(*args, **kwargs)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = get_current_user()
        if not user or user.role != "admin":
            return redirect(url_for("login", next=request.path))
        return fn(*args, **kwargs)
    return wrapper


def business_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = get_current_user()
        if not user or user.role != "business":
            return redirect(url_for("login", next=request.path))
        if not user.is_active:
            return "Your account is not active yet. Please wait for admin approval.", 403
        if not user.business_id:
            return "No business is attached to this account.", 400
        return fn(*args, **kwargs)
    return wrapper


# ============================================================
# Flask app
# ============================================================

app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY


# ============================================================
# Public chat + lead page
# ============================================================

CHAT_PAGE_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <title>{{ biz.name if biz else "LocalChat" }} – AI Chat</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    :root {
      --bg: #020617;
      --bg-card: #020617;
      --bg-chat: #020617;
      --border-subtle: rgba(148,163,184,0.45);
      --accent: #6366f1;
      --accent-strong: #4f46e5;
      --text: #e5e7eb;
      --text-soft: #9ca3af;
      --danger: #f97373;
      --radius-lg: 22px;
      --shadow-strong: 0 26px 80px rgba(15,23,42,0.98);
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
      background:
        radial-gradient(circle at top, rgba(37,99,235,0.32) 0, transparent 55%),
        radial-gradient(circle at bottom right, rgba(236,72,153,0.18) 0, transparent 50%),
        #020617;
      color: var(--text);
      min-height: 100vh;
      display: flex;
      align-items: stretch;
      justify-content: center;
      padding: 18px;
    }
    .shell {
      width: 100%;
      max-width: 1120px;
      display: grid;
      grid-template-columns: minmax(0, 0.9fr) minmax(0, 1.1fr);
      gap: 18px;
      align-items: stretch;
    }
    @media (max-width: 840px) {
      .shell {
        grid-template-columns: minmax(0, 1fr);
      }
    }
    .card {
      background: rgba(15,23,42,0.92);
      border-radius: var(--radius-lg);
      border: 1px solid rgba(148,163,184,0.35);
      box-shadow: var(--shadow-strong);
      padding: 18px 18px 20px;
      backdrop-filter: blur(20px);
    }
    .biz-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 10px;
    }
    .biz-title {
      font-size: 18px;
      font-weight: 600;
    }
    .biz-sub {
      font-size: 13px;
      color: var(--text-soft);
      margin-top: 4px;
    }
    .pill {
      font-size: 11px;
      padding: 4px 10px;
      border-radius: 999px;
      border: 1px solid rgba(148,163,184,0.6);
      color: var(--text-soft);
      background: rgba(15,23,42,0.9);
    }
    .section-label {
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--text-soft);
      margin: 14px 0 6px;
    }
    .grid-two {
      display: grid;
      grid-template-columns: minmax(0,1fr) minmax(0,1fr);
      gap: 12px;
    }
    @media (max-width: 840px) {
      .grid-two { grid-template-columns: minmax(0, 1fr); }
    }
    .info-block {
      font-size: 13px;
      color: var(--text-soft);
      line-height: 1.5;
      white-space: pre-line;
      border-radius: 14px;
      border: 1px solid rgba(148,163,184,0.35);
      padding: 10px 11px;
      background: radial-gradient(circle at top left, rgba(15,23,42,0.9) 0, rgba(15,23,42,1) 55%);
    }
    .chat-shell {
      display: flex;
      flex-direction: column;
      height: 100%;
    }
    .chat-window {
      flex: 1;
      min-height: 260px;
      max-height: 520px;
      border-radius: 16px;
      border: 1px solid rgba(148,163,184,0.45);
      background: radial-gradient(circle at top, rgba(15,23,42,0.9) 0, rgba(15,23,42,1) 55%);
      padding: 10px 10px 12px;
      overflow-y: auto;
      font-size: 13px;
    }
    .msg-row {
      display: flex;
      margin-bottom: 8px;
    }
    .msg-row.user {
      justify-content: flex-end;
    }
    .bubble {
      max-width: 82%;
      padding: 7px 9px;
      border-radius: 14px;
      line-height: 1.4;
      white-space: pre-wrap;
    }
    .bubble.user {
      background: linear-gradient(135deg, #4f46e5, #6366f1);
      color: white;
      border-bottom-right-radius: 4px;
    }
    .bubble.bot {
      background: rgba(15,23,42,0.95);
      color: var(--text);
      border: 1px solid rgba(148,163,184,0.45);
      border-bottom-left-radius: 4px;
    }
    .chat-input-row {
      display: flex;
      gap: 8px;
      margin-top: 10px;
    }
    .chat-input-row textarea {
      flex: 1;
      resize: none;
      min-height: 44px;
      max-height: 88px;
      border-radius: 12px;
      border: 1px solid rgba(148,163,184,0.55);
      padding: 8px 10px;
      background: rgba(15,23,42,0.98);
      color: var(--text);
      font-family: inherit;
      font-size: 13px;
    }
    .chat-input-row textarea:focus {
      outline: none;
      border-color: var(--accent);
    }
    .btn {
      border-radius: 999px;
      padding: 10px 16px;
      font-size: 13px;
      border: none;
      cursor: pointer;
      font-family: inherit;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
    }
    .btn-primary {
      background: linear-gradient(135deg, #4f46e5, #6366f1);
      color: #f9fafb;
    }
    .btn-primary:disabled {
      opacity: 0.6;
      cursor: default;
    }
    .chip {
      font-size: 11px;
      color: var(--text-soft);
      padding: 4px 8px;
      border-radius: 999px;
      border: 1px solid rgba(148,163,184,0.45);
      display: inline-flex;
      align-items: center;
      gap: 5px;
      margin-top: 6px;
    }
    .lead-card {
      margin-top: 12px;
      border-radius: 16px;
      border: 1px solid rgba(148,163,184,0.45);
      padding: 10px 11px 12px;
      background: rgba(15,23,42,0.96);
    }
    .lead-row {
      display: grid;
      grid-template-columns: minmax(0,1fr) minmax(0,1fr);
      gap: 8px;
      margin-top: 6px;
    }
    .lead-row input, .lead-row textarea {
      width: 100%;
      border-radius: 9px;
      border: 1px solid rgba(148,163,184,0.55);
      background: rgba(15,23,42,1);
      color: var(--text);
      font-size: 13px;
      padding: 7px 9px;
      font-family: inherit;
      resize: vertical;
    }
    .lead-row textarea {
      grid-column: 1 / span 2;
      min-height: 60px;
    }
    .lead-row input:focus, .lead-row textarea:focus {
      outline: none;
      border-color: var(--accent);
    }
    .lead-footer {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-top: 8px;
      gap: 8px;
      font-size: 12px;
      color: var(--text-soft);
    }
    .status-ok { color: #22c55e; }
    .status-err { color: var(--danger); }
  </style>
</head>
<body>
  <div class="shell">
    <div class="card">
      {% if biz %}
        <div class="biz-header">
          <div>
            <div class="biz-title">{{ biz.name }}</div>
            <div class="biz-sub">{{ biz.blurb or "Ask anything about our services, pricing, or hours." }}</div>
          </div>
          <div class="pill">Powered by LocalChat</div>
        </div>

        <div class="section-label">Overview</div>
        <div class="info-block">
          <strong>Location:</strong> {{ biz.location or "—" }}{% if biz.address %}<br>{{ biz.address }}{% endif %}<br>
          {% if biz.contact %}<strong>Contact:</strong> {{ biz.contact }}<br>{% endif %}
          {% if biz.booking_url %}<strong>Booking:</strong> {{ biz.booking_url }}{% endif %}
        </div>

        <div class="section-label">Details</div>
        <div class="grid-two">
          <div class="info-block">
            <strong>Hours</strong><br>
            {{ biz.hours or "Not provided yet." }}
          </div>
          <div class="info-block">
            <strong>Services</strong><br>
            {{ biz.services or "Not provided yet." }}
          </div>
        </div>
      {% else %}
        <div class="biz-header">
          <div>
            <div class="biz-title">LocalChat Demo</div>
            <div class="biz-sub">No business found for this link.</div>
          </div>
        </div>
      {% endif %}
    </div>

    <div class="card chat-shell">
      <div class="biz-header" style="margin-bottom: 6px;">
        <div>
          <div class="biz-title">Chat with us</div>
          <div class="biz-sub">Ask about availability, services, pricing, or how to book.</div>
        </div>
      </div>

      <div id="chat-window" class="chat-window">
        <div class="msg-row bot">
          <div class="bubble bot">
            Hi! I'm the AI assistant{% if biz %} for {{ biz.name }}{% endif %}. How can I help you today?
          </div>
        </div>
      </div>

      <div class="chat-input-row">
        <textarea id="chat-input" placeholder="Type your question..."></textarea>
        <button id="chat-send" class="btn btn-primary">Send</button>
      </div>

      <div class="chip">
        <span style="width: 7px; height: 7px; border-radius: 999px; background: #22c55e;"></span>
        Typically replies in under 10 seconds
      </div>

      <div class="lead-card">
        <div style="font-size: 13px; font-weight: 500;">Prefer a human follow-up?</div>
        <div style="font-size: 12px; color: var(--text-soft); margin-top: 2px;">
          Drop your details and the team will reach out.
        </div>

        <div class="lead-row">
          <input id="lead-name" placeholder="Your name" />
          <input id="lead-email" placeholder="Your email" />
          <input id="lead-phone" placeholder="Phone (optional)" />
          <textarea id="lead-message" placeholder="What are you looking for? (optional)"></textarea>
        </div>

        <div class="lead-footer">
          <div id="lead-status"></div>
          <button id="lead-send" class="btn btn-primary" style="padding: 7px 12px; font-size: 12px;">
            Send to team
          </button>
        </div>
      </div>
    </div>
  </div>

<script>
  const businessId = "{{ biz.business_id if biz else '' }}";
  const chatWindow = document.getElementById("chat-window");
  const chatInput = document.getElementById("chat-input");
  const chatSend = document.getElementById("chat-send");

  function appendMessage(role, text) {
    const row = document.createElement("div");
    row.className = "msg-row " + (role === "user" ? "user" : "bot");
    const bubble = document.createElement("div");
    bubble.className = "bubble " + (role === "user" ? "user" : "bot");
    bubble.textContent = text;
    row.appendChild(bubble);
    chatWindow.appendChild(row);
    chatWindow.scrollTop = chatWindow.scrollHeight;
  }

  async function sendChat() {
    const text = chatInput.value.trim();
    if (!text || !businessId) return;
    appendMessage("user", text);
    chatInput.value = "";
    chatInput.focus();
    chatSend.disabled = true;
    const thinkingId = "thinking-" + Date.now();
    appendMessage("bot", "Thinking...");
    const lastBubble = chatWindow.querySelector(".msg-row.bot:last-child .bubble.bot");
    lastBubble.id = thinkingId;

    try {
      const resp = await fetch("/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ business_id: businessId, message: text }),
      });
      const data = await resp.json();
      const reply = data.reply || "Sorry, something went wrong.";
      const thinkingEl = document.getElementById(thinkingId);
      if (thinkingEl) {
        thinkingEl.textContent = reply;
      } else {
        appendMessage("bot", reply);
      }
    } catch (err) {
      const thinkingEl = document.getElementById(thinkingId);
      if (thinkingEl) {
        thinkingEl.textContent = "Server error. Please try again.";
      } else {
        appendMessage("bot", "Server error. Please try again.");
      }
    } finally {
      chatSend.disabled = false;
    }
  }

  chatSend.addEventListener("click", sendChat);
  chatInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendChat();
    }
  });

  // Lead capture
  const leadName = document.getElementById("lead-name");
  const leadEmail = document.getElementById("lead-email");
  const leadPhone = document.getElementById("lead-phone");
  const leadMessage = document.getElementById("lead-message");
  const leadSend = document.getElementById("lead-send");
  const leadStatus = document.getElementById("lead-status");

  async function sendLead() {
    const name = leadName.value.trim();
    const email = leadEmail.value.trim();
    const phone = leadPhone.value.trim();
    const msg = leadMessage.value.trim();

    if (!businessId) {
      leadStatus.textContent = "No business attached to this chat.";
      leadStatus.className = "status-err";
      return;
    }
    if (!name || !email) {
      leadStatus.textContent = "Name and email are required.";
      leadStatus.className = "status-err";
      return;
    }

    leadSend.disabled = true;
    leadStatus.textContent = "Sending...";
    leadStatus.className = "";

    try {
      const resp = await fetch("/lead", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          business_id: businessId,
          name,
          email,
          phone,
          message: msg
        }),
      });
      const data = await resp.json();
      if (resp.ok) {
        leadStatus.textContent = "Sent! The team will follow up.";
        leadStatus.className = "status-ok";
        leadName.value = "";
        leadEmail.value = "";
        leadPhone.value = "";
        leadMessage.value = "";
      } else {
        leadStatus.textContent = data.message || "Error sending. Try again.";
        leadStatus.className = "status-err";
      }
    } catch (err) {
      leadStatus.textContent = "Server error. Please try again.";
      leadStatus.className = "status-err";
    } finally {
      leadSend.disabled = false;
    }
  }

  leadSend.addEventListener("click", sendLead);
</script>

</body>
</html>
"""


@app.route("/")
def index():
    business_id = request.args.get("id", "").strip() or "demo"
    biz = get_business_by_id(business_id)
    return render_template_string(CHAT_PAGE_HTML, biz=biz)


# ============================================================
# Chat + lead API
# ============================================================

@app.route("/chat", methods=["POST"])
def chat():
    try:
        data = request.get_json(force=True)
        business_id = (data.get("business_id") or "").strip()
        message = (data.get("message") or "").strip()
        if not business_id or not message:
            return jsonify({"reply": "Missing business_id or message."}), 400

        biz = get_business_by_id(business_id)
        if not biz:
            return jsonify({"reply": "Business not found for this link."}), 404

        sys_prompt = system_prompt_for_business(biz)
        append_chat_log(business_id, "user", message)
        reply = call_openai_chat(sys_prompt, message)
        append_chat_log(business_id, "assistant", reply)
        return jsonify({"reply": reply})
    except Exception as e:
        print("ERROR in /chat:", repr(e))
        return jsonify({"reply": "Server error: something went wrong talking to the AI."}), 500


@app.route("/lead", methods=["POST"])
def lead():
    try:
        data = request.get_json(force=True)
        business_id = (data.get("business_id") or "").strip()
        name = (data.get("name") or "").strip()
        email = (data.get("email") or "").strip()
        phone = (data.get("phone") or "").strip()
        msg = (data.get("message") or "").strip()

        if not business_id or not name or not email:
            return jsonify({"message": "business_id, name, and email are required."}), 400

        db = SessionLocal()
        try:
            # Save to DB
            new_lead = Lead(
                business_id=business_id,
                name=name,
                email=email,
                phone=phone,
                message=msg,
            )
            db.add(new_lead)
            db.commit()

            # Also append to JSON file for backup
            try:
                existing = []
                if os.path.exists(LEADS_FILE):
                    with open(LEADS_FILE, "r") as f:
                        existing = json.load(f)
                existing.append({
                    "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
                    "business_id": business_id,
                    "name": name,
                    "email": email,
                    "phone": phone,
                    "message": msg,
                })
                with open(LEADS_FILE, "w") as f:
                    json.dump(existing, f, indent=2)
            except Exception as e_file:
                print("Error writing leads file:", repr(e_file))

            # Notify business owner by email
            biz = db.query(Business).filter(Business.business_id == business_id).first()
            owner = db.query(User).filter(
                User.business_id == business_id,
                User.role == "business"
            ).first()

            subject = f"New lead for {biz.name if biz else business_id}"
            body_lines = [
                f"Business: {biz.name if biz else business_id}",
                f"Business ID: {business_id}",
                "",
                "Lead details:",
                f"Name:  {name}",
                f"Email: {email}",
                f"Phone: {phone or '-'}",
                "",
                "Message:",
                msg or "(none)",
                "",
                "Sent via LocalChat AI",
            ]
            body = "\n".join(body_lines)

            if owner and owner.email:
                send_email(owner.email, subject, body)
            if ADMIN_EMAIL:
                send_email(ADMIN_EMAIL, subject, body)

        finally:
            db.close()

        return jsonify({"message": "Lead saved."})
    except Exception as e:
        print("ERROR in /lead:", repr(e))
        return jsonify({"message": "Server error."}), 500


# ============================================================
# Widget JS
# ============================================================

@app.route("/widget.js")
def widget_js():
    """
    Embed script for the floating chat bubble.

    Usage on any site:
      <script src="https://YOUR-APP-URL/widget.js"
              data-business-id="demo"
              data-base-url="https://YOUR-APP-URL"
              async></script>
    """
    script = r"""
(function() {
  var currentScript = document.currentScript;
  var bizId = currentScript.getAttribute("data-business-id") || "demo";
  var baseUrl = currentScript.getAttribute("data-base-url") || window.location.origin;

  function createWidget() {
    var bubbleSize = 56;
    var z = 2147483647;

    var bubble = document.createElement("div");
    bubble.id = "localchat-bubble";
    bubble.style.position = "fixed";
    bubble.style.right = "20px";
    bubble.style.bottom = "20px";
    bubble.style.width = bubbleSize + "px";
    bubble.style.height = bubbleSize + "px";
    bubble.style.borderRadius = "999px";
    bubble.style.background = "linear-gradient(135deg,#4f46e5,#6366f1)";
    bubble.style.boxShadow = "0 16px 30px rgba(15,23,42,0.85)";
    bubble.style.display = "flex";
    bubble.style.alignItems = "center";
    bubble.style.justifyContent = "center";
    bubble.style.color = "#ffffff";
    bubble.style.cursor = "pointer";
    bubble.style.zIndex = z;
    bubble.style.fontFamily = "-apple-system,BlinkMacSystemFont,system-ui,sans-serif";
    bubble.style.fontSize = "26px";
    bubble.style.transition = "transform 0.18s ease-out, box-shadow 0.18s ease-out";
    bubble.style.userSelect = "none";
    bubble.title = "Chat with us";
    bubble.innerHTML = "💬";

    bubble.addEventListener("mouseenter", function() {
      bubble.style.transform = "scale(1.05)";
      bubble.style.boxShadow = "0 20px 40px rgba(15,23,42,0.95)";
    });
    bubble.addEventListener("mouseleave", function() {
      bubble.style.transform = "scale(1.0)";
      bubble.style.boxShadow = "0 16px 30px rgba(15,23,42,0.85)";
    });

    var panel = document.createElement("div");
    panel.id = "localchat-panel";
    panel.style.position = "fixed";
    panel.style.right = "20px";
    panel.style.bottom = (bubbleSize + 18) + "px";
    panel.style.width = "360px";
    panel.style.height = "520px";
    panel.style.maxWidth = "95vw";
    panel.style.maxHeight = "80vh";
    panel.style.borderRadius = "18px";
    panel.style.overflow = "hidden";
    panel.style.background = "#050816";
    panel.style.boxShadow = "0 24px 60px rgba(15,23,42,0.98)";
    panel.style.border = "1px solid rgba(148,163,184,0.45)";
    panel.style.display = "none";
    panel.style.zIndex = z;

    var header = document.createElement("div");
    header.style.height = "44px";
    header.style.display = "flex";
    header.style.alignItems = "center";
    header.style.justifyContent = "space-between";
    header.style.padding = "0 12px";
    header.style.background = "rgba(15,23,42,0.96)";
    header.style.color = "#e5e7eb";
    header.style.fontSize = "13px";
    header.style.borderBottom = "1px solid rgba(148,163,184,0.45)";

    var titleWrap = document.createElement("div");
    titleWrap.style.display = "flex";
    titleWrap.style.alignItems = "center";
    var dot = document.createElement("div");
    dot.style.width = "8px";
    dot.style.height = "8px";
    dot.style.borderRadius = "999px";
    dot.style.background = "#22c55e";
    dot.style.marginRight = "8px";
    var titleText = document.createElement("div");
    titleText.textContent = "Live chat";
    titleText.style.fontWeight = "500";
    titleWrap.appendChild(dot);
    titleWrap.appendChild(titleText);

    var closeBtn = document.createElement("button");
    closeBtn.textContent = "×";
    closeBtn.style.border = "none";
    closeBtn.style.background = "transparent";
    closeBtn.style.color = "#9ca3af";
    closeBtn.style.cursor = "pointer";
    closeBtn.style.fontSize = "20px";
    closeBtn.style.padding = "0";
    closeBtn.style.lineHeight = "1";
    closeBtn.onclick = function() {
      panel.style.display = "none";
    };

    header.appendChild(titleWrap);
    header.appendChild(closeBtn);

    var iframe = document.createElement("iframe");
    iframe.style.width = "100%";
    iframe.style.height = "calc(100% - 44px)";
    iframe.style.border = "none";
    iframe.style.background = "transparent";
    iframe.src = baseUrl + "/?id=" + encodeURIComponent(bizId);

    panel.appendChild(header);
    panel.appendChild(iframe);

    bubble.addEventListener("click", function() {
      panel.style.display = (panel.style.display === "none" || panel.style.display === "") ? "block" : "none";
    });

    function handleResize() {
      if (window.innerWidth <= 600) {
        panel.style.width = "100vw";
        panel.style.height = "75vh";
        panel.style.right = "0";
        panel.style.left = "0";
        panel.style.borderRadius = "18px 18px 0 0";
        panel.style.bottom = (bubbleSize + 12) + "px";
      } else {
        panel.style.width = "360px";
        panel.style.height = "520px";
        panel.style.right = "20px";
        panel.style.left = "auto";
        panel.style.borderRadius = "18px";
        panel.style.bottom = (bubbleSize + 18) + "px";
      }
    }
    window.addEventListener("resize", handleResize);
    handleResize();

    document.body.appendChild(bubble);
    document.body.appendChild(panel);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", createWidget);
  } else {
    createWidget();
  }
})();
"""
    return Response(script, mimetype="application/javascript")


# ============================================================
# Auth pages: login, logout, signup, forgot/reset
# ============================================================

LOGIN_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8" />
  <title>Login · LocalChat</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    body {
      margin: 0;
      min-height: 100vh;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
      background: radial-gradient(circle at top, #1e293b 0, #020617 55%, #000 100%);
      color: #e5e7eb;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 20px;
    }
    .card {
      width: 100%;
      max-width: 380px;
      background: rgba(15,23,42,0.98);
      border-radius: 20px;
      border: 1px solid rgba(148,163,184,0.45);
      padding: 22px 22px 24px;
      box-shadow: 0 30px 80px rgba(15,23,42,1);
    }
    h1 {
      margin: 0 0 4px;
      font-size: 20px;
    }
    .sub {
      font-size: 13px;
      color: #9ca3af;
      margin-bottom: 18px;
    }
    label {
      display: block;
      font-size: 12px;
      color: #9ca3af;
      margin-bottom: 4px;
    }
    input {
      width: 100%;
      border-radius: 10px;
      border: 1px solid rgba(148,163,184,0.55);
      background: #020617;
      color: #e5e7eb;
      font-size: 13px;
      padding: 8px 10px;
      margin-bottom: 10px;
      font-family: inherit;
    }
    input:focus {
      outline: none;
      border-color: #6366f1;
    }
    .btn {
      width: 100%;
      border-radius: 999px;
      border: none;
      padding: 9px 14px;
      font-family: inherit;
      font-size: 14px;
      margin-top: 4px;
      cursor: pointer;
      background: linear-gradient(135deg, #4f46e5, #6366f1);
      color: #f9fafb;
    }
    .small-row {
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-size: 12px;
      margin-top: 12px;
    }
    a {
      color: #818cf8;
      text-decoration: none;
    }
    a:hover {
      text-decoration: underline;
    }
    .msg {
      font-size: 12px;
      margin-bottom: 8px;
    }
    .msg-err { color: #f97373; }
    .msg-ok { color: #22c55e; }
  </style>
</head>
<body>
  <div class="card">
    <h1>Sign in</h1>
    <div class="sub">Access your LocalChat dashboard.</div>

    {% if message %}
      <div class="msg {{ 'msg-err' if error else 'msg-ok' }}">{{ message }}</div>
    {% endif %}

    <form method="post">
      <label>Email</label>
      <input type="email" name="email" required />

      <label>Password</label>
      <input type="password" name="password" required />

      <button class="btn" type="submit">Continue</button>
    </form>

    <div class="small-row">
      <a href="{{ url_for('signup') }}">Create an account</a>
      <a href="{{ url_for('forgot_password') }}">Forgot password?</a>
    </div>
  </div>
</body>
</html>
"""


@app.route("/login", methods=["GET", "POST"])
def login():
    message = None
    error = False
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = (request.form.get("password") or "").strip()
        db = SessionLocal()
        try:
            user = db.query(User).filter(User.email == email).first()
            if not user or not check_password_hash(user.password_hash, password):
                message = "Invalid email or password."
                error = True
            else:
                if not user.is_active:
                    message = "Your account is pending approval. You'll get an email once it's active."
                    error = True
                else:
                    session["user_id"] = user.id
                    next_url = request.args.get("next") or (url_for("dashboard") if user.role == "business" else url_for("admin_businesses"))
                    return redirect(next_url)
        finally:
            db.close()
    return render_template_string(LOGIN_HTML, message=message, error=error)


@app.route("/logout")
def logout():
    session.pop("user_id", None)
    return redirect(url_for("login"))


SIGNUP_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8" />
  <title>Sign up · LocalChat</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    body {
      margin: 0;
      min-height: 100vh;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
      background: radial-gradient(circle at top, #1e293b 0, #020617 55%, #000 100%);
      color: #e5e7eb;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 20px;
    }
    .card {
      width: 100%;
      max-width: 480px;
      background: rgba(15,23,42,0.98);
      border-radius: 20px;
      border: 1px solid rgba(148,163,184,0.45);
      padding: 22px 22px 24px;
      box-shadow: 0 30px 80px rgba(15,23,42,1);
    }
    h1 {
      margin: 0 0 4px;
      font-size: 20px;
    }
    .sub {
      font-size: 13px;
      color: #9ca3af;
      margin-bottom: 18px;
    }
    label {
      display: block;
      font-size: 12px;
      color: #9ca3af;
      margin-bottom: 4px;
    }
    input, textarea, select {
      width: 100%;
      border-radius: 10px;
      border: 1px solid rgba(148,163,184,0.55);
      background: #020617;
      color: #e5e7eb;
      font-size: 13px;
      padding: 8px 10px;
      margin-bottom: 10px;
      font-family: inherit;
      resize: vertical;
    }
    input:focus, textarea:focus, select:focus {
      outline: none;
      border-color: #6366f1;
    }
    .row-two {
      display: grid;
      grid-template-columns: minmax(0,1fr) minmax(0,1fr);
      gap: 10px;
    }
    .btn {
      width: 100%;
      border-radius: 999px;
      border: none;
      padding: 9px 14px;
      font-family: inherit;
      font-size: 14px;
      margin-top: 4px;
      cursor: pointer;
      background: linear-gradient(135deg, #4f46e5, #6366f1);
      color: #f9fafb;
    }
    .foot {
      margin-top: 10px;
      font-size: 12px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    a {
      color: #818cf8;
      text-decoration: none;
    }
    a:hover {
      text-decoration: underline;
    }
    .msg {
      font-size: 12px;
      margin-bottom: 8px;
    }
    .msg-err { color: #f97373; }
    .msg-ok { color: #22c55e; }
  </style>
</head>
<body>
  <div class="card">
    <h1>Create your account</h1>
    <div class="sub">Set up your business and get an AI chat widget in minutes.</div>

    {% if message %}
      <div class="msg {{ 'msg-err' if error else 'msg-ok' }}">{{ message }}</div>
    {% endif %}

    <form method="post">
      <label>Your name</label>
      <input type="text" name="owner_name" required />

      <label>Email</label>
      <input type="email" name="email" required />

      <label>Password</label>
      <input type="password" name="password" required />

      <div class="row-two">
        <div>
          <label>Business name</label>
          <input type="text" name="business_name" required />
        </div>
        <div>
          <label>Category</label>
          <select name="category">
            <option value="">Select…</option>
            <option>Barbershop</option>
            <option>Dentist</option>
            <option>Auto repair</option>
            <option>Restaurant</option>
            <option>Clinic</option>
            <option>Other</option>
          </select>
        </div>
      </div>

      <label>Business phone (optional)</label>
      <input type="text" name="phone" />

      <label>Business address (optional)</label>
      <input type="text" name="address" />

      <label>Booking link (Calendly or similar, optional)</label>
      <input type="url" name="booking_url" />

      <label>Short description (optional)</label>
      <textarea name="blurb" rows="3" placeholder="What should the AI say about your business?"></textarea>

      <button class="btn" type="submit">Sign up</button>
    </form>

    <div class="foot">
      <span>Already have an account?</span>
      <a href="{{ url_for('login') }}">Sign in</a>
    </div>
  </div>
</body>
</html>
"""


@app.route("/signup", methods=["GET", "POST"])
def signup():
    message = None
    error = False
    if request.method == "POST":
        owner_name = (request.form.get("owner_name") or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        password = (request.form.get("password") or "").strip()
        business_name = (request.form.get("business_name") or "").strip()
        category = (request.form.get("category") or "").strip()
        phone = (request.form.get("phone") or "").strip()
        address = (request.form.get("address") or "").strip()
        booking_url = (request.form.get("booking_url") or "").strip()
        blurb = (request.form.get("blurb") or "").strip()

        if not owner_name or not email or not password or not business_name:
            message = "Name, email, password, and business name are required."
            error = True
        else:
            db = SessionLocal()
            try:
                existing = db.query(User).filter(User.email == email).first()
                if existing:
                    message = "An account with this email already exists."
                    error = True
                else:
                    biz_id = slugify_business_id(business_name)
                    biz = Business(
                        business_id=biz_id,
                        name=business_name,
                        location="",
                        contact=phone,
                        address=address,
                        booking_url=booking_url,
                        blurb=blurb,
                        category=category,
                    )
                    db.add(biz)
                    db.commit()

                    user = User(
                        email=email,
                        password_hash=generate_password_hash(password, method="pbkdf2:sha256"),
                        role="business",
                        business_id=biz_id,
                        is_active=False,  # require admin approval
                    )
                    db.add(user)
                    db.commit()

                    message = "Account created! You'll get an email once an admin approves your account."
                    error = False

                    # notify admin of new signup
                    if ADMIN_EMAIL:
                        subject = "New LocalChat business signup"
                        body = (
                            f"Owner: {owner_name}\n"
                            f"Email: {email}\n"
                            f"Business: {business_name}\n"
                            f"Business ID: {biz_id}\n"
                            f"Category: {category}\n"
                            f"Phone: {phone}\n"
                            f"Address: {address}\n"
                            f"Booking URL: {booking_url}\n\n"
                            "Log into the admin panel to review and approve this account."
                        )
                        send_email(ADMIN_EMAIL, subject, body)
            finally:
                db.close()

    return render_template_string(SIGNUP_HTML, message=message, error=error)


FORGOT_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8" />
  <title>Reset password · LocalChat</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    body {
      margin: 0;
      min-height: 100vh;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
      background: radial-gradient(circle at top, #1e293b 0, #020617 55%, #000 100%);
      color: #e5e7eb;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 20px;
    }
    .card {
      width: 100%;
      max-width: 380px;
      background: rgba(15,23,42,0.98);
      border-radius: 20px;
      border: 1px solid rgba(148,163,184,0.45);
      padding: 22px 22px 24px;
      box-shadow: 0 30px 80px rgba(15,23,42,1);
    }
    h1 { margin: 0 0 4px; font-size: 20px; }
    .sub { font-size: 13px; color: #9ca3af; margin-bottom: 18px; }
    label { display: block; font-size: 12px; color: #9ca3af; margin-bottom: 4px; }
    input {
      width: 100%;
      border-radius: 10px;
      border: 1px solid rgba(148,163,184,0.55);
      background: #020617;
      color: #e5e7eb;
      font-size: 13px;
      padding: 8px 10px;
      margin-bottom: 10px;
      font-family: inherit;
    }
    input:focus { outline: none; border-color: #6366f1; }
    .btn {
      width: 100%;
      border-radius: 999px;
      border: none;
      padding: 9px 14px;
      font-family: inherit;
      font-size: 14px;
      margin-top: 4px;
      cursor: pointer;
      background: linear-gradient(135deg, #4f46e5, #6366f1);
      color: #f9fafb;
    }
    .msg { font-size: 12px; margin-bottom: 8px; }
    .msg-err { color: #f97373; }
    .msg-ok { color: #22c55e; }
    a { color: #818cf8; text-decoration: none; }
    a:hover { text-decoration: underline; }
  </style>
</head>
<body>
  <div class="card">
    <h1>Reset password</h1>
    <div class="sub">We'll email you a secure link to set a new password.</div>

    {% if message %}
      <div class="msg {{ 'msg-err' if error else 'msg-ok' }}">{{ message }}</div>
    {% endif %}

    <form method="post">
      <label>Email</label>
      <input type="email" name="email" required />
      <button class="btn" type="submit">Send reset link</button>
    </form>

    <div style="margin-top: 10px; font-size: 12px;">
      <a href="{{ url_for('login') }}">Back to sign in</a>
    </div>
  </div>
</body>
</html>
"""


def create_password_reset_token(db, user_id: int, ttl_minutes: int = 30) -> str:
    token = secrets.token_urlsafe(32)
    expires_at = datetime.datetime.utcnow() + datetime.timedelta(minutes=ttl_minutes)
    prt = PasswordResetToken(
        user_id=user_id,
        token=token,
        expires_at=expires_at,
        used_at=None,
    )
    db.add(prt)
    db.commit()
    return token


def consume_password_reset_token(db, token: str):
    if not token:
        return None
    now = datetime.datetime.utcnow()
    prt = db.query(PasswordResetToken).filter(PasswordResetToken.token == token).first()
    if not prt:
        return None
    if prt.used_at is not None:
        return None
    if prt.expires_at < now:
        return None
    user = db.query(User).filter(User.id == prt.user_id).first()
    if not user:
        return None
    prt.used_at = now
    db.commit()
    return user


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    message = None
    error = False
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        db = SessionLocal()
        try:
            user = db.query(User).filter(User.email == email).first()
            if not user:
                message = "If that email exists, you'll get a reset link shortly."
                error = False
            else:
                token = create_password_reset_token(db, user.id)
                base = request.url_root.rstrip("/")
                reset_url = f"{base}{url_for('reset_password', token=token)}"
                subject = "Reset your LocalChat password"
                body = (
                    "You requested a password reset for your LocalChat account.\n\n"
                    f"Click this link to set a new password:\n{reset_url}\n\n"
                    "If you didn't request this, you can ignore this email."
                )
                send_email(user.email, subject, body)
                message = "If that email exists, you'll get a reset link shortly."
                error = False
        finally:
            db.close()
    return render_template_string(FORGOT_HTML, message=message, error=error)


RESET_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8" />
  <title>Choose a new password · LocalChat</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    body {
      margin: 0;
      min-height: 100vh;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
      background: radial-gradient(circle at top, #1e293b 0, #020617 55%, #000 100%);
      color: #e5e7eb;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 20px;
    }
    .card {
      width: 100%;
      max-width: 380px;
      background: rgba(15,23,42,0.98);
      border-radius: 20px;
      border: 1px solid rgba(148,163,184,0.45);
      padding: 22px 22px 24px;
      box-shadow: 0 30px 80px rgba(15,23,42,1);
    }
    h1 { margin: 0 0 4px; font-size: 20px; }
    .sub { font-size: 13px; color: #9ca3af; margin-bottom: 18px; }
    label { display: block; font-size: 12px; color: #9ca3af; margin-bottom: 4px; }
    input {
      width: 100%;
      border-radius: 10px;
      border: 1px solid rgba(148,163,184,0.55);
      background: #020617;
      color: #e5e7eb;
      font-size: 13px;
      padding: 8px 10px;
      margin-bottom: 10px;
      font-family: inherit;
    }
    input:focus { outline: none; border-color: #6366f1; }
    .btn {
      width: 100%;
      border-radius: 999px;
      border: none;
      padding: 9px 14px;
      font-family: inherit;
      font-size: 14px;
      margin-top: 4px;
      cursor: pointer;
      background: linear-gradient(135deg, #4f46e5, #6366f1);
      color: #f9fafb;
    }
    .msg { font-size: 12px; margin-bottom: 8px; }
    .msg-err { color: #f97373; }
    .msg-ok { color: #22c55e; }
    a { color: #818cf8; text-decoration: none; }
    a:hover { text-decoration: underline; }
  </style>
</head>
<body>
  <div class="card">
    <h1>Set a new password</h1>
    <div class="sub">Choose a strong password you haven't used before.</div>

    {% if message %}
      <div class="msg {{ 'msg-err' if error else 'msg-ok' }}">{{ message }}</div>
    {% endif %}

    {% if allow_form %}
      <form method="post">
        <label>New password</label>
        <input type="password" name="password" required />
        <button class="btn" type="submit">Update password</button>
      </form>
    {% else %}
      <div style="margin-top: 10px; font-size: 12px;">
        <a href="{{ url_for('login') }}">Back to sign in</a>
      </div>
    {% endif %}
  </div>
</body>
</html>
"""


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    message = None
    error = False
    allow_form = True
    db = SessionLocal()
    try:
        user = consume_password_reset_token(db, token)
        if not user:
            message = "That reset link is invalid or has expired."
            error = True
            allow_form = False
        else:
            if request.method == "POST":
                password = (request.form.get("password") or "").strip()
                if not password:
                    message = "Password is required."
                    error = True
                else:
                    user.password_hash = generate_password_hash(password, method="pbkdf2:sha256")
                    db.commit()
                    message = "Password updated. You can now sign in."
                    error = False
                    allow_form = False
    finally:
        db.close()
    return render_template_string(RESET_HTML, message=message, error=error, allow_form=allow_form)


# ============================================================
# Admin: business + owner approval
# ============================================================

ADMIN_BUSINESSES_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8" />
  <title>Admin · Businesses · LocalChat</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    body {
      margin: 0;
      min-height: 100vh;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
      background: #020617;
      color: #e5e7eb;
      padding: 24px;
    }
    .shell {
      max-width: 1100px;
      margin: 0 auto;
    }
    h1 {
      margin: 0 0 8px;
      font-size: 22px;
    }
    .sub {
      font-size: 13px;
      color: #9ca3af;
      margin-bottom: 20px;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }
    th, td {
      padding: 8px 6px;
      border-bottom: 1px solid rgba(148,163,184,0.35);
    }
    th {
      text-align: left;
      color: #9ca3af;
      font-weight: 500;
    }
    tr:hover td {
      background: rgba(15,23,42,0.9);
    }
    .pill {
      font-size: 11px;
      padding: 3px 8px;
      border-radius: 999px;
      border: 1px solid rgba(148,163,184,0.6);
      color: #9ca3af;
      display: inline-block;
    }
    .pill-pending {
      border-color: #f97373;
      color: #fecaca;
    }
    .pill-active {
      border-color: #22c55e;
      color: #bbf7d0;
    }
    .btn {
      border-radius: 999px;
      border: none;
      padding: 5px 10px;
      font-family: inherit;
      font-size: 12px;
      cursor: pointer;
    }
    .btn-approve {
      background: linear-gradient(135deg, #16a34a, #22c55e);
      color: #f9fafb;
    }
    .btn-deactivate {
      background: #111827;
      color: #e5e7eb;
      border: 1px solid rgba(148,163,184,0.5);
    }
    .top-row {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 14px;
    }
    a {
      color: #818cf8;
      text-decoration: none;
      font-size: 13px;
    }
    a:hover { text-decoration: underline; }
    .msg {
      font-size: 13px;
      margin-bottom: 10px;
    }
    .msg-ok { color: #22c55e; }
    .msg-err { color: #f97373; }
  </style>
</head>
<body>
  <div class="shell">
    <div class="top-row">
      <div>
        <h1>Businesses & owners</h1>
        <div class="sub">Approve new owner accounts and see their embed codes.</div>
      </div>
      <div>
        <a href="{{ url_for('dashboard') }}">Go to my dashboard</a> ·
        <a href="{{ url_for('logout') }}">Log out</a>
      </div>
    </div>

    {% if message %}
      <div class="msg {{ 'msg-ok' if not error else 'msg-err' }}">{{ message }}</div>
    {% endif %}

    <table>
      <thead>
        <tr>
          <th>Business</th>
          <th>Business ID</th>
          <th>Owner email</th>
          <th>Status</th>
          <th>Embed snippet</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        {% for row in rows %}
          <tr>
            <td>{{ row.biz.name }}</td>
            <td>{{ row.biz.business_id }}</td>
            <td>{{ row.owner.email if row.owner else "—" }}</td>
            <td>
              {% if row.owner %}
                {% if row.owner.is_active %}
                  <span class="pill pill-active">Active</span>
                {% else %}
                  <span class="pill pill-pending">Pending</span>
                {% endif %}
              {% else %}
                <span class="pill">No owner</span>
              {% endif %}
            </td>
            <td>
              <code style="font-size:11px;">&lt;script src="{{ base_url }}/widget.js" data-business-id="{{ row.biz.business_id }}" data-base-url="{{ base_url }}" async&gt;&lt;/script&gt;</code>
            </td>
            <td>
              {% if row.owner %}
                <form method="post" style="display:inline;">
                  <input type="hidden" name="user_id" value="{{ row.owner.id }}" />
                  {% if not row.owner.is_active %}
                    <button class="btn btn-approve" type="submit" name="action" value="approve">Approve</button>
                  {% else %}
                    <button class="btn btn-deactivate" type="submit" name="action" value="deactivate">Deactivate</button>
                  {% endif %}
                </form>
              {% endif %}
            </td>
          </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
</body>
</html>
"""


@app.route("/admin/businesses", methods=["GET", "POST"])
@admin_required
def admin_businesses():
    message = None
    error = False
    db = SessionLocal()
    try:
        if request.method == "POST":
            action = request.form.get("action")
            user_id = request.form.get("user_id")
            if action and user_id:
                owner = db.query(User).filter(User.id == int(user_id)).first()
                if owner:
                    if action == "approve":
                        owner.is_active = True
                        db.commit()
                        message = f"Approved owner {owner.email}."
                        error = False

                        # email owner
                        base = request.url_root.rstrip("/")
                        dash_url = f"{base}{url_for('dashboard')}"
                        subject = "Your LocalChat account is now active"
                        body = (
                            "Your LocalChat business account has been approved.\n\n"
                            f"You can now sign in here:\n{dash_url}\n\n"
                            "From your dashboard you can copy your chat widget embed code and manage your business info."
                        )
                        send_email(owner.email, subject, body)
                    elif action == "deactivate":
                        owner.is_active = False
                        db.commit()
                        message = f"Deactivated owner {owner.email}."
                        error = False

        businesses = db.query(Business).order_by(Business.name).all()
        rows = []
        for biz in businesses:
            owner = (
                db.query(User)
                .filter(User.business_id == biz.business_id, User.role == "business")
                .first()
            )
            rows.append({"biz": biz, "owner": owner})

        base_url = request.url_root.rstrip("/")
        return render_template_string(
            ADMIN_BUSINESSES_HTML,
            rows=rows,
            base_url=base_url,
            message=message,
            error=error,
        )
    finally:
        db.close()


# ============================================================
# Business owner dashboard
# ============================================================

DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8" />
  <title>Dashboard · LocalChat</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    body {
      margin: 0;
      min-height: 100vh;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
      background: #020617;
      color: #e5e7eb;
      padding: 24px;
    }
    .shell {
      max-width: 1100px;
      margin: 0 auto;
    }
    h1 { margin: 0 0 6px; font-size: 22px; }
    .sub { font-size: 13px; color: #9ca3af; margin-bottom: 18px; }
    .top-row {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 14px;
    }
    a { color: #818cf8; text-decoration: none; font-size: 13px; }
    a:hover { text-decoration: underline; }
    .grid {
      display: grid;
      grid-template-columns: minmax(0,1.2fr) minmax(0,1fr);
      gap: 16px;
    }
    @media (max-width: 900px) {
      .grid { grid-template-columns: minmax(0,1fr); }
    }
    .card {
      background: #020617;
      border-radius: 18px;
      border: 1px solid rgba(148,163,184,0.45);
      padding: 16px 16px 18px;
    }
    h2 {
      margin: 0 0 6px;
      font-size: 15px;
    }
    .section-sub {
      font-size: 12px;
      color: #9ca3af;
      margin-bottom: 10px;
    }
    code {
      font-size: 11px;
      background: #020617;
      padding: 8px 10px;
      border-radius: 10px;
      display: block;
      border: 1px solid rgba(148,163,184,0.45);
      overflow-x: auto;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 12px;
    }
    th, td {
      padding: 6px 4px;
      border-bottom: 1px solid rgba(148,163,184,0.35);
    }
    th {
      text-align: left;
      color: #9ca3af;
      font-weight: 500;
    }
    tr:hover td {
      background: rgba(15,23,42,0.9);
    }
  </style>
</head>
<body>
  <div class="shell">
    <div class="top-row">
      <div>
        <h1>Welcome, {{ user.email }}</h1>
        <div class="sub">Manage your chat widget and see recent leads.</div>
      </div>
      <div>
        <a href="{{ url_for('logout') }}">Log out</a>
      </div>
    </div>

    <div class="grid">
      <div class="card">
        <h2>Your embed code</h2>
        <div class="section-sub">Paste this into your website's HTML where you want the floating chat bubble.</div>
        <code>
&lt;script
  src="{{ base_url }}/widget.js"
  data-business-id="{{ biz.business_id }}"
  data-base-url="{{ base_url }}"
  async&gt;&lt;/script&gt;
        </code>
        <div class="section-sub" style="margin-top: 10px;">
          Business: <strong>{{ biz.name }}</strong> ({{ biz.business_id }}) · Category: {{ biz.category or "—" }}
        </div>
      </div>

      <div class="card">
        <h2>Recent leads</h2>
        <div class="section-sub">Last 10 leads from your widget.</div>
        {% if leads %}
          <table>
            <thead>
              <tr>
                <th>When</th>
                <th>Name</th>
                <th>Email</th>
                <th>Phone</th>
              </tr>
            </thead>
            <tbody>
              {% for lead in leads %}
                <tr>
                  <td>{{ lead.created_at.strftime('%b %d %H:%M') }}</td>
                  <td>{{ lead.name }}</td>
                  <td>{{ lead.email }}</td>
                  <td>{{ lead.phone or '—' }}</td>
                </tr>
              {% endfor %}
            </tbody>
          </table>
        {% else %}
          <div class="section-sub">No leads yet. Once people start chatting and leaving their info, they'll show up here.</div>
        {% endif %}
      </div>
    </div>
  </div>
</body>
</html>
"""


@app.route("/dashboard")
@business_required
def dashboard():
    user = get_current_user()
    db = SessionLocal()
    try:
        biz = db.query(Business).filter(Business.business_id == user.business_id).first()
        leads = (
            db.query(Lead)
            .filter(Lead.business_id == user.business_id)
            .order_by(Lead.created_at.desc())
            .limit(10)
            .all()
        )
    finally:
        db.close()
    base_url = request.url_root.rstrip("/")
    return render_template_string(DASHBOARD_HTML, user=user, biz=biz, leads=leads, base_url=base_url)


# ============================================================
# Run
# ============================================================

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0")
