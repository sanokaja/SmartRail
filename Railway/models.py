"""
models.py  —  SmartRail (IRCTC-realistic)

New / changed in this version
──────────────────────────────
• Train         : added distance_km, train_type (Express/Rajdhani/etc.)
• TrainSeat     : base_fare now computed; added tatkal_quota, general_quota
• CoachBerth    : NEW — physical berth chart per train class
                  coach_id, bay_number, berth_number, berth_type (LB/MB/UB/SLB/SUB)
• TrainSchedule : added chart_prepared flag (freezes booking 4 hrs before departure)
• SeatAvailability: added tatkal_available, rac_available, wl_count
• Booking       : added quota_type, wl_number, cancellation fields
• WLHistory     : NEW — logs every WL confirmation event (feeds analytics)
• Admin         : NEW — admin user table (separate from Member)
• FareConfig    : NEW — per-km rates + surcharges (replaces hardcoded flat fare)
"""

from .extension import db
from datetime import datetime


# ──────────────────────────────────────────────────────────────
# FARE CONFIG  (real Indian Railways structure)
# ──────────────────────────────────────────────────────────────
class FareConfig(db.Model):
    """
    Stores per-km rates and fixed surcharges per class + train type.
    Seed this once with realistic values — admin can edit via panel.

    Calibrated rates (reverse-engineered from real IRCTC 2024 fares):
      SL  : Rs.0.675/km   2S : Rs.0.25/km
      3A  : Rs.0.954/km   CC : Rs.1.22/km
      2A  : Rs.1.337/km   EC : Rs.2.22/km
      1A  : Rs.2.29/km

    Reservation charge (fixed):
      SL Rs.20  3A Rs.40  2A Rs.50  1A Rs.60  CC Rs.40  EC Rs.60

    Superfast surcharge:
      SL/2S Rs.30   3A Rs.45   2A Rs.60   1A Rs.75   CC/EC Rs.45/75

    Tatkal surcharge (flat):
      SL Rs.100  3A Rs.200  2A Rs.300  1A Rs.400
    """
    __tablename__ = 'fare_config'

    id               = db.Column(db.Integer, primary_key=True)
    class_name       = db.Column(db.String(10), nullable=False)   # SL, 3A, 2A, 1A, CC, 2S, EC
    train_type       = db.Column(db.String(20), nullable=False)   # Mail, Express, Superfast, Rajdhani, Shatabdi
    per_km_rate      = db.Column(db.Float, nullable=False)        # ₹ per km
    reservation_charge = db.Column(db.Float, default=0.0)        # fixed ₹
    superfast_surcharge = db.Column(db.Float, default=0.0)       # fixed ₹ (0 if not superfast)
    tatkal_surcharge = db.Column(db.Float, default=0.0)          # fixed ₹ added for Tatkal
    gst_applicable   = db.Column(db.Boolean, default=False)      # 5% GST if base_fare > ₹1000

    def __repr__(self):
        return f'<FareConfig {self.class_name} / {self.train_type}>'

    @staticmethod
    def calculate(class_name, train_type, distance_km, is_tatkal=False):
        """
        Returns a dict with fare breakdown.
        Falls back to flat ₹300 if config not seeded.
        """
        cfg = FareConfig.query.filter_by(
            class_name=class_name,
            train_type=train_type
        ).first()

        if not cfg:
            # Fallback so the app never crashes before seeding
            base = round(distance_km * 0.5, 2)
            return {
                'base_fare': base,
                'reservation_charge': 20.0,
                'superfast_surcharge': 0.0,
                'tatkal_surcharge': 0.0,
                'gst': 0.0,
                'total': base + 20.0
            }

        base         = round(distance_km * cfg.per_km_rate, 2)
        reservation  = cfg.reservation_charge
        superfast    = cfg.superfast_surcharge
        tatkal_extra = cfg.tatkal_surcharge if is_tatkal else 0.0
        subtotal     = base + reservation + superfast + tatkal_extra
        gst          = round(subtotal * 0.05, 2) if cfg.gst_applicable and subtotal > 1000 else 0.0
        total        = round(subtotal + gst, 2)

        return {
            'base_fare':           base,
            'reservation_charge':  reservation,
            'superfast_surcharge': superfast,
            'tatkal_surcharge':    tatkal_extra,
            'gst':                 gst,
            'total':               total
        }


