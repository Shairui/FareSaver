"""
FareSaver v2 — Live Fare Prediction
Run: streamlit run app_v2.py
Extra deps: pip install geopy xgboost scikit-learn
"""

import os
import pickle
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components

# ── Optional deps ──────────────────────────────────────────────────
try:
    from geopy.geocoders import Nominatim
    from geopy.exc import GeocoderTimedOut, GeocoderServiceError
    GEOPY_OK = True
except ImportError:
    GEOPY_OK = False

try:
    import xgboost as xgb
    from sklearn.model_selection import train_test_split
    ML_OK = True
except ImportError:
    ML_OK = False


# ── Config ─────────────────────────────────────────────────────────
MODEL_PATH = "fare_model.pkl"
DATA_PATH  = "uber.csv"
FEATURES   = [
    "distance_km", "hour_sin", "hour_cos", "dow_sin", "dow_cos",
    "month", "passenger_count", "is_weekend", "is_rush_hour",
]

# Fallback demo fares (used when model / geocoding unavailable)
DEMO_FARES = [
    10.2, 9.8, 9.5, 9.3, 9.1, 9.8, 11.2, 13.8, 14.2, 13.5, 11.8, 11.2,
    11.5, 11.3, 11.0, 11.8, 13.2, 14.0, 13.8, 12.5, 12.0, 11.5, 11.0, 10.6,
]


# ── Model utilities ────────────────────────────────────────────────
@st.cache_resource(show_spinner="Training fare model (first run only) …")
def load_or_train_model():
    """Return trained XGBoost model, or None if data/deps missing."""
    if os.path.exists(MODEL_PATH):
        with open(MODEL_PATH, "rb") as f:
            return pickle.load(f)

    if not (os.path.exists(DATA_PATH) and ML_OK):
        return None

    df = pd.read_csv(DATA_PATH)
    df["pickup_datetime"] = pd.to_datetime(df["pickup_datetime"], errors="coerce")
    df = df.dropna(subset=["pickup_datetime", "fare_amount"])
    df = df[(df["fare_amount"] >= 2.50) & (df["fare_amount"] <= 150)]
    bbox = dict(lon_min=-74.1, lon_max=-73.7, lat_min=40.5, lat_max=40.95)
    df = df[
        df["pickup_longitude"].between(bbox["lon_min"], bbox["lon_max"]) &
        df["pickup_latitude"].between(bbox["lat_min"], bbox["lat_max"])
    ]

    def _hav(lon1, lat1, lon2, lat2):
        R = 6371
        p1, p2 = np.radians(lat1), np.radians(lat2)
        dp = np.radians(lat2 - lat1)
        dl = np.radians(lon2 - lon1)
        a = np.sin(dp / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
        return 2 * R * np.arcsin(np.sqrt(a))

    df["distance_km"]  = _hav(
        df["pickup_longitude"], df["pickup_latitude"],
        df["dropoff_longitude"], df["dropoff_latitude"],
    ).clip(0.1, 80)
    df["hour"]         = df["pickup_datetime"].dt.hour
    df["day_of_week"]  = df["pickup_datetime"].dt.dayofweek
    df["month"]        = df["pickup_datetime"].dt.month
    df["is_weekend"]   = (df["day_of_week"] >= 5).astype(int)
    df["is_rush_hour"] = df["hour"].isin([7, 8, 17, 18, 19]).astype(int)
    df["hour_sin"]     = np.sin(2 * np.pi * df["hour"] / 24)
    df["hour_cos"]     = np.cos(2 * np.pi * df["hour"] / 24)
    df["dow_sin"]      = np.sin(2 * np.pi * df["day_of_week"] / 7)
    df["dow_cos"]      = np.cos(2 * np.pi * df["day_of_week"] / 7)

    df_m = df[FEATURES + ["fare_amount"]].dropna()
    X, y = df_m[FEATURES], df_m["fare_amount"]
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, random_state=42
    )
    model = xgb.XGBRegressor(
        n_estimators=400, learning_rate=0.05, max_depth=6,
        subsample=0.8, colsample_bytree=0.8, min_child_weight=5,
        reg_alpha=0.1, reg_lambda=1.0, random_state=42, n_jobs=-1, verbosity=0,
    )
    model.fit(X_train, y_train, eval_set=[(X_test, y_test)], verbose=False)
    with open(MODEL_PATH, "wb") as f:
        pickle.dump(model, f)
    return model


@st.cache_data(ttl=3600, show_spinner=False)
def geocode_address(address: str):
    """Return (lat, lon) or None."""
    if not GEOPY_OK:
        return None
    try:
        geo = Nominatim(user_agent="faresaver_v2", timeout=10)
        loc = geo.geocode(address)
        if loc:
            return loc.latitude, loc.longitude
    except (GeocoderTimedOut, GeocoderServiceError):
        pass
    return None


