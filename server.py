# HedgeAI Live Fetcher Server
# Fetches live NIFTY option and spot data via Zerodha Kite API
# and serves it for your Shopify HedgeAI UI.
# Author: Jiyansh & ChatGPT

import os
import json
import math
from datetime import datetime
from typing import Optional, Dict, Any
from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.middleware.cors import CORSMiddleware
from kiteconnect import KiteConnect
from apscheduler.schedulers.background import BackgroundScheduler
from scipy.stats import norm
from scipy.optimize import brentq

# ================= CONFIG =================
KITE_API_KEY = os.getenv("KITE_API_KEY", "").strip()
KITE_API_SECRET = os.getenv("KITE_API_SECRET", "").strip()
ADMIN_KEY = os.getenv("ADMIN_KEY", "").strip()  # protect admin routes
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
CACHE_FILE = os.getenv("CACHE_FILE", "latest.json")
ACCESS_TOKEN_FILE = os.getenv("ACCESS_TOKEN_FILE", "access_token.txt")

if not KITE_API_KEY:
    raise RuntimeError("Set KITE_API_KEY in Render environment vars.")
if not ADMIN_KEY:
    raise RuntimeError("Set ADMIN_KEY env var (any strong secret).")

app = FastAPI(title="HedgeAI Live Fetcher")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # later limit to your Shopify domain
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ================= Black-Scholes helpers =================
def bs_price(is_call, S, K, T, r, sigma, q=0.0):
    if T <= 0:
        return max(0.0, (S - K) if is_call else (K - S))
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if is_call:
        return S * math.exp(-q*T) * norm.cdf(d1) - K * math.exp(-r*T) * norm.cdf(d2)
    else:
        return K * math.exp(-r*T) * norm.cdf(-d2) - S * math.exp(-q*T) * norm.cdf(-d1)

def implied_vol(mkt_price, is_call, S, K, T, r, q=0.0):
    if mkt_price is None or mkt_price <= 0 or T <= 0:
        return None
    try:
        f = lambda sig: bs_price(is_call, S, K, T, r, sig, q) - mkt_price
        return float(brentq(f, 1e-6, 5.0, maxiter=200))
    except Exception:
        return None

# ================= Kite Helpers =================
def read_access_token() -> Optional[str]:
    if os.path.exists(ACCESS_TOKEN_FILE):
        return open(ACCESS_TOKEN_FILE).read().strip()
    return None

def write_access_token(tok: str):
    with open(ACCESS_TOKEN_FILE, "w") as f:
        f.write(tok.strip())

def make_kite_client() -> KiteConnect:
    kite = KiteConnect(api_key=KITE_API_KEY)
    tok = read_access_token()
    if tok:
        kite.set_access_token(tok)
    return kite

# ================= Market Fetch Logic =================
def fetch_market_snapshot(underlying="NIFTY 50", strikes_around=5) -> Dict[str, Any]:
    kite = make_kite_client()
    data = {"fetched_at": datetime.utcnow().isoformat() + "Z", "underlying": underlying}
    try:
        # --- Spot quote ---
        spot_key = f"NSE:{underlying}"
        q = kite.quote(spot_key)
        spot = q[spot_key]["last_price"]
        data["spot"] = spot

        # --- Instruments list ---
        instruments = kite.instruments("NFO")
        opts = [i for i in instruments if i["name"] == "NIFTY" and i["segment"] == "NFO-OPT"]
        expiries = sorted({o["expiry"] for o in opts})
        expiry = expiries[0] if expiries else None
        if not expiry:
            data["error"] = "No expiry found"
            return data

        strikes = sorted({o["strike"] for o in opts if o["expiry"] == expiry})
        near = sorted(strikes, key=lambda s: abs(s - spot))[:strikes_around]
        chain_syms = [
            o["tradingsymbol"] for o in opts if o["strike"] in near and o["expiry"] == expiry
        ]

        # --- Quotes for those ---
        quotes = kite.quote(chain_syms)
        rows = []
        for sym, qd in quotes.items():
            last = qd.get("last_price")
            instrument = next(i for i in opts if i["tradingsymbol"] == sym)
            strike = instrument["strike"]
            typ = instrument["instrument_type"]
            ex = datetime.strptime(instrument["expiry"], "%Y-%m-%d")
            T = max((ex - datetime.utcnow()).days / 365.0, 0.0001)
            iv = implied_vol(last, typ == "CE", spot, strike, T, 0.06)
            rows.append({
                "symbol": sym,
                "strike": strike,
                "type": typ,
                "last": last,
                "iv": iv,
                "expiry": instrument["expiry"]
            })

        data["expiry"] = expiry
        data["chain"] = rows
        return data

    except Exception as e:
        return {"error": str(e)}

# ================= Polling & Caching =================
scheduler = BackgroundScheduler()
latest_cache = {"time": None, "data": None}

def poll_job():
    try:
        d = fetch_market_snapshot()
        latest_cache["time"] = datetime.utcnow().isoformat() + "Z"
        latest_cache["data"] = d
        with open(CACHE_FILE, "w") as f:
            json.dump({"cached_at": latest_cache["time"], "data": d}, f)
        print("[poll] Updated at", latest_cache["time"])
    except Exception as e:
        print("[poll error]", e)

scheduler.add_job(poll_job, "interval", seconds=POLL_INTERVAL_SECONDS, max_instances=1)
scheduler.start()

# Initial poll at startup
try:
    poll_job()
except Exception as e:
    print("initial poll failed:", e)

# ================= API =================
def check_admin_key(x_admin_key: Optional[str]):
    if x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

@app.post("/admin/set_token")
async def set_token(req: Request, x_admin_key: Optional[str] = Header(None)):
    check_admin_key(x_admin_key)
    body = await req.json()
    tok = body.get("access_token")
    if not tok or len(tok) < 10:
        raise HTTPException(status_code=400, detail="invalid token")
    write_access_token(tok)
    poll_job()
    return {"status": "ok", "message": "token saved and fetch started"}

@app.get("/latest")
async def latest():
    if latest_cache["data"] is None:
        if os.path.exists(CACHE_FILE):
            return json.load(open(CACHE_FILE))
        raise HTTPException(status_code=503, detail="No cached data yet")
    return {"cached_at": latest_cache["time"], "data": latest_cache["data"]}

@app.get("/health")
async def health():
    return {"status": "ok", "token_present": bool(read_access_token())}

# graceful shutdown
import atexit
@atexit.register
def shutdown():
    try:
        scheduler.shutdown()
    except:
        pass