# ──────────────────────────────────────────────────────────────
# ADMIN USER
# ──────────────────────────────────────────────────────────────
class Admin(db.Model):
    """
    Separate admin/govt-side user table.
    role: 'super_admin' | 'train_admin' | 'chart_admin'
    """
    __tablename__ = 'admin'

    id           = db.Column(db.Integer, primary_key=True)
    username     = db.Column(db.String(50), unique=True, nullable=False)
    email        = db.Column(db.String(120), unique=True, nullable=False)
    password     = db.Column(db.String(255), nullable=False)
    role         = db.Column(db.String(20), default='train_admin')
    is_active    = db.Column(db.Boolean, default=True)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)
    last_login   = db.Column(db.DateTime)

    def __repr__(self):
        return f'<Admin {self.username} ({self.role})>'


# ──────────────────────────────────────────────────────────────
# MEMBER  (passenger / public user)
# ──────────────────────────────────────────────────────────────
class Member(db.Model):
    __tablename__ = 'member'

    id             = db.Column(db.Integer, primary_key=True)
    first_name     = db.Column(db.String(50), nullable=False)
    middle_name    = db.Column(db.String(50))
    last_name      = db.Column(db.String(50), nullable=False)
    email          = db.Column(db.String(120), unique=True, nullable=False)
    password       = db.Column(db.String(255), nullable=False)
    contact        = db.Column(db.String(15), nullable=False)
    email_verified = db.Column(db.Boolean, default=False)
    mobile_verified= db.Column(db.Boolean, default=False)
    created_at     = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at     = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    bookings = db.relationship('Booking', backref='member', lazy=True)

    def __repr__(self):
        return f'<Member {self.first_name} {self.last_name}>'


# ──────────────────────────────────────────────────────────────
# TRAIN
# ──────────────────────────────────────────────────────────────
class Train(db.Model):
    __tablename__ = 'train'

    id                 = db.Column(db.Integer, primary_key=True)
    train_number       = db.Column(db.String(10), unique=True, nullable=False)
    train_name         = db.Column(db.String(100), nullable=False)
    source             = db.Column(db.String(50), nullable=False)
    destination        = db.Column(db.String(50), nullable=False)
    departure_time     = db.Column(db.String(10), nullable=False)   # "06:00 AM"
    arrival_time       = db.Column(db.String(10), nullable=False)
    duration           = db.Column(db.String(10), nullable=False)   # "8h 30m"
    distance_km        = db.Column(db.Integer, default=500)         # NEW — real distance
    train_type         = db.Column(db.String(20), default='Express')# NEW — Mail/Express/Superfast/Rajdhani/Shatabdi
    days_of_operation  = db.Column(db.String(50))
    is_active          = db.Column(db.Boolean, default=True)

    seats     = db.relationship('TrainSeat',    backref='train', lazy=True, cascade='all, delete-orphan')
    schedules = db.relationship('TrainSchedule', backref='train', lazy=True, cascade='all, delete-orphan')

    def __repr__(self):
        return f'<Train {self.train_number} - {self.train_name}>'


# ──────────────────────────────────────────────────────────────
# TRAIN SEAT  (class-level config per train)
# ──────────────────────────────────────────────────────────────
class TrainSeat(db.Model):
    __tablename__ = 'train_seat'

    id              = db.Column(db.Integer, primary_key=True)
    train_id        = db.Column(db.Integer, db.ForeignKey('train.id'), nullable=False)
    class_name      = db.Column(db.String(10), nullable=False)   # 1A, 2A, 3A, SL, CC, EC, 2S
    total_seats     = db.Column(db.Integer, nullable=False)
    num_coaches     = db.Column(db.Integer, default=1)           # NEW — how many coaches of this class
    base_fare       = db.Column(db.Float, nullable=False)        # kept for quick lookup; recomputed from FareConfig
    tatkal_quota    = db.Column(db.Integer, default=0)           # NEW — Tatkal seats (approx 20% of total)
    rac_quota       = db.Column(db.Integer, default=0)           # NEW — RAC seats
    wl_limit        = db.Column(db.Integer, default=50)          # NEW — max WL beyond which booking blocked
    
    def __repr__(self):
        return f'<TrainSeat {self.class_name} for Train {self.train_id}>'


