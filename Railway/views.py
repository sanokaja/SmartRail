"""
views.py  —  Passenger-facing routes

  • confirm_booking   : uses FareConfig.calculate() for real fare breakdown
                        assigns berths from CoachBerth table
                        issues real WL numbers; logs WLHistory on cancellation
  • cancel_ticket     : real refund policy via _compute_refund()
                        auto-processes WL queue after cancellation
  • search_trains     : checks chart_prepared flag (no booking after chart locked)
  • tatkal_quota      : enforced at booking time via TrainSeat.tatkal_quota
  • waitlist_predictor: rule-based estimator using IRCTC demand patterns
"""

from flask import Blueprint, request, redirect, render_template, url_for, flash, session, jsonify
from .extension import db
from .models import (Member, Train, TrainSeat, TrainSchedule, SeatAvailability,
                     Booking, CoachBerth, FareConfig, WLHistory)
from datetime import datetime, timedelta, date
import random
import string
import smtplib
import os
import uuid
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

main = Blueprint("main", __name__)

# ================================================================
# WL PREDICTION — Random Forest with rule-based fallback
# ================================================================
import pickle, pathlib

_WL_MODEL      = None
_WL_MODEL_PATH = pathlib.Path(__file__).parent / 'wl_model.pkl'
_CLASS_MAP     = {'SL':0,'3A':1,'2A':2,'1A':3,'CC':4,'EC':5,'2S':6}


def _load_wl_model():
    global _WL_MODEL
    if _WL_MODEL is None and _WL_MODEL_PATH.exists():
        try:
            with open(_WL_MODEL_PATH, 'rb') as f:
                _WL_MODEL = pickle.load(f)
        except Exception as e:
            print(f"Model load error: {e}")
    return _WL_MODEL


def _predict_wl(wl_number, days_until, class_name,
                month=6, is_weekday=True, is_festival=False):
    """
    WL confirmation predictor.
    Uses RandomForest if wl_model.pkl present, else rule-based.
    """
    from .models import WLHistory
    total_rows = WLHistory.query.count()
    prob       = None

    # Try ML model
    if _WL_MODEL_PATH.exists() and total_rows >= 50:
        try:
            md = _load_wl_model()
            if md:
                wl_bkt  = min(6, [5,10,20,30,40,60,999].index(
                              next(v for v in [5,10,20,30,40,60,999]
                                   if v >= max(1, wl_number))))
                day_bkt = min(5, [7,15,30,45,60,999].index(
                              next(v for v in [7,15,30,45,60,999]
                                   if v >= max(1, days_until))))
                feats = [[wl_number, days_until, month,
                          int(is_weekday), int(is_festival),
                          _CLASS_MAP.get(class_name, 1),
                          wl_bkt, day_bkt]]
                prob = float(md['model'].predict_proba(feats)[0][1])
        except Exception as e:
            print(f"ML predict error: {e}")
            prob = None

    # Rule-based fallback
    if prob is None:
        if wl_number <= 10:   base = 0.88
        elif wl_number <= 20: base = 0.65
        elif wl_number <= 30: base = 0.42
        elif wl_number <= 40: base = 0.22
        elif wl_number <= 50: base = 0.10
        else:                 base = 0.04
        base += (days_until / 30) * 0.18
        base += {'SL':0.12,'3A':0.0,'2A':-0.08,'1A':-0.15}.get(class_name, 0)
        if is_festival: base -= 0.10
        if is_weekday:  base += 0.05
        prob = max(0.02, min(0.97, base))
        model_used = 'RuleBased'
    else:
        model_used = 'RandomForest'

    probability = round(prob * 100, 1)

    if probability >= 75:
        action = 'HOLD YOUR TICKET'
        advice = 'Strong chance of confirmation. Avoid cancelling. Check again 48 hrs before journey.'
        status = 'high'
    elif probability >= 55:
        action = 'BOOK A BACKUP'
        advice = "Decent chance but not guaranteed. Book an alternate as backup. Wait until 48hr window before cancelling."
        status = 'medium'
    elif probability >= 35:
        action = 'BOOK ALTERNATE NOW'
        advice = 'Unlikely to confirm. Book alternate immediately. Cancel before 48hr mark for 75% refund.'
        status = 'low'
    else:
        action = 'CANCEL NOW'
        advice = 'Very unlikely to confirm. Cancel immediately to maximise your refund.'
        status = 'very_low'

    return {
        'probability':  probability,
        'status':       status,
        'action':       action,
        'advice':       advice,
        'will_confirm': probability >= 55,
        'model_used':   model_used,
        'trained_on':   total_rows,
    }


# ==================== EMAIL CONFIGURATION ====================
from dotenv import load_dotenv
load_dotenv()

EMAIL_HOST     = 'smtp.gmail.com'
EMAIL_PORT     = 587
EMAIL_ADDRESS  = os.environ.get('FLASK_EMAIL_ADDRESS', '')
EMAIL_PASSWORD = os.environ.get('FLASK_EMAIL_PASSWORD', '')

TWILIO_ACCOUNT_SID  = os.environ.get('FLASK_TWILIO_ACCOUNT_SID', '')
TWILIO_AUTH_TOKEN   = os.environ.get('FLASK_TWILIO_AUTH_TOKEN', '')
TWILIO_PHONE_NUMBER = os.environ.get('FLASK_TWILIO_PHONE_NUMBER', '')


# ==================== HELPERS ====================

def generate_otp(length=6):
    return ''.join(random.choices(string.digits, k=length))

def send_email(to_email, subject, body):
    if not EMAIL_ADDRESS or not EMAIL_PASSWORD:
        print("ERROR: Email credentials not configured in .env")
        return False
    try:
        msg = MIMEMultipart()
        msg['From'] = EMAIL_ADDRESS
        msg['To']   = to_email
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'html'))
        server = smtplib.SMTP(EMAIL_HOST, EMAIL_PORT)
        server.starttls()
        server.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
        server.send_message(msg)
        server.quit()
        return True
    except Exception as e:
        print(f"Error sending email: {e}")
        return False

def send_sms(mobile, message):
    try:
        from twilio.rest import Client
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        client.messages.create(body=message, from_=TWILIO_PHONE_NUMBER, to=f'+91{mobile}')
        return True
    except Exception as e:
        print(f"Error sending SMS: {e}")
        return False

