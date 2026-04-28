"""
__init__.py  —  SmartRail application factory
"""

from flask import Flask
from .extension import db
from .views import main
from .admin_views import admin_bp
import os
from dotenv import load_dotenv
load_dotenv()


def create_app():
    app = Flask(__name__)

    # Load config from env (FLASK_ prefix)
    app.config.from_prefixed_env()

    app.config.setdefault('SECRET_KEY', 'dev-secret-key-change-in-production')
    app.config.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///railway.db')
    app.config.setdefault('SQLALCHEMY_TRACK_MODIFICATIONS', False)

    db.init_app(app)

    # Register blueprints
    app.register_blueprint(main)
    app.register_blueprint(admin_bp)   # admin panel at /admin/*

    with app.app_context():
        from . import models
        db.create_all()

        from .models import FareConfig
        if FareConfig.query.count() == 0:
            _seed_fares()

        from .views import start_wl_scheduler
        start_wl_scheduler(app)

    return app


def _seed_fares():
    """
    Seed Indian Railways fare rates on first run.

    Rates are reverse-engineered from live IRCTC prices (2024) so the
    formula  (distance × per_km_rate) + reservation + superfast
    produces realistic ticket amounts:

      SL  Express  362 km  → ~₹264      (Chennai–Bengaluru)
      3A  Express  362 km  → ~₹430
      2A  Express  362 km  → ~₹534
      3A  Rajdhani 1531 km → ~₹1546     (Howrah Rajdhani)
      2A  Rajdhani 1531 km → ~₹2157
      1A  Rajdhani 1531 km → ~₹3641
      CC  Shatabdi 705 km  → ~₹945      (Bhopal Shatabdi)
      EC  Shatabdi 705 km  → ~₹1700
      2S  Express  200 km  → ~₹65
    """
    from .models import FareConfig
    defaults = [
        # (class, train_type,   per_km,  res,  sf,  tatkal, gst?)
        # ── Sleeper ──────────────────────────────────────────────
        ('SL',  'Mail',          0.675,   20,   0,   100,  False),
        ('SL',  'Express',       0.675,   20,   0,   100,  False),
        ('SL',  'Superfast',     0.675,   20,  30,   100,  False),
        # ── Third AC ─────────────────────────────────────────────
        ('3A',  'Mail',          0.954,   40,   0,   200,  False),
        ('3A',  'Express',       0.954,   40,   0,   200,  False),
        ('3A',  'Superfast',     0.954,   40,  45,   200,  False),
        ('3A',  'Rajdhani',      0.954,   40,  45,   200,  False),
        # ── Second AC ────────────────────────────────────────────
        ('2A',  'Mail',          1.337,   50,   0,   300,  False),
        ('2A',  'Express',       1.337,   50,   0,   300,  False),
        ('2A',  'Superfast',     1.337,   50,  60,   300,  False),
        ('2A',  'Rajdhani',      1.337,   50,  60,   300,  False),
        # ── First AC ─────────────────────────────────────────────
        ('1A',  'Rajdhani',      2.29,    60,  75,   400,  False),
        ('1A',  'Shatabdi',      2.29,    60,  75,   400,  False),
        # ── Chair Car / Executive Chair (Shatabdi) ───────────────
        ('CC',  'Shatabdi',      1.22,    40,  45,   150,  False),
        ('EC',  'Shatabdi',      2.22,    60,  75,   300,  False),
        # ── Second Seating ────────────────────────────────────────
        ('2S',  'Express',       0.25,    15,   0,    50,  False),
        ('2S',  'Superfast',     0.25,    15,  30,    50,  False),
        ('2S',  'Mail',          0.25,    15,   0,    50,  False),
    ]
    for cls, typ, pkm, res, sf, tk, gst in defaults:
        db.session.add(FareConfig(
            class_name=cls, train_type=typ, per_km_rate=pkm,
            reservation_charge=res, superfast_surcharge=sf,
            tatkal_surcharge=tk, gst_applicable=gst
        ))
    db.session.commit()