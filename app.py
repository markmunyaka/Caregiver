import os
import requests
from flask import Flask, render_template, request, url_for, redirect
from flask_sqlalchemy import SQLAlchemy
from dotenv import load_dotenv
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse
from telegram import Bot
import openai
from scraper import run_scraper
from flask import session, flash
from werkzeug.security import generate_password_hash, check_password_hash
import secrets as _secrets

load_dotenv()

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv("DATABASE_URL", "sqlite:///database.db")
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.secret_key = os.getenv("SECRET_KEY", "change-me")
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

db = SQLAlchemy(app)

# Twilio client
TW_SID = os.getenv("TWILIO_ACCOUNT_SID")
TW_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TW_FROM = os.getenv("TWILIO_FROM_NUMBER")
twilio_client = Client(TW_SID, TW_TOKEN) if TW_SID and TW_TOKEN else None

# Telegram bot
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
telegram_bot = Bot(token=TELEGRAM_BOT_TOKEN) if TELEGRAM_BOT_TOKEN else None

# OpenAI
openai.api_key = os.getenv("OPENAI_API_KEY")

OMAN_TZ = os.getenv("OMAN_TIMEZONE", "Asia/Muscat")

class Hospital(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(256))
    phone = db.Column(db.String(64), unique=True)
    city = db.Column(db.String(100))
    type = db.Column(db.String(50))
    verified = db.Column(db.Boolean, default=False)
    weight = db.Column(db.Float, default=0.0)
    last_called = db.Column(db.DateTime, nullable=True)

class CallLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    hospital_id = db.Column(db.Integer, db.ForeignKey('hospital.id'))
    hospital_name = db.Column(db.String(256))
    phone = db.Column(db.String(64))
    status = db.Column(db.String(50))
    duration_seconds = db.Column(db.Integer, default=0)
    transcript = db.Column(db.Text)
    summary = db.Column(db.Text)
    recording_url = db.Column(db.String(400))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    hungup_by = db.Column(db.String(50), nullable=True)

db.create_all()

def send_telegram(text):
    if not telegram_bot:
        print("Telegram not configured. Message:", text)
        return
    try:
        telegram_bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text)
    except Exception as e:
        print("Telegram send error:", e)

def send_telegram_audio(file_url, caption):
    if not telegram_bot:
        print("Telegram not configured. Audio URL:", file_url)
        return
    try:
        telegram_bot.send_audio(chat_id=TELEGRAM_CHAT_ID, audio=file_url, caption=caption)
    except Exception as e:
        print("Telegram audio error:", e)

CALL_SCRIPT_INTRO = (
    "Good morning. My name is Mark. I'm calling to ask if your hospital currently has any job openings "
    "for caregivers for foreign applicants. If yes, do you provide visa sponsorship?"
)

scheduler = BackgroundScheduler(timezone=OMAN_TZ)

def build_call_queue(limit=20):
    threshold = datetime.utcnow() - timedelta(days=2)
    hospitals = Hospital.query.filter(Hospital.verified == True).all()
    queue = []
    for h in hospitals:
        recency_penalty = 0
        if h.last_called and h.last_called > threshold:
            recency_penalty = -1.5
        score = (h.weight or 0.0) + recency_penalty
        queue.append((score, h))
    queue.sort(key=lambda x: x[0], reverse=True)
    return [h for _, h in queue][:limit]

def schedule_calls_job():
    queue = build_call_queue(limit=20)
    for hospital in queue:
        if not hospital.phone:
            continue
        try:
            make_call(hospital.id, hospital.name, hospital.phone)
        except Exception as e:
            print("Error initiating call to", hospital.phone, e)

def make_call(hospital_id, hospital_name, phone):
    if not twilio_client:
        send_telegram(f"⚠️ Twilio not configured. Would call {hospital_name} ({phone})")
        return None
    try:
        twilio_call = twilio_client.calls.create(
            to=phone,
            from_=TW_FROM,
            url=f"{get_base_url()}/voice?hospital_id={hospital_id}",
            status_callback=f"{get_base_url()}/webhook/status",
            status_callback_event=["completed", "failed", "no-answer", "busy"],
            record=True,
            recording_status_callback=f"{get_base_url()}/webhook/recording"
        )
        hospital = Hospital.query.get(hospital_id)
        hospital.last_called = datetime.utcnow()
        db.session.commit()
        send_telegram(f"📞 Calling: {hospital_name} ({phone})")
        return twilio_call.sid
    except Exception as e:
        print("Twilio call error:", e)
        send_telegram(f"⚠️ Call failed to initiate: {hospital_name} ({phone}). Error: {e}")
        return None

def get_base_url():
    base = os.getenv("PUBLIC_BASE_URL")
    if base:
        return base.rstrip("/")
    return os.getenv("LOCAL_BASE_URL", "http://localhost:8000")