def send_verification_email(email, otp):
    subject = "Railway Booking - Email Verification"
    body = f"""<html><body><div style="font-family:Arial;max-width:600px;margin:0 auto;padding:20px">
        <h2>Email Verification</h2>
        <p>Your OTP:</p>
        <div style="background:#f4f4f4;padding:20px;text-align:center;font-size:32px;
                    font-weight:bold;letter-spacing:5px;border-radius:5px;margin:20px 0">{otp}</div>
        <p>Valid for 10 minutes.</p></div></body></html>"""
    return send_email(email, subject, body)

def send_password_reset_email(email, otp):
    subject = "Railway Booking - Password Reset"
    body = f"""<html><body><div style="font-family:Arial;max-width:600px;margin:0 auto;padding:20px">
        <h2>Password Reset</h2>
        <p>Your OTP:</p>
        <div style="background:#f4f4f4;padding:20px;text-align:center;font-size:32px;
                    font-weight:bold;letter-spacing:5px;border-radius:5px;margin:20px 0">{otp}</div>
        <p>Valid for 10 minutes. If you didn't request this, ignore.</p></div></body></html>"""
    return send_email(email, subject, body)

def send_booking_confirmation_email(email, ticket_data):
    subject = f"Booking Confirmation - PNR {ticket_data['pnr']}"
    passengers_html = ""
    for idx, p in enumerate(ticket_data['passengers'], 1):
        passengers_html += f"""<tr>
            <td style="padding:10px;border:1px solid #ddd">{idx}</td>
            <td style="padding:10px;border:1px solid #ddd">{p['name']}</td>
            <td style="padding:10px;border:1px solid #ddd">{p['age']}</td>
            <td style="padding:10px;border:1px solid #ddd">{p['gender']}</td>
            <td style="padding:10px;border:1px solid #ddd">{p['coach']}</td>
            <td style="padding:10px;border:1px solid #ddd">{p['seat']}</td>
            <td style="padding:10px;border:1px solid #ddd">{p['berth']}</td></tr>"""

    status_color = '#4caf50' if ticket_data['booking_status'] == 'Confirmed' else '#ff9800'

    # Fare breakdown section
    fare_html = f"""
        <h3 style="color:#333">Fare Breakdown</h3>
        <table style="width:100%;border-collapse:collapse">
          <tr><td style="padding:8px;border:1px solid #ddd">Base Fare</td>
              <td style="padding:8px;border:1px solid #ddd;text-align:right">₹{ticket_data.get('base_fare',0):.2f}</td></tr>
          <tr><td style="padding:8px;border:1px solid #ddd">Reservation Charge</td>
              <td style="padding:8px;border:1px solid #ddd;text-align:right">₹{ticket_data.get('reservation_charge',0):.2f}</td></tr>
          {'<tr><td style="padding:8px;border:1px solid #ddd">Superfast Surcharge</td><td style="padding:8px;border:1px solid #ddd;text-align:right">₹'+str(ticket_data.get('superfast_surcharge',0))+'</td></tr>' if ticket_data.get('superfast_surcharge',0) > 0 else ''}
          {'<tr><td style="padding:8px;border:1px solid #ddd">Tatkal Surcharge</td><td style="padding:8px;border:1px solid #ddd;text-align:right">₹'+str(ticket_data.get('tatkal_surcharge',0))+'</td></tr>' if ticket_data.get('tatkal_surcharge',0) > 0 else ''}
          {'<tr><td style="padding:8px;border:1px solid #ddd">GST (5%)</td><td style="padding:8px;border:1px solid #ddd;text-align:right">₹'+str(ticket_data.get('gst',0))+'</td></tr>' if ticket_data.get('gst',0) > 0 else ''}
          <tr style="font-weight:bold;background:#f8f9ff"><td style="padding:8px;border:1px solid #ddd">Total</td>
              <td style="padding:8px;border:1px solid #ddd;text-align:right">₹{ticket_data.get('total_fare',0):.2f}</td></tr>
        </table>"""

    body = f"""<html><body style="font-family:Arial;background:#f4f4f4">
    <div style="max-width:700px;margin:20px auto;background:white;padding:30px;border-radius:10px">
      <div style="background:linear-gradient(135deg,#667eea,#764ba2);color:white;padding:20px;text-align:center;border-radius:5px">
        <h1 style="margin:0">🚂 Railway Reservation</h1></div>
      <div style="text-align:center;margin:15px 0">
        <span style="display:inline-block;padding:8px 20px;background:{status_color};color:white;border-radius:20px;font-weight:bold">
          {ticket_data['booking_status'].upper()}</span></div>
      <div style="background:#f8f9ff;padding:15px;text-align:center;font-size:24px;font-weight:bold;
                  color:#667eea;letter-spacing:2px;border-radius:5px;border:2px dashed #667eea;margin:20px 0">
        PNR: {ticket_data['pnr']}</div>
      <div style="background:#f8f9ff;padding:20px;border-radius:5px;margin:20px 0">
        <div style="display:flex;justify-content:space-between;font-size:18px;font-weight:bold">
          <span>{ticket_data['source']}</span><span style="color:#667eea">→</span><span>{ticket_data['destination']}</span></div>
        <div style="text-align:center;color:#666;margin-top:10px">
          <strong>{ticket_data['train_name']}</strong> ({ticket_data['train_number']})</div>
      </div>
      <h3>Passenger Details</h3>
      <table style="width:100%;border-collapse:collapse">
        <thead><tr style="background:#667eea;color:white">
          <th style="padding:12px">S.No</th><th style="padding:12px">Name</th>
          <th style="padding:12px">Age</th><th style="padding:12px">Gender</th>
          <th style="padding:12px">Coach</th><th style="padding:12px">Seat</th>
          <th style="padding:12px">Berth</th></tr></thead>
        <tbody>{passengers_html}</tbody></table>
      {fare_html}
      <div style="margin-top:30px;padding-top:20px;border-top:2px dashed #ddd;text-align:center;color:#666;font-size:12px">
        <p>• Carry valid ID proof • Arrive 30 min before departure • Keep PNR for reference</p></div>
    </div></body></html>"""
    return send_email(email, subject, body)