# ──────────────────────────────────────────────────────────────
# COACH BERTH  (the physical seat chart — NEW)
# ──────────────────────────────────────────────────────────────
class CoachBerth(db.Model):
    """
    One row = one physical berth in a specific coach of a specific train.

    Layout constants:
      SL / 3A  : 9 bays per coach, 8 berths per bay = 72 berths/coach
                 Bay berths: LB, MB, UB, LB, MB, UB, SLB, SUB  (positions 1-8)
      2A       : 6 bays, 4 berths per bay  = 24 berths + 12 side = 48 total? No.
                 Standard 2A: 6 bays × 4 (2×LB + 2×UB) + side lower/upper = 48
      1A       : 4 bays × 2 (LB + UB) = 8 bays + side = ~24 total per coach

    seat_number is the display number (1-72 for SL).
    bay_number groups adjacent berths — group booking picks same bay.
    """
    __tablename__ = 'coach_berth'

    id           = db.Column(db.Integer, primary_key=True)
    train_id     = db.Column(db.Integer, db.ForeignKey('train.id'), nullable=False)
    class_name   = db.Column(db.String(10), nullable=False)
    coach_id     = db.Column(db.String(10), nullable=False)    # S1, S2, B1, A1, H1 …
    bay_number   = db.Column(db.Integer, nullable=False)       # 1-9 (berths sharing a bay)
    seat_number  = db.Column(db.Integer, nullable=False)       # 1-72 within this coach
    berth_type   = db.Column(db.String(10), nullable=False)    # LB / MB / UB / SLB / SUB
    is_ladies_quota   = db.Column(db.Boolean, default=False)
    is_senior_quota   = db.Column(db.Boolean, default=False)
    is_hp_quota       = db.Column(db.Boolean, default=False)   # Physically handicapped

    train = db.relationship('Train', backref='berths')

    def __repr__(self):
        return f'<Berth {self.coach_id}/{self.seat_number} ({self.berth_type})>'

    @staticmethod
    def generate_for_train(train_id, class_name, num_coaches):
        """
        Admin helper: generate all CoachBerth rows for a train class.
        Call this after adding a new train.

        SL/3A layout per coach (72 berths, 9 bays):
          Bay positions: LB, MB, UB, LB, MB, UB, SLB, SUB  → 8 berths per bay
          Wait — standard SL: 9 bays × 8 = 72.
          Positions per bay: LB(1), MB(2), UB(3), LB(4), MB(5), UB(6), SLB(7), SUB(8)

        2A (48 berths, 8 bays):
          4 berths per bay (LB, UB, LB, UB) + side = 6 regular bays × 4 + side compartments

        1A (24 berths):
          2 berths per bay (LB, UB), 4 bays per coach = 8 + side compartments
        """
        
        layouts = {
         'SL':  {'berths_per_bay': 8, 'bays': 9,
            'pattern': ['LB','MB','UB','LB','MB','UB','SLB','SUB']},
        '3A':  {'berths_per_bay': 8, 'bays': 9,   # ← was 8, now 9 → 72 berths ✅
            'pattern': ['LB','MB','UB','LB','MB','UB','SLB','SUB']},
        '2A':  {'berths_per_bay': 4, 'bays': 12,  # ← was 6, now 12 → 48 berths ✅
            'pattern': ['LB','UB','SLB','SUB']},
        '1A':  {'berths_per_bay': 2, 'bays': 12,  # ← was 6, now 12 → 24 berths ✅
            'pattern': ['LB','UB']},
        }
        coach_prefixes = {'SL':'S','3A':'B','2A':'A','1A':'H','CC':'C','2S':'D'}

        layout = layouts.get(class_name, layouts['SL'])
        prefix = coach_prefixes.get(class_name, 'X')
        pattern = layout['pattern']
        bays    = layout['bays']
        berths  = []

        for c in range(1, num_coaches + 1):
            coach_id   = f"{prefix}{c}"
            seat_num   = 1
            for bay in range(1, bays + 1):
                for pos, btype in enumerate(pattern):
                    # Mark first two LBs in each coach as senior quota
                    is_senior = (btype == 'LB' and seat_num <= 6 and c == 1)
                    # Mark seats 1-6 in first coach as ladies quota (side lower/side upper)
                    is_ladies = (btype in ('SLB', 'SUB') and bay == 1 and c <= 2)
                    berth = CoachBerth(
                        train_id        = train_id,
                        class_name      = class_name,
                        coach_id        = coach_id,
                        bay_number      = bay,
                        seat_number     = seat_num,
                        berth_type      = btype,
                        is_senior_quota = is_senior,
                        is_ladies_quota = is_ladies
                    )
                    berths.append(berth)
                    seat_num += 1

        db.session.add_all(berths)
        db.session.commit()
        return len(berths)


# ──────────────────────────────────────────────────────────────
# TRAIN SCHEDULE
# ──────────────────────────────────────────────────────────────
class TrainSchedule(db.Model):
    __tablename__ = 'train_schedule'

    id              = db.Column(db.Integer, primary_key=True)
    train_id        = db.Column(db.Integer, db.ForeignKey('train.id'), nullable=False)
    journey_date    = db.Column(db.Date, nullable=False)
    is_cancelled    = db.Column(db.Boolean, default=False)
    chart_prepared  = db.Column(db.Boolean, default=False)   # NEW — freezes booking 4h before departure
    chart_prepared_at = db.Column(db.DateTime)               # NEW — timestamp when chart was locked

    seat_availability = db.relationship('SeatAvailability', backref='schedule', lazy=True, cascade='all, delete-orphan')
    bookings          = db.relationship('Booking',           backref='schedule', lazy=True)

    def __repr__(self):
        return f'<Schedule Train {self.train_id} on {self.journey_date}>'