@app.route("/")
def index():
    calls = CallLog.query.order_by(CallLog.id.desc()).limit(200).all()
    hospitals = Hospital.query.order_by(Hospital.weight.desc()).all()
    return render_template("index.html", calls=calls, hospitals=hospitals)

@app.route("/call/<int:call_id>")
def call_detail(call_id):
    call = CallLog.query.get_or_404(call_id)
    return render_template("call_detail.html", call=call)

@app.route("/voice")
def voice():
    hospital_id = request.args.get("hospital_id")
    response = VoiceResponse()
    response.say(CALL_SCRIPT_INTRO, voice='man', language='en-GB')
    return str(response)

@app.route("/webhook/status", methods=["POST"])
def twilio_status():
    data = request.form.to_dict()
    call_sid = data.get("CallSid")
    call_status = data.get("CallStatus")
    to_number = data.get("To")
    duration = data.get("CallDuration")
    hospital = Hospital.query.filter_by(phone=to_number).first()
    hospital_name = hospital.name if hospital else to_number
    duration_seconds = int(duration) if duration else 0
    status_text = call_status
    cl = CallLog(
        hospital_id = hospital.id if hospital else None,
        hospital_name=hospital_name,
        phone=to_number,
        status=status_text,
        duration_seconds=duration_seconds,
        created_at=datetime.utcnow()
    )
    db.session.add(cl)
    db.session.commit()
    if call_status in ("failed", "no-answer", "busy"):
        send_telegram(f"⚠️ Call failed or unanswered — {hospital_name} ({to_number})")
    elif call_status == "completed":
        send_telegram(f"✅ Call ended successfully — {hospital_name} ({to_number}), Duration: {duration_seconds}s")
    else:
        send_telegram(f"ℹ️ Call status update — {hospital_name} ({to_number}): {call_status}")
    return ("", 204)

@app.route("/webhook/recording", methods=["POST"])
def recording_callback():
    data = request.form.to_dict()
    recording_url = data.get("RecordingUrl")
    to_number = data.get("To")
    duration = int(float(data.get("RecordingDuration") or 0))
    hospital = Hospital.query.filter_by(phone=to_number).first()
    hospital_name = hospital.name if hospital else to_number
    mp3_url = recording_url + ".mp3"
    cl = CallLog.query.filter_by(phone=to_number).order_by(CallLog.created_at.desc()).first()
    if not cl:
        cl = CallLog(hospital_id=hospital.id if hospital else None,
                     hospital_name=hospital_name, phone=to_number, status="recorded",
                     created_at=datetime.utcnow())
        db.session.add(cl)
        db.session.commit()
    cl.recording_url = mp3_url
    cl.duration_seconds = duration
    db.session.commit()
    transcript_text = ""
    try:
        r = requests.get(mp3_url)
        audio_bytes = r.content
        with open("temp_recording.mp3", "wb") as f:
            f.write(audio_bytes)
        with open("temp_recording.mp3", "rb") as audio_file:
            transcript_resp = openai.Audio.transcriptions.create(
                file=audio_file,
                model="whisper-1"
            )
            transcript_text = transcript_resp.get("text", "")
    except Exception as e:
        print("Transcription error:", e)
        transcript_text = ""
    cl.transcript = transcript_text
    db.session.commit()
    summary = ""
    try:
        prompt = f"Summarize this caregiver job inquiry call in 2 short sentences and include whether visa sponsorship was mentioned:\n\n{transcript_text}"
        resp = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=[
                {"role":"system","content":"You are a concise assistant summarizing call transcripts."},
                {"role":"user","content":prompt}
            ],
            max_tokens=200
        )
        summary = resp["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print("Summary generation error:", e)
        summary = "Summary generation failed."
    cl.summary = summary
    db.session.commit()
    send_telegram(f"📋 Call Summary — {hospital_name}\n{summary}")
    try:
        send_telegram_audio(mp3_url, caption=f"Recording — {hospital_name}")
    except Exception as e:
        print("Telegram audio send failed:", e)
    try:
        update_learning_from_transcript(hospital, transcript_text)
    except Exception as e:
        print("Learning update failed:", e)
    return ("", 204)

def update_learning_from_transcript(hospital, transcript):
    if not hospital:
        return
    text = (transcript or "").lower()
    delta = 0.0
    if "visa" in text or "sponsor" in text or "sponsorship" in text:
        delta += 5.0
    if "no vacancy" in text or "no vacancies" in text or "no openings" in text or "not available" in text:
        delta -= 2.0
    if len(text.split()) > 40:
        delta += 1.5
    hospital.weight = (hospital.weight or 0.0) + delta
    hospital.weight = max(-20.0, min(50.0, hospital.weight))
    db.session.commit()

@app.route("/admin/scrape", methods=["POST"])
def admin_scrape():
    results = run_scraper()
    added = 0
    for r in results:
        phone = r.get("phone")
        if not phone:
            continue
        phone_norm = phone.strip()
        if phone_norm.startswith("0"):
            phone_norm = "+968" + phone_norm[1:]
        existing = Hospital.query.filter_by(phone=phone_norm).first()
        if not existing:
            h = Hospital(name=r.get("name"), phone=phone_norm, city=r.get("city"), type=r.get("type"), verified=True)
            db.session.add(h)
            added += 1
    db.session.commit()
    send_telegram(f"🗂️ Scraper run complete — {len(results)} results, {added} new entries added.")
    return {"added": added, "found": len(results)}

@app.route("/admin/run_schedule", methods=["POST"])
def admin_run_schedule():
    schedule_calls_job()
    return {"status": "scheduled"}


# ----- SIMPLE AUTH (SESSION-BASED) -----
# Default credentials: username 'mark' and password 'caregiver2025' (you can override via env)
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "mark")
# If ADMIN_PASSWORD_HASH provided use it, otherwise create a hash from default 'caregiver2025'
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH")
if not ADMIN_PASSWORD_HASH:
    # create a hash of the default password; in production set ADMIN_PASSWORD_HASH in env instead
    ADMIN_PASSWORD_HASH = generate_password_hash(os.getenv("ADMIN_PASSWORD", "caregiver2025"))

