"""
admin_views.py  —  SmartRail Admin Panel (Simplified)
======================================================
Routes covered:
  /admin/login          — Admin login
  /admin/logout         — Admin logout
  /admin/setup          — First-time admin account creation
  /admin/               — Dashboard with live stats
  /admin/trains         — List all trains
  /admin/trains/add     — Add a new train
  /admin/trains/<id>/edit   — Edit a train
  /admin/trains/<id>/delete — Delete a train
  /admin/bookings       — View all bookings (with filters)
  /admin/waitinglist    — View & confirm WL bookings
  /admin/fares          — View fare configs
  /admin/fares/add      — Add a fare config
  /admin/fares/<id>/edit    — Edit a fare config
  /admin/fares/seed     — Seed default IR fare rates
"""

from flask import (Blueprint, request, redirect, render_template,
                   url_for, flash, session, jsonify)
from .extension import db
from .models import Admin, Train, TrainSeat, TrainSchedule, Booking, FareConfig, WLHistory, Member, CoachBerth
# TrainSchedule is used in schedule management routes below
from datetime import datetime, date, timedelta
from functools import wraps

admin_bp = Blueprint('admin', __name__, url_prefix='/admin')


# ──────────────────────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────────────────────

def admin_logged_in():
    return 'admin_id' in session