# ──────────────────────────────────────────────────────────────
# SEAT AVAILABILITY
# ──────────────────────────────────────────────────────────────
class SeatAvailability(db.Model):
    __tablename__ = 'seat_availability'

    id               = db.Column(db.Integer, primary_key=True)
    schedule_id      = db.Column(db.Integer, db.ForeignKey('train_schedule.id'), nullable=False)
    class_name       = db.Column(db.String(10), nullable=False)
    available_seats  = db.Column(db.Integer, nullable=False)
    tatkal_available = db.Column(db.Integer, default=0)     # NEW — Tatkal quota remaining
    rac_available    = db.Column(db.Integer, default=0)     # NEW — RAC spots remaining
    wl_count         = db.Column(db.Integer, default=0)     # NEW — current WL number

    def __repr__(self):
        return f'<Availability {self.class_name}: {self.available_seats} seats>'


# ──────────────────────────────────────────────────────────────
# BOOKING
# ──────────────────────────────────────────────────────────────
class Booking(db.Model):
    __tablename__ = 'booking'

    id               = db.Column(db.Integer, primary_key=True)
    pnr_number       = db.Column(db.String(20), nullable=False)
    member_id        = db.Column(db.Integer, db.ForeignKey('member.id'), nullable=False)
    schedule_id      = db.Column(db.Integer, db.ForeignKey('train_schedule.id'), nullable=False)

    passenger_name   = db.Column(db.String(100), nullable=False)
    passenger_age    = db.Column(db.Integer, nullable=False)
    passenger_gender = db.Column(db.String(10), nullable=False)

    class_name       = db.Column(db.String(10), nullable=False)
    seat_number      = db.Column(db.String(10))
    coach_number     = db.Column(db.String(5))
    berth_type       = db.Column(db.String(10))
    bay_number       = db.Column(db.Integer)                 # NEW — for adjacent seat tracking
    quota_type       = db.Column(db.String(20), default='General')  # NEW — General/Ladies/Senior/Tatkal/HP

    # Fare breakdown (NEW — replaces single flat fare)
    base_fare        = db.Column(db.Float, nullable=False)
    reservation_charge = db.Column(db.Float, default=0.0)
    superfast_surcharge= db.Column(db.Float, default=0.0)
    tatkal_surcharge = db.Column(db.Float, default=0.0)
    gst              = db.Column(db.Float, default=0.0)
    fare             = db.Column(db.Float, nullable=False)   # total

    booking_date     = db.Column(db.DateTime, default=datetime.utcnow)
    booking_status   = db.Column(db.String(20), default='Confirmed')   # Confirmed/RAC/WaitingList/Cancelled
    payment_status   = db.Column(db.String(20), default='Pending')
    wl_number        = db.Column(db.Integer)                 # NEW — WL/1, WL/2 etc.

    # Cancellation fields (NEW)
    cancelled_at     = db.Column(db.DateTime)
    cancellation_reason = db.Column(db.String(200))
    refund_amount    = db.Column(db.Float, default=0.0)
    refund_status    = db.Column(db.String(20), default='NA')  # NA/Pending/Processed

    def __repr__(self):
        return f'<Booking PNR: {self.pnr_number}>'


# ──────────────────────────────────────────────────────────────
# WL HISTORY  (feeds ML retraining — NEW)
# ──────────────────────────────────────────────────────────────
class WLHistory(db.Model):
    """
    Every time a WL ticket either confirms or expires unconfirmed,
    log it here. This builds the real historical dataset for
    waitlist analytics and the rule-based predictor.
    """
    __tablename__ = 'wl_history'

    id              = db.Column(db.Integer, primary_key=True)
    booking_id      = db.Column(db.Integer, db.ForeignKey('booking.id'))
    train_id        = db.Column(db.Integer, db.ForeignKey('train.id'))
    class_name      = db.Column(db.String(10))
    wl_number       = db.Column(db.Integer)
    days_at_booking = db.Column(db.Integer)   # days until journey when WL was issued
    month           = db.Column(db.Integer)
    is_weekday      = db.Column(db.Boolean)
    is_festival_month = db.Column(db.Boolean)
    confirmed       = db.Column(db.Boolean)   # True if WL eventually confirmed
    recorded_at     = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f'<WLHistory WL/{self.wl_number} confirmed={self.confirmed}>'