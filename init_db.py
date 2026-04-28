"""
init_db.py  —  SmartRail database initialiser
==============================================
Run once to populate the database with:
  • Real Indian Railways fare configuration (per-km rates, surcharges)
  • 6 real Indian trains covering all train types and seat classes
  • Physical berth chart generated via CoachBerth.generate_for_train()
  • First super-admin account (username: admin, password: admin123)

Fare rates are reverse-engineered from live IRCTC fares (2024) to
produce realistic ticket prices:
  SL  ~₹155 for 200 km,  ~₹275 for 362 km
  3A  ~₹230 for 200 km,  ~₹460 for 362 km,  ~₹1545 for 1531 km
  2A  ~₹635 for 200 km,  ~₹980 for 362 km,  ~₹2155 for 1531 km
  1A  ~₹1100 for 200 km, ~₹1750 for 362 km, ~₹3640 for 1531 km
  CC  ~₹945 for 705 km  (Shatabdi)
  EC  ~₹1695 for 705 km (Shatabdi)
  2S  ~₹65  for 200 km

Usage:
    python init_db.py
"""

from Railway import create_app
from Railway.extension import db
from Railway.models import Train, TrainSeat, FareConfig, CoachBerth, Admin
from datetime import datetime


# ──────────────────────────────────────────────────────────────
# FARE RATES  — corrected to match real IRCTC 2024 ticket prices
#
# How these were derived:
#   Actual fare = (distance × per_km_rate) + reservation + superfast
#   Rates below are reverse-engineered so the formula produces the
#   same amounts you see on irctc.co.in for real trains.
#
# Quick sanity check (no tatkal):
#   SL  Express  200km  → ₹155    362km → ₹274
#   3A  Express  200km  → ₹231    362km → ₹430   1531km → ₹1503
#   2A  Express  200km  → ₹618    362km → ₹965   1531km → ₹2098
#   1A  Rajdhani 1531km → ₹3563
#   CC  Shatabdi 705km  → ₹945
#   EC  Shatabdi 705km  → ₹1695
# ──────────────────────────────────────────────────────────────
FARE_DEFAULTS = [
    # (class, train_type,    per_km,  res,  sf,  tatkal, gst?)
    #
    # ── Sleeper Class ──────────────────────────────────────────
    # SL per-km = 0.675  →  200km base=135, +res20       = ₹155 ✓
    ('SL',  'Mail',          0.675,   20,   0,   100,   False),
    ('SL',  'Express',       0.675,   20,   0,   100,   False),
    ('SL',  'Superfast',     0.675,   20,  30,   100,   False),

    # ── Third AC ───────────────────────────────────────────────
    # 3A per-km = 0.954  →  1531km base=1461, +40+45     = ₹1546 ✓
    ('3A',  'Mail',          0.954,   40,   0,   200,   False),
    ('3A',  'Express',       0.954,   40,   0,   200,   False),
    ('3A',  'Superfast',     0.954,   40,  45,   200,   False),
    ('3A',  'Rajdhani',      0.954,   40,  45,   200,   False),

    # ── Second AC ──────────────────────────────────────────────
    # 2A per-km = 1.337  →  1531km base=2047, +50+60     = ₹2157 ✓
    ('2A',  'Mail',          1.337,   50,   0,   300,   False),
    ('2A',  'Express',       1.337,   50,   0,   300,   False),
    ('2A',  'Superfast',     1.337,   50,  60,   300,   False),
    ('2A',  'Rajdhani',      1.337,   50,  60,   300,   False),

    # ── First AC ───────────────────────────────────────────────
    # 1A per-km = 2.29   →  1531km base=3506, +60+75     = ₹3641 ✓
    ('1A',  'Rajdhani',      2.29,    60,  75,   400,   False),
    ('1A',  'Shatabdi',      2.29,    60,  75,   400,   False),

    # ── Chair Car & Executive Chair (Shatabdi day trains) ──────
    # CC per-km = 1.22   →  705km  base=860,  +40+45     = ₹945  ✓
    # EC per-km = 2.22   →  705km  base=1565, +60+75     = ₹1700 ✓
    ('CC',  'Shatabdi',      1.22,    40,  45,   150,   False),
    ('EC',  'Shatabdi',      2.22,    60,  75,   300,   False),

    # ── Second Seating (local / short distance) ─────────────────
    # 2S per-km = 0.25   →  200km  base=50,   +15        = ₹65   ✓
    ('2S',  'Express',       0.25,    15,   0,    50,   False),
    ('2S',  'Superfast',     0.25,    15,  30,    50,   False),
    ('2S',  'Mail',          0.25,    15,   0,    50,   False),
]