def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dp = np.radians(lat2 - lat1)
    dl = np.radians(lon2 - lon1)
    a = np.sin(dp / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return 2 * R * np.arcsin(np.sqrt(a))


def predict_fares(model, distance_km: float, trip_date: date) -> list:
    """Predict fare for each of the 24 departure hours."""
    dow     = trip_date.weekday()
    month   = trip_date.month
    is_wknd = int(dow >= 5)
    rows = [
        {
            "distance_km":     distance_km,
            "hour_sin":        np.sin(2 * np.pi * h / 24),
            "hour_cos":        np.cos(2 * np.pi * h / 24),
            "dow_sin":         np.sin(2 * np.pi * dow / 7),
            "dow_cos":         np.cos(2 * np.pi * dow / 7),
            "month":           month,
            "passenger_count": 1,
            "is_weekend":      is_wknd,
            "is_rush_hour":    int(h in [7, 8, 17, 18, 19]),
        }
        for h in range(24)
    ]
    return model.predict(pd.DataFrame(rows)[FEATURES]).tolist()


# ── Page config ────────────────────────────────────────────────────
st.set_page_config(
    page_title="FareSaver - Beat Surge Pricing",
    page_icon="car",
    layout="centered",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&display=swap');

:root {
  --bg: #0b0b0b;
  --surface: #141414;
  --surface-2: #191919;
  --ink: #f5f1e8;
  --muted: #b8b1a4;
  --line: #303030;
  --soft-line: #252525;
  --accent: #18c45c;
  --accent-dark: #0f9f48;
  --warm: #efe4c8;
  --danger: #e04b43;
  --warn: #f59e0b;
}

.stApp {
  background:
    radial-gradient(circle at top right, rgba(24,196,92,0.12), transparent 24%),
    linear-gradient(180deg, #0f0f0f 0%, #090909 100%) !important;
}

.main .block-container {
  max-width: 1040px;
  padding-top: 2rem;
  padding-bottom: 4rem;
}

html, body, [class*="css"], p, div, li, span, label, button, input {
  font-family: 'Inter', system-ui, sans-serif !important;
  color: var(--ink);
}

div[data-testid="stTabs"] {
  margin-bottom: 1.1rem !important;
}

div[data-testid="stTabs"] button {
  font-size: 0.98rem !important;
  font-weight: 700 !important;
  color: #a79f93 !important;
  background: transparent !important;
  border: none !important;
  padding: 0.9rem 1.15rem !important;
}

div[data-testid="stTabs"] button[aria-selected="true"] {
  color: var(--warm) !important;
  border-bottom: 2px solid var(--accent) !important;
}

div[data-testid="stTabsContent"] {
  padding-top: 0 !important;
}

.stTextInput > div,
.stDateInput > div,
.stSelectbox > div {
  margin-top: 0 !important;
}

.stTextInput [data-baseweb="input"],
.stDateInput > div > div,
.stSelectbox [data-baseweb="select"] > div {
  min-height: 52px !important;
  background: var(--surface) !important;
  color: var(--warm) !important;
  border: 1px solid #3a3a3a !important;
  border-radius: 16px !important;
  padding: 0 14px !important;
  box-shadow: none !important;
  display: flex !important;
  align-items: center !important;
}

.stTextInput [data-baseweb="input"] > div {
  background: transparent !important;
}

.stTextInput input,
.stDateInput input,
.stSelectbox [data-baseweb="select"] input {
  background: transparent !important;
  color: var(--warm) !important;
  border: none !important;
  outline: none !important;
  box-shadow: none !important;
  font-size: 15px !important;
  padding: 0 !important;
}

.stTextInput [data-baseweb="input"]:focus-within,
.stDateInput > div > div:focus-within,
.stSelectbox [data-baseweb="select"] > div:focus-within {
  border: 1px solid var(--accent) !important;
  box-shadow: none !important;
  outline: none !important;
}

.stSelectbox [data-baseweb="select"] > div > div,
.stSelectbox [data-baseweb="select"] span,
.stSelectbox [data-baseweb="select"] div[aria-selected="true"],
.stSelectbox [data-baseweb="select"] div {
  color: var(--warm) !important;
}

.stSelectbox [data-baseweb="select"] [data-testid="stMarkdownContainer"],
.stSelectbox [data-baseweb="select"] [data-testid="stMarkdownContainer"] p,
.stSelectbox [data-baseweb="select"] [data-testid="stMarkdownContainer"] span {
  color: var(--warm) !important;
}

.stDateInput input {
  color-scheme: dark;
}

.stSelectbox [data-baseweb="select"] svg {
  fill: var(--warm) !important;
}

[data-baseweb="popover"] * {
  background: #171717 !important;
  color: var(--warm) !important;
}

[data-baseweb="menu"] li:hover {
  background: #232323 !important;
}

label,
.stTextInput label,
.stDateInput label,
.stSelectbox label,
.stSlider label {
  color: #4d4d4d !important;
  color: #b8b1a4 !important;
  font-size: 0.78rem !important;
  font-weight: 700 !important;
  letter-spacing: 0.04em !important;
  text-transform: uppercase !important;
}

div[data-testid="stButton"] > button[kind="primary"] {
  background: var(--warm) !important;
  color: #111 !important;
  border: 1px solid var(--warm) !important;
  border-radius: 999px !important;
  font-weight: 800 !important;
  font-size: 0.96rem !important;
  padding: 0.9rem 0 !important;
}

div[data-testid="stButton"] > button[kind="primary"] *,
div[data-testid="stButton"] > button:not([kind="primary"]) * {
  color: inherit !important;
}

div[data-testid="stButton"] > button[kind="primary"]:hover {
  background: #fff4db !important;
  border-color: #fff4db !important;
}

div[data-testid="stButton"] > button:not([kind="primary"]) {
  background: var(--surface) !important;
  color: var(--warm) !important;
  border: 1px solid #3a3a3a !important;
  border-radius: 999px !important;
  font-weight: 700 !important;
}

.stInfo,
.stSuccess {
  border-radius: 18px !important;
  border: 1px solid var(--line) !important;
}

.stSuccess {
  background: rgba(24,196,92,0.10) !important;
  color: #b7f5cb !important;
}

.stInfo {
  background: rgba(255,255,255,0.05) !important;
  color: var(--warm) !important;
}

.hero-shell {
  background:
    radial-gradient(circle at top right, rgba(24,196,92,0.16), transparent 24%),
    linear-gradient(145deg, #151515 0%, #111111 100%);
  border-radius: 34px;
  padding: 22px;
  margin-bottom: 22px;
  box-shadow: 0 24px 60px rgba(0,0,0,0.14);
}

.hero-panel {
  border: 1px solid rgba(255,255,255,0.08);
  border-radius: 28px;
  padding: 30px 34px 34px;
  min-height: 440px;
  background:
    linear-gradient(90deg, rgba(255,255,255,0.03), transparent 35%),
    linear-gradient(180deg, rgba(255,255,255,0.02), transparent 45%);
}

.hero-topline {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  color: #8ef0b7;
  font-size: 0.78rem;
  font-weight: 800;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  border: 1px solid rgba(255,255,255,0.08);
  border-radius: 999px;
  padding: 10px 14px;
  background: rgba(255,255,255,0.03);
}

.hero-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.1fr) minmax(250px, 0.9fr);
  gap: 24px;
  align-items: end;
  margin-top: 28px;
}

.hero-copy h1 {
  color: #fff !important;
  font-size: clamp(5rem, 10vw, 8.6rem) !important;
  line-height: 0.88 !important;
  letter-spacing: -0.07em !important;
  font-weight: 900 !important;
  margin: 0 0 22px !important;
  max-width: 760px !important;
}

.hero-copy h1 .mute {
  color: rgba(255,255,255,0.34);
}

.hero-copy h1 .accent-line {
  color: #8ef0b7;
  white-space: nowrap;
}

.hero-copy h1 .light-line {
  color: #f5f1e8;
  white-space: nowrap;
}

.hero-copy h1 .accent {
  color: #8ef0b7;
}

.hero-copy p {
  color: rgba(255,255,255,0.78);
  font-size: 1.07rem;
  line-height: 1.7;
  max-width: 520px;
  margin: 0 0 28px;
}

.hero-actions {
  display: flex;
  flex-direction: column;
  gap: 10px;
  align-items: stretch;
}

.hero-pill {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 14px 18px;
  border-radius: 999px;
  font-size: 0.92rem;
  font-weight: 700;
  border: 1px solid rgba(255,255,255,0.10);
  color: #fff;
  background: rgba(255,255,255,0.05);
  text-align: center;
}

.hero-pill.primary {
  background: #fff;
  color: #111;
  border-color: #fff;
}

.hero-stack {
  display: grid;
  gap: 12px;
}

.hero-float {
  background: rgba(255,255,255,0.06);
  border: 1px solid rgba(255,255,255,0.08);
  border-radius: 24px;
  padding: 20px 22px;
  backdrop-filter: blur(8px);
}

.hero-float-label {
  color: var(--warm);
  font-size: 0.75rem;
  font-weight: 800;
  letter-spacing: 0.10em;
  text-transform: uppercase;
  margin-bottom: 8px;
}

.hero-float-value {
  color: #fff;
  font-size: 2.7rem;
  font-weight: 900;
  line-height: 0.95;
  letter-spacing: -0.05em;
}

.hero-float-copy {
  color: rgba(255,255,255,0.78);
  font-size: 0.95rem;
  line-height: 1.55;
  margin-top: 10px;
}

.stat-strip {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 14px;
  margin-bottom: 28px;
}

.stat-item {
  background: var(--surface);
  border: 1px solid var(--soft-line);
  border-radius: 24px;
  padding: 26px 24px;
}

.stat-num {
  font-size: 3.7rem;
  font-weight: 900;
  line-height: 0.95;
  letter-spacing: -0.05em;
  color: var(--ink);
}

.stat-num em {
  color: var(--accent);
  font-style: normal;
  font-size: 0.72em;
}

.stat-lbl {
  margin-top: 12px;
  color: var(--muted);
  font-size: 1rem;
  line-height: 1.5;
}

.section-kicker {
  font-size: 0.78rem;
  font-weight: 800;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--warm) !important;
  margin: 24px 0 10px;
}

.section-title {
  font-size: 2.55rem;
  font-weight: 900;
  line-height: 0.98;
  letter-spacing: -0.05em;
  color: var(--ink);
  margin: 0 0 18px;
}

.soft-grid {
  display: grid;
  gap: 14px;
}

.step-row,
.persona,
.a-card,
.testi,
.val-card,
.team-card,
.plan-free,
.plan-prem {
  border-radius: 24px;
  border: 1px solid var(--soft-line);
  background: var(--surface);
}

.step-row {
  display: flex;
  align-items: flex-start;
  gap: 18px;
  padding: 22px 22px;
}

.step-num {
  width: 42px;
  height: 42px;
  border-radius: 14px;
  background: var(--ink);
  color: #fff;
  font-size: 1rem;
  font-weight: 800;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

.step-body strong,
.p-title,
.a-title,
.val-title,
.team-name {
  display: block;
  font-size: 1.02rem;
  font-weight: 800;
  color: var(--ink);
  margin-bottom: 6px;
}

.step-body p,
.p-body,
.a-body,
.val-body,
.team-bio {
  font-size: 0.98rem;
  color: var(--muted);
  line-height: 1.65;
  margin: 0;
}

.persona,
.a-card {
  display: flex;
  gap: 14px;
  padding: 22px;
  align-items: flex-start;
}

.p-dot {
  width: 12px;
  height: 12px;
  border-radius: 999px;
  margin-top: 7px;
  flex-shrink: 0;
}

.cta-band {
  background: #111;
  border-radius: 28px;
  padding: 30px 30px;
  margin: 26px 0;
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 18px;
  align-items: center;
}

.cta-title {
  color: #fff;
  font-size: 2.35rem;
  font-weight: 900;
  line-height: 0.95;
  letter-spacing: -0.05em;
}

.cta-copy {
  color: rgba(255,255,255,0.72);
  margin-top: 10px;
  font-size: 0.98rem;
  line-height: 1.65;
}

.cta-chip {
  background: var(--accent);
  color: #082a14;
  padding: 14px 18px;
  border-radius: 999px;
  font-size: 0.92rem;
  font-weight: 800;
}

.plan-free,
.plan-prem {
  padding: 28px 24px;
}

.plan-prem {
  background: linear-gradient(180deg, rgba(24,196,92,0.18) 0%, rgba(10,43,21,0.92) 100%);
  border-color: rgba(24,196,92,0.45);
  box-shadow: 0 0 0 1px rgba(24,196,92,0.12) inset;
}

.savings-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 14px;
  margin-top: 8px;
  width: 100%;
}

.savings-card {
  background: var(--surface);
  border: 1px solid var(--soft-line);
  border-radius: 24px;
  padding: 24px 22px;
  min-width: 0;
}

.savings-card.featured {
  background: rgba(24,196,92,0.10);
  border-color: rgba(24,196,92,0.35);
}

.savings-kicker {
  color: var(--warm);
  font-size: 0.78rem;
  font-weight: 800;
  letter-spacing: 0.10em;
  text-transform: uppercase;
  margin-bottom: 12px;
}

.savings-value {
  color: var(--warm);
  font-size: clamp(2.45rem, 3.6vw, 3.35rem);
  font-weight: 900;
  line-height: 0.92;
  letter-spacing: -0.06em;
  white-space: nowrap;
  overflow: hidden;
}

.savings-unit {
  font-size: 0.62em;
  letter-spacing: -0.03em;
  margin-left: 0.06em;
  color: var(--accent);
}

.savings-value.accent {
  color: var(--accent);
}

.savings-copy {
  color: var(--muted);
  font-size: 1rem;
  line-height: 1.6;
  margin-top: 12px;
}

.plan-name {
  font-size: 0.8rem;
  font-weight: 800;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--warm);
}

