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
  <title>Cardholics AI – Chat widget for local businesses</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    :root { color-scheme: dark; }
    body {
      margin: 0;
      min-height: 100vh;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
      background:
        radial-gradient(circle at top, rgba(37,99,235,0.3) 0, transparent 55%),
        radial-gradient(circle at bottom right, rgba(236,72,153,0.2) 0, transparent 50%),
        #020617;
      color: #e5e7eb;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 24px;
    }
    .nav {
      position: absolute;
      top: 18px;
      right: 24px;
      font-size: 13px;
    }
    .nav a {
      color: #9ca3af;
      text-decoration: none;
      margin-left: 14px;
    }
    .nav a:hover { text-decoration: underline; }
    .shell {
      width: 100%;
      max-width: 960px;
      display: grid;
      grid-template-columns: minmax(0,1.2fr) minmax(0,1fr);
      gap: 24px;
    }
    @media (max-width: 840px) { .shell { grid-template-columns: minmax(0,1fr); } }
    .hero { padding: 20px 18px; }
    .badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      font-size: 11px;
      padding: 4px 10px;
      border-radius: 999px;
      border: 1px solid rgba(148,163,184,0.6);
      background: rgba(15,23,42,0.9);
      margin-bottom: 10px;
      color: #9ca3af;
    }
    h1 {
      font-size: 32px;
      line-height: 1.2;
      margin: 0 0 10px;
    }
    .hero-sub {
      font-size: 14px;
      color: #9ca3af;
      max-width: 460px;
      margin-bottom: 18px;
    }
    .cta-row {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      align-items: center;
      margin-bottom: 14px;
    }
    .btn {
      border-radius: 999px;
      border: none;
      padding: 10px 18px;
      font-family: inherit;
      font-size: 14px;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      text-decoration: none;
    }
    .btn-primary {
      background: linear-gradient(135deg, #4f46e5, #6366f1);
      color: #f9fafb;
    }
    .btn-ghost {
      background: transparent;
      color: #e5e7eb;
      border: 1px solid rgba(148,163,184,0.5);
    }
    .tiny { font-size: 11px; color: #6b7280; }
    .panel {
      background: rgba(15,23,42,0.98);
      border-radius: 20px;
      border: 1px solid rgba(148,163,184,0.45);
      box-shadow: 0 26px 80px rgba(15,23,42,1);
      padding: 16px 16px 18px;
      font-size: 13px;
    }
    .plan-title { font-size: 15px; font-weight: 600; margin-bottom: 4px; }
    .price { font-size: 24px; font-weight: 600; margin: 4px 0; }
    .price span {
      font-size: 13px;
      color: #9ca3af;
      font-weight: 400;
    }
    ul {
      list-style: none;
      padding-left: 0;
      margin: 10px 0 14px;
      color: #9ca3af;
      font-size: 12px;
    }
    li::before { content: "• "; color: #6366f1; }
  </style>
</head>
<body>
  <div class="nav">
    <a href="{{ url_for('login') }}">Login</a>
    <a href="{{ url_for('signup') }}">Sign up</a>
  </div>
  <div class="shell">
    <div class="hero">
      <div class="badge">
        <span>⚡</span> AI chat widget for local shops & services
      </div>
      <h1>Turn website visitors into booked appointments.</h1>
      <div class="hero-sub">
        Cardholics AI sits on your site as a floating chat bubble, answers common
        questions, and sends warm leads straight to your inbox.
      </div>
      <div class="cta-row">
        <a href="{{ url_for('signup') }}" class="btn btn-primary">Get started free</a>
        <a href="{{ url_for('pricing') }}" class="btn btn-ghost">View plans</a>
      </div>
      <div class="tiny">No code required. Copy–paste one line of script.</div>
    </div>
    <div class="panel">
      <div class="plan-title">Starter plan</div>
      <div class="price">$0 <span>/ beta</span></div>
      <ul>
        <li>1 website</li>
        <li>1 chat widget</li>
        <li>Up to 500 messages / month</li>
        <li>Leads emailed instantly</li>
      </ul>
      <a href="{{ url_for('signup', plan='starter') }}" class="btn btn-primary" style="width:100%; justify-content:center;">
        Choose Starter
      </a>
    </div>
  </div>
</body>
</html>
"""


PRICING_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8" />
  <title>Pricing · Cardholics AI</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    body {
      margin: 0;
      min-height: 100vh;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
      background: #020617;
      color: #e5e7eb;
      display: flex;
      justify-content: center;
      padding: 32px 16px;
    }
    .shell { width: 100%; max-width: 960px; }
    h1 { margin: 0 0 6px; font-size: 24px; }
    .sub { font-size: 13px; color: #9ca3af; margin-bottom: 20px; }
    .grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0,1fr));
      gap: 16px;
    }
    @media (max-width: 900px) { .grid { grid-template-columns: minmax(0,1fr); } }
    .card {
      background: #020617;
      border-radius: 18px;
      border: 1px solid rgba(148,163,184,0.45);
      padding: 16px 16px 18px;
    }
    .plan { font-size: 14px; font-weight: 600; margin-bottom: 4px; }
    .price { font-size: 22px; font-weight: 600; margin: 4px 0; }
    .price span { font-size: 13px; color: #9ca3af; font-weight: 400; }
    ul {
      list-style: none;
      padding-left: 0;
      margin: 10px 0 14px;
      color: #9ca3af;
      font-size: 12px;
    }
    li::before { content: "• "; color: #6366f1; }
    .btn {
      width: 100%;
      border-radius: 999px;
      border: none;
      padding: 9px 14px;
      font-family: inherit;
      font-size: 13px;
      cursor: pointer;
      background: linear-gradient(135deg, #4f46e5, #6366f1);
      color: #f9fafb;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      justify-content: center;
    }
    .top-nav {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 16px;
      font-size: 13px;
    }
    .top-nav a {
      color: #9ca3af;
      text-decoration: none;
      margin-left: 14px;
    }
    .top-nav a:hover { text-decoration: underline; }
  </style>
</head>
<body>
  <div class="shell">
    <div class="top-nav">
      <div>
        <a href="{{ url_for('index') }}">← Back to home</a>
      </div>
      <div>
        <a href="{{ url_for('login') }}">Login</a>
        <a href="{{ url_for('signup') }}">Sign up</a>
      </div>
    </div>
    <h1>Simple pricing for growing local businesses</h1>
    <div class="sub">Start on the free beta plan. Upgrade later when you're ready.</div>
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
  <title>Login · Cardholics AI</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    body {
      margin: 0;
      min-height: 100vh;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
      background: #020617;
      color: #e5e7eb;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 24px;
    }
    .card {
      width: 100%;
      max-width: 420px;
      background: #020617;
      border-radius: 20px;
      border: 1px solid rgba(148,163,184,0.45);
      padding: 22px 22px 24px;
    }
    h1 { margin: 0 0 6px; font-size: 20px; }
    .sub { font-size: 13px; color: #9ca3af; margin-bottom: 16px; }
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
    .msg { font-size: 12px; margin-bottom: 8px; color: #f97373; }
    .foot {
      margin-top: 10px;
      font-size: 12px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }
    a { color: #818cf8; text-decoration: none; }
    a:hover { text-decoration: underline; }
  </style>
</head>
<body>
  <div class="card">
    <h1>Welcome back</h1>
    <div class="sub">Sign in to manage your chat widget and leads.</div>

    {% if error %}
      <div class="msg">{{ error }}</div>
    {% endif %}

    <form method="post">
      <label>Email</label>
      <input type="email" name="email" required />

      <label>Password</label>
      <input type="password" name="password" required />

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
  <title>Sign up · Cardholics AI</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    body {
      margin: 0;
      min-height: 100vh;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
      background: #020617;
      color: #e5e7eb;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 24px;
    }
    .card {
      width: 100%;
      max-width: 520px;
      background: #020617;
      border-radius: 20px;
      border: 1px solid rgba(148,163,184,0.45);
      padding: 22px 22px 24px;
    }
    h1 { margin: 0 0 6px; font-size: 20px; }
    .sub { font-size: 13px; color: #9ca3af; margin-bottom: 16px; }
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
      box-sizing: border-box;
    }
    textarea { min-height: 70px; resize: vertical; }
    input:focus, textarea:focus, select:focus {
      outline: none;
      border-color: #6366f1;
    }
    .row {
      display: grid;
      grid-template-columns: repeat(2, minmax(0,1fr));
      gap: 8px;
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
    .msg {
      font-size: 12px;
      margin-bottom: 8px;
    }
    .msg-error { color: #f97373; }
    .msg-ok { color: #22c55e; }
    a { color: #818cf8; text-decoration: none; font-size: 12px; }
    a:hover { text-decoration: underline; }
    .foot {
      margin-top: 10px;
      display: flex;
      justify-content: space-between;
      gap: 8px;
      flex-wrap: wrap;
      font-size: 12px;
    }
  </style>
</head>
<body>
  <div class="card">
    <h1>Create your account</h1>
    <div class="sub">Set up Cardholics AI for your business. No card required during beta.</div>

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
  <title>Reset password · Cardholics AI</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    body {
      margin: 0;
      min-height: 100vh;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
      background: #020617;
      color: #e5e7eb;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 24px;
    }
    .card {
      width: 100%;
      max-width: 420px;
      background: #020617;
      border-radius: 20px;
      border: 1px solid rgba(148,163,184,0.45);
      padding: 22px 22px 24px;
    }
    h1 { margin: 0 0 6px; font-size: 20px; }
    .sub { font-size: 13px; color: #9ca3af; margin-bottom: 16px; }
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
    .msg { font-size: 12px; margin-bottom: 8px; color: #22c55e; }
    a { color: #818cf8; text-decoration: none; }
    a:hover { text-decoration: underline; }
  </style>
</head>
<body>
  <div class="card">
    <h1>Reset your password</h1>
    <div class="sub">Enter your email and we'll send you a reset link.</div>

    {% if message %}
      <div class="msg">{{ message }}</div>
    {% endif %}

    <form method="post">
      <label>Email</label>
      <input type="email" name="email" required />
      <button class="btn" type="submit">Send reset link</button>
    </form>
    <p style="font-size:12px; margin-top:10px;">
      <a href="{{ url_for('login') }}">&larr; Back to login</a>
    </p>
  </div>
</body>
</html>
"""


RESET_PASSWORD_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8" />
  <title>Choose new password · Cardholics AI</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    body {
      margin: 0;
      min-height: 100vh;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
      background: #020617;
      color: #e5e7eb;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 24px;
    }
    .card {
      width: 100%;
      max-width: 420px;
      background: #020617;
      border-radius: 20px;
      border: 1px solid rgba(148,163,184,0.45);
      padding: 22px 22px 24px;
    }
    h1 { margin: 0 0 6px; font-size: 20px; }
    .sub { font-size: 13px; color: #9ca3af; margin-bottom: 16px; }
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
    .msg { font-size: 12px; margin-bottom: 8px; }
    .msg-err { color: #f97373; }
    .msg-ok { color: #22c55e; }
    a { color: #818cf8; text-decoration: none; }
    a:hover { text-decoration: underline; }
  </style>
</head>
<body>
  <div class="card">
    <h1>Choose a new password</h1>
    <div class="sub">Enter a new password for your account.</div>

    {% if message %}
      <div class="msg {{ 'msg-err' if error else 'msg-ok' }}">{{ message }}</div>
    {% endif %}

    {% if valid %}
      <form method="post">
        <label>New password</label>
        <input type="password" name="password" required />
        <button class="btn" type="submit">Update password</button>
      </form>
    {% else %}
      <p style="font-size:12px;">This reset link is invalid or has expired.</p>
    {% endif %}

    <p style="font-size:12px; margin-top:10px;">
      <a href="{{ url_for('login') }}">&larr; Back to login</a>
    </p>
  </div>
</body>
</html>
"""


DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8" />
  <title>Dashboard · Cardholics AI</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    body {
      margin: 0;
      min-height: 100vh;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
      background: #020617;
      color: #e5e7eb;
    }
    .shell {
      max-width: 980px;
      margin: 0 auto;
      padding: 22px 18px 40px;
    }
    h1 { margin: 0 0 4px; font-size: 22px; }
    .sub { font-size: 13px; color: #9ca3af; margin-bottom: 14px; }
    .top {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      margin-bottom: 16px;
    }
    .top a {
      color: #9ca3af;
      font-size: 12px;
      text-decoration: none;
    }
    .top a:hover { text-decoration: underline; }
    .card {
      background: #020617;
      border-radius: 18px;
      border: 1px solid rgba(148,163,184,0.5);
      padding: 14px 14px 16px;
      margin-bottom: 16px;
      font-size: 13px;
    }
    code {
      font-size: 11px;
      background: #020617;
      border-radius: 8px;
      padding: 6px 8px;
      display: block;
      border: 1px solid rgba(148,163,184,0.4);
      white-space: pre;
      overflow-x: auto;
    }
    .tag {
      display: inline-block;
      font-size: 11px;
      color: #9ca3af;
      border-radius: 999px;
      border: 1px solid rgba(148,163,184,0.5);
      padding: 2px 8px;
      margin-bottom: 6px;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 12px;
      margin-top: 8px;
    }
    th, td {
      padding: 6px 4px;
      border-bottom: 1px solid rgba(31,41,55,1);
      text-align: left;
    }
    th { color: #9ca3af; font-weight: 500; }
  </style>
</head>
<body>
  <div class="shell">
    <div class="top">
      <div>
        <h1>{{ biz.name }}</h1>
        <div class="sub">Manage your widget and view recent leads.</div>
      </div>
      <div>
        <a href="{{ url_for('logout') }}">Log out</a>
      </div>
    </div>

    <div class="card">
      <div class="tag">Widget embed code</div>
      <p>Paste this right before <code>&lt;/body&gt;</code> on your website.</p>
      <code>&lt;iframe
  src="{{ public_url }}?id={{ biz.business_id }}"
  style="width:100%;max-width:420px;height:520px;border:none;border-radius:18px;box-shadow:0 20px 60px rgba(15,23,42,0.9);"
  loading="lazy"
&gt;&lt;/iframe&gt;</code>
    </div>

    <div class="card">
      <div class="tag">Recent leads</div>
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
        <p>No leads yet. Once visitors start chatting and leaving their info, they’ll appear here.</p>
      {% endif %}
    </div>
  </div>
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
    body {
      margin: 0;
      min-height: 100vh;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
      background: #020617;
      color: #e5e7eb;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 16px;
    }
    .shell {
      width: 100%;
      max-width: 420px;
      background: #020617;
      border-radius: 18px;
      border: 1px solid rgba(148,163,184,0.5);
      padding: 12px 12px 14px;
      box-shadow: 0 26px 80px rgba(15,23,42,1);
      display: flex;
      flex-direction: column;
      height: 540px;
    }
    .header {
      padding: 4px 4px 8px;
      border-bottom: 1px solid rgba(31,41,55,1);
      display: flex;
      align-items: center;
      gap: 10px;
    }
    .avatar {
      width: 30px;
      height: 30px;
      border-radius: 999px;
      background: linear-gradient(135deg, #4f46e5, #6366f1);
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 16px;
    }
    .title { font-size: 14px; }
    .subtitle { font-size: 11px; color: #9ca3af; }
    .messages {
      flex: 1;
      overflow-y: auto;
      padding: 8px 2px;
      font-size: 13px;
    }
    .msg { margin-bottom: 8px; max-width: 90%; }
    .msg.me {
      margin-left: auto;
      text-align: right;
    }
    .bubble {
      display: inline-block;
      padding: 6px 9px;
      border-radius: 12px;
      background: #111827;
    }
    .msg.me .bubble {
      background: #4f46e5;
    }
    .input-row {
      border-top: 1px solid rgba(31,41,55,1);
      padding-top: 6px;
      display: flex;
      gap: 6px;
    }
    input {
      flex: 1;
      border-radius: 999px;
      border: 1px solid rgba(148,163,184,0.55);
      background: #020617;
      color: #e5e7eb;
      font-size: 13px;
      padding: 7px 10px;
      font-family: inherit;
    }
    input:focus {
      outline: none;
      border-color: #6366f1;
    }
    button {
      border-radius: 999px;
      border: none;
      padding: 7px 12px;
      font-size: 13px;
      font-family: inherit;
      cursor: pointer;
      background: #4f46e5;
      color: #f9fafb;
    }
  </style>
</head>
<body>
  <div class="shell">
    <div class="header">
      <div class="avatar">✂️</div>
      <div>
        <div class="title">{{ biz.name }}</div>
        <div class="subtitle">Ask a question about hours, services, or booking.</div>
      </div>
    </div>
    <div class="messages" id="messages">
      <div class="msg">
        <div class="bubble">Hey! I’m the assistant for {{ biz.name }}. How can I help?</div>
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
                        subject = "New Cardholics AI signup"
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

                subject = "Reset your Cardholics AI password"
                body = (
                    "You requested a password reset for your Cardholics AI account.\n\n"
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

        subject = "Your Cardholics AI account is approved"
        body = (
            "Your Cardholics AI account has been approved.\n\n"
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
            f"You received a new lead from your Cardholics AI widget.\n\n"
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