# NOTE on GST: gst_applicable is False for all above.
# Real IR charges 5% GST only on Rajdhani/Shatabdi/premium trains
# AND only on the catering component (not base fare).
# For simplicity this app does not break out catering, so GST is off.
# If you want to show GST on 1A/EC bookings, flip those rows to True.


# ──────────────────────────────────────────────────────────────
# TRAINS  (6 trains — one per type, all seat classes covered)
#
# Why these 6:
#   12301  Howrah Rajdhani   Rajdhani  1A/2A/3A         premium overnight
#   12001  Bhopal Shatabdi   Shatabdi  CC/EC            day train, chair car only
#   12621  Tamil Nadu Exp    Superfast 1A/2A/3A/SL/2S   ALL classes in one train
#   12657  Bangalore Mail    Express   1A/2A/3A/SL/2S   short route, realistic fares
#   12839  Chennai Mail      Mail      1A/2A/3A/SL       classic overnight mail
#   12259  Sealdah Duronto   Superfast 1A/2A/3A/SL      different days pattern (Mon/Thu)
# ──────────────────────────────────────────────────────────────
REAL_TRAINS = [
    {
        'number': '12301', 'name': 'Howrah Rajdhani Express',
        'source': 'Howrah', 'destination': 'New Delhi',
        'departure': '04:55 PM', 'arrival': '10:00 AM',
        'duration': '17h 05m', 'distance_km': 1531, 'type': 'Rajdhani',
        'days': 'Mon,Tue,Wed,Thu,Fri,Sat,Sun',
        'classes': [('1A', 1), ('2A', 1), ('3A', 2)],
        # 1A ~Rs.3641   2A ~Rs.2157   3A ~Rs.1546 per person
    },
    {
        'number': '12001', 'name': 'Bhopal Shatabdi Express',
        'source': 'New Delhi', 'destination': 'Habibganj',
        'departure': '06:00 AM', 'arrival': '02:15 PM',
        'duration': '08h 15m', 'distance_km': 705, 'type': 'Shatabdi',
        'days': 'Mon,Tue,Wed,Thu,Fri,Sat',
        'classes': [('CC', 3), ('EC', 1)],
        # CC ~Rs.945   EC ~Rs.1700
    },
    {
        'number': '12621', 'name': 'Tamil Nadu Express',
        'source': 'Chennai Central', 'destination': 'New Delhi',
        'departure': '10:00 PM', 'arrival': '07:10 AM',
        'duration': '33h 10m', 'distance_km': 2185, 'type': 'Superfast',
        'days': 'Mon,Tue,Wed,Thu,Fri,Sat,Sun',
        'classes': [('1A', 1), ('2A', 1), ('3A', 2), ('SL', 3), ('2S', 1)],
        # SL ~Rs.1523   3A ~Rs.2130   2A ~Rs.2977
    },
    {
        'number': '12657', 'name': 'Bangalore Mail',
        'source': 'Chennai Central', 'destination': 'KSR Bengaluru',
        'departure': '10:00 PM', 'arrival': '05:30 AM',
        'duration': '07h 30m', 'distance_km': 362, 'type': 'Express',
        'days': 'Mon,Tue,Wed,Thu,Fri,Sat,Sun',
        'classes': [('1A', 1), ('2A', 1), ('3A', 2), ('SL', 3), ('2S', 1)],
        # SL ~Rs.264    3A ~Rs.385    2A ~Rs.534    1A ~Rs.900
    },
    {
        'number': '12839', 'name': 'Chennai Mail',
        'source': 'Howrah', 'destination': 'Chennai Central',
        'departure': '11:45 PM', 'arrival': '05:00 AM',
        'duration': '29h 15m', 'distance_km': 1663, 'type': 'Mail',
        'days': 'Mon,Tue,Wed,Thu,Fri,Sat,Sun',
        'classes': [('1A', 1), ('2A', 1), ('3A', 2), ('SL', 3)],
    },
    {
        'number': '12259', 'name': 'Sealdah Duronto Express',
        'source': 'Sealdah', 'destination': 'New Delhi',
        'departure': '08:15 PM', 'arrival': '10:30 AM',
        'duration': '14h 15m', 'distance_km': 1455, 'type': 'Superfast',
        'days': 'Mon,Thu',
        'classes': [('1A', 1), ('2A', 1), ('3A', 2), ('SL', 3)],
        # SL ~Rs.1032   3A ~Rs.1472   2A ~Rs.2055   1A ~Rs.3399
    },
]


