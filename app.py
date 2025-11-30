import os
import json
import datetime
import secrets
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
)

from werkzeug.security import generate_password_hash, check_password_hash

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

# ----------------- Config & env -----------------

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "").strip()
EMAIL_FROM_ADDRESS = os.getenv("EMAIL_FROM_ADDRESS", "LocalChat <no-reply@example.com>")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "").strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "changeme")
FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "dev-secret-key")

DB_URL = os.getenv("DATABASE_URL", "sqlite:///app3.db")

CHAT_LOG_FILE = "chat_logs.txt"

# ----------------- SQLAlchemy models -----------------

Base = declarative_base()
engine = create_engine(DB_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine)


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
    blurb = Column(Text, default="")
    booking_url = Column(Text, default="")
    address = Column(Text, default="")
    category = Column(Text, default="")


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(32), nullable=False, default="business")  # "admin" or "business"
    business_id = Column(String(64), nullable=True)
    is_active = Column(Boolean, default=True)
    reset_token = Column(String(128), nullable=True)
    reset_expires_at = Column(DateTime, nullable=True)


class Lead(Base):
    __tablename__ = "leads"

    id = Column(Integer, primary_key=True)
    business_id = Column(String(64), index=True, nullable=False)
    name = Column(String(200), default="")
    email = Column(String(255), default="")
    phone = Column(String(100), default="")
    message = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


# ----------------- Flask app -----------------

app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY


# ----------------- Helpers -----------------


def get_db():
    return SessionLocal()


def get_current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None, None, None
    db = get_db()
    try:
        u = db.query(User).filter(User.id == user_id).first()
        if not u:
            return None, None, None
        return u, u.role, u.business_id
    finally:
        db.close()


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        u, role, biz_id = get_current_user()
        if not u:
            return redirect(url_for("login"))
        return f(*args, **kwargs)

    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        u, role, biz_id = get_current_user()
        if not u or role != "admin":
            return redirect(url_for("login"))
        return f(*args, **kwargs)

    return wrapper


def business_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        u, role, biz_id = get_current_user()
        if not u or role != "business":
            return redirect(url_for("login"))
        if not u.is_active:
            return "Your account is pending approval.", 403
        return f(*args, **kwargs)

    return wrapper


def slugify_business_id(name: str) -> str:
    raw = (name or "").strip().lower()
    slug = "".join(c if c.isalnum() else "-" for c in raw)
    slug = "-".join(filter(None, slug.split("-")))
    if not slug:
        slug = "biz"
    base = slug[:32]

    db = get_db()
    try:
        candidate = base
        i = 1
        while db.query(Business).filter(Business.business_id == candidate).first():
            i += 1
            candidate = f"{base}-{i}"
        return candidate
    finally:
        db.close()


def send_email(to_email: str, subject: str, text_body: str):
    if not RESEND_API_KEY or not EMAIL_FROM_ADDRESS:
        print("[EMAIL DISABLED]", subject, "->", to_email)
        print(text_body)
        return
    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "from": EMAIL_FROM_ADDRESS,
                "to": [to_email],
                "subject": subject,
                "text": text_body,
            },
            timeout=15,
        )
        if resp.status_code >= 300:
            print("[EMAIL ERROR]", resp.status_code, resp.text)
    except Exception as e:
        print("[EMAIL EXCEPTION]", repr(e))


def init_db():
    """Create tables and seed default admin + demo business if needed."""
    Base.metadata.create_all(engine)
    db = get_db()
    try:
        # admin user
        admin = db.query(User).filter(User.role == "admin").first()
        if not admin:
            email = ADMIN_EMAIL or "admin@example.com"
            pw = ADMIN_PASSWORD or "changeme"
            admin = User(
                email=email,
                password_hash=generate_password_hash(pw),
                role="admin",
                is_active=True,
            )
            db.add(admin)
            db.commit()
            print(f"[INIT] Created admin user {email} / {pw}")

        # demo business
        demo = db.query(Business).filter(Business.business_id == "demo").first()
        if not demo:
            demo = Business(
                business_id="demo",
                name="Demo Barber Shop",
                hours="Mon–Fri: 9am–6pm\nSat: 10am–4pm\nSun: Closed",
                services="- Haircut\n- Beard trim\n- Fade\n- Kids cut",
                pricing="- Haircut: $30\n- Beard trim: $15\n- Bundle: $40",
                location="123 College Ave, Ithaca, NY",
                contact="(555) 555-1234\nhello@demobarber.com",
                faqs="- Do you take walk-ins?\nYes, but appointments are prioritized.\n\n- Do you cut kids' hair?\nYes, from ages 5 and up.",
                blurb="Clean fades, sharp lines, no awkward small talk.",
                booking_url="https://calendly.com",
                address="123 College Ave, Ithaca, NY",
                category="barbershop",
            )
            db.add(demo)
            db.commit()
    finally:
        db.close()


# ----------------- HTML templates -----------------