def is_logged_in():
    return session.get("logged_in") == True and session.get("username") == ADMIN_USERNAME

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        if username == ADMIN_USERNAME and check_password_hash(ADMIN_PASSWORD_HASH, password):
            session['logged_in'] = True
            session['username'] = username
            flash('Login successful.', 'success')
            return redirect(url_for('index'))
        else:
            flash('Invalid credentials', 'danger')
            return redirect(url_for('login'))
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# wrap-protect index and call_detail via simple check
_original_index = index
def _protected_index():
    if not is_logged_in():
        return redirect(url_for('login'))
    return _original_index()
app.view_functions['index'] = _protected_index

_original_call_detail = call_detail
def _protected_call_detail(call_id):
    if not is_logged_in():
        return redirect(url_for('login'))
    return _original_call_detail(call_id)
app.view_functions['call_detail'] = _protected_call_detail
scheduler.add_job(lambda: safe_scrape_job(), 'cron', day_of_week='sun', hour=7, minute=0, id='weekly_scrape')
scheduler.add_job(lambda: schedule_calls_job(), 'cron', day_of_week='mon,tue,wed,thu', hour=int(os.getenv("CALL_START_HOUR", 8)), minute=0, id='daily_calls')

# ----- MORNING 'AGENT LIVE' NOTIFICATION -----
MORNING_NOTIFY_HOUR = int(os.getenv('MORNING_NOTIFY_HOUR', 7))
MORNING_NOTIFY_MINUTE = int(os.getenv('MORNING_NOTIFY_MINUTE', 55))

def morning_agent_notification():
    # count today's scheduled targets approximate (queue length)
    queue = build_call_queue(limit=50)
    hospitals_count = sum(1 for h in queue if h.type and 'Hospital' in h.type or True)
    elderly_count = sum(1 for h in queue if h.type and 'Elderly' in (h.type or ''))
    # pick a short motivational line (random)
    motivators = [
        "Let's find new opportunities today! 💪",
        "Every call could be the one — go get it! 🌟",
        "Small steps lead to big changes — let's call! 🚀",
        "Positive energy today — new chances await! ✨"
    ]
    import random
    line = random.choice(motivators)
    text = (f"🌅 Good morning, Mark!\n🤖 Your AI Caregiver Agent is live and ready to start calling.\n"
            f"🕒 Next calls begin at {os.getenv('CALL_START_HOUR', '8')}:00 Oman time.\n"
            f"📞 Today's targets: approx {len(queue)} contacts.\n"
            f"{line}")
    send_telegram(text)

# schedule morning message Mon-Thu at specified time (5 min before calls by default)
scheduler.add_job(lambda: morning_agent_notification(), 'cron', day_of_week='mon,tue,wed,thu', hour=MORNING_NOTIFY_HOUR, minute=MORNING_NOTIFY_MINUTE, id='morning_notify')

scheduler.start()

def safe_scrape_job():
    try:
        results = run_scraper()
        added = 0
        for r in results:
            phone = r.get("phone")
            if not phone:
                continue
            phone_norm = phone.strip()
            if phone_norm.startswith("0"):
                phone_norm = "+968" + phone_norm[1:]
            existing = Hospital.query.filter_by(phone=phone_norm).first()
            if not existing:
                h = Hospital(name=r.get("name"), phone=phone_norm, city=r.get("city"), type=r.get("type"), verified=True)
                db.session.add(h)
                added += 1
        db.session.commit()
        send_telegram(f"🗂️ Scheduled Scrape complete — {len(results)} results, {added} added.")
    except Exception as e:
        print("Scheduled scrape error:", e)
        send_telegram(f"⚠️ Scheduled scrape failed: {e}")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