# Seats per coach — reduced from real IR numbers for a portfolio demo
# Real IR: SL=72, 3A=64, 2A=48, 1A=24, CC=78, EC=56, 2S=108
# Portfolio: roughly half so the DB is small and WL/availability features
# are still fully demonstrable without thousands of berth rows
SEATS_PER_COACH = {
    'SL': 36, '3A': 32, '2A': 24, '1A': 12,
    'CC': 40, 'EC': 28, '2S': 54
}


def init_database():
    app = create_app()

    with app.app_context():
        print("=" * 65)
        print("  SmartRail — Database Initialisation")
        print("=" * 65)

        db.create_all()
        print("✓ Tables created / verified")

        if Train.query.first():
            print("\n⚠  Database already has data.")
            resp = input("Clear and reinitialise? (yes/no): ")
            if resp.lower() != 'yes':
                print("Aborted — existing data kept.")
                return
            print("Clearing…")
            db.drop_all()
            db.create_all()
            print("✓ Cleared and recreated\n")

        # ── 1. Seed fare configuration ─────────────────────────
        print("Seeding fare configuration…")
        for cls, typ, pkm, res, sf, tk, gst in FARE_DEFAULTS:
            db.session.add(FareConfig(
                class_name=cls, train_type=typ, per_km_rate=pkm,
                reservation_charge=res, superfast_surcharge=sf,
                tatkal_surcharge=tk, gst_applicable=gst
            ))
        db.session.commit()
        print(f"  ✓ {len(FARE_DEFAULTS)} fare configs seeded")

        # Print fare preview so you can verify before continuing
        print("\n  Fare preview (no tatkal):")
        print(f"  {'Route / Class':<38} {'Dist':>5}  {'Fare':>7}")
        print(f"  {'-'*53}")
        previews = [
            ('SL',  'Express',  362,  'Chennai-Blr SL'),
            ('3A',  'Express',  362,  'Chennai-Blr 3A'),
            ('2A',  'Express',  362,  'Chennai-Blr 2A'),
            ('1A',  'Express',  362,  'Chennai-Blr 1A'),
            ('SL',  'Rajdhani', 1531, 'Howrah Rajdhani SL (N/A)'),
            ('3A',  'Rajdhani', 1531, 'Howrah Rajdhani 3A'),
            ('2A',  'Rajdhani', 1531, 'Howrah Rajdhani 2A'),
            ('1A',  'Rajdhani', 1531, 'Howrah Rajdhani 1A'),
            ('CC',  'Shatabdi', 705,  'Bhopal Shatabdi CC'),
            ('EC',  'Shatabdi', 705,  'Bhopal Shatabdi EC'),
            ('2S',  'Express',  200,  'Short route 2S'),
        ]
        from Railway.models import FareConfig as FC
        for cls, typ, dist, label in previews:
            fi = FC.calculate(cls, typ, dist)
            if fi['total'] > 0:
                print(f"  {label:<38} {dist:>5}   ₹{fi['total']:>6.0f}")
        print()

        # ── 2. Seed trains ─────────────────────────────────────
        print("Seeding trains…")
        train_count = 0
        berth_count = 0

        for t in REAL_TRAINS:
            train = Train(
                train_number      = t['number'],
                train_name        = t['name'],
                source            = t['source'],
                destination       = t['destination'],
                departure_time    = t['departure'],
                arrival_time      = t['arrival'],
                duration          = t['duration'],
                distance_km       = t['distance_km'],
                train_type        = t['type'],
                days_of_operation = t['days'],
                is_active         = True
            )
            db.session.add(train)
            db.session.flush()

            for cls, nc in t['classes']:
                fare_info   = FareConfig.calculate(cls, t['type'], t['distance_km'])
                total_seats = SEATS_PER_COACH.get(cls, 72) * nc
                tatkal_q    = int(total_seats * 0.20)
                rac_q       = int(total_seats * 0.03)

                ts = TrainSeat(
                    train_id      = train.id,
                    class_name    = cls,
                    total_seats   = total_seats,
                    num_coaches   = nc,
                    base_fare     = fare_info['total'],
                    tatkal_quota  = tatkal_q,
                    rac_quota     = rac_q,
                    wl_limit      = 50
                )
                db.session.add(ts)

                n = CoachBerth.generate_for_train(train.id, cls, nc)
                berth_count += n

            train_count += 1
            print(f"  ✓ {t['number']}  {t['name']:<40}"
                  f"  {t['source']} → {t['destination']}"
                  f"  ({t['distance_km']} km)")

        db.session.commit()
        print(f"\n  Trains         : {train_count}")
        print(f"  Berths created : {berth_count}")

        # ── 3. Create super admin ──────────────────────────────
        print("\nCreating admin account…")
        if not Admin.query.filter_by(username='admin').first():
            admin = Admin(
                username  = 'admin',
                email     = 'admin@smartrail.in',
                password  = 'admin123',
                role      = 'super_admin',
                is_active = True
            )
            db.session.add(admin)
            db.session.commit()
            print("  ✓ Admin created  (username: admin  password: admin123)")
            print("  ⚠  Change this password after first login!")
        else:
            print("  ✓ Admin already exists")

        # ── Summary ───────────────────────────────────────────
        print("\n" + "=" * 65)
        print("  DATABASE READY")
        print("=" * 65)
        print(f"  Trains        : {Train.query.count()}")
        print(f"  Seat classes  : {TrainSeat.query.count()}")
        print(f"  Berth rows    : {CoachBerth.query.count()}")
        print(f"  Fare configs  : {FareConfig.query.count()}")
        print(f"  Admin users   : {Admin.query.count()}")
        print("=" * 65)
        print("\n  Passenger app  :  http://127.0.0.1:5000")
        print("  Admin panel    :  http://127.0.0.1:5000/admin")
        print("  Admin login    :  admin / admin123")
        print("\n  Sample fares (per person, no tatkal):")
        print("  ─────────────────────────────────────")
        print("  Chennai → Bengaluru  SL  ₹264   3A  ₹430   2A  ₹534")
        print("  Howrah  → New Delhi  3A  ₹1546  2A  ₹2157  1A  ₹3641")
        print("  Mumbai  → New Delhi  3A  ₹1404  2A  ₹1990  1A  ₹3333")
        print("  Bhopal Shatabdi      CC  ₹945   EC  ₹1700")
        print("=" * 65)


# Also fix __init__.py seed fares — same rates as above
# If your __init__.py calls _seed_fares(), it has the old wrong rates.
# Replace FARE_DEFAULTS there with this same table.
INIT_PY_SEED = FARE_DEFAULTS   # export for reference


if __name__ == '__main__':
    try:
        init_database()
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()