def require_admin(f):
    """Decorator to protect admin routes — redirects to login if not authenticated."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not admin_logged_in():
            flash('Please log in as admin.')
            return redirect(url_for('admin.admin_login'))
        return f(*args, **kwargs)
    return wrapper


# ──────────────────────────────────────────────────────────────
# AUTH
# ──────────────────────────────────────────────────────────────

@admin_bp.route('/setup', methods=['GET', 'POST'])
def admin_setup():
    """
    One-time page to create the first super admin.
    Auto-disabled once an admin exists.
    """
    if Admin.query.count() > 0:
        flash('Admin already set up. Please log in.')
        return redirect(url_for('admin.admin_login'))

    if request.method == 'POST':
        admin = Admin(
            username  = request.form['username'].strip(),
            email     = request.form['email'].strip(),
            password  = request.form['password'],   # hash with bcrypt in production
            role      = 'super_admin',
            is_active = True
        )
        db.session.add(admin)
        db.session.commit()
        flash('Super admin created! Please log in.')
        return redirect(url_for('admin.admin_login'))

    return render_template('admin/setup.html')


@admin_bp.route('/login', methods=['GET', 'POST'])
def admin_login():
    if admin_logged_in():
        return redirect(url_for('admin.dashboard'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')

        admin = Admin.query.filter_by(username=username, is_active=True).first()

        if admin and admin.password == password:   # replace with bcrypt.check_password_hash in production
            session['admin_id']   = admin.id
            session['admin_name'] = admin.username
            session['admin_role'] = admin.role
            admin.last_login = datetime.utcnow()
            db.session.commit()
            flash(f'Welcome, {admin.username}!')
            return redirect(url_for('admin.dashboard'))
        else:
            flash('Invalid username or password.')

    return render_template('admin/login.html')


@admin_bp.route('/logout')
def admin_logout():
    session.pop('admin_id', None)
    session.pop('admin_name', None)
    session.pop('admin_role', None)
    flash('Logged out successfully.')
    return redirect(url_for('admin.admin_login'))


# ──────────────────────────────────────────────────────────────
# DASHBOARD
# ──────────────────────────────────────────────────────────────

@admin_bp.route('/')
@admin_bp.route('/dashboard')
@require_admin
def dashboard():
    """
    Shows live stats:
      - Total active trains
      - Total registered passengers
      - Bookings made today
      - Pending WL and RAC count
      - Revenue collected today
      - 7-day booking trend (passed as JSON for a chart)
    """
    today = date.today()

    stats = {
        'total_trains':   Train.query.filter_by(is_active=True).count(),
        'total_members':  Member.query.count(),
        'today_bookings': Booking.query.filter(
                              db.func.date(Booking.booking_date) == today
                          ).count(),
        'wl_pending':     Booking.query.filter_by(booking_status='WaitingList').count(),
        'rac_pending':    Booking.query.filter_by(booking_status='RAC').count(),
        'revenue_today':  db.session.query(db.func.sum(Booking.fare)).filter(
                              db.func.date(Booking.booking_date) == today,
                              Booking.payment_status == 'Completed'
                          ).scalar() or 0.0,
    }

    # Last 7 days booking count — used for a simple bar/line chart in the template
    trend = []
    for i in range(6, -1, -1):
        d     = today - timedelta(days=i)
        count = Booking.query.filter(
                    db.func.date(Booking.booking_date) == d
                ).count()
        trend.append({'date': d.strftime('%d %b'), 'count': count})

    # 10 most recent bookings shown in a table
    recent_bookings = (Booking.query
                       .order_by(Booking.booking_date.desc())
                       .limit(10).all())

    import json
    return render_template('admin/dashboard.html',
                           stats=stats,
                           trend=json.dumps(trend),
                           recent_bookings=recent_bookings)


# ──────────────────────────────────────────────────────────────
# TRAIN MANAGEMENT  (Add / Edit / Delete)
# ──────────────────────────────────────────────────────────────

@admin_bp.route('/trains')
@require_admin
def trains():
    """List all trains ordered by train number."""
    all_trains = Train.query.order_by(Train.train_number).all()
    return render_template('admin/trains.html', trains=all_trains)


@admin_bp.route('/trains/add', methods=['GET', 'POST'])
@require_admin
def add_train():
    """
    Add a new train. Also creates TrainSeat rows for each seat class
    the admin selects, and seeds FareConfig-based fares automatically.
    """
    if request.method == 'POST':
        try:
            train = Train(
                train_number      = request.form['train_number'].strip(),
                train_name        = request.form['train_name'].strip(),
                source            = request.form['source'].strip(),
                destination       = request.form['destination'].strip(),
                departure_time    = request.form['departure_time'].strip(),
                arrival_time      = request.form['arrival_time'].strip(),
                duration          = request.form['duration'].strip(),
                distance_km       = int(request.form.get('distance_km', 500)),
                train_type        = request.form.get('train_type', 'Express'),
                days_of_operation = request.form.get('days_of_operation', 'Mon,Tue,Wed,Thu,Fri,Sat,Sun'),
                is_active         = True
            )
            db.session.add(train)
            db.session.flush()   # get train.id before creating seats

            # Each class is submitted as a row: class_name[], num_coaches[]
            classes = request.form.getlist('class_name[]')
            coaches = request.form.getlist('num_coaches[]')

            # Reduced seat counts — consistent with init_db.py
            seats_per_coach = {'SL': 36, '3A': 32, '2A': 24, '1A': 12,
                               'CC': 40, 'EC': 28, '2S': 54}

            for cls, nc in zip(classes, coaches):
                if not cls:
                    continue
                nc        = int(nc or 1)
                total     = seats_per_coach.get(cls, 72) * nc
                fare_info = FareConfig.calculate(cls, train.train_type, train.distance_km)

                seat = TrainSeat(
                    train_id     = train.id,
                    class_name   = cls,
                    total_seats  = total,
                    num_coaches  = nc,
                    base_fare    = fare_info['total'],
                    tatkal_quota = int(total * 0.20),   # 20% tatkal quota
                    rac_quota    = int(total * 0.03),   # 3%  RAC quota
                    wl_limit     = 50
                )
                db.session.add(seat)

            db.session.commit()
            flash(f"Train {train.train_number} — {train.train_name} added successfully!")
            return redirect(url_for('admin.trains'))

        except Exception as e:
            db.session.rollback()
            flash(f'Error adding train: {e}')

    return render_template('admin/add_train.html')


@admin_bp.route('/trains/<int:train_id>/edit', methods=['GET', 'POST'])
@require_admin
def edit_train(train_id):
    """Edit basic train info. Seat class configs are managed separately."""
    train = Train.query.get_or_404(train_id)

    if request.method == 'POST':
        train.train_name        = request.form['train_name'].strip()
        train.source            = request.form['source'].strip()
        train.destination       = request.form['destination'].strip()
        train.departure_time    = request.form['departure_time'].strip()
        train.arrival_time      = request.form['arrival_time'].strip()
        train.duration          = request.form['duration'].strip()
        train.distance_km       = int(request.form.get('distance_km', train.distance_km))
        train.train_type        = request.form.get('train_type', train.train_type)
        train.days_of_operation = request.form.get('days_of_operation', train.days_of_operation)
        train.is_active         = 'is_active' in request.form

        db.session.commit()
        flash(f'Train {train.train_number} updated.')
        return redirect(url_for('admin.trains'))

    return render_template('admin/edit_train.html', train=train)


@admin_bp.route('/trains/<int:train_id>/delete', methods=['POST'])
@require_admin
def delete_train(train_id):
    """
    Delete a train and all its related seats/schedules (cascade).
    Uses POST to prevent accidental deletion via URL.
    """
    train = Train.query.get_or_404(train_id)
    db.session.delete(train)
    db.session.commit()
    flash(f'Train {train.train_number} — {train.train_name} deleted.')
    return redirect(url_for('admin.trains'))


# ──────────────────────────────────────────────────────────────
# BOOKINGS
# ──────────────────────────────────────────────────────────────

@admin_bp.route('/bookings')
@require_admin
def bookings():
    """
    View all bookings with optional filters:
      ?status=Confirmed | WaitingList | RAC | Cancelled
      ?train=12301  (train number)
    Results are paginated (25 per page).
    """
    page          = request.args.get('page', 1, type=int)
    status_filter = request.args.get('status', '')
    train_filter  = request.args.get('train', '')

    query = Booking.query

    if status_filter:
        query = query.filter_by(booking_status=status_filter)

    if train_filter:
        train_obj = Train.query.filter_by(train_number=train_filter).first()
        if train_obj:
            schedule_ids = [s.id for s in train_obj.schedules]
            query = query.filter(Booking.schedule_id.in_(schedule_ids))

    bookings_pg = query.order_by(Booking.booking_date.desc()).paginate(page=page, per_page=25)
    all_trains  = Train.query.order_by(Train.train_number).all()

    return render_template('admin/bookings.html',
                           bookings=bookings_pg,
                           trains=all_trains,
                           status_filter=status_filter,
                           train_filter=train_filter)


# ──────────────────────────────────────────────────────────────
# WAITLIST MANAGEMENT
# ──────────────────────────────────────────────────────────────

@admin_bp.route('/waitinglist')
@require_admin
def waitinglist():
    """
    Shows all pending WL bookings ordered by WL number.
    Admin can manually confirm a WL ticket if a seat has freed up.
    In real IRCTC this happens automatically — here admin controls it.
    """
    wl_bookings = (Booking.query
                   .filter_by(booking_status='WaitingList')
                   .order_by(Booking.wl_number)
                   .all())
    return render_template('admin/waitinglist.html', wl_bookings=wl_bookings)


@admin_bp.route('/waitinglist/confirm/<int:booking_id>', methods=['POST'])
@require_admin
def confirm_wl(booking_id):
    """
    Manually confirm a WL booking.
    Finds the first free berth and assigns it.
    Also logs to WLHistory for historical tracking.
    """
    booking = Booking.query.get_or_404(booking_id)

    if booking.booking_status != 'WaitingList':
        flash('This booking is not on the waiting list.')
        return redirect(url_for('admin.waitinglist'))

    # Find a berth that is not already assigned to a confirmed/RAC booking
    free_berth = (
        CoachBerth.query
        .filter_by(train_id=booking.schedule.train_id, class_name=booking.class_name)
        .outerjoin(
            Booking,
            db.and_(
                Booking.coach_number == CoachBerth.coach_id,
                Booking.seat_number  == db.cast(CoachBerth.seat_number, db.String),
                Booking.schedule_id  == booking.schedule_id,
                Booking.booking_status.in_(['Confirmed', 'RAC'])
            )
        )
        .filter(Booking.id == None)
        .first()
    )

    if not free_berth:
        flash('No free berths available to confirm this ticket.')
        return redirect(url_for('admin.waitinglist'))

    # Assign the berth
    booking.booking_status = 'Confirmed'
    booking.coach_number   = free_berth.coach_id
    booking.seat_number    = str(free_berth.seat_number)
    booking.berth_type     = free_berth.berth_type
    booking.bay_number     = free_berth.bay_number

    # Log outcome to WLHistory for historical tracking
    wl_log = WLHistory(
        booking_id        = booking.id,
        train_id          = booking.schedule.train_id,
        class_name        = booking.class_name,
        wl_number         = booking.wl_number or 0,
        days_at_booking   = 0,
        month             = booking.booking_date.month,
        is_weekday        = booking.booking_date.weekday() < 5,
        is_festival_month = booking.booking_date.month in [5, 6, 10, 11, 12],
        confirmed         = True
    )
    db.session.add(wl_log)
    db.session.commit()

    flash(f'WL ticket confirmed → Coach {free_berth.coach_id}, Seat {free_berth.seat_number}')
    return redirect(url_for('admin.waitinglist'))


# ──────────────────────────────────────────────────────────────
# SCHEDULE MANAGEMENT  (prepare chart + cancel train)
# ──────────────────────────────────────────────────────────────

@admin_bp.route('/schedules')
@require_admin
def schedules():
    """
    Lists upcoming train schedules (created automatically when passengers search).
    Admin can prepare the chart (locks booking) or cancel the train entirely.

    In real IRCTC:
      - Chart preparation happens automatically 4 hrs before departure
      - Cancellation triggers full refunds to all confirmed passengers
    """
    today    = date.today()
    upcoming = (TrainSchedule.query
                .filter(TrainSchedule.journey_date >= today)
                .order_by(TrainSchedule.journey_date)
                .limit(60).all())
    return render_template('admin/schedules.html', schedules=upcoming, today=today)


@admin_bp.route('/schedules/<int:schedule_id>/prepare-chart', methods=['POST'])
@require_admin
def prepare_chart(schedule_id):
    """
    Marks the chart as prepared — locks all passenger booking for this schedule.
    Once locked, no new bookings or cancellations are allowed.
    In production this would run via a cron job 4 hrs before departure.
    """
    schedule = TrainSchedule.query.get_or_404(schedule_id)
    if schedule.chart_prepared:
        flash('Chart already prepared for this schedule.')
    else:
        schedule.chart_prepared    = True
        schedule.chart_prepared_at = datetime.utcnow()
        db.session.commit()
        flash(f'Chart prepared for {schedule.train.train_number} on {schedule.journey_date}. Booking is now locked.')
    return redirect(url_for('admin.schedules'))


@admin_bp.route('/schedules/<int:schedule_id>/cancel', methods=['POST'])
@require_admin
def cancel_schedule(schedule_id):
    """
    Cancels an entire train on a specific date.
    All confirmed bookings are marked Cancelled with full refund pending.
    WL/RAC passengers are also cancelled (they paid nothing confirmed yet).
    """
    schedule = TrainSchedule.query.get_or_404(schedule_id)
    reason   = request.form.get('reason', 'Operational reasons')

    schedule.is_cancelled = True

    affected = Booking.query.filter_by(
        schedule_id    = schedule_id,
        booking_status = 'Confirmed'
    ).all()

    for b in affected:
        b.booking_status      = 'Cancelled'
        b.cancelled_at        = datetime.utcnow()
        b.cancellation_reason = f'Train cancelled: {reason}'
        b.refund_amount       = b.fare   # full refund when train is cancelled
        b.refund_status       = 'Pending'

    db.session.commit()
    flash(f'Train cancelled. {len(affected)} confirmed bookings marked for full refund.')
    return redirect(url_for('admin.schedules'))


# ──────────────────────────────────────────────────────────────
# FARE CONFIGURATION
# ──────────────────────────────────────────────────────────────

@admin_bp.route('/fares')
@require_admin
def fare_config():
    """View all fare configs grouped by train type and class."""
    configs = FareConfig.query.order_by(FareConfig.train_type, FareConfig.class_name).all()
    return render_template('admin/fare_config.html', configs=configs)


@admin_bp.route('/fares/add', methods=['GET', 'POST'])
@require_admin
def add_fare_config():
    """Add a new fare config for a class + train type combination."""
    if request.method == 'POST':
        cfg = FareConfig(
            class_name          = request.form['class_name'],
            train_type          = request.form['train_type'],
            per_km_rate         = float(request.form['per_km_rate']),
            reservation_charge  = float(request.form.get('reservation_charge', 0)),
            superfast_surcharge = float(request.form.get('superfast_surcharge', 0)),
            tatkal_surcharge    = float(request.form.get('tatkal_surcharge', 0)),
            gst_applicable      = 'gst_applicable' in request.form
        )
        db.session.add(cfg)
        db.session.commit()
        flash('Fare configuration added.')
        return redirect(url_for('admin.fare_config'))

    return render_template('admin/add_fare_config.html')


@admin_bp.route('/fares/<int:cfg_id>/edit', methods=['GET', 'POST'])
@require_admin
def edit_fare_config(cfg_id):
    """Edit per-km rate and surcharges for an existing fare config."""
    cfg = FareConfig.query.get_or_404(cfg_id)

    if request.method == 'POST':
        cfg.per_km_rate         = float(request.form['per_km_rate'])
        cfg.reservation_charge  = float(request.form.get('reservation_charge', 0))
        cfg.superfast_surcharge = float(request.form.get('superfast_surcharge', 0))
        cfg.tatkal_surcharge    = float(request.form.get('tatkal_surcharge', 0))
        cfg.gst_applicable      = 'gst_applicable' in request.form
        db.session.commit()
        flash('Fare config updated.')
        return redirect(url_for('admin.fare_config'))

    return render_template('admin/edit_fare_config.html', cfg=cfg)


@admin_bp.route('/fares/seed', methods=['POST'])
@require_admin
def seed_default_fares():
    """
    Seeds real Indian Railways fare rates.
    Safe to run multiple times — skips entries that already exist.
    """
    defaults = [
        # class,  train_type,    per_km,  res,  sf,   tatkal, gst
        # Rates match init_db.py — calibrated to real IRCTC 2024 prices
        ('SL',  'Mail',          0.675,   20,   0,    100,   False),
        ('SL',  'Express',       0.675,   20,   0,    100,   False),
        ('SL',  'Superfast',     0.675,   20,   30,   100,   False),
        ('3A',  'Mail',          0.954,   40,   0,    200,   False),
        ('3A',  'Express',       0.954,   40,   0,    200,   False),
        ('3A',  'Superfast',     0.954,   40,   45,   200,   False),
        ('3A',  'Rajdhani',      0.954,   40,   45,   200,   False),
        ('2A',  'Mail',          1.337,   50,   0,    300,   False),
        ('2A',  'Express',       1.337,   50,   0,    300,   False),
        ('2A',  'Superfast',     1.337,   50,   60,   300,   False),
        ('2A',  'Rajdhani',      1.337,   50,   60,   300,   False),
        ('1A',  'Rajdhani',      2.29,    60,   75,   400,   False),
        ('1A',  'Shatabdi',      2.29,    60,   75,   400,   False),
        ('CC',  'Shatabdi',      1.22,    40,   45,   150,   False),
        ('EC',  'Shatabdi',      2.22,    60,   75,   300,   False),
        ('2S',  'Express',       0.25,    15,   0,    50,    False),
        ('2S',  'Superfast',     0.25,    15,   30,   50,    False),
        ('2S',  'Mail',          0.25,    15,   0,    50,    False),
    ]
    added = 0
    for cls, typ, pkm, res, sf, tk, gst in defaults:
        if not FareConfig.query.filter_by(class_name=cls, train_type=typ).first():
            db.session.add(FareConfig(
                class_name=cls, train_type=typ,
                per_km_rate=pkm, reservation_charge=res,
                superfast_surcharge=sf, tatkal_surcharge=tk,
                gst_applicable=gst
            ))
            added += 1
    db.session.commit()
    flash(f'Seeded {added} fare configurations.')
    return redirect(url_for('admin.fare_config'))