def generate_pnr():
    while True:
        pnr = str(uuid.uuid4().int % 10_000_000_000).zfill(10)
        if not Booking.query.filter_by(pnr_number=pnr).first():
            return pnr

def is_logged_in():
    return 'user_id' in session

def _compute_refund(booking, cancel_date):
    """Real IRCTC refund policy."""
    journey = datetime.combine(booking.schedule.journey_date, datetime.min.time())
    hours_left = (journey - cancel_date).total_seconds() / 3600
    flat = {'SL': 30, '3A': 240, '2A': 300, '1A': 360}
    charge = flat.get(booking.class_name, 60)
    if hours_left > 48:
        refund = booking.fare - charge
    elif hours_left > 24:
        refund = booking.fare * 0.75
    elif hours_left > 4:
        refund = booking.fare * 0.50
    else:
        refund = 0.0
    return round(max(0.0, refund), 2)


# ── Coach layout (for routes that don't use CoachBerth DB yet) ──
_BERTH_ORDER = {
    'SL': ['LB','MB','UB','LB','MB','UB','SLB','SUB'],
    '3A': ['LB','MB','UB','LB','MB','UB','SLB','SUB'],
    '2A': ['LB','UB','SLB','SUB'],
    '1A': ['LB','UB'],
}
_COACH_IDS = {
    'SL': [f'S{i}' for i in range(1,10)],
    '3A': [f'B{i}' for i in range(1,7)],
    '2A': [f'A{i}' for i in range(1,5)],
    '1A': [f'H{i}' for i in range(1,3)],
}
_SEATS_PER_COACH = {'SL':72,'3A':64,'2A':48,'1A':24}

def _get_berth(class_name, seat_index):
    order = _BERTH_ORDER.get(class_name, _BERTH_ORDER['SL'])
    return order[seat_index % len(order)]



# ==================== OTP ROUTES ====================

@main.route('/api/send-email-otp', methods=['POST'])
def send_email_otp():
    email = request.json.get('email')
    if not email:
        return jsonify({'success': False, 'message': 'Email is required'}), 400
    if Member.query.filter_by(email=email).first():
        return jsonify({'success': False, 'message': 'Email already registered'}), 400
    otp = generate_otp()
    session[f'email_otp_{email}']      = otp
    session[f'email_otp_time_{email}'] = datetime.now().isoformat()
    if send_verification_email(email, otp):
        return jsonify({'success': True, 'message': f'OTP sent to {email}'})
    return jsonify({'success': False, 'message': 'Failed to send OTP'}), 500

@main.route('/api/verify-email-otp', methods=['POST'])
def verify_email_otp():
    email = request.json.get('email')
    otp   = request.json.get('otp')
    stored_otp  = session.get(f'email_otp_{email}')
    otp_time    = session.get(f'email_otp_time_{email}')
    if not stored_otp or not otp_time:
        return jsonify({'success': False, 'message': 'OTP not found'}), 400
    if datetime.now() - datetime.fromisoformat(otp_time) > timedelta(minutes=10):
        return jsonify({'success': False, 'message': 'OTP expired'}), 400
    if otp == stored_otp:
        session[f'email_verified_{email}'] = True
        return jsonify({'success': True, 'message': 'Email verified'})
    return jsonify({'success': False, 'message': 'Invalid OTP'}), 400

@main.route('/api/send-mobile-otp', methods=['POST'])
def send_mobile_otp():
    mobile = request.json.get('mobile')
    if not mobile or len(mobile) != 10:
        return jsonify({'success': False, 'message': 'Valid 10-digit number required'}), 400
    otp = generate_otp()
    session[f'mobile_otp_{mobile}']      = otp
    session[f'mobile_otp_time_{mobile}'] = datetime.now().isoformat()
    if send_sms(mobile, f"Railway Booking OTP: {otp}. Valid 10 min."):
        return jsonify({'success': True, 'message': f'OTP sent to {mobile}'})
    return jsonify({'success': False, 'message': 'Failed to send OTP'}), 500

@main.route('/api/verify-mobile-otp', methods=['POST'])
def verify_mobile_otp():
    mobile = request.json.get('mobile')
    otp    = request.json.get('otp')
    stored_otp = session.get(f'mobile_otp_{mobile}')
    otp_time   = session.get(f'mobile_otp_time_{mobile}')
    if not stored_otp or not otp_time:
        return jsonify({'success': False, 'message': 'OTP not found'}), 400
    if datetime.now() - datetime.fromisoformat(otp_time) > timedelta(minutes=10):
        return jsonify({'success': False, 'message': 'OTP expired'}), 400
    if otp == stored_otp:
        session[f'mobile_verified_{mobile}'] = True
        return jsonify({'success': True, 'message': 'Mobile verified'})
    return jsonify({'success': False, 'message': 'Invalid OTP'}), 400


# ==================== MAIN ROUTES ====================

@main.route('/')
def index():
    return redirect(url_for('main.land') if is_logged_in() else url_for('main.login'))

@main.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user = Member.query.filter_by(
            first_name=request.form['username'],
            password=request.form['password']
        ).first()
        if user:
            if not user.email_verified:
                flash("Please verify your email first.")
                return redirect(url_for('main.login'))
            session['user_id']  = user.id
            session['username'] = user.first_name
            session['email']    = user.email
            flash(f"Welcome back, {user.first_name}!")
            return redirect(url_for('main.land'))
        flash("Invalid Username or Password")
    return render_template('login.html')

@main.route('/logout')
def logout():
    session.clear()
    flash("Logged out successfully")
    return redirect(url_for('main.login'))

@main.route('/land')
def land():
    if not is_logged_in():
        return redirect(url_for('main.login'))
    return render_template("land.html")

@main.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        first_name     = request.form.get('first_name')
        middle_name    = request.form.get('middle_name', '')
        last_name      = request.form.get('last_name')
        email          = request.form.get('email')
        password       = request.form.get('password')
        contact        = request.form.get('contact')
        email_verified = request.form.get('email_verified') == 'true'
        mobile_verified= request.form.get('mobile_verified') == 'true'

        if not email_verified or not mobile_verified:
            flash("Please verify both email and mobile number")
            return redirect(url_for('main.register'))

        if Member.query.filter_by(email=email).first():
            flash("Email already registered. Please login.")
            return redirect(url_for('main.login'))

        try:
            member = Member(
                first_name=first_name, middle_name=middle_name,
                last_name=last_name, email=email, password=password,
                contact=contact, email_verified=True, mobile_verified=True
            )
            db.session.add(member)
            db.session.commit()
            flash("Registration successful! Please login.")
            return redirect(url_for("main.login"))
        except Exception as e:
            db.session.rollback()
            flash(f"Error creating account: {e}")
    return render_template("register.html")