LANDING_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <title>NovuChat – Chat widget for local businesses</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    :root { color-scheme: dark; }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
      background: linear-gradient(180deg, #0a0a0f 0%, #020617 100%);
      color: #e5e7eb;
      padding: 0;
    }
    .nav {
      position: fixed;
      top: 0;
      left: 0;
      right: 0;
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 20px 32px;
      background: rgba(2, 6, 23, 0.8);
      backdrop-filter: blur(20px);
      border-bottom: 1px solid rgba(148, 163, 184, 0.1);
      z-index: 100;
    }
    .nav-logo {
      display: flex;
      align-items: center;
      gap: 10px;
      font-size: 18px;
      font-weight: 600;
      color: #e5e7eb;
      text-decoration: none;
      transition: transform 0.3s cubic-bezier(0.4, 0, 0.2, 1);
    }
    .nav-logo:hover {
      transform: translateY(-1px);
    }
    .logo-icon {
      width: 32px;
      height: 32px;
      border-radius: 10px;
      background: linear-gradient(135deg, #4f46e5, #6366f1);
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 18px;
      font-weight: 700;
      color: white;
      box-shadow: 0 4px 12px rgba(79, 70, 229, 0.4);
      animation: logoPulse 3s ease-in-out infinite;
      position: relative;
      overflow: hidden;
    }
    .logo-icon::before {
      content: '';
      position: absolute;
      top: -50%;
      left: -50%;
      width: 200%;
      height: 200%;
      background: linear-gradient(45deg, transparent, rgba(255, 255, 255, 0.1), transparent);
      animation: logoShine 3s ease-in-out infinite;
    }
    @keyframes logoPulse {
      0%, 100% { 
        transform: scale(1); 
        box-shadow: 0 4px 12px rgba(79, 70, 229, 0.4);
      }
      50% { 
        transform: scale(1.05); 
        box-shadow: 0 6px 20px rgba(79, 70, 229, 0.6);
      }
    }
    @keyframes logoShine {
      0% { transform: translateX(-100%) translateY(-100%) rotate(45deg); }
      100% { transform: translateX(100%) translateY(100%) rotate(45deg); }
    }
    .nav-links {
      display: flex;
      gap: 20px;
      align-items: center;
    }
    .nav a {
      color: #9ca3af;
      text-decoration: none;
      font-size: 14px;
      transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
    }
    .nav a:hover { 
      color: #e5e7eb;
      transform: translateY(-1px);
    }
    @keyframes fadeInUp {
      from {
        opacity: 0;
        transform: translateY(30px);
      }
      to {
        opacity: 1;
        transform: translateY(0);
      }
    }
    @keyframes fadeIn {
      from { opacity: 0; }
      to { opacity: 1; }
    }
    @keyframes slideInRight {
      from {
        opacity: 0;
        transform: translateX(30px);
      }
      to {
        opacity: 1;
        transform: translateX(0);
      }
    }
    .container {
      max-width: 1280px;
      margin: 0 auto;
      padding: 120px 32px 80px;
      animation: fadeIn 0.6s ease-out;
    }
    .hero {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 48px;
      align-items: center;
      margin-bottom: 120px;
    }
    .hero-content {
      animation: fadeInUp 0.8s ease-out;
    }
    .demo-chat {
      animation: slideInRight 0.8s ease-out 0.2s both;
    }
    @media (max-width: 968px) {
      .hero { grid-template-columns: 1fr; gap: 40px; }
      .container { padding: 100px 24px 60px; }
    }
    .hero-content h1 {
      font-size: 56px;
      line-height: 1.1;
      margin: 0 0 20px;
      font-weight: 700;
      letter-spacing: -0.02em;
      background: linear-gradient(135deg, #e5e7eb 0%, #9ca3af 100%);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      background-clip: text;
      animation: fadeInUp 1s ease-out 0.2s both;
    }
    @media (max-width: 640px) {
      .hero-content h1 { font-size: 40px; }
    }
    .hero-content .subtitle {
      font-size: 20px;
      color: #9ca3af;
      line-height: 1.6;
      margin-bottom: 32px;
    }
    @media (max-width: 640px) {
      .hero-content .subtitle { font-size: 16px; }
    }
    .cta-row {
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
      margin-bottom: 16px;
    }
    .btn {
      border-radius: 12px;
      border: none;
      padding: 14px 28px;
      font-family: inherit;
      font-size: 15px;
      font-weight: 500;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      text-decoration: none;
      transition: all 0.2s;
    }
    .btn-primary {
      background: linear-gradient(135deg, #4f46e5, #6366f1);
      color: #f9fafb;
      box-shadow: 0 4px 14px rgba(79, 70, 229, 0.4);
      position: relative;
      overflow: hidden;
    }
    .btn-primary::before {
      content: '';
      position: absolute;
      top: 0;
      left: -100%;
      width: 100%;
      height: 100%;
      background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.2), transparent);
      transition: left 0.5s;
    }
    .btn-primary:hover::before {
      left: 100%;
    }
    .btn-primary:hover {
      transform: translateY(-2px);
      box-shadow: 0 8px 24px rgba(79, 70, 229, 0.6);
    }
    .btn-primary:active {
      transform: translateY(0);
    }
    .btn-ghost {
      background: rgba(148, 163, 184, 0.1);
      color: #e5e7eb;
      border: 1px solid rgba(148, 163, 184, 0.2);
    }
    .btn-ghost:hover {
      background: rgba(148, 163, 184, 0.15);
      border-color: rgba(148, 163, 184, 0.3);
    }
    .hero-note {
      font-size: 13px;
      color: #6b7280;
    }
    .demo-chat {
      background: rgba(15, 23, 42, 0.6);
      backdrop-filter: blur(20px);
      border-radius: 20px;
      border: 1px solid rgba(148, 163, 184, 0.2);
      box-shadow: 0 20px 60px rgba(0, 0, 0, 0.5);
      overflow: hidden;
      height: 520px;
      display: flex;
      flex-direction: column;
      transition: transform 0.3s cubic-bezier(0.4, 0, 0.2, 1), box-shadow 0.3s;
    }
    .demo-chat:hover {
      transform: translateY(-4px);
      box-shadow: 0 24px 72px rgba(0, 0, 0, 0.6);
    }
    .chat-header {
      padding: 16px 20px;
      border-bottom: 1px solid rgba(148, 163, 184, 0.1);
      display: flex;
      align-items: center;
      gap: 12px;
      background: rgba(15, 23, 42, 0.4);
    }
    .chat-avatar {
      width: 40px;
      height: 40px;
      border-radius: 12px;
      background: linear-gradient(135deg, #4f46e5, #6366f1);
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 20px;
      flex-shrink: 0;
    }
    .chat-header-info {
      flex: 1;
    }
    .chat-header-name {
      font-size: 15px;
      font-weight: 600;
      margin-bottom: 2px;
    }
    .chat-header-status {
      font-size: 12px;
      color: #22c55e;
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .chat-status-dot {
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: #22c55e;
    }
    .chat-messages {
      flex: 1;
      overflow-y: auto;
      padding: 20px;
      display: flex;
      flex-direction: column;
      gap: 16px;
    }
    .chat-msg {
      display: flex;
      gap: 8px;
      max-width: 85%;
    }
    .chat-msg.user {
      align-self: flex-end;
      flex-direction: row-reverse;
    }
    .chat-bubble {
      padding: 12px 16px;
      border-radius: 16px;
      font-size: 14px;
      line-height: 1.5;
    }
    .chat-msg.assistant .chat-bubble {
      background: rgba(148, 163, 184, 0.1);
      border: 1px solid rgba(148, 163, 184, 0.15);
    }
    .chat-msg.user .chat-bubble {
      background: linear-gradient(135deg, #4f46e5, #6366f1);
      color: #f9fafb;
    }
    .chat-input-area {
      padding: 16px 20px;
      border-top: 1px solid rgba(148, 163, 184, 0.1);
      background: rgba(15, 23, 42, 0.4);
    }
    .chat-input {
      width: 100%;
      padding: 12px 16px;
      border-radius: 12px;
      border: 1px solid rgba(148, 163, 184, 0.2);
      background: rgba(2, 6, 23, 0.6);
      color: #9ca3af;
      font-size: 14px;
      font-family: inherit;
      pointer-events: none;
    }
    .how-it-works {
      margin-bottom: 120px;
    }
    .section-title {
      text-align: center;
      font-size: 42px;
      font-weight: 700;
      margin-bottom: 16px;
      letter-spacing: -0.02em;
    }
    @media (max-width: 640px) {
      .section-title { font-size: 32px; }
    }
    .section-subtitle {
      text-align: center;
      font-size: 18px;
      color: #9ca3af;
      margin-bottom: 64px;
      max-width: 600px;
      margin-left: auto;
      margin-right: auto;
    }
    .steps {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 32px;
      margin-bottom: 80px;
    }
    @media (max-width: 968px) {
      .steps { grid-template-columns: 1fr; gap: 24px; }
    }
    .step {
      text-align: center;
      padding: 32px 24px;
      background: rgba(15, 23, 42, 0.4);
      border-radius: 16px;
      border: 1px solid rgba(148, 163, 184, 0.1);
      transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
      animation: fadeInUp 0.6s ease-out;
    }
    .step:nth-child(1) { animation-delay: 0.1s; }
    .step:nth-child(2) { animation-delay: 0.2s; }
    .step:nth-child(3) { animation-delay: 0.3s; }
    .step:hover {
      transform: translateY(-8px);
      border-color: rgba(148, 163, 184, 0.3);
      box-shadow: 0 12px 32px rgba(0, 0, 0, 0.3);
    }
    .step-number {
      width: 48px;
      height: 48px;
      border-radius: 12px;
      background: linear-gradient(135deg, #4f46e5, #6366f1);
      color: #f9fafb;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 20px;
      font-weight: 600;
      margin: 0 auto 20px;
      box-shadow: 0 4px 12px rgba(79, 70, 229, 0.4);
      transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
      position: relative;
      overflow: hidden;
    }
    .step-number::before {
      content: '';
      position: absolute;
      top: 50%;
      left: 50%;
      width: 0;
      height: 0;
      border-radius: 50%;
      background: rgba(255, 255, 255, 0.3);
      transform: translate(-50%, -50%);
      transition: width 0.6s, height 0.6s;
    }
    .step:hover .step-number {
      transform: scale(1.1) rotate(5deg);
      box-shadow: 0 6px 20px rgba(79, 70, 229, 0.6);
    }
    .step:hover .step-number::before {
      width: 100px;
      height: 100px;
    }
    .step h3 {
      font-size: 20px;
      margin: 0 0 12px;
      font-weight: 600;
    }
    .step p {
      font-size: 15px;
      color: #9ca3af;
      line-height: 1.6;
      margin: 0;
    }
    .flow-visual {
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 24px;
      flex-wrap: wrap;
      padding: 48px;
      background: rgba(15, 23, 42, 0.3);
      border-radius: 20px;
      border: 1px solid rgba(148, 163, 184, 0.1);
    }
    @media (max-width: 768px) {
      .flow-visual { flex-direction: column; }
    }
    .flow-item {
      text-align: center;
      flex: 1;
      min-width: 200px;
      animation: fadeInUp 0.6s ease-out;
    }
    .flow-item:nth-child(1) { animation-delay: 0.1s; }
    .flow-item:nth-child(3) { animation-delay: 0.2s; }
    .flow-item:nth-child(5) { animation-delay: 0.3s; }
    .flow-icon {
      width: 64px;
      height: 64px;
      border-radius: 16px;
      background: linear-gradient(135deg, rgba(79, 70, 229, 0.2), rgba(99, 102, 241, 0.2));
      border: 1px solid rgba(79, 70, 229, 0.3);
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 28px;
      margin: 0 auto 16px;
      transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
    }
    .flow-item:hover .flow-icon {
      transform: scale(1.1) rotate(5deg);
      background: linear-gradient(135deg, rgba(79, 70, 229, 0.3), rgba(99, 102, 241, 0.3));
      box-shadow: 0 8px 24px rgba(79, 70, 229, 0.4);
    }
    .flow-item h4 {
      font-size: 18px;
      margin: 0 0 8px;
      font-weight: 600;
    }
    .flow-item p {
      font-size: 14px;
      color: #9ca3af;
      margin: 0;
    }
    .flow-arrow {
      font-size: 24px;
      color: #6366f1;
      animation: arrowPulse 2s ease-in-out infinite;
    }
    @keyframes arrowPulse {
      0%, 100% { 
        opacity: 0.6;
        transform: translateX(0);
      }
      50% { 
        opacity: 1;
        transform: translateX(4px);
      }
    }
    @media (max-width: 768px) {
      .flow-arrow { transform: rotate(90deg); }
    }
  </style>
</head>
<body>
  <nav class="nav">
    <a href="{{ url_for('index') }}" class="nav-logo">
      <div class="logo-icon">N</div>
      <span>NovuChat</span>
    </a>
    <div class="nav-links">
      <a href="{{ url_for('pricing') }}">Pricing</a>
      <a href="{{ url_for('login') }}">Login</a>
      <a href="{{ url_for('signup') }}" class="btn btn-primary" style="padding: 8px 20px; font-size: 14px;">Sign up</a>
        </div>
  </nav>

  <div class="container">
    <div class="hero">
      <div class="hero-content">
        <h1>Turn website visitors into booked appointments</h1>
        <div class="subtitle">
          NovuChat sits on your site as a floating chat widget, answers common
          questions, and sends warm leads straight to your inbox.
      </div>
        <div class="cta-row">
          <a href="{{ url_for('signup') }}" class="btn btn-primary">Get started free</a>
          <a href="{{ url_for('pricing') }}" class="btn btn-ghost">View plans</a>
        </div>
        <div class="hero-note">No code required. Copy–paste one line of script.</div>
    </div>

      <div class="demo-chat">
        <div class="chat-header">
          <div class="chat-avatar">✂️</div>
          <div class="chat-header-info">
            <div class="chat-header-name">NovuChat Demo</div>
            <div class="chat-header-status">
              <span class="chat-status-dot"></span>
              <span>Online</span>
          </div>
          </div>
        </div>
        <div class="chat-messages" id="demoMessages">
          <div class="chat-msg assistant">
            <div class="chat-bubble">Hey! I'm the assistant for NovuChat Demo. How can I help you today?</div>
            </div>
          </div>
        <div class="chat-input-area">
          <input type="text" class="chat-input" placeholder="Try NovuChat on your site — this is a demo" disabled />
        </div>
        </div>
      </div>

    <div class="how-it-works">
      <h2 class="section-title">How it works</h2>
      <p class="section-subtitle">Get started in three simple steps</p>
      
      <div class="steps">
        <div class="step">
          <div class="step-number">1</div>
          <h3>Copy a script snippet</h3>
          <p>We generate a one-line embed code for your business. No technical knowledge needed.</p>
        </div>
        <div class="step">
          <div class="step-number">2</div>
          <h3>Paste on your site</h3>
          <p>Add the script to your website. The chat widget appears instantly in the corner.</p>
        </div>
        <div class="step">
          <div class="step-number">3</div>
          <h3>Get leads in your inbox</h3>
          <p>Visitors chat, ask questions, and leave their info. You get notified immediately.</p>
        </div>
          </div>

      <div class="flow-visual">
        <div class="flow-item">
          <div class="flow-icon">💬</div>
          <h4>Floating chat widget</h4>
          <p>Appears on your website</p>
          </div>
        <div class="flow-arrow">→</div>
        <div class="flow-item">
          <div class="flow-icon">🤖</div>
          <h4>Answers questions</h4>
          <p>24/7 AI assistant</p>
        </div>
        <div class="flow-arrow">→</div>
        <div class="flow-item">
          <div class="flow-icon">📧</div>
          <h4>Sends leads to inbox</h4>
          <p>Instant notifications</p>
        </div>
      </div>
    </div>
  </div>

  <script>
    (function() {
      const messages = [
        { text: "What are your hours?", user: true, delay: 2000 },
        { text: "We're open Monday through Friday 9am-7pm, Saturday 10am-5pm, and closed Sundays. Walk-ins welcome!", user: false, delay: 3000 },
        { text: "How much for a fade?", user: true, delay: 4000 },
        { text: "A classic fade is $35. We also offer skin fades for $40. Would you like to book an appointment?", user: false, delay: 5000 },
        { text: "Yes, can I book for tomorrow at 2pm?", user: true, delay: 6000 },
        { text: "I'd be happy to help you book! Could you share your name and email so I can send you a confirmation?", user: false, delay: 7000 }
      ];

      const messagesEl = document.getElementById('demoMessages');
      let currentIndex = 0;

      function addMessage(text, isUser) {
        const msgDiv = document.createElement('div');
        msgDiv.className = 'chat-msg ' + (isUser ? 'user' : 'assistant');
        const bubble = document.createElement('div');
        bubble.className = 'chat-bubble';
      bubble.textContent = text;
        msgDiv.appendChild(bubble);
        messagesEl.appendChild(msgDiv);
        messagesEl.scrollTop = messagesEl.scrollHeight;
      }

      function showNextMessage() {
        if (currentIndex >= messages.length) return;
        const msg = messages[currentIndex];
        setTimeout(() => {
          addMessage(msg.text, msg.user);
          currentIndex++;
          if (currentIndex < messages.length) {
            showNextMessage();
          }
        }, msg.delay);
      }

      setTimeout(showNextMessage, 1500);
    })();
  </script>
</body>
</html>
"""


PRICING_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8" />
  <title>Pricing · NovuChat</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
      background: linear-gradient(180deg, #0a0a0f 0%, #020617 100%);
      color: #e5e7eb;
    }
    .top-nav {
      position: fixed;
      top: 0;
      left: 0;
      right: 0;
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 20px 32px;
      background: rgba(2, 6, 23, 0.8);
      backdrop-filter: blur(20px);
      border-bottom: 1px solid rgba(148, 163, 184, 0.1);
      z-index: 100;
    }
    @media (max-width: 640px) {
      .top-nav { padding: 16px 20px; }
    }
    .nav-logo {
      display: flex;
      align-items: center;
      gap: 10px;
      font-size: 18px;
      font-weight: 600;
      color: #e5e7eb;
      text-decoration: none;
      transition: transform 0.3s cubic-bezier(0.4, 0, 0.2, 1);
    }
    .nav-logo:hover {
      transform: translateY(-1px);
    }
    .logo-icon {
      width: 32px;
      height: 32px;
      border-radius: 10px;
      background: linear-gradient(135deg, #4f46e5, #6366f1);
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 18px;
      font-weight: 700;
      color: white;
      box-shadow: 0 4px 12px rgba(79, 70, 229, 0.4);
      animation: logoPulse 3s ease-in-out infinite;
      position: relative;
      overflow: hidden;
    }
    .logo-icon::before {
      content: '';
      position: absolute;
      top: -50%;
      left: -50%;
      width: 200%;
      height: 200%;
      background: linear-gradient(45deg, transparent, rgba(255, 255, 255, 0.1), transparent);
      animation: logoShine 3s ease-in-out infinite;
    }
    @keyframes logoPulse {
      0%, 100% { 
        transform: scale(1); 
        box-shadow: 0 4px 12px rgba(79, 70, 229, 0.4);
      }
      50% { 
        transform: scale(1.05); 
        box-shadow: 0 6px 20px rgba(79, 70, 229, 0.6);
      }
    }
    @keyframes logoShine {
      0% { transform: translateX(-100%) translateY(-100%) rotate(45deg); }
      100% { transform: translateX(100%) translateY(100%) rotate(45deg); }
    }
    .nav-links {
      display: flex;
      gap: 20px;
      align-items: center;
    }
    .nav-links a {
      color: #9ca3af;
      text-decoration: none;
      font-size: 14px;
      transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
    }
    .nav-links a:hover {
      color: #e5e7eb;
      transform: translateY(-1px);
    }
    @keyframes fadeInUp {
      from {
        opacity: 0;
        transform: translateY(30px);
      }
      to {
        opacity: 1;
        transform: translateY(0);
      }
    }
    @keyframes fadeIn {
      from { opacity: 0; }
      to { opacity: 1; }
    }
    .container {
      max-width: 1200px;
      margin: 0 auto;
      padding: 120px 32px 80px;
      animation: fadeIn 0.6s ease-out;
    }
    @media (max-width: 640px) {
      .container { padding: 100px 24px 60px; }
    }
    .page-header {
      text-align: center;
      margin-bottom: 64px;
      animation: fadeInUp 0.8s ease-out;
    }
    .page-header h1 {
      font-size: 48px;
      font-weight: 700;
      margin: 0 0 16px;
      letter-spacing: -0.02em;
      background: linear-gradient(135deg, #e5e7eb 0%, #9ca3af 100%);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      background-clip: text;
    }
    @media (max-width: 640px) {
      .page-header h1 { font-size: 36px; }
    }
    .page-header .sub {
      font-size: 18px;
      color: #9ca3af;
      max-width: 600px;
      margin: 0 auto;
      line-height: 1.6;
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 24px;
    }
    @media (max-width: 968px) {
      .grid { grid-template-columns: 1fr; }
    }
    .card {
      background: rgba(15, 23, 42, 0.4);
      backdrop-filter: blur(20px);
      border-radius: 20px;
      border: 1px solid rgba(148, 163, 184, 0.2);
      padding: 32px 24px;
      transition: all 0.4s cubic-bezier(0.4, 0, 0.2, 1);
      animation: fadeInUp 0.6s ease-out;
      position: relative;
      overflow: hidden;
    }
    .card:nth-child(1) { animation-delay: 0.1s; }
    .card:nth-child(2) { animation-delay: 0.2s; }
    .card:nth-child(3) { animation-delay: 0.3s; }
    .card::before {
      content: '';
      position: absolute;
      top: 0;
      left: -100%;
      width: 100%;
      height: 100%;
      background: linear-gradient(90deg, transparent, rgba(79, 70, 229, 0.05), transparent);
      transition: left 0.6s;
    }
    .card:hover::before {
      left: 100%;
    }
    .card:hover {
      transform: translateY(-8px) scale(1.02);
      border-color: rgba(148, 163, 184, 0.4);
      box-shadow: 0 16px 48px rgba(79, 70, 229, 0.3);
    }
    .plan {
      font-size: 16px;
      font-weight: 600;
      margin-bottom: 8px;
      color: #e5e7eb;
    }
    .price {
      font-size: 36px;
      font-weight: 700;
      margin: 12px 0;
      letter-spacing: -0.02em;
    }
    .price span {
      font-size: 16px;
      color: #9ca3af;
      font-weight: 400;
    }
    ul {
      list-style: none;
      padding: 0;
      margin: 24px 0;
      color: #9ca3af;
      font-size: 14px;
      line-height: 1.8;
    }
    li {
      padding-left: 20px;
      position: relative;
    }
    li::before {
      content: "✓";
      position: absolute;
      left: 0;
      color: #6366f1;
      font-weight: 600;
    }
    .btn {
      width: 100%;
      border-radius: 12px;
      border: none;
      padding: 14px 24px;
      font-family: inherit;
      font-size: 15px;
      font-weight: 500;
      cursor: pointer;
      background: linear-gradient(135deg, #4f46e5, #6366f1);
      color: #f9fafb;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
      margin-top: 8px;
      position: relative;
      overflow: hidden;
    }
    .btn::before {
      content: '';
      position: absolute;
      top: 0;
      left: -100%;
      width: 100%;
      height: 100%;
      background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.2), transparent);
      transition: left 0.5s;
    }
    .btn:hover::before {
      left: 100%;
    }
    .btn:hover {
      transform: translateY(-2px);
      box-shadow: 0 8px 24px rgba(79, 70, 229, 0.6);
    }
    .btn:active {
      transform: translateY(0);
    }
  </style>
</head>
<body>
  <nav class="top-nav">
    <a href="{{ url_for('index') }}" class="nav-logo">
      <div class="logo-icon">N</div>
      <span>NovuChat</span>
    </a>
    <div class="nav-links">
      <a href="{{ url_for('index') }}">Home</a>
      <a href="{{ url_for('login') }}">Login</a>
      <a href="{{ url_for('signup') }}" class="btn" style="padding: 8px 20px; font-size: 14px; margin: 0;">Sign up</a>
    </div>
  </nav>

  <div class="container">
    <div class="page-header">
      <h1>Simple pricing for growing local businesses</h1>
      <div class="sub">Start on the free beta plan. Upgrade later when you're ready.</div>
    </div>
    <div class="grid">
      <div class="card">
        <div class="plan">Starter</div>
        <div class="price">$0 <span>/ beta</span></div>
        <ul>
          <li>1 website</li>
          <li>1 chat widget</li>
          <li>Up to 500 messages / month</li>
          <li>Email lead notifications</li>
        </ul>
        <a href="{{ url_for('signup', plan='starter') }}" class="btn">Choose Starter</a>
      </div>
      <div class="card">
        <div class="plan">Growth</div>
        <div class="price">$29 <span>/ month</span></div>
        <ul>
          <li>3 websites</li>
          <li>Priority AI responses</li>
          <li>Lead export (CSV)</li>
          <li>Basic customization</li>
        </ul>
        <a href="{{ url_for('signup', plan='growth') }}" class="btn">Choose Growth</a>
      </div>
      <div class="card">
        <div class="plan">Scale</div>
        <div class="price">$79 <span>/ month</span></div>
        <ul>
          <li>Unlimited widgets</li>
          <li>Team accounts</li>
          <li>Advanced analytics</li>
          <li>Priority support</li>
        </ul>
        <a href="{{ url_for('signup', plan='scale') }}" class="btn">Choose Scale</a>
      </div>
    </div>
  </div>
</body>
</html>
"""


LOGIN_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8" />
  <title>Login · NovuChat</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
      background: linear-gradient(180deg, #0a0a0f 0%, #020617 100%);
      color: #e5e7eb;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 24px;
    }
    .card {
      width: 100%;
      max-width: 440px;
      background: rgba(15, 23, 42, 0.6);
      backdrop-filter: blur(20px);
      border-radius: 20px;
      border: 1px solid rgba(148, 163, 184, 0.2);
      padding: 40px 32px;
      box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
    }
    @media (max-width: 640px) {
      .card { padding: 32px 24px; }
    }
    .logo {
      text-align: center;
      margin-bottom: 32px;
    }
    .logo a {
      font-size: 24px;
      font-weight: 700;
      color: #e5e7eb;
      text-decoration: none;
    }
    h1 {
      margin: 0 0 8px;
      font-size: 28px;
      font-weight: 700;
      text-align: center;
      letter-spacing: -0.02em;
    }
    .sub {
      font-size: 15px;
      color: #9ca3af;
      margin-bottom: 32px;
      text-align: center;
      line-height: 1.5;
    }
    label {
      display: block;
      font-size: 14px;
      color: #e5e7eb;
      margin-bottom: 8px;
      font-weight: 500;
    }
    input {
      width: 100%;
      border-radius: 12px;
      border: 1px solid rgba(148, 163, 184, 0.2);
      background: rgba(2, 6, 23, 0.6);
      color: #e5e7eb;
      font-size: 15px;
      padding: 12px 16px;
      margin-bottom: 20px;
      font-family: inherit;
      transition: all 0.2s;
    }
    input:focus {
      outline: none;
      border-color: #6366f1;
      box-shadow: 0 0 0 3px rgba(99, 102, 241, 0.1);
    }
    input::placeholder {
      color: #6b7280;
    }
    .btn {
      width: 100%;
      border-radius: 12px;
      border: none;
      padding: 14px 24px;
      font-family: inherit;
      font-size: 15px;
      font-weight: 500;
      margin-top: 8px;
      cursor: pointer;
      background: linear-gradient(135deg, #4f46e5, #6366f1);
      color: #f9fafb;
      transition: all 0.2s;
    }
    .btn:hover {
      transform: translateY(-1px);
      box-shadow: 0 4px 14px rgba(79, 70, 229, 0.4);
    }
    .msg {
      font-size: 14px;
      margin-bottom: 20px;
      padding: 12px 16px;
      border-radius: 12px;
      background: rgba(249, 115, 115, 0.1);
      border: 1px solid rgba(249, 115, 115, 0.2);
      color: #fca5a5;
    }
    .foot {
      margin-top: 24px;
      font-size: 14px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      flex-wrap: wrap;
      text-align: center;
    }
    .foot > * {
      flex: 1;
      min-width: 120px;
    }
    a {
      color: #818cf8;
      text-decoration: none;
      transition: color 0.2s;
    }
    a:hover {
      color: #a5b4fc;
      text-decoration: underline;
    }
  </style>
</head>
<body>
  <div class="card">
    <div class="logo">
      <a href="{{ url_for('index') }}">NovuChat</a>
    </div>
    <h1>Welcome back</h1>
    <div class="sub">Sign in to manage your chat widget and leads</div>

    {% if error %}
      <div class="msg">{{ error }}</div>
    {% endif %}

    <form method="post">
      <label>Email</label>
      <input type="email" name="email" placeholder="you@example.com" required autocomplete="email" />

      <label>Password</label>
      <input type="password" name="password" placeholder="••••••••" required autocomplete="current-password" />

      <button class="btn" type="submit">Log in</button>
    </form>

    <div class="foot">
      <span>Need an account? <a href="{{ url_for('signup') }}">Sign up</a></span>
      <a href="{{ url_for('forgot_password') }}">Forgot password?</a>
    </div>
  </div>
</body>
</html>
"""


SIGNUP_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8" />
  <title>Sign up · NovuChat</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
      background: linear-gradient(180deg, #0a0a0f 0%, #020617 100%);
      color: #e5e7eb;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 24px;
    }
    .card {
      width: 100%;
      max-width: 600px;
      background: rgba(15, 23, 42, 0.6);
      backdrop-filter: blur(20px);
      border-radius: 20px;
      border: 1px solid rgba(148, 163, 184, 0.2);
      padding: 40px 32px;
      box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
    }
    @media (max-width: 640px) {
      .card { padding: 32px 24px; }
    }
    .logo {
      text-align: center;
      margin-bottom: 32px;
    }
    .logo a {
      font-size: 24px;
      font-weight: 700;
      color: #e5e7eb;
      text-decoration: none;
    }
    h1 {
      margin: 0 0 8px;
      font-size: 28px;
      font-weight: 700;
      text-align: center;
      letter-spacing: -0.02em;
    }
    .sub {
      font-size: 15px;
      color: #9ca3af;
      margin-bottom: 32px;
      text-align: center;
      line-height: 1.5;
    }
    label {
      display: block;
      font-size: 14px;
      color: #e5e7eb;
      margin-bottom: 8px;
      font-weight: 500;
    }
    input, textarea, select {
      width: 100%;
      border-radius: 12px;
      border: 1px solid rgba(148, 163, 184, 0.2);
      background: rgba(2, 6, 23, 0.6);
      color: #e5e7eb;
      font-size: 15px;
      padding: 12px 16px;
      margin-bottom: 20px;
      font-family: inherit;
      box-sizing: border-box;
      transition: all 0.2s;
    }
    textarea {
      min-height: 80px;
      resize: vertical;
    }
    input:focus, textarea:focus, select:focus {
      outline: none;
      border-color: #6366f1;
      box-shadow: 0 0 0 3px rgba(99, 102, 241, 0.1);
    }
    input::placeholder, textarea::placeholder {
      color: #6b7280;
    }
    .row {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 16px;
    }
    @media (max-width: 640px) {
      .row { grid-template-columns: 1fr; }
    }
    .btn {
      width: 100%;
      border-radius: 12px;
      border: none;
      padding: 14px 24px;
      font-family: inherit;
      font-size: 15px;
      font-weight: 500;
      margin-top: 8px;
      cursor: pointer;
      background: linear-gradient(135deg, #4f46e5, #6366f1);
      color: #f9fafb;
      transition: all 0.2s;
    }
    .btn:hover {
      transform: translateY(-1px);
      box-shadow: 0 4px 14px rgba(79, 70, 229, 0.4);
    }
    .msg {
      font-size: 14px;
      margin-bottom: 20px;
      padding: 12px 16px;
      border-radius: 12px;
    }
    .msg-error {
      background: rgba(249, 115, 115, 0.1);
      border: 1px solid rgba(249, 115, 115, 0.2);
      color: #fca5a5;
    }
    .msg-ok {
      background: rgba(34, 197, 94, 0.1);
      border: 1px solid rgba(34, 197, 94, 0.2);
      color: #86efac;
    }
    .foot {
      margin-top: 24px;
      display: flex;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
      font-size: 14px;
      text-align: center;
    }
    .foot > * {
      flex: 1;
      min-width: 120px;
    }
    a {
      color: #818cf8;
      text-decoration: none;
      transition: color 0.2s;
    }
    a:hover {
      color: #a5b4fc;
      text-decoration: underline;
    }
  </style>
</head>
<body>
  <div class="card">
    <div class="logo">
      <a href="{{ url_for('index') }}">NovuChat</a>
    </div>
    <h1>Create your account</h1>
    <div class="sub">Set up NovuChat for your business. No card required during beta.</div>

    {% if message %}
      <div class="msg {{ 'msg-error' if error else 'msg-ok' }}">{{ message }}</div>
    {% endif %}

    <form method="post">
      <div class="row">
        <div>
          <label>Your name</label>
          <input type="text" name="owner_name" required />
        </div>
        <div>
          <label>Work email</label>
          <input type="email" name="email" required />
        </div>
      </div>

      <div class="row">
        <div>
          <label>Password</label>
          <input type="password" name="password" required />
        </div>
        <div>
          <label>Plan</label>
          <select name="plan">
            <option value="{{ plan or 'starter' }}">Starter (beta)</option>
            <option value="growth">Growth</option>
            <option value="scale">Scale</option>
          </select>
        </div>
      </div>

      <label>Business name</label>
      <input type="text" name="business_name" required />

      <div class="row">
        <div>
          <label>Phone</label>
          <input type="text" name="phone" />
        </div>
        <div>
          <label>Category (barbershop, dentist, etc.)</label>
          <input type="text" name="category" />
        </div>
      </div>

      <label>Business address</label>
      <input type="text" name="address" />

      <label>Online booking link (Calendly, etc.)</label>
      <input type="url" name="booking_url" placeholder="https://..." />

      <label>One-line blurb</label>
      <textarea name="blurb" placeholder="E.g. Clean fades, sharp lines, no awkward small talk."></textarea>

      <button class="btn" type="submit">Create account</button>
    </form>

    <div class="foot">
      <span>Already have an account? <a href="{{ url_for('login') }}">Log in</a></span>
      <a href="{{ url_for('index') }}">Back to site</a>
    </div>
  </div>
</body>
</html>
"""


FORGOT_PASSWORD_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8" />
  <title>Reset password · NovuChat</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
      background: linear-gradient(180deg, #0a0a0f 0%, #020617 100%);
      color: #e5e7eb;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 24px;
    }
    .card {
      width: 100%;
      max-width: 440px;
      background: rgba(15, 23, 42, 0.6);
      backdrop-filter: blur(20px);
      border-radius: 20px;
      border: 1px solid rgba(148, 163, 184, 0.2);
      padding: 40px 32px;
      box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
    }
    @media (max-width: 640px) {
      .card { padding: 32px 24px; }
    }
    .logo {
      text-align: center;
      margin-bottom: 32px;
    }
    .logo a {
      font-size: 24px;
      font-weight: 700;
      color: #e5e7eb;
      text-decoration: none;
    }
    h1 {
      margin: 0 0 8px;
      font-size: 28px;
      font-weight: 700;
      text-align: center;
      letter-spacing: -0.02em;
    }
    .sub {
      font-size: 15px;
      color: #9ca3af;
      margin-bottom: 32px;
      text-align: center;
      line-height: 1.5;
    }
    label {
      display: block;
      font-size: 14px;
      color: #e5e7eb;
      margin-bottom: 8px;
      font-weight: 500;
    }
    input {
      width: 100%;
      border-radius: 12px;
      border: 1px solid rgba(148, 163, 184, 0.2);
      background: rgba(2, 6, 23, 0.6);
      color: #e5e7eb;
      font-size: 15px;
      padding: 12px 16px;
      margin-bottom: 20px;
      font-family: inherit;
      transition: all 0.2s;
    }
    input:focus {
      outline: none;
      border-color: #6366f1;
      box-shadow: 0 0 0 3px rgba(99, 102, 241, 0.1);
    }
    input::placeholder {
      color: #6b7280;
    }
    .btn {
      width: 100%;
      border-radius: 12px;
      border: none;
      padding: 14px 24px;
      font-family: inherit;
      font-size: 15px;
      font-weight: 500;
      margin-top: 8px;
      cursor: pointer;
      background: linear-gradient(135deg, #4f46e5, #6366f1);
      color: #f9fafb;
      transition: all 0.2s;
    }
    .btn:hover {
      transform: translateY(-1px);
      box-shadow: 0 4px 14px rgba(79, 70, 229, 0.4);
    }
    .msg {
      font-size: 14px;
      margin-bottom: 20px;
      padding: 12px 16px;
      border-radius: 12px;
      background: rgba(34, 197, 94, 0.1);
      border: 1px solid rgba(34, 197, 94, 0.2);
      color: #86efac;
    }
    .back-link {
      margin-top: 24px;
      text-align: center;
      font-size: 14px;
    }
    a {
      color: #818cf8;
      text-decoration: none;
      transition: color 0.2s;
    }
    a:hover {
      color: #a5b4fc;
      text-decoration: underline;
    }
  </style>
</head>
<body>
  <div class="card">
    <div class="logo">
      <a href="{{ url_for('index') }}">NovuChat</a>
    </div>
    <h1>Reset your password</h1>
    <div class="sub">Enter your email and we'll send you a reset link</div>

    {% if message %}
      <div class="msg">{{ message }}</div>
    {% endif %}

    <form method="post">
      <label>Email</label>
      <input type="email" name="email" placeholder="you@example.com" required />
      <button class="btn" type="submit">Send reset link</button>
    </form>
    <div class="back-link">
      <a href="{{ url_for('login') }}">&larr; Back to login</a>
    </div>
  </div>
</body>
</html>
"""


RESET_PASSWORD_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8" />
  <title>Choose new password · NovuChat</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
      background: linear-gradient(180deg, #0a0a0f 0%, #020617 100%);
      color: #e5e7eb;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 24px;
    }
    .card {
      width: 100%;
      max-width: 440px;
      background: rgba(15, 23, 42, 0.6);
      backdrop-filter: blur(20px);
      border-radius: 20px;
      border: 1px solid rgba(148, 163, 184, 0.2);
      padding: 40px 32px;
      box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
    }
    @media (max-width: 640px) {
      .card { padding: 32px 24px; }
    }
    .logo {
      text-align: center;
      margin-bottom: 32px;
    }
    .logo a {
      font-size: 24px;
      font-weight: 700;
      color: #e5e7eb;
      text-decoration: none;
    }
    h1 {
      margin: 0 0 8px;
      font-size: 28px;
      font-weight: 700;
      text-align: center;
      letter-spacing: -0.02em;
    }
    .sub {
      font-size: 15px;
      color: #9ca3af;
      margin-bottom: 32px;
      text-align: center;
      line-height: 1.5;
    }
    label {
      display: block;
      font-size: 14px;
      color: #e5e7eb;
      margin-bottom: 8px;
      font-weight: 500;
    }
    input {
      width: 100%;
      border-radius: 12px;
      border: 1px solid rgba(148, 163, 184, 0.2);
      background: rgba(2, 6, 23, 0.6);
      color: #e5e7eb;
      font-size: 15px;
      padding: 12px 16px;
      margin-bottom: 20px;
      font-family: inherit;
      transition: all 0.2s;
    }
    input:focus {
      outline: none;
      border-color: #6366f1;
      box-shadow: 0 0 0 3px rgba(99, 102, 241, 0.1);
    }
    input::placeholder {
      color: #6b7280;
    }
    .btn {
      width: 100%;
      border-radius: 12px;
      border: none;
      padding: 14px 24px;
      font-family: inherit;
      font-size: 15px;
      font-weight: 500;
      margin-top: 8px;
      cursor: pointer;
      background: linear-gradient(135deg, #4f46e5, #6366f1);
      color: #f9fafb;
      transition: all 0.2s;
    }
    .btn:hover {
      transform: translateY(-1px);
      box-shadow: 0 4px 14px rgba(79, 70, 229, 0.4);
    }
    .msg {
      font-size: 14px;
      margin-bottom: 20px;
      padding: 12px 16px;
      border-radius: 12px;
    }
    .msg-err {
      background: rgba(249, 115, 115, 0.1);
      border: 1px solid rgba(249, 115, 115, 0.2);
      color: #fca5a5;
    }
    .msg-ok {
      background: rgba(34, 197, 94, 0.1);
      border: 1px solid rgba(34, 197, 94, 0.2);
      color: #86efac;
    }
    .invalid-msg {
      font-size: 14px;
      color: #9ca3af;
      text-align: center;
      padding: 16px;
      background: rgba(148, 163, 184, 0.05);
      border-radius: 12px;
      margin-bottom: 20px;
    }
    .back-link {
      margin-top: 24px;
      text-align: center;
      font-size: 14px;
    }
    a {
      color: #818cf8;
      text-decoration: none;
      transition: color 0.2s;
    }
    a:hover {
      color: #a5b4fc;
      text-decoration: underline;
    }
  </style>
</head>
<body>
  <div class="card">
    <div class="logo">
      <a href="{{ url_for('index') }}">NovuChat</a>
    </div>
    <h1>Choose a new password</h1>
    <div class="sub">Enter a new password for your account</div>

    {% if message %}
      <div class="msg {{ 'msg-err' if error else 'msg-ok' }}">{{ message }}</div>
    {% endif %}

    {% if valid %}
    <form method="post">
      <label>New password</label>
        <input type="password" name="password" placeholder="••••••••" required />
        <button class="btn" type="submit">Update password</button>
    </form>
    {% else %}
      <div class="invalid-msg">This reset link is invalid or has expired.</div>
    {% endif %}

    <div class="back-link">
      <a href="{{ url_for('login') }}">&larr; Back to login</a>
    </div>
  </div>
</body>
</html>
"""


DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8" />
  <title>Dashboard · NovuChat</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
      background: linear-gradient(180deg, #0a0a0f 0%, #020617 100%);
      color: #e5e7eb;
    }
    .top-nav {
      background: rgba(2, 6, 23, 0.8);
      backdrop-filter: blur(20px);
      border-bottom: 1px solid rgba(148, 163, 184, 0.1);
      padding: 16px 32px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    @media (max-width: 640px) {
      .top-nav { padding: 16px 20px; }
    }
    .nav-logo {
      display: flex;
      align-items: center;
      gap: 10px;
      font-size: 18px;
      font-weight: 600;
      color: #e5e7eb;
      text-decoration: none;
      transition: transform 0.3s cubic-bezier(0.4, 0, 0.2, 1);
    }
    .nav-logo:hover {
      transform: translateY(-1px);
    }
    .logo-icon {
      width: 32px;
      height: 32px;
      border-radius: 10px;
      background: linear-gradient(135deg, #4f46e5, #6366f1);
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 18px;
      font-weight: 700;
      color: white;
      box-shadow: 0 4px 12px rgba(79, 70, 229, 0.4);
      animation: logoPulse 3s ease-in-out infinite;
      position: relative;
      overflow: hidden;
    }
    .logo-icon::before {
      content: '';
      position: absolute;
      top: -50%;
      left: -50%;
      width: 200%;
      height: 200%;
      background: linear-gradient(45deg, transparent, rgba(255, 255, 255, 0.1), transparent);
      animation: logoShine 3s ease-in-out infinite;
    }
    @keyframes logoPulse {
      0%, 100% { 
        transform: scale(1); 
        box-shadow: 0 4px 12px rgba(79, 70, 229, 0.4);
      }
      50% { 
        transform: scale(1.05); 
        box-shadow: 0 6px 20px rgba(79, 70, 229, 0.6);
      }
    }
    @keyframes logoShine {
      0% { transform: translateX(-100%) translateY(-100%) rotate(45deg); }
      100% { transform: translateX(100%) translateY(100%) rotate(45deg); }
    }
    @keyframes fadeInUp {
      from {
        opacity: 0;
        transform: translateY(30px);
      }
      to {
        opacity: 1;
        transform: translateY(0);
      }
    }
    @keyframes fadeIn {
      from { opacity: 0; }
      to { opacity: 1; }
    }
    .nav-user {
      display: flex;
      align-items: center;
      gap: 16px;
    }
    .nav-user a {
      color: #9ca3af;
      font-size: 14px;
      text-decoration: none;
      transition: color 0.2s;
    }
    .nav-user a:hover {
      color: #e5e7eb;
    }
    @keyframes fadeIn {
      from { opacity: 0; }
      to { opacity: 1; }
    }
    .container {
      max-width: 1080px;
      margin: 0 auto;
      padding: 40px 32px;
      animation: fadeIn 0.6s ease-out;
    }
    @media (max-width: 640px) {
      .container { padding: 32px 20px; }
    }
    .page-header {
      margin-bottom: 32px;
      animation: fadeInUp 0.8s ease-out;
    }
    .page-header h1 {
      font-size: 32px;
      font-weight: 700;
      margin: 0 0 8px;
      letter-spacing: -0.02em;
      background: linear-gradient(135deg, #e5e7eb 0%, #9ca3af 100%);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      background-clip: text;
    }
    .page-header .subtitle {
      font-size: 16px;
      color: #9ca3af;
    }
    .card {
      background: rgba(15, 23, 42, 0.4);
      backdrop-filter: blur(20px);
      border-radius: 16px;
      border: 1px solid rgba(148, 163, 184, 0.2);
      padding: 24px;
      margin-bottom: 24px;
      transition: all 0.4s cubic-bezier(0.4, 0, 0.2, 1);
      animation: fadeInUp 0.6s ease-out;
      position: relative;
      overflow: hidden;
    }
    .card:nth-child(1) { animation-delay: 0.1s; }
    .card:nth-child(2) { animation-delay: 0.2s; }
    .card::before {
      content: '';
      position: absolute;
      top: 0;
      left: -100%;
      width: 100%;
      height: 100%;
      background: linear-gradient(90deg, transparent, rgba(79, 70, 229, 0.05), transparent);
      transition: left 0.6s;
    }
    .card:hover::before {
      left: 100%;
    }
    .card:hover {
      transform: translateY(-4px);
      border-color: rgba(148, 163, 184, 0.3);
      box-shadow: 0 12px 32px rgba(79, 70, 229, 0.2);
    }
    .card-title {
      font-size: 18px;
      font-weight: 600;
      margin: 0 0 16px;
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .card-title::before {
      content: "";
      width: 4px;
      height: 18px;
      background: linear-gradient(135deg, #4f46e5, #6366f1);
      border-radius: 2px;
    }
    .code-block {
      background: rgba(2, 6, 23, 0.6);
      border: 1px solid rgba(148, 163, 184, 0.2);
      border-radius: 12px;
      padding: 20px;
      margin-top: 16px;
      position: relative;
    }
    .code-block code {
      font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
      font-size: 13px;
      color: #e5e7eb;
      white-space: pre;
      overflow-x: auto;
      display: block;
      line-height: 1.6;
    }
    .copy-btn {
      position: absolute;
      top: 16px;
      right: 16px;
      padding: 6px 12px;
      background: rgba(79, 70, 229, 0.2);
      border: 1px solid rgba(79, 70, 229, 0.3);
      border-radius: 8px;
      color: #818cf8;
      font-size: 12px;
      cursor: pointer;
      transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
    }
    .copy-btn:hover {
      background: rgba(79, 70, 229, 0.3);
      border-color: rgba(79, 70, 229, 0.4);
      transform: translateY(-2px);
      box-shadow: 0 4px 12px rgba(79, 70, 229, 0.3);
    }
    .copy-btn:active {
      transform: translateY(0);
    }
    .card p {
      color: #9ca3af;
      font-size: 14px;
      line-height: 1.6;
      margin: 0 0 16px;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 14px;
    }
    thead {
      border-bottom: 1px solid rgba(148, 163, 184, 0.2);
    }
    th {
      text-align: left;
      padding: 12px 16px;
      font-weight: 600;
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: #9ca3af;
    }
    td {
      padding: 16px;
      border-bottom: 1px solid rgba(148, 163, 184, 0.1);
      color: #e5e7eb;
    }
    tbody tr:hover {
      background: rgba(148, 163, 184, 0.05);
    }
    tbody tr:last-child td {
      border-bottom: none;
    }
    .empty-state {
      text-align: center;
      padding: 48px 24px;
      color: #9ca3af;
    }
    .empty-state p {
      margin: 0;
      font-size: 15px;
    }
  </style>
</head>
<body>
  <nav class="top-nav">
    <a href="{{ url_for('index') }}" class="nav-logo">
      <div class="logo-icon">N</div>
      <span>NovuChat</span>
    </a>
    <div class="nav-user">
      <a href="{{ url_for('logout') }}">Logout</a>
    </div>
  </nav>

  <div class="container">
    <div class="page-header">
      <h1>{{ biz.name }}</h1>
      <div class="subtitle">Manage your widget and view recent leads</div>
  </div>

    <div class="card">
      <h2 class="card-title">Widget embed code</h2>
      <p>Paste this right before <code>&lt;/body&gt;</code> on your website.</p>
      <div class="code-block">
        <button class="copy-btn" onclick="copyCode()">Copy</button>
        <code id="embedCode">&lt;iframe
  src="{{ public_url }}?id={{ biz.business_id }}"
  style="width:100%;max-width:420px;height:520px;border:none;border-radius:18px;box-shadow:0 20px 60px rgba(15,23,42,0.9);"
  loading="lazy"
&gt;&lt;/iframe&gt;</code>
      </div>
  </div>

    <div class="card">
      <h2 class="card-title">Recent leads</h2>
      {% if leads %}
        <table>
          <thead>
            <tr>
              <th>When</th>
              <th>Name</th>
              <th>Email</th>
              <th>Message</th>
            </tr>
          </thead>
          <tbody>
            {% for l in leads %}
              <tr>
                <td>{{ l.created_at.strftime("%Y-%m-%d %H:%M") }}</td>
                <td>{{ l.name or "—" }}</td>
                <td>{{ l.email or "—" }}</td>
                <td>{{ (l.message or "")[:80] }}{% if l.message and l.message|length > 80 %}…{% endif %}</td>
              </tr>
            {% endfor %}
          </tbody>
        </table>
      {% else %}
        <div class="empty-state">
          <p>No leads yet. Once visitors start chatting and leaving their info, they'll appear here.</p>
  </div>
      {% endif %}
    </div>
  </div>

  <script>
    function copyCode() {
      const code = document.getElementById('embedCode').textContent;
      navigator.clipboard.writeText(code).then(() => {
        const btn = document.querySelector('.copy-btn');
        const original = btn.textContent;
        btn.textContent = 'Copied!';
        setTimeout(() => {
          btn.textContent = original;
        }, 2000);
      });
    }
  </script>
</body>
</html>
"""


CHAT_PAGE_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8" />
  <title>{{ biz.name }} · Chat</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
      background: linear-gradient(180deg, #0a0a0f 0%, #020617 100%);
      color: #e5e7eb;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 16px;
    }
    .shell {
      width: 100%;
      max-width: 420px;
      background: rgba(15, 23, 42, 0.6);
      backdrop-filter: blur(20px);
      border-radius: 20px;
      border: 1px solid rgba(148, 163, 184, 0.2);
      padding: 0;
      box-shadow: 0 20px 60px rgba(0, 0, 0, 0.5);
      display: flex;
      flex-direction: column;
      height: 540px;
      overflow: hidden;
    }
    .header {
      padding: 16px 20px;
      border-bottom: 1px solid rgba(148, 163, 184, 0.1);
      display: flex;
      align-items: center;
      gap: 12px;
      background: rgba(15, 23, 42, 0.4);
    }
    .avatar {
      width: 40px;
      height: 40px;
      border-radius: 12px;
      background: linear-gradient(135deg, #4f46e5, #6366f1);
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 20px;
      flex-shrink: 0;
    }
    .header-info {
      flex: 1;
    }
    .title {
      font-size: 15px;
      font-weight: 600;
      margin-bottom: 2px;
    }
    .subtitle {
      font-size: 12px;
      color: #9ca3af;
    }
    .messages {
      flex: 1;
      overflow-y: auto;
      padding: 20px;
      display: flex;
      flex-direction: column;
      gap: 16px;
      font-size: 14px;
    }
    .messages::-webkit-scrollbar {
      width: 4px;
    }
    .messages::-webkit-scrollbar-thumb {
      background: rgba(148, 163, 184, 0.2);
      border-radius: 2px;
    }
    .msg {
      display: flex;
      gap: 8px;
      max-width: 85%;
    }
    .msg.me {
      align-self: flex-end;
      flex-direction: row-reverse;
    }
    .bubble {
      padding: 12px 16px;
      border-radius: 16px;
      line-height: 1.5;
      word-wrap: break-word;
    }
    .msg:not(.me) .bubble {
      background: rgba(148, 163, 184, 0.1);
      border: 1px solid rgba(148, 163, 184, 0.15);
    }
    .msg.me .bubble {
      background: linear-gradient(135deg, #4f46e5, #6366f1);
      color: #f9fafb;
    }
    .input-row {
      border-top: 1px solid rgba(148, 163, 184, 0.1);
      padding: 16px 20px;
      display: flex;
      gap: 12px;
      background: rgba(15, 23, 42, 0.4);
    }
    input {
      flex: 1;
      border-radius: 12px;
      border: 1px solid rgba(148, 163, 184, 0.2);
      background: rgba(2, 6, 23, 0.6);
      color: #e5e7eb;
      font-size: 14px;
      padding: 12px 16px;
      font-family: inherit;
      transition: all 0.2s;
    }
    input:focus {
      outline: none;
      border-color: #6366f1;
      box-shadow: 0 0 0 3px rgba(99, 102, 241, 0.1);
    }
    input::placeholder {
      color: #6b7280;
    }
    button {
      border-radius: 12px;
      border: none;
      padding: 12px 20px;
      font-size: 14px;
      font-weight: 500;
      font-family: inherit;
      cursor: pointer;
      background: linear-gradient(135deg, #4f46e5, #6366f1);
      color: #f9fafb;
      transition: all 0.2s;
    }
    button:hover {
      transform: translateY(-1px);
      box-shadow: 0 4px 12px rgba(79, 70, 229, 0.4);
    }
    button:active {
      transform: translateY(0);
    }
  </style>
</head>
<body>
  <div class="shell">
    <div class="header">
      <div class="avatar">✂️</div>
      <div class="header-info">
        <div class="title">{{ biz.name }}</div>
        <div class="subtitle">Ask a question about hours, services, or booking</div>
    </div>
    </div>
    <div class="messages" id="messages">
      <div class="msg">
        <div class="bubble">Hey! I'm the assistant for {{ biz.name }}. How can I help?</div>
  </div>
    </div>
    <div class="input-row">
      <input id="input" type="text" placeholder="Ask something…" />
      <button id="send">Send</button>
    </div>
  </div>
  <script>
    const bizId = "{{ biz.business_id }}";
    const messagesEl = document.getElementById("messages");
    const inputEl = document.getElementById("input");
    const sendBtn = document.getElementById("send");

    function addMessage(text, me=false) {
      const wrapper = document.createElement("div");
      wrapper.className = "msg" + (me ? " me" : "");
      const bubble = document.createElement("div");
      bubble.className = "bubble";
      bubble.textContent = text;
      wrapper.appendChild(bubble);
      messagesEl.appendChild(wrapper);
      messagesEl.scrollTop = messagesEl.scrollHeight;
    }

    async function send() {
      const text = inputEl.value.trim();
      if (!text) return;
      addMessage(text, true);
      inputEl.value = "";
      sendBtn.disabled = true;
      sendBtn.textContent = "Sending...";
      try {
        const resp = await fetch("/chat", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({ business_id: bizId, message: text })
        });
        const data = await resp.json();
        addMessage(data.reply || "Sorry, something went wrong.");
      } catch (e) {
        addMessage("Sorry, something went wrong.");
      } finally {
        sendBtn.disabled = false;
        sendBtn.textContent = "Send";
      }
    }

    sendBtn.addEventListener("click", send);
    inputEl.addEventListener("keydown", (e) => {
      if (e.key === "Enter") send();
    });
  </script>
</body>
</html>
"""


ADMIN_BUSINESSES_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8" />
  <title>Admin · Businesses</title>
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
    .shell { max-width: 960px; margin: 0 auto; }
    h1 { margin: 0 0 10px; font-size: 22px; }
    table { width: 100%; border-collapse: collapse; font-size: 12px; }
    th, td {
      padding: 6px 4px;
      border-bottom: 1px solid rgba(31,41,55,1);
      text-align: left;
    }
    th { color: #9ca3af; font-weight: 500; }
    a {
      color: #818cf8;
      text-decoration: none;
      font-size: 12px;
    }
    a:hover { text-decoration: underline; }
    .tag {
      display: inline-block;
      font-size: 11px;
      border-radius: 999px;
      border: 1px solid rgba(148,163,184,0.5);
      padding: 2px 8px;
      margin-right: 4px;
    }
  </style>
</head>
<body>
  <div class="shell">
    <h1>Businesses</h1>
    <p style="font-size:12px; color:#9ca3af;">Approve or deactivate accounts. Pending accounts are shown first.</p>
    <p style="font-size:12px;"><a href="{{ url_for('logout') }}">Log out</a></p>
    <table>
      <thead>
        <tr>
          <th>Status</th>
          <th>Business</th>
          <th>Owner email</th>
          <th>Plan / Category</th>
          <th>Business ID</th>
          <th>Actions</th>
        </tr>
      </thead>
      <tbody>
        {% for row in rows %}
          <tr>
            <td>
              {% if not row.user.is_active %}
                <span class="tag">Pending</span>
              {% else %}
                <span class="tag">Active</span>
              {% endif %}
            </td>
            <td>{{ row.business.name }}</td>
            <td>{{ row.user.email }}</td>
            <td>{{ row.business.category or "—" }}</td>
            <td>{{ row.business.business_id }}</td>
            <td>
              {% if not row.user.is_active %}
                <a href="{{ url_for('admin_approve', user_id=row.user.id) }}">Approve</a>
              {% else %}
                <a href="{{ url_for('admin_deactivate', user_id=row.user.id) }}">Deactivate</a>
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


# ----------------- Routes -----------------


@app.route("/")
def index():
    business_id = (request.args.get("id") or "").strip()
    if business_id:
        db = get_db()
        try:
            biz = db.query(Business).filter(Business.business_id == business_id).first()
        finally:
            db.close()
        if not biz:
            return "Business not found.", 404
        return render_template_string(CHAT_PAGE_HTML, biz=biz)
    return render_template_string(LANDING_HTML)


@app.route("/pricing")
def pricing():
    return render_template_string(PRICING_HTML)


@app.route("/signup", methods=["GET", "POST"])
def signup():
    message = None
    error = False
    plan = (request.args.get("plan") or "").strip().lower()

    if request.method == "POST":
        owner_name = (request.form.get("owner_name") or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        password = (request.form.get("password") or "").strip()
        business_name = (request.form.get("business_name") or "").strip()
        phone = (request.form.get("phone") or "").strip()
        category = (request.form.get("category") or "").strip()
        address = (request.form.get("address") or "").strip()
        booking_url = (request.form.get("booking_url") or "").strip()
        blurb = (request.form.get("blurb") or "").strip()
        plan = (request.form.get("plan") or plan).strip().lower()

        if not owner_name or not email or not password or not business_name:
            message = "Name, email, password, and business name are required."
            error = True
        else:
            db = get_db()
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
                        category=category or plan,
                    )
                    db.add(biz)
                    db.commit()

                    user = User(
                        email=email,
                        password_hash=generate_password_hash(password),
                        role="business",
                        business_id=biz_id,
                        is_active=False,  # require admin approval
                    )
                    db.add(user)
                    db.commit()

                    message = "Account created! You'll get an email once an admin approves your account."
                    error = False

                    if ADMIN_EMAIL:
                        subject = "New NovuChat signup"
                        body = (
                            f"Owner: {owner_name}\n"
                            f"Email: {email}\n"
                            f"Business: {business_name}\n"
                            f"Business ID: {biz_id}\n"
                            f"Plan: {plan or 'not specified'}\n"
                            f"Phone: {phone}\n"
                            f"Address: {address}\n"
                            f"Booking URL: {booking_url}\n"
                            f"Category: {category}\n\n"
                            "Log into the admin panel to review and approve this account."
                        )
                        send_email(ADMIN_EMAIL, subject, body)
            finally:
                db.close()

    return render_template_string(SIGNUP_HTML, message=message, error=error, plan=plan)


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""

        db = get_db()
        try:
            user = db.query(User).filter(User.email == email).first()
        finally:
            db.close()

        if not user or not check_password_hash(user.password_hash, password):
            error = "Invalid email or password."
        elif user.role == "business" and not user.is_active:
            error = "Your account is pending approval."
        else:
            session["user_id"] = user.id
            if user.role == "admin":
                return redirect(url_for("admin_businesses"))
            else:
                return redirect(url_for("dashboard"))

    return render_template_string(LOGIN_HTML, error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    message = None
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        db = get_db()
        try:
            user = db.query(User).filter(User.email == email).first()
            if user:
                token = secrets.token_urlsafe(48)
                expires = datetime.datetime.utcnow() + datetime.timedelta(hours=1)
                user.reset_token = token
                user.reset_expires_at = expires
                db.commit()

                base_url = request.url_root.rstrip("/")
                reset_link = f"{base_url}{url_for('reset_password')}?token={token}"

                subject = "Reset your NovuChat password"
                body = (
                    "You requested a password reset for your NovuChat account.\n\n"
                    f"Click this link to choose a new password:\n{reset_link}\n\n"
                    "This link will expire in 1 hour. If you didn't request this, you can ignore this email."
                )
                send_email(user.email, subject, body)
    finally:
        db.close()
        message = "If an account exists for that email, you'll receive a reset link shortly."

    return render_template_string(FORGOT_PASSWORD_HTML, message=message)


@app.route("/reset-password", methods=["GET", "POST"])
def reset_password():
    token = (request.args.get("token") or "").strip()
    if not token:
        return render_template_string(RESET_PASSWORD_HTML, message="Invalid link.", error=True, valid=False)

    db = get_db()
    try:
        user = db.query(User).filter(User.reset_token == token).first()
        now = datetime.datetime.utcnow()
        valid = bool(user and user.reset_expires_at and user.reset_expires_at > now)

        message = None
        error = False

        if request.method == "POST" and valid:
            password = (request.form.get("password") or "").strip()
            if not password:
                message = "Password cannot be empty."
                error = True
            else:
                user.password_hash = generate_password_hash(password)
                user.reset_token = None
                user.reset_expires_at = None
                db.commit()
                message = "Your password has been updated. You can now log in."
                error = False
                valid = False  # hide form after success
                return render_template_string(
                    RESET_PASSWORD_HTML,
                    message=message,
                    error=error,
                    valid=False,
                )

        if not valid and message is None:
            message = "This reset link is invalid or has expired."
            error = True

        return render_template_string(RESET_PASSWORD_HTML, message=message, error=error, valid=valid)
    finally:
        db.close()


@app.route("/dashboard")
@business_required
def dashboard():
    user, role, business_id = get_current_user()
    db = get_db()
    try:
            biz = db.query(Business).filter(Business.business_id == business_id).first()
        leads = (
            db.query(Lead)
            .filter(Lead.business_id == business_id)
            .order_by(Lead.created_at.desc())
            .limit(50)
            .all()
        )
    finally:
        db.close()

    public_url = request.url_root.rstrip("/")
    return render_template_string(DASHBOARD_HTML, biz=biz, leads=leads, public_url=public_url)


@app.route("/admin/businesses")
@admin_required
def admin_businesses():
    db = get_db()
    try:
        users = db.query(User).filter(User.role == "business").all()
        rows = []
        for u in users:
            biz = db.query(Business).filter(Business.business_id == u.business_id).first()
            if biz:
                rows.append({"user": u, "business": biz})
        rows.sort(key=lambda r: (r["user"].is_active, r["business"].name))
    finally:
        db.close()
    return render_template_string(ADMIN_BUSINESSES_HTML, rows=rows)


@app.route("/admin/approve/<int:user_id>")
@admin_required
def admin_approve(user_id):
    db = get_db()
    try:
        u = db.query(User).filter(User.id == user_id).first()
        if not u:
            return "User not found", 404
        u.is_active = True
        db.commit()

        subject = "Your NovuChat account is approved"
        body = (
            "Your NovuChat account has been approved.\n\n"
            "You can now log in and access your dashboard.\n\n"
            "Login here: /login"
        )
        send_email(u.email, subject, body)
    finally:
        db.close()
    return redirect(url_for("admin_businesses"))


@app.route("/admin/deactivate/<int:user_id>")
@admin_required
def admin_deactivate(user_id):
    db = get_db()
    try:
        u = db.query(User).filter(User.id == user_id).first()
        if not u:
            return "User not found", 404
        u.is_active = False
        db.commit()
    finally:
        db.close()
    return redirect(url_for("admin_businesses"))


@app.route("/chat", methods=["POST"])
def chat():
    try:
        data = request.get_json(force=True)
        business_id = (data.get("business_id") or "").strip()
        user_message = (data.get("message") or "").strip()

        if not business_id or not user_message:
            return jsonify({"reply": "Missing business_id or message."}), 400

        db = get_db()
        try:
            biz = db.query(Business).filter(Business.business_id == business_id).first()
        finally:
            db.close()

        if not biz:
            return jsonify({"reply": "Business not found."}), 404

        system_prompt = f"""
You are a helpful, concise AI assistant for the business "{biz.name}".

You MUST follow these rules:
- Answer ONLY using the data provided below (Hours, Services, Pricing, Location, Contact, FAQs, Booking URL).
- If the answer is not clearly supported by this data, say you are not sure and ask the user to contact the business directly.
- Do NOT invent prices, policies, availability, discounts, or guarantees.
- Keep answers brief and clear (usually under 5 sentences) unless the user asks for more detail.
- If the user asks about booking or making an appointment, direct them to the booking link or contact information.

BUSINESS PROFILE
----------------
Name: {biz.name}
Location: {biz.location}
Address: {biz.address}
Contact: {biz.contact}
Booking URL: {biz.booking_url}

Hours:
{biz.hours}

Services:
{biz.services}

Pricing:
{biz.pricing}

FAQs:
{biz.faqs}
""".strip()

        if not OPENAI_API_KEY:
            reply_text = "AI is not configured yet."
    else:
            resp = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "gpt-4o-mini",
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_message},
                    ],
                    "temperature": 0.3,
                },
                timeout=60,
            )
            resp.raise_for_status()
            data_json = resp.json()
            reply_text = (
                data_json.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
                .strip()
            )
            if not reply_text:
                reply_text = "Sorry, I couldn't generate a reply."

        ts = datetime.datetime.now().isoformat()
        log_line = f"{ts} | {business_id} | USER: {user_message} | BOT: {reply_text}\n"
        with open(CHAT_LOG_FILE, "a") as logf:
            logf.write(log_line)

        return jsonify({"reply": reply_text})
    except Exception as e:
        print("ERROR in /chat:", repr(e))
        return jsonify({"reply": "Sorry, something went wrong with the AI."}), 500


@app.route("/lead", methods=["POST"])
def lead():
    data = request.get_json(force=True)
    business_id = (data.get("business_id") or "").strip()
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip()
    phone = (data.get("phone") or "").strip()
    message = (data.get("message") or "").strip()

    if not business_id or not email:
        return jsonify({"ok": False, "error": "Missing business_id or email."}), 400

    db = get_db()
    try:
        biz = db.query(Business).filter(Business.business_id == business_id).first()
        if not biz:
            return jsonify({"ok": False, "error": "Business not found."}), 404

        lead_obj = Lead(
            business_id=business_id,
            name=name,
            email=email,
            phone=phone,
            message=message,
        )
        db.add(lead_obj)
        db.commit()

        subject = f"New lead from {biz.name}"
        body = (
            f"You received a new lead from your NovuChat widget.\n\n"
            f"Name: {name}\n"
            f"Email: {email}\n"
            f"Phone: {phone}\n\n"
            f"Message:\n{message}\n\n"
        )
        if biz.contact:
            contact_email = None
            for token in biz.contact.replace(",", " ").split():
                if "@" in token:
                    contact_email = token.strip()
                    break
            if contact_email:
                send_email(contact_email, subject, body)
        if ADMIN_EMAIL:
            send_email(ADMIN_EMAIL, "[Copy] " + subject, body)

    finally:
        db.close()

    return jsonify({"ok": True})


# ----------------- Startup -----------------

init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)