.plan-prem .plan-name {
  color: var(--warm);
}

.plan-price {
  margin: 12px 0 20px;
  font-size: 3rem;
  font-weight: 900;
  line-height: 0.95;
  letter-spacing: -0.05em;
  color: var(--ink);
}

.plan-prem .plan-price {
  color: #8ef0b7;
}

.plan-price span {
  font-size: 1rem;
  font-weight: 600;
  color: #777;
}

.plan-prem .plan-price span {
  color: rgba(255,255,255,0.6);
}

.plan-list {
  list-style: none;
  margin: 0;
  padding: 0;
}

.plan-list li {
  padding: 11px 0;
  border-bottom: 1px solid var(--soft-line);
  color: var(--muted);
  font-size: 0.98rem;
}

.plan-list li:last-child {
  border-bottom: none;
}

.plan-prem .plan-list li {
  color: rgba(245,241,232,0.92);
  border-bottom-color: rgba(142,240,183,0.14);
}

.plan-list .off {
  color: #9b9b9b;
}

.testi,
.val-card,
.team-card {
  padding: 24px 22px;
  height: 100%;
}

.val-card {
  display: flex;
  flex-direction: column;
  min-height: 230px;
}

.val-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 16px;
  align-items: stretch;
}

.testi {
  display: flex;
  flex-direction: column;
  min-height: 265px;
}