@main.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        if 'send_otp' in request.form:
            email = request.form.get('email')
            user  = Member.query.filter_by(email=email).first()
            if user:
                otp = generate_otp()
                session['reset_email']    = email
                session['reset_otp']      = otp
                session['reset_otp_time'] = datetime.now().isoformat()
                send_password_reset_email(email, otp)
            flash("If an account exists, you will receive an OTP")
            return render_template('forgot_password.html', show_otp=True)

        elif 'verify_otp' in request.form:
            stored  = session.get('reset_otp')
            otp_time= session.get('reset_otp_time')
            if not stored or datetime.now() - datetime.fromisoformat(otp_time) > timedelta(minutes=10):
                flash("OTP expired.")
                return redirect(url_for('main.forgot_password'))
            if request.form.get('otp') == stored:
                session['otp_verified'] = True
                return render_template('forgot_password.html', show_reset=True)
            flash("Invalid OTP.")
            return render_template('forgot_password.html', show_otp=True)

        elif 'reset_password' in request.form:
            if not session.get('otp_verified'):
                flash("Verify OTP first.")
                return redirect(url_for('main.forgot_password'))
            pw1 = request.form.get('password')
            pw2 = request.form.get('confirm_password')
            if pw1 != pw2:
                flash("Passwords don't match.")
                return render_template('forgot_password.html', show_reset=True)
            user = Member.query.filter_by(email=session.get('reset_email')).first()
            if user:
                user.password = pw1
                db.session.commit()
            flash("Password reset successful!")
            return redirect(url_for('main.login'))

    return render_template('forgot_password.html')


@main.route('/search-trains', methods=['POST'])
def search_trains():
    if not is_logged_in():
        return redirect(url_for('main.login'))

    source      = request.form.get('source', '').strip()
    destination = request.form.get('destination', '').strip()
    date_str    = request.form.get('date', '').strip()

    if not source or not destination or not date_str:
        flash("Please fill in all fields")
        return redirect(url_for('main.land'))

    try:
        journey_date   = datetime.strptime(date_str, '%Y-%m-%d').date()
        formatted_date = journey_date.strftime('%B %d, %Y')
    except Exception:
        flash("Invalid date format")
        return redirect(url_for('main.land'))

    trains = Train.query.filter(
        db.func.lower(Train.source)      == source.lower(),
        db.func.lower(Train.destination) == destination.lower(),
        Train.is_active == True
    ).all()

    if not trains:
        flash(f"No trains found from '{source}' to '{destination}'")
        return redirect(url_for('main.land'))

    session['search_source']         = source
    session['search_destination']    = destination
    session['search_date']           = date_str
    session['search_formatted_date'] = formatted_date

    trains_data = []
    for train in trains:
        schedule = TrainSchedule.query.filter_by(
            train_id=train.id, journey_date=journey_date
        ).first()

        if not schedule:
            schedule = TrainSchedule(train_id=train.id, journey_date=journey_date)
            db.session.add(schedule)
            db.session.flush()
            for seat in train.seats:
                db.session.add(SeatAvailability(
                    schedule_id      = schedule.id,
                    class_name       = seat.class_name,
                    available_seats  = seat.total_seats,
                    tatkal_available = seat.tatkal_quota,
                    rac_available    = seat.rac_quota,
                    wl_count         = 0
                ))
            db.session.commit()

        seat_data = []
        for seat in train.seats:
            avail = SeatAvailability.query.filter_by(
                schedule_id=schedule.id, class_name=seat.class_name
            ).first()
            available = avail.available_seats if avail else seat.total_seats
            wl_count  = avail.wl_count if avail else 0

            # Compute real fare
            fare_info = FareConfig.calculate(seat.class_name, train.train_type, train.distance_km)

            if available > 0:
                status = 'limited' if available <= seat.total_seats * 0.2 else 'available'
            elif wl_count < seat.wl_limit:
                status = 'waitlist'
            else:
                status = 'unavailable'
             
            seat_data.append({
                'class_name': seat.class_name,
                'available':  available,
                'wl_count':   wl_count,
                'price':      fare_info['total'],
                'fare_breakdown': fare_info,
                'status':     status,
                'schedule_id': schedule.id
            })

        train_status = 'available'
        if all(s['status'] == 'unavailable' for s in seat_data):
            train_status = 'unavailable'
        elif all(s['status'] in ('unavailable', 'waitlist') for s in seat_data):
            train_status = 'waitlist'

        # Check chart prepared (no booking after chart lock)
        chart_locked = schedule.chart_prepared

        trains_data.append({
            'name':           train.train_name,
            'number':         train.train_number,
            'departure_time': train.departure_time,
            'arrival_time':   train.arrival_time,
            'duration':       train.duration,
            'distance_km':    train.distance_km,
            'train_type':     train.train_type,
            'status':         train_status,
            'chart_locked':   chart_locked,
            'seats':          seat_data
        })

    return render_template('train_results.html',
                           trains=trains_data, source=source,
                           destination=destination, date=formatted_date,
                           search_date=date_str)


@main.route('/book')
def book_ticket():
    if not is_logged_in():
        return redirect(url_for('main.login'))

    train_number = request.args.get('train')
    class_name   = request.args.get('class')
    schedule_id  = request.args.get('schedule_id')

    if not all([train_number, class_name, schedule_id]):
        flash("Invalid booking request")
        return redirect(url_for('main.land'))

    train    = Train.query.filter_by(train_number=train_number).first()
    schedule = db.session.get(TrainSchedule, int(schedule_id))

    if not train or not schedule:
        flash("Train or schedule not found")
        return redirect(url_for('main.land'))

    if schedule.chart_prepared:
        flash("Chart has been prepared for this train. Booking is now closed.")
        return redirect(url_for('main.land'))

    seat = TrainSeat.query.filter_by(train_id=train.id, class_name=class_name).first()
    if not seat:
        flash("Seat class not available")
        return redirect(url_for('main.land'))

    # Real fare breakdown
    fare_info = FareConfig.calculate(class_name, train.train_type, train.distance_km)

    return render_template('booking.html',
                           train=train, class_name=class_name,
                           base_fare=fare_info['total'],
                           fare_breakdown=fare_info,
                           source=session.get('search_source', ''),
                           destination=session.get('search_destination', ''),
                           date=session.get('search_formatted_date', ''),
                           journey_date=session.get('search_date', ''),
                           schedule_id=schedule_id)


