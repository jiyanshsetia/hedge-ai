from fastapi import FastAPI, Request, Header
from fastapi.responses import JSONResponse
from apscheduler.schedulers.background import BackgroundScheduler
from kiteconnect import KiteConnect
import requests, os, time

app = FastAPI()

# Environment variables (set these in Render)
KITE_API_KEY = os.getenv("KITE_API_KEY")
KITE_API_SECRET = os.getenv("KITE_API_SECRET")
ADMIN_KEY = os.getenv("ADMIN_KEY", "HedgeAI_Admin_2025!")
FETCH_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", 60))

kite = None
access_token = None
cached_data = {}
scheduler = BackgroundScheduler()

def fetch_data():
    global kite, cached_data
    if not kite:
        return
    try:
        profile = kite.profile()
        instruments = kite.ltp(["NSE:NIFTY 50", "NSE:BANKNIFTY"])
        spot = {k:v['last_price'] for k,v in instruments.items()}

        # Get option chain for NIFTY
        chain = kite.quote("NSE:NIFTY 50")
        cached_data = {
            "cached_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "spot": spot,
            "chain": chain
        }
        print("✅ Data refreshed at", cached_data["cached_at"])
    except Exception as e:
        print("❌ Error fetching data:", e)

@app.get("/")
def home():
    return {"status": "ok", "message": "HedgeAI Live Server Running"}

@app.get("/latest")
def latest():
    if not cached_data:
        return {"message": "No cached data yet"}
    return {"status": "ok", "data": cached_data}

@app.post("/admin/set_token")
async def set_token(req: Request, x_admin_key: str = Header(None)):
    global kite, access_token
    if x_admin_key != ADMIN_KEY:
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    body = await req.json()
    token = body.get("access_token")
    if not token:
        return JSONResponse(status_code=400, content={"error": "missing token"})
    access_token = token
    kite = KiteConnect(api_key=KITE_API_KEY)
    kite.set_access_token(access_token)
    fetch_data()
    if not scheduler.running:
        scheduler.add_job(fetch_data, "interval", seconds=FETCH_INTERVAL)
        scheduler.start()
    return {"status": "ok", "message": "token saved and fetch started"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=10000)