.testi-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 16px;
  align-items: stretch;
}

.testi-copy {
  flex: 1;
}

.testi-q {
  font-size: 2.5rem;
  color: #d9d9d9;
  line-height: 1;
  margin-bottom: 10px;
}

.testi-name {
  margin-top: 14px;
}

.testi-role,
.team-role {
  color: var(--warm);
  font-size: 0.84rem;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.avatar {
  width: 76px;
  height: 76px;
  border-radius: 24px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 1.7rem;
  font-weight: 900;
  background: #202020;
  margin: 0 auto 16px;
}

.d-cta {
  background: #111;
  border-radius: 28px;
  padding: 30px 30px;
}

.d-cta .dt {
  color: #fff;
  font-size: 2.25rem;
  font-weight: 900;
  line-height: 0.95;
  letter-spacing: -0.05em;
}

.d-cta .ds {
  color: rgba(255,255,255,0.72);
  font-size: 0.98rem;
  line-height: 1.7;
  margin-top: 10px;
}

.sep {
  border: none;
  border-top: 1px solid var(--line);
  margin: 32px 0;
}

.foot {
  text-align: center;
  color: #8c857a;
  font-size: 0.84rem;
  padding-top: 26px;
}

@media (max-width: 900px) {
  .hero-panel {
    padding: 24px 22px 24px;
    min-height: auto;
  }

  .hero-grid,
  .stat-strip,
  .cta-band,
  .savings-grid,
  .val-grid,
  .testi-grid {
    grid-template-columns: 1fr;
  }

  .hero-copy h1 {
    font-size: clamp(3.9rem, 16vw, 5.8rem) !important;
  }

  .section-title {
    font-size: 2rem;
  }

  .cta-title,
  .d-cta .dt {
    font-size: 1.8rem;
  }
}
</style>
""",
    unsafe_allow_html=True,
)


# ── Helpers ────────────────────────────────────────────────────────
def hour_label(h):
    if h == 0:   return "12 am"
    if h < 12:   return f"{h} am"
    if h == 12:  return "12 pm"
    return f"{h - 12} pm"


def fare_color(f, lo, hi):
    mid = lo + (hi - lo) * 0.45
    if f >= mid + (hi - mid) * 0.5:
        return "#e04b43"
    if f >= lo + (mid - lo) * 0.5:
        return "#f59e0b"
    return "#18c45c"


# ── Tabs ───────────────────────────────────────────────────────────
tab_home, tab_plan, tab_premium, tab_about = st.tabs(
    ["Home", "Plan My Trip", "Premium", "About Us"]
)


# ══════════════════════════════════════════════════════════════════
# HOME TAB
# ══════════════════════════════════════════════════════════════════
with tab_home:
    st.markdown(
        """
    <div class="hero-shell">
      <div class="hero-panel">
        <div class="hero-topline">Fare intelligence · free</div>
        <div class="hero-grid">
          <div class="hero-copy">
            <h1>Stop<br><span class="accent-line">Over</span><br><span class="light-line">paying</span><br>for your <span class="accent">Uber</span> ride.</h1>
            <p>FareSaver shows when surge pricing is worth waiting out. Enter your route, see the cheap window, and leave when the price finally makes sense.</p>
            <div class="hero-actions">
              <span id="fs-open-btn" class="hero-pill primary" style="cursor:pointer;">Open fare forecast</span>
              <span class="hero-pill">No credit card required</span>
              <span class="hero-pill">Built for flexible riders</span>
            </div>
          </div>
          <div class="hero-stack">
            <div class="hero-float">
              <div class="hero-float-label">Rush-hour difference</div>
              <div class="hero-float-value">+40%</div>
              <div class="hero-float-copy">Average fares spike hard during commute windows. The cheap hours are repeatable.</div>
            </div>
            <div class="hero-float">
              <div class="hero-float-label">Average saving</div>
              <div class="hero-float-value">$4.91</div>
              <div class="hero-float-copy">One smart trip can already cover the feeling that you finally beat surge.</div>
            </div>
          </div>
        </div>
      </div>
    </div>
    """,
        unsafe_allow_html=True,
    )

    components.html("""
<script>
(function attach() {
  var btn = window.parent.document.getElementById('fs-open-btn');
  if (!btn) { setTimeout(attach, 150); return; }
  if (btn._fs) return;
  btn._fs = true;
  btn.addEventListener('click', function() {
    var tabs = window.parent.document.querySelectorAll('[data-baseweb="tab"]');
    if (tabs.length > 1) tabs[1].click();
  });
})();
</script>
""", height=0, scrolling=False)


# ══════════════════════════════════════════════════════════════════
# PLAN MY TRIP TAB  ← dynamic predictions
# ══════════════════════════════════════════════════════════════════
with tab_plan:
    if "forecast_shown" not in st.session_state:
        st.session_state.forecast_shown = False
    if "alert_shown" not in st.session_state:
        st.session_state.alert_shown = False
    if "alert_threshold" not in st.session_state:
        st.session_state.alert_threshold = 10.0

    st.markdown("<div class='section-kicker'>Forecast</div>", unsafe_allow_html=True)
    st.markdown("<div class='section-title'>Plan My Trip</div>", unsafe_allow_html=True)

    col_a, col_b = st.columns(2)
    with col_a:
        origin = st.text_input("From", value="University of Toronto, Toronto")
    with col_b:
        dest = st.text_input("To", value="Toronto Pearson Airport")

    col_c, col_d = st.columns(2)
    with col_c:
        trip_date = st.date_input("Date", value=date.today() + timedelta(days=1))
    with col_d:
        window = st.selectbox(
            "How flexible is your timing?",
            [
                "Very flexible (+/- 3 hours)",
                "Somewhat flexible (+/- 2 hours)",
                "Not very flexible (+/- 1 hour)",
            ],
        )

    if st.button("Show Fare Forecast", type="primary", use_container_width=True):
        st.session_state.forecast_shown = True
        st.session_state.alert_shown = False

    if st.session_state.forecast_shown:

        # ── Resolve fares ──────────────────────────────────────────
        FARES      = None
        distance_km = None
        used_model  = False

        model = load_or_train_model()
        if model is not None:
            with st.spinner("Locating addresses…"):
                orig_coords = geocode_address(origin)
                dest_coords = geocode_address(dest)

            if orig_coords and dest_coords:
                distance_km = haversine_km(*orig_coords, *dest_coords)
                with st.spinner("Predicting fares…"):
                    FARES = predict_fares(model, distance_km, trip_date)
                used_model = True
            else:
                st.warning(
                    "Couldn't locate one or both addresses — showing demo forecast. "
                    "Try adding the city, e.g. *'University of Toronto, Toronto, Canada'*."
                )

        if FARES is None:
            FARES = DEMO_FARES

        # ── Derived stats ──────────────────────────────────────────
        best_hour    = int(np.argmin(FARES))
        CHEAPEST     = round(FARES[best_hour], 2)
        current_hour = datetime.now().hour
        SURGE_NOW    = round(FARES[current_hour], 2)
        SAVING       = round(max(SURGE_NOW - CHEAPEST, 0), 2)
        # monthly estimate: ~3 trips/week, 35% during surge hours
        MONTHLY_SAVING = round(SAVING * 3 * 4.33 * 0.35, 2)

        # ── Best-window result card ────────────────────────────────
        if distance_km is not None:
            st.caption(
                f"Route distance: **{distance_km:.1f} km** · "
                f"Fares calibrated to NYC historical data — relative timing patterns apply."
            )

        result_html = (
            '<div style="background:#111;border:1px solid #1f1f1f;border-radius:28px;padding:28px 26px;margin:20px 0;">'
            '<div style="font-size:11px;font-weight:800;letter-spacing:0.14em;text-transform:uppercase;color:#8ef0b7;margin-bottom:22px;">Best window found</div>'
            '<div style="display:flex;gap:0;align-items:stretch;flex-wrap:wrap;">'
            '<div style="flex:1;min-width:180px;padding-right:22px;border-right:1px solid rgba(255,255,255,0.08);margin-bottom:10px;">'
            '<div style="font-size:12px;color:rgba(255,255,255,0.58);margin-bottom:8px;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;">Cheapest departure</div>'
            f'<div style="font-size:2rem;font-weight:900;color:#fff;letter-spacing:-0.04em;line-height:1;">{hour_label(best_hour)}</div>'
            "</div>"
            '<div style="flex:1;min-width:160px;padding:0 22px;border-right:1px solid rgba(255,255,255,0.08);margin-bottom:10px;">'
            '<div style="font-size:12px;color:rgba(255,255,255,0.58);margin-bottom:8px;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;">Estimated fare</div>'
            f'<div style="font-size:2rem;font-weight:900;color:#8ef0b7;letter-spacing:-0.04em;line-height:1;">${CHEAPEST:.2f}</div>'
            "</div>"
            '<div style="flex:1;min-width:160px;padding-left:22px;margin-bottom:10px;">'
            '<div style="font-size:12px;color:rgba(255,255,255,0.58);margin-bottom:8px;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;">You save vs now</div>'
            f'<div style="font-size:2rem;font-weight:900;color:#8ef0b7;letter-spacing:-0.04em;line-height:1;">-${SAVING:.2f}</div>'
            "</div>"
            "</div>"
            "</div>"
        )
        st.markdown(result_html, unsafe_allow_html=True)

        # ── Bar chart ──────────────────────────────────────────────
        f_min  = min(FARES)
        f_max  = max(FARES)
        colors = [fare_color(f, f_min, f_max) for f in FARES]
        labels = [hour_label(h) for h in range(24)]

        fig = go.Figure(
            go.Bar(
                x=labels,
                y=FARES,
                marker_color=colors,
                marker_line_width=0,
                hovertemplate="<b>%{x}</b><br>Estimated fare: $%{y:.2f}<extra></extra>",
            )
        )
        fig.update_layout(
            title=dict(
                text="Fare Forecast — Departure Hour",
                font=dict(size=15, color="#f5f1e8", family="Inter"),
            ),
            xaxis=dict(
                tickangle=-45,
                tickfont=dict(size=12, color="#f5f1e8"),
                showgrid=False,
                zeroline=False,
                linecolor="#3a3a3a",
            ),
            yaxis=dict(
                title=dict(text="Estimated fare ($)", font=dict(color="#f5f1e8", size=13)),
                range=[0, f_max * 1.18],
                tickfont=dict(size=12, color="#f5f1e8"),
                gridcolor="#262626",
                zeroline=False,
            ),
            plot_bgcolor="#111111",
            paper_bgcolor="#111111",
            margin=dict(l=10, r=10, t=80, b=60),
            height=460,
            showlegend=False,
            bargap=0.3,
        )
        fig.add_hline(
            y=SURGE_NOW,
            line_dash="dot",
            line_color="#e04b43",
            line_width=1.5,
            annotation_text=f"Current fare ${SURGE_NOW:.2f}",
            annotation_position="top right",
            annotation_font_color="#e04b43",
            annotation_font_size=13,
        )
        fig.add_vrect(
            x0=hour_label(max(best_hour - 1, 0)),
            x1=hour_label(min(best_hour + 1, 23)),
            fillcolor="#18c45c",
            opacity=0.10,
            line_width=0,
            annotation_text="Best window",
            annotation_position="top left",
            annotation_font_color="#0f9f48",
            annotation_font_size=13,
            annotation_yshift=40,
        )
        st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})

        lc1, lc2, lc3 = st.columns(3)
        surge_thresh  = f_min + (f_max - f_min) * 0.72
        mod_thresh    = f_min + (f_max - f_min) * 0.23
        lc1.markdown(
            f"<span style='font-size:14px;color:#f5f1e8;'><b>Surge</b> · above ${surge_thresh:.2f}</span>",
            unsafe_allow_html=True,
        )
        lc2.markdown(
            f"<span style='font-size:14px;color:#f5f1e8;'><b>Moderate</b> · ${mod_thresh:.2f}–${surge_thresh:.2f}</span>",
            unsafe_allow_html=True,
        )
        lc3.markdown(
            f"<span style='font-size:14px;color:#f5f1e8;'><b>Cheap</b> · under ${mod_thresh:.2f}</span>",
            unsafe_allow_html=True,
        )

        # ── Savings cards ──────────────────────────────────────────
        st.markdown("<div class='sep'></div>", unsafe_allow_html=True)
        st.markdown("<div class='section-kicker'>Savings</div>", unsafe_allow_html=True)
        st.markdown("<div class='section-title'>Your Savings</div>", unsafe_allow_html=True)

        st.markdown(
            f"""
        <div class="savings-grid">
          <div class="savings-card">
            <div class="savings-kicker">Fare right now</div>
            <div class="savings-value">${SURGE_NOW:.2f}</div>
            <div class="savings-copy">Estimated fare if you book at {hour_label(current_hour)}.</div>
          </div>
          <div class="savings-card featured">
            <div class="savings-kicker" style="color:#8ef0b7;">Cheapest window</div>
            <div class="savings-value accent">${CHEAPEST:.2f}</div>
            <div class="savings-copy">Depart at {hour_label(best_hour)} — save ${SAVING:.2f} vs now.</div>
          </div>
          <div class="savings-card">
            <div class="savings-kicker">If you do this weekly</div>
            <div class="savings-value">${MONTHLY_SAVING:.2f}<span class="savings-unit">/mo</span></div>
            <div class="savings-copy">Estimated over 3 trips/week with 35% during surge.</div>
          </div>
        </div>
        """,
            unsafe_allow_html=True,
        )

        # ── Price alert ────────────────────────────────────────────
        st.markdown("<div class='sep'></div>", unsafe_allow_html=True)
        st.markdown("<div class='section-kicker'>Alerts</div>", unsafe_allow_html=True)
        st.markdown("<div class='section-title'>Price Alert</div>", unsafe_allow_html=True)

        alert_default = round(CHEAPEST + SAVING * 0.4, 2)
        nc1, nc2 = st.columns([3, 1])
        threshold = nc1.slider(
            "Alert me when the estimated fare drops below:",
            round(float(f_min), 1),
            round(float(f_max), 1),
            min(alert_default, round(float(f_max), 1)),
            0.5,
            format="$%.2f",
        )
        if nc2.button("Set Alert", use_container_width=True):
            st.session_state.alert_shown = True
            st.session_state.alert_threshold = threshold

        if st.session_state.alert_shown:
            st.markdown(
                f"""
<div style="
  animation: fadeSlideIn 0.28s cubic-bezier(0.22,1,0.36,1);
  background: linear-gradient(135deg, rgba(24,196,92,0.14) 0%, rgba(10,43,21,0.88) 100%);
  border: 1px solid rgba(24,196,92,0.42);
  border-radius: 22px;
  padding: 26px 28px;
  margin-top: 14px;
  box-shadow: 0 8px 32px rgba(0,0,0,0.28), 0 0 0 1px rgba(24,196,92,0.08) inset;
">
  <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px;">
    <span style="
      background:rgba(24,196,92,0.18);
      border:1px solid rgba(24,196,92,0.35);
      border-radius:999px;
      width:32px;height:32px;
      display:flex;align-items:center;justify-content:center;
      font-size:1rem;
    ">✓</span>
    <span style="color:#8ef0b7;font-size:0.78rem;font-weight:800;letter-spacing:0.13em;text-transform:uppercase;">Alert set</span>
  </div>
  <div style="color:#fff;font-size:1.45rem;font-weight:900;letter-spacing:-0.03em;line-height:1.1;margin-bottom:10px;">
    Watching for fares below <span style="color:#8ef0b7;">${st.session_state.alert_threshold:.2f}</span>
  </div>
  <div style="color:rgba(255,255,255,0.65);font-size:0.94rem;line-height:1.65;">
    You'll be notified when the estimated fare hits your target. &nbsp;
    <span style="color:#8ef0b7;font-weight:700;">Custom alerts are a Premium feature</span>
    &nbsp;— upgrade for unlimited alerts and a 7-day forecast.
  </div>
</div>
<style>
@keyframes fadeSlideIn {{
  from {{ opacity:0; transform:translateY(10px); }}
  to   {{ opacity:1; transform:translateY(0); }}
}}
</style>
""",
                unsafe_allow_html=True,
            )


# ══════════════════════════════════════════════════════════════════
# PREMIUM TAB  (unchanged)
# ══════════════════════════════════════════════════════════════════
with tab_premium:
    st.markdown("<div class='section-kicker'>Premium</div>", unsafe_allow_html=True)
    st.markdown("<div class='section-title'>Forecast further. Act faster.</div>", unsafe_allow_html=True)
    st.markdown(
        """
    <div class="cta-band" style="margin-top:10px;">
      <div>
        <div class="cta-title">Save on one trip.<br>See seven days ahead.</div>
        <div class="cta-copy">Most Premium users recover the monthly fee the first time they avoid a real surge. Alerts make the product feel automatic instead of manual.</div>
      </div>
      <div class="cta-chip">$2.20 / month</div>
    </div>
    """,
        unsafe_allow_html=True,
    )

    col_free, col_prem = st.columns(2)

    with col_free:
        st.markdown(
            """
        <div class="plan-free">
          <div class="plan-name">Free</div>
          <div class="plan-price">$0 <span>/ month</span></div>
          <ul class="plan-list">
            <li>6-hour fare forecast</li>
            <li>Cheapest window highlight</li>
            <li>Up to 3 trip lookups per day</li>
          </ul>
        </div>
        """,
            unsafe_allow_html=True,
        )

    with col_prem:
        st.markdown(
            """
        <div class="plan-prem">
          <div class="plan-name">Premium</div>
          <div class="plan-price">$2.20 <span>/ month</span></div>
          <ul class="plan-list">
            <li>6-hour fare forecast</li>
            <li>Cheapest window highlight</li>
            <li>Unlimited trip lookups</li>
            <li>7-day fare forecast</li>
            <li>Custom price alerts</li>
            <li>Priority support</li>
          </ul>
        </div>
        """,
            unsafe_allow_html=True,
        )

    st.markdown("<br>", unsafe_allow_html=True)

    if st.button("Start Free 7-Day Trial", type="primary", use_container_width=True):
        st.balloons()
        st.success("**Trial started!** No credit card required. Cancel anytime.")
    st.caption("No credit card required · Cancel anytime · Billed monthly after trial")

    st.markdown("<div class='sep'></div>", unsafe_allow_html=True)
    st.markdown("<div class='section-kicker'>Alerts preview</div>", unsafe_allow_html=True)
    st.markdown(
        """
    <div class="a-card">
      <div>
        <div class="a-title">Surge alert</div>
        <div class="a-body">Fares near you are 40% above average. Cheapest window: after 7 pm. Waiting could save $4.90.</div>
      </div>
    </div>
    <div class="a-card">
      <div>
        <div class="a-title">Your window is now</div>
        <div class="a-body">Fares just dropped to $9. Book in the next 30 minutes to lock in the saving.</div>
      </div>
    </div>
    <div class="a-card">
      <div>
        <div class="a-title">Weekly recap</div>
        <div class="a-body">You saved $18 this week using FareSaver. Keep the habit and the savings compound.</div>
      </div>
    </div>
    """,
        unsafe_allow_html=True,
    )

    st.markdown("<div class='sep'></div>", unsafe_allow_html=True)
    st.markdown("<div class='section-kicker'>Social proof</div>", unsafe_allow_html=True)
    st.markdown(
        """
    <div class="testi-grid">
      <div class="testi">
        <div class="testi-q">"</div>
        <div class="testi-copy">I shifted my commute by 25 minutes and cut my weekly Uber bill almost in half.</div>
        <div class="testi-name">Sarah K.</div>
        <div class="testi-role">U of T student</div>
      </div>
      <div class="testi">
        <div class="testi-q">"</div>
        <div class="testi-copy">The alert is the killer feature. It pinged me and I saved $6 on the spot.</div>
        <div class="testi-name">Marcus L.</div>
        <div class="testi-role">Downtown professional</div>
      </div>
      <div class="testi">
        <div class="testi-q">"</div>
        <div class="testi-copy">$2.20 pays for itself on the first ride. It feels obvious once you try it.</div>
        <div class="testi-name">Jamie T.</div>
        <div class="testi-role">Frequent traveller</div>
      </div>
    </div>
    """,
        unsafe_allow_html=True,
    )


# ══════════════════════════════════════════════════════════════════
# ABOUT TAB  (unchanged)
# ══════════════════════════════════════════════════════════════════
with tab_about:
    st.markdown("<div class='section-kicker'>About FareSaver</div>", unsafe_allow_html=True)
    st.markdown("<div class='section-title'>Riders deserve pricing clarity.</div>", unsafe_allow_html=True)
    st.markdown(
        """
    <div class="cta-band" style="margin-top:10px;">
      <div>
        <div class="cta-title">We built the page Uber never gives you.</div>
        <div class="cta-copy">Ride-hailing platforms know when prices will spike. Riders usually do not. FareSaver closes that gap with a forecast that feels simple enough for everyday use.</div>
      </div>
      <div class="cta-chip">Consumer-first pricing</div>
    </div>
    """,
        unsafe_allow_html=True,
    )

    st.markdown("<div class='section-kicker'>Mission</div>", unsafe_allow_html=True)
    st.markdown(
        """
    <div class="persona">
      <div>
        <div class="p-title">More transparency, less guesswork</div>
        <div class="p-body">A $5 saving per trip is not trivial. For students and city commuters, that is real money back every month. Our job is to make timing visible before you book.</div>
      </div>
    </div>
    """,
        unsafe_allow_html=True,
    )

    st.markdown("<div class='sep'></div>", unsafe_allow_html=True)
    st.markdown("<div class='section-kicker'>What we stand for</div>", unsafe_allow_html=True)
    st.markdown(
        """
    <div class="val-grid">
      <div class="val-card">
        <div class="val-title">Transparency</div>
        <div class="val-body">Every savings claim comes from actual fare analysis. No vague promises, no invented numbers.</div>
      </div>
      <div class="val-card">
        <div class="val-title">Simplicity</div>
        <div class="val-body">Enter your route, read the forecast, and act. The value should be obvious without needing a dashboard tutorial.</div>
      </div>
      <div class="val-card">
        <div class="val-title">Fairness</div>
        <div class="val-body">The core product stays free. Premium is there for riders who want alerts and deeper planning, not for basic access.</div>
      </div>
    </div>
    """,
        unsafe_allow_html=True,
    )

    st.markdown("<div class='sep'></div>", unsafe_allow_html=True)
    st.markdown("<div class='section-kicker'>The team</div>", unsafe_allow_html=True)
    st.markdown(
        """
    <div class="team-card">
      <div class="avatar">X</div>
      <div class="team-name">Xuezhu Liu</div>
      <div class="team-role">Founder & CEO</div>
      <div class="team-bio">Formal RBC analyst and daily Uber rider who got tired of opening the app and seeing surge at exactly the wrong moment.</div>
    </div>
    """,
        unsafe_allow_html=True,
    )

    st.markdown("<div class='sep'></div>", unsafe_allow_html=True)
    st.markdown(
        """
    <div class="d-cta">
      <div class="dt">Want to ride along?</div>
      <div class="ds">Riders, investors, and partners can reach us at <strong style="color:#fff;">xzhu.liu@rotman.utoronto.ca</strong>.</div>
    </div>
    """,
        unsafe_allow_html=True,
    )


st.markdown(
    "<div class='foot'>FareSaver · 8542 Marketing Strategy</div>",
    unsafe_allow_html=True,
)
