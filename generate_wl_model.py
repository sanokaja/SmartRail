"""
generate_wl_model.py
Place this in your Railway PROJECT ROOT (same level as run.py / app.py).
Run once:  python generate_wl_model.py

It reads WLHistory from your DB, trains Random Forest,
and saves wl_model.pkl inside the Railway/ folder.
"""

import sys, os, pickle, pathlib
import pandas as pd
import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import cross_val_score

# ── Point to your Flask app ────────────────────────────────────
sys.path.insert(0, os.path.dirname(__file__))
from Railway import create_app
from Railway.extension import db
from Railway.models import WLHistory

MODEL_PATH = pathlib.Path(__file__).parent / 'Railway' / 'wl_model.pkl'
CLASS_MAP  = {'SL': 0, '3A': 1, '2A': 2, '1A': 3,
              'CC': 4, 'EC': 5, '2S': 6}
MIN_ROWS   = 50   # minimum rows needed to train

app = create_app()

with app.app_context():

    rows = WLHistory.query.all()
    print(f"WLHistory rows found: {len(rows)}")

    if len(rows) < MIN_ROWS:
        # ── Not enough real data — generate synthetic to bootstrap ──
        print(f"Less than {MIN_ROWS} rows. Generating synthetic training data...")
        import random
        random.seed(42)

        CLASS_BASE = {'SL': 0.72, '3A': 0.58, '2A': 0.44, '1A': 0.30}
        synthetic  = []

        for _ in range(600):
            cls     = random.choices(['SL','3A','2A','1A'], weights=[40,30,20,10])[0]
            wl_num  = random.randint(1, 60)
            days    = random.randint(1, 60)
            month   = random.randint(1, 12)
            weekday = random.random() > 0.35
            festval = month in [5, 6, 10, 11, 12]

            base = CLASS_BASE[cls]
            if wl_num <= 5:    base += 0.30
            elif wl_num <= 10: base += 0.20
            elif wl_num <= 20: base += 0.08
            elif wl_num <= 30: base -= 0.05
            elif wl_num <= 40: base -= 0.18
            else:              base -= 0.30
            base += (days / 60) * 0.15
            if festval:  base -= 0.12
            if weekday:  base += 0.06
            base += random.uniform(-0.08, 0.08)
            confirmed = random.random() < max(0.03, min(0.97, base))

            synthetic.append({
                'wl_number':        wl_num,
                'days_at_booking':  days,
                'month':            month,
                'is_weekday':       int(weekday),
                'is_festival_month':int(festval),
                'class_encoded':    CLASS_MAP.get(cls, 1),
                'confirmed':        int(confirmed),
            })

        df = pd.DataFrame(synthetic)
        print(f"Synthetic rows generated: {len(df)}")

    else:
        # ── Use real data from DB ──────────────────────────────────
        df = pd.DataFrame([{
            'wl_number':        r.wl_number,
            'days_at_booking':  r.days_at_booking,
            'month':            r.month,
            'is_weekday':       int(r.is_weekday),
            'is_festival_month':int(r.is_festival_month),
            'class_encoded':    CLASS_MAP.get(r.class_name, 1),
            'confirmed':        int(r.confirmed),
        } for r in rows])
        print(f"Using real data: {len(df)} rows")

    # ── Feature engineering ────────────────────────────────────
    df['wl_bucket'] = pd.cut(
        df['wl_number'],
        bins=[0,5,10,20,30,40,60,999],
        labels=[0,1,2,3,4,5,6]
    ).astype(int)

    df['days_bucket'] = pd.cut(
        df['days_at_booking'],
        bins=[0,7,15,30,45,60,999],
        labels=[0,1,2,3,4,5]
    ).astype(int)

    FEATURES = [
        'wl_number', 'days_at_booking', 'month',
        'is_weekday', 'is_festival_month',
        'class_encoded', 'wl_bucket', 'days_bucket'
    ]

    X = df[FEATURES]
    y = df['confirmed']

    # ── Train Random Forest ────────────────────────────────────
    rf = RandomForestClassifier(
        n_estimators    = 200,
        max_depth       = 8,
        min_samples_leaf= 5,
        class_weight    = 'balanced',
        random_state    = 42
    )
    rf.fit(X, y)

    cv = cross_val_score(rf, X, y, cv=5, scoring='accuracy')
    print(f"CV Accuracy: {cv.mean():.1%} ± {cv.std():.1%}")

    # ── Save model ─────────────────────────────────────────────
    model_data = {
        'model':      rf,
        'features':   FEATURES,
        'class_map':  CLASS_MAP,
        'trained_on': len(df),
        'cv_accuracy': round(float(cv.mean()), 3),
    }

    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(MODEL_PATH, 'wb') as f:
        pickle.dump(model_data, f)

    print(f"Model saved to {MODEL_PATH}")
    print(f"Trained on {len(df)} rows")
    print(f"Place wl_model.pkl in your Railway/ folder if not already there")