@main.route('/confirm-booking', methods=['POST'])
def confirm_booking():
    if not is_logged_in():
        return redirect(url_for('main.login'))

    train_number     = request.form.get('train_number')
    class_name       = request.form.get('class_name')
    journey_date_str = request.form.get('journey_date')
    schedule_id_raw  = request.form.get('schedule_id')
    is_tatkal        = request.form.get('is_tatkal', '0') == '1'
    quota_type       = request.form.get('quota_type', 'General')

    try:
        schedule_id  = int(schedule_id_raw)
        journey_date = datetime.strptime(journey_date_str, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        flash("Invalid booking data")
        return redirect(url_for('main.land'))

    train    = Train.query.filter_by(train_number=train_number).first()
    schedule = db.session.get(TrainSchedule, schedule_id)

    if not train or not schedule:
        flash("Train or schedule not found")
        return redirect(url_for('main.land'))

    if schedule.chart_prepared:
        flash("Chart has been prepared. Booking is closed.")
        return redirect(url_for('main.land'))

    seat = TrainSeat.query.filter_by(train_id=train.id, class_name=class_name).first()
    if not seat:
        flash("Seat class not available")
        return redirect(url_for('main.land'))

    # Collect passengers
    passengers = []
    i = 1
    while f'passenger_name_{i}' in request.form:
        name   = request.form.get(f'passenger_name_{i}')
        age    = request.form.get(f'passenger_age_{i}')
        gender = request.form.get(f'passenger_gender_{i}')
        if name and age and gender:
            passengers.append({'name': name, 'age': int(age), 'gender': gender})
        i += 1

    if not passengers:
        flash("Please add at least one passenger")
        return redirect(url_for('main.land'))

    # Tatkal: only allow D-1 at 10:00 AM or after
    if is_tatkal:
        now       = datetime.now()
        open_time = datetime.combine(journey_date - timedelta(days=1),
                                     datetime.min.time().replace(hour=10))
        if now < open_time:
            flash(f"Tatkal booking opens at 10:00 AM on {open_time.strftime('%d %b %Y')}.")
            return redirect(url_for('main.land'))

    # Real fare calculation
    fare_info = FareConfig.calculate(class_name, train.train_type,
                                     train.distance_km, is_tatkal=is_tatkal)
    per_passenger_fare = fare_info['total']

    availability = SeatAvailability.query.filter_by(
        schedule_id=schedule_id, class_name=class_name
    ).first()
    if not availability:
        availability = SeatAvailability(
            schedule_id     = schedule_id,
            class_name      = class_name,
            available_seats = seat.total_seats,
            tatkal_available= seat.tatkal_quota,
            rac_available   = seat.rac_quota,
            wl_count        = 0
        )
        db.session.add(availability)
        db.session.flush()

    # Determine booking status
    if is_tatkal:
        if availability.tatkal_available >= len(passengers):
            booking_status = 'Confirmed'
        else:
            flash("No Tatkal seats available.")
            return redirect(url_for('main.land'))
    elif availability.available_seats >= len(passengers):
        booking_status = 'Confirmed'
    elif availability.wl_count < seat.wl_limit:
        booking_status = 'WaitingList'
    else:
        flash("No seats available. WL limit reached.")
        return redirect(url_for('main.land'))

    pnr = generate_pnr()

    try:
        current_wl = availability.wl_count

        for idx, passenger in enumerate(passengers):
            if booking_status == 'Confirmed':
                # Allocate from CoachBerth
                existing_booked = Booking.query.filter_by(
                    schedule_id=schedule_id,
                    class_name=class_name,
                    booking_status='Confirmed'
                ).all()
                booked_pairs = set()
                for b in existing_booked:
                    if b.coach_number and b.seat_number:
                        try:
                            booked_pairs.add((b.coach_number, int(b.seat_number)))
                        except ValueError:
                            pass

                free = (CoachBerth.query
                        .filter_by(train_id=train.id, class_name=class_name)
                        .all())
                chosen = next(
                    (b for b in free if (b.coach_id, b.seat_number) not in booked_pairs),
                    None
                )
                if chosen:
                    coach_num = chosen.coach_id
                    seat_num  = str(chosen.seat_number)
                    berth     = chosen.berth_type
                    bay_num   = chosen.bay_number
                else:
                    total_conf = Booking.query.filter_by(
                        schedule_id=schedule_id, class_name=class_name,
                        booking_status='Confirmed'
                    ).count()
                    coach_num = f"{class_name[0]}1"
                    seat_num  = str(total_conf + idx + 1)
                    berth     = _get_berth(class_name, total_conf + idx)
                    bay_num   = None

            elif booking_status == 'WaitingList':
                current_wl += 1
                coach_num = seat_num = berth = 'WL'
                bay_num   = None
            

            booking = Booking(
                pnr_number          = pnr,
                member_id           = session['user_id'],
                schedule_id         = schedule_id,
                passenger_name      = passenger['name'],
                passenger_age       = passenger['age'],
                passenger_gender    = passenger['gender'],
                class_name          = class_name,
                seat_number         = seat_num,
                coach_number        = coach_num,
                berth_type          = berth,
                bay_number          = bay_num,
                quota_type          = 'Tatkal' if is_tatkal else quota_type,
                base_fare           = fare_info['base_fare'],
                reservation_charge  = fare_info['reservation_charge'],
                superfast_surcharge = fare_info['superfast_surcharge'],
                tatkal_surcharge    = fare_info['tatkal_surcharge'],
                gst                 = fare_info['gst'],
                fare                = per_passenger_fare,
                booking_status      = booking_status,
                payment_status      = 'Pending',
                wl_number           = current_wl if booking_status == 'WaitingList' else None
            )
            db.session.add(booking)

            # Log to WLHistory if WL (confirmed=False; updated later when confirmed)
            if booking_status == 'WaitingList':
                days_until = (journey_date - datetime.now().date()).days
                wl_log = WLHistory(
                    train_id          = train.id,
                    class_name        = class_name,
                    wl_number         = current_wl,
                    days_at_booking   = days_until,
                    month             = journey_date.month,
                    is_weekday        = journey_date.weekday() < 5,
                    is_festival_month = journey_date.month in [5,6,10,11,12],
                    confirmed         = False
                )
                db.session.add(wl_log)

        # Update availability
        if booking_status == 'Confirmed':
            availability.available_seats -= len(passengers)
            if is_tatkal:
                availability.tatkal_available -= len(passengers)
        elif booking_status == 'WaitingList':
            availability.wl_count = current_wl
        

        db.session.commit()

    except Exception as e:
        db.session.rollback()
        import traceback; traceback.print_exc()
        flash(f"Error creating booking: {e}")
        return redirect(url_for('main.land'))

    booking_summary = {
        'train_name':       train.train_name,
        'source':           session.get('search_source', 'N/A'),
        'destination':      session.get('search_destination', 'N/A'),
        'date':             session.get('search_formatted_date', journey_date.strftime('%B %d, %Y')),
        'class_name':       class_name,
        'passenger_count':  len(passengers),
        'base_fare':        fare_info['base_fare'] * len(passengers),
        'reservation_charge': fare_info['reservation_charge'] * len(passengers),
        'superfast_surcharge': fare_info['superfast_surcharge'] * len(passengers),
        'tatkal_surcharge': fare_info['tatkal_surcharge'] * len(passengers),
        'gst':              fare_info['gst'] * len(passengers),
        'total_amount':     per_passenger_fare * len(passengers),
        'booking_status':   booking_status,
        'wl_number':        current_wl if booking_status == 'WaitingList' else None
    }

    if booking_status == 'WaitingList':
        days_until  = (journey_date - datetime.now().date()).days
        wl_pred     = _predict_wl(
            wl_number   = current_wl,
            days_until  = days_until,
            class_name  = class_name,
            month       = journey_date.month,
            is_weekday  = journey_date.weekday() < 5,
            is_festival = journey_date.month in [5, 6, 10, 11, 12]
        )
        total_fare               = per_passenger_fare * len(passengers)
        flat                     = {'SL':30,'3A':240,'2A':300,'1A':360}.get(class_name, 60)
        wl_pred['refund_now']    = round(max(0, total_fare - flat), 2)
        wl_pred['refund_48hr']   = round(total_fare * 0.75, 2)
        wl_pred['refund_24hr']   = round(total_fare * 0.50, 2)
        booking_summary['wl_prediction'] = wl_pred

    return render_template('payment.html', pnr_number=pnr, booking=booking_summary)


@main.route('/process-payment', methods=['POST'])
def process_payment():
    if not is_logged_in():
        return redirect(url_for('main.login'))

    pnr_number = request.form.get('pnr_number')
    bookings   = Booking.query.filter_by(pnr_number=pnr_number).all()

    if not bookings:
        flash("Booking not found")
        return redirect(url_for('main.land'))

    for b in bookings:
        b.payment_status = 'Completed'
    db.session.commit()

    first      = bookings[0]
    schedule   = first.schedule
    train      = schedule.train
    user       = db.session.get(Member, session['user_id'])

    ticket_data = {
        'pnr':               pnr_number,
        'train_name':        train.train_name,
        'train_number':      train.train_number,
        'source':            train.source,
        'destination':       train.destination,
        'departure_time':    train.departure_time,
        'arrival_time':      train.arrival_time,
        'journey_date':      schedule.journey_date.strftime('%B %d, %Y'),
        'class_name':        first.class_name,
        'booking_status':    first.booking_status,
        'booking_date':      first.booking_date.strftime('%B %d, %Y %I:%M %p'),
        'base_fare':         sum(b.base_fare for b in bookings),
        'reservation_charge':sum(b.reservation_charge for b in bookings),
        'superfast_surcharge':sum(b.superfast_surcharge for b in bookings),
        'tatkal_surcharge':  sum(b.tatkal_surcharge for b in bookings),
        'gst':               sum(b.gst for b in bookings),
        'total_fare':        sum(b.fare for b in bookings),
        'passengers': [{
            'name':   b.passenger_name,
            'age':    b.passenger_age,
            'gender': b.passenger_gender,
            'coach':  b.coach_number or 'WL',
            'seat':   b.seat_number or 'WL',
            'berth':  b.berth_type or 'WL'
        } for b in bookings]
    }

    try:
        send_booking_confirmation_email(user.email, ticket_data)
        flash(f"Payment successful! Confirmation sent to {user.email}. PNR: {pnr_number}")
    except Exception as e:
        flash(f"Payment successful! PNR: {pnr_number} (Email failed)")

    return redirect(url_for('main.view_ticket', pnr=pnr_number))


@main.route('/view-ticket/<pnr>')
def view_ticket(pnr):
    if not is_logged_in():
        return redirect(url_for('main.login'))

    bookings = Booking.query.filter_by(pnr_number=pnr, member_id=session['user_id']).all()
    if not bookings:
        flash("Ticket not found")
        return redirect(url_for('main.land'))

    first  = bookings[0]
    sched  = first.schedule
    train  = sched.train

    ticket = {
        'pnr':             pnr,
        'train_name':      train.train_name,
        'train_number':    train.train_number,
        'source':          train.source,
        'destination':     train.destination,
        'departure_time':  train.departure_time,
        'arrival_time':    train.arrival_time,
        'journey_date':    sched.journey_date.strftime('%B %d, %Y'),
        'journey_date_raw':sched.journey_date,
        'class_name':      first.class_name,
        'booking_status':  first.booking_status,
        'payment_status':  first.payment_status,
        'booking_date':    first.booking_date.strftime('%B %d, %Y %I:%M %p'),
        'wl_number':       first.wl_number,
        'quota_type':      first.quota_type,
        'base_fare':       sum(b.base_fare for b in bookings),
        'reservation_charge': sum(b.reservation_charge for b in bookings),
        'superfast_surcharge': sum(b.superfast_surcharge for b in bookings),
        'tatkal_surcharge': sum(b.tatkal_surcharge for b in bookings),
        'gst':             sum(b.gst for b in bookings),
        'total_fare':      sum(b.fare for b in bookings),
        'passengers': [{
            'name':   b.passenger_name,
            'age':    b.passenger_age,
            'gender': b.passenger_gender,
            'coach':  b.coach_number or 'TBA',
            'seat':   b.seat_number or 'TBA',
            'berth':  b.berth_type or 'TBA'
        } for b in bookings]
    }

    return render_template('view_ticket.html', ticket=ticket, now=datetime.now())


@main.route('/my-tickets')
def my_tickets():
    if not is_logged_in():
        return redirect(url_for('main.login'))

    bookings = Booking.query.filter_by(
        member_id=session['user_id']
    ).order_by(Booking.booking_date.desc()).all()

    tickets = {}
    for b in bookings:
        if b.pnr_number not in tickets:
            sched = b.schedule
            train = sched.train
            tickets[b.pnr_number] = {
                'pnr':             b.pnr_number,
                'train_name':      train.train_name,
                'train_number':    train.train_number,
                'source':          train.source,
                'destination':     train.destination,
                'journey_date':    sched.journey_date,
                'class_name':      b.class_name,
                'status':          b.booking_status,
                'booking_date':    b.booking_date,
                'passenger_count': 1,
                'wl_number':       b.wl_number
            }
        else:
            tickets[b.pnr_number]['passenger_count'] += 1

    return render_template('my_tickets.html', tickets=list(tickets.values()), now=datetime.now())


@main.route('/profile')
def profile():
    if not is_logged_in():
        return redirect(url_for('main.login'))
    user = db.session.get(Member, session['user_id'])
    total_bookings = db.session.query(Booking.pnr_number).filter_by(
        member_id=session['user_id']
    ).distinct().count()
    return render_template('profile.html', user=user, total_bookings=total_bookings)


@main.route('/cancel-ticket/<pnr>', methods=['POST'])
def cancel_ticket(pnr):
    if not is_logged_in():
        return jsonify({'success': False, 'message': 'Please login first'}), 401

    bookings = Booking.query.filter_by(pnr_number=pnr, member_id=session['user_id']).all()
    if not bookings:
        return jsonify({'success': False, 'message': 'Ticket not found'}), 404

    if bookings[0].booking_status == 'Cancelled':
        return jsonify({'success': False, 'message': 'Already cancelled'}), 400

    schedule = bookings[0].schedule
    if schedule.journey_date < datetime.now().date():
        return jsonify({'success': False, 'message': 'Cannot cancel past journey'}), 400

    if schedule.chart_prepared:
        return jsonify({'success': False, 'message': 'Chart prepared — cancellation at station only'}), 400

    try:
        cancel_time   = datetime.now()
        total_refund  = 0.0

        for b in bookings:
            refund            = _compute_refund(b, cancel_time)
            b.booking_status  = 'Cancelled'
            b.cancelled_at    = cancel_time
            b.refund_amount   = refund
            b.refund_status   = 'Pending'
            total_refund     += refund

            # Log confirmed=False WL outcome
            if b.booking_status == 'WaitingList':
                wl_log = WLHistory.query.filter_by(booking_id=b.id).first()
                if wl_log:
                    wl_log.confirmed = False

        # Restore availability
        avail = SeatAvailability.query.filter_by(
            schedule_id=bookings[0].schedule_id,
            class_name=bookings[0].class_name
        ).first()
        if avail:
            avail.available_seats += len(bookings)

        db.session.commit()

        # Auto-process WL queue
        _auto_process_wl(bookings[0].schedule_id, bookings[0].class_name)

        return jsonify({
            'success': True,
            'message': f'Ticket {pnr} cancelled. Refund ₹{total_refund:.2f} in 5-7 days.'
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500


def _auto_process_wl(schedule_id, class_name):
    """Move WL → Confirmed when seats free up."""
    schedule = db.session.get(TrainSchedule, schedule_id)
    if not schedule:
        return

    wl_queue = (Booking.query
                .filter_by(schedule_id=schedule_id, class_name=class_name,
                           booking_status='WaitingList')
                .order_by(Booking.wl_number)
                .all())

    for booking in wl_queue:
        free_berth = (CoachBerth.query
                      .filter_by(train_id=schedule.train_id, class_name=class_name)
                      .outerjoin(
                          Booking,
                          db.and_(
                              Booking.coach_number == CoachBerth.coach_id,
                              Booking.seat_number  == db.cast(CoachBerth.seat_number, db.String),
                              Booking.schedule_id  == schedule_id,
                              Booking.booking_status.in_(['Confirmed', 'RAC'])
                          )
                      )
                      .filter(Booking.id == None)
                      .first())

        if not free_berth:
            break

        booking.booking_status = 'Confirmed'
        booking.coach_number   = free_berth.coach_id
        booking.seat_number    = str(free_berth.seat_number)
        booking.berth_type     = free_berth.berth_type
        booking.bay_number     = free_berth.bay_number

        wl_log = WLHistory.query.filter_by(
            booking_id=booking.id
        ).order_by(WLHistory.recorded_at.desc()).first()
        if wl_log:
            wl_log.confirmed = True

    db.session.commit()


@main.route('/delete-ticket/<pnr>', methods=['POST'])
def delete_ticket(pnr):
    if not is_logged_in():
        return jsonify({'success': False, 'message': 'Please login first'}), 401

    bookings = Booking.query.filter_by(pnr_number=pnr, member_id=session['user_id']).all()
    if not bookings:
        return jsonify({'success': False, 'message': 'Ticket not found'}), 404

    schedule = bookings[0].schedule
    if (schedule.journey_date >= datetime.now().date()
            and bookings[0].booking_status == 'Confirmed'):
        return jsonify({'success': False, 'message': 'Cancel first before deleting'}), 400

    try:
        for b in bookings:
            db.session.delete(b)
        db.session.commit()
        return jsonify({'success': True, 'message': f'Ticket {pnr} deleted.'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500


# ================================================================
@main.route('/waitlist-predictor', methods=['GET', 'POST'])
def waitlist_predictor():
    if not is_logged_in():
        return redirect(url_for('main.login'))

    prediction = None
    if request.method == 'POST':
        wl_number        = int(request.form.get('wl_number', 1))
        train_number     = request.form.get('train_number', '')
        journey_date_str = request.form.get('journey_date', '')
        class_name       = request.form.get('class_name', 'SL')

        journey_date = datetime.strptime(journey_date_str, '%Y-%m-%d').date()
        days_until   = (journey_date - datetime.now().date()).days

        result = _predict_wl(
            wl_number  = wl_number,
            days_until = days_until,
            class_name = class_name,
            month      = journey_date.month,
            is_weekday = journey_date.weekday() < 5,
            is_festival= journey_date.month in [5, 6, 10, 11, 12]
        )
        prediction = {
            **result,
            'wl_number':    wl_number,
            'train_number': train_number,
            'journey_date': journey_date_str,
            'class_name':   class_name,
            'days_until':   days_until
        }

        # Show real historical confirmation rate from WLHistory DB
        if class_name:
            total = WLHistory.query.filter_by(class_name=class_name).count()
            if total > 10:
                confirmed = WLHistory.query.filter_by(
                    class_name=class_name, confirmed=True
                ).filter(WLHistory.wl_number <= wl_number).count()
                wl_total  = WLHistory.query.filter_by(
                    class_name=class_name
                ).filter(WLHistory.wl_number <= wl_number).count()
                if wl_total > 0:
                    prediction['historical_rate'] = round(confirmed / wl_total * 100, 1)
                    prediction['historical_sample'] = wl_total

    return render_template('waitlist_predictor.html', prediction=prediction)


@main.route('/api/fare-preview', methods=['POST'])
def fare_preview():
    """AJAX endpoint — returns real fare breakdown before booking."""
    data       = request.get_json()
    class_name = data.get('class_name', 'SL')
    train_id   = data.get('train_id')
    is_tatkal  = bool(data.get('is_tatkal', False))
    passengers = int(data.get('passengers', 1))

    train = db.session.get(Train, int(train_id)) if train_id else None
    if not train:
        return jsonify({'error': 'Train not found'}), 404

    fare_info = FareConfig.calculate(class_name, train.train_type,
                                     train.distance_km, is_tatkal=is_tatkal)
    return jsonify({
        'per_passenger': fare_info,
        'total': {k: round(v * passengers, 2) for k, v in fare_info.items()}
    })
# ================================================================
# WL ALERT SCHEDULER — paste this at the END of your views.py
# ================================================================

def _send_wl_alerts(app):
    with app.app_context():
        now   = datetime.now()
        today = now.date()
        wl_bookings = (Booking.query
                       .filter_by(booking_status='WaitingList', payment_status='Completed')
                       .join(TrainSchedule, Booking.schedule_id == TrainSchedule.id)
                       .filter(TrainSchedule.journey_date >= today)
                       .all())
        for booking in wl_bookings:
            member   = db.session.get(Member, booking.member_id)
            schedule = booking.schedule
            train    = schedule.train
            if not member or not schedule or not train:
                continue
            journey_dt = datetime.combine(schedule.journey_date, datetime.min.time())
            days_left  = (schedule.journey_date - today).days
            hours_left = (journey_dt - now).total_seconds() / 3600
            pred = _predict_wl(
                wl_number  = booking.wl_number or 1,
                days_until = days_left,
                class_name = booking.class_name,
                month      = schedule.journey_date.month,
                is_weekday = schedule.journey_date.weekday() < 5,
                is_festival= schedule.journey_date.month in [5,6,10,11,12]
            )
            prob    = pred['probability']
            action  = pred['action']
            flat    = {'SL':30,'3A':240,'2A':300,'1A':360}.get(booking.class_name, 60)
            refund75= round(booking.fare * 0.75, 2)
            refund  = round(max(0, booking.fare - flat), 2)

            if 4 < days_left <= 5:
                msg  = (f"Your WL/{booking.wl_number} on {train.train_name} "
                        f"({train.train_number}) — {schedule.journey_date.strftime('%d %b %Y')}.\n"
                        f"Confirmation chance: {prob}%.\n{action}\nPNR: {booking.pnr_number}")
                subj = f"WL Update — {train.train_number} on {schedule.journey_date.strftime('%d %b')}"
                send_email(member.email, subj, f"<pre>{msg}</pre>")
                if member.contact: send_sms(member.contact, msg)

            elif 44 <= hours_left <= 50:
                msg  = (f"URGENT: 48hrs left. WL/{booking.wl_number} on {train.train_name} "
                        f"({schedule.journey_date.strftime('%d %b %Y')}).\n"
                        f"Confirmation chance: {prob}%.\nLast chance 75% refund: Rs.{refund75}.\n"
                        f"{action}\nPNR: {booking.pnr_number}")
                subj = f"URGENT WL Alert — {train.train_number} 48hrs Left"
                send_email(member.email, subj, f"<pre>{msg}</pre>")
                if member.contact: send_sms(member.contact, msg)

            elif schedule.chart_prepared and hours_left > 0:
                if booking.booking_status == 'WaitingList':
                    msg  = (f"Chart prepared. WL/{booking.wl_number} on {train.train_name} — NOT CONFIRMED.\n"
                            f"Refund Rs.{refund} in 5-7 days.\nPNR: {booking.pnr_number}")
                    subj = f"WL Not Confirmed — {train.train_number}"
                elif booking.booking_status == 'Confirmed':
                    msg  = (f"Your WL ticket CONFIRMED on {train.train_name} ({train.train_number}).\n"
                            f"Coach: {booking.coach_number}  Seat: {booking.seat_number}  "
                            f"Berth: {booking.berth_type}\nPNR: {booking.pnr_number}")
                    subj = f"WL Confirmed! — {train.train_number}"
                else:
                    continue
                send_email(member.email, subj, f"<pre>{msg}</pre>")
                if member.contact: send_sms(member.contact, msg)


def start_wl_scheduler(app):
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        scheduler = BackgroundScheduler()
        scheduler.add_job(_send_wl_alerts, 'interval', hours=6,
                          args=[app], id='wl_alerts', replace_existing=True)
        scheduler.start()
        print("✓ WL Alert Scheduler started (runs every 6 hours)")
    except ImportError:
        print("⚠ APScheduler not installed — run: pip install apscheduler")
    except Exception as e:
        print(f"⚠ Scheduler error: {e}")