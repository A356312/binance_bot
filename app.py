import os
import math
from fastapi import FastAPI, Request, HTTPException
from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceOrderException
import uvicorn

# --- Konfiguration über Umgebungsvariablen (Railway) ---
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")
WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN")  # optionaler einfacher Schutz
SYMBOL = os.getenv("SYMBOL", "ETHUSDC")
BASE_ASSET = os.getenv("BASE_ASSET", "ETH")
QUOTE_ASSET = os.getenv("QUOTE_ASSET", "USDC")

if not BINANCE_API_KEY or not BINANCE_API_SECRET:
    raise RuntimeError("BINANCE_API_KEY und BINANCE_API_SECRET müssen gesetzt sein.")

client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)
app = FastAPI()


# --- Hilfsfunktionen ---
def truncate(number: float, decimals: int) -> float:
    factor = 10 ** decimals
    return math.floor(number * factor) / factor

def round_step_size(quantity: float, step_size: float) -> float:
    return math.floor(quantity / step_size) * step_size

def get_symbol_filters(symbol: str):
    info = client.get_symbol_info(symbol)
    if not info:
        raise HTTPException(status_code=400, detail=f"Symbol nicht gefunden: {symbol}")

    step_size = None
    min_qty = None
    min_notional = None

    for f in info.get("filters", []):
        if f.get("filterType") == "LOT_SIZE":
            step_size = float(f["stepSize"])
            min_qty = float(f["minQty"])
        # Je nach Symbol/Ära heißt der Filter "MIN_NOTIONAL" oder "NOTIONAL"
        if f.get("filterType") in ("MIN_NOTIONAL", "NOTIONAL"):
            # Feldname bleibt minNotional
            if "minNotional" in f:
                min_notional = float(f["minNotional"])

    return step_size, min_qty, min_notional


# --- Healthcheck ---
@app.get("/")
async def root():
    return {"status": "ok", "symbol": SYMBOL}


# --- TradingView Webhook ---
# Beispiel-Payload: {"token":"<dein_token>", "message":"buy"}
@app.post("/webhook")
async def webhook(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # optionaler Token-Check
    if WEBHOOK_TOKEN:
        if body.get("token") != WEBHOOK_TOKEN:
            raise HTTPException(status_code=401, detail="Ungültiger Token")

    # Textfeld kann variieren: "message", "action" o.ä.
    text = (body.get("message") or body.get("action") or "").strip().lower()
    if text not in ("buy", "sell"):
        raise HTTPException(status_code=400, detail="Erwarte 'buy' oder 'sell' in der Nachricht")

    step_size, min_qty, min_notional = get_symbol_filters(SYMBOL)

    try:
        if text == "buy":
            # verfügbares USDC-Guthaben
            usdc = client.get_asset_balance(asset=QUOTE_ASSET)
            usdc_free = float(usdc["free"])
            # 98% einsetzen, auf 2 Nachkommastellen kürzen (USDC)
            quote_qty = truncate(usdc_free * 0.98, 2)

            # Mindestnotional prüfen, falls bekannt (z.B. 5 USDC)
            if min_notional is not None and quote_qty < min_notional:
                raise HTTPException(
                    status_code=400,
                    detail=f"Guthaben zu gering. quoteOrderQty={quote_qty} < minNotional={min_notional}",
                )
            if quote_qty < 5:  # zusätzliche einfache Schranke
                raise HTTPException(status_code=400, detail=f"Guthaben zu gering. USDC={usdc_free}")

            order = client.create_order(
                symbol=SYMBOL,
                side=Client.SIDE_BUY,
                type=Client.ORDER_TYPE_MARKET,
                quoteOrderQty=quote_qty,
            )

        elif text == "sell":
            if step_size is None or min_qty is None:
                raise HTTPException(status_code=400, detail="Konnte LOT_SIZE Filter nicht bestimmen.")

            eth = client.get_asset_balance(asset=BASE_ASSET)
            raw_qty = float(eth["free"])
            qty = round_step_size(raw_qty, float(step_size))

            # auf sinnvolle Dezimalstellen runden (aus step_size ableiten)
            step_str = f"{step_size:.10f}".rstrip("0")
            decimals = len(step_str.split(".")[1]) if "." in step_str else 0
            qty = round(qty, decimals)

            if qty < min_qty:
                raise HTTPException(
                    status_code=400,
                    detail=f"Zu wenig {BASE_ASSET}. Menge={qty} < minQty={min_qty}",
                )

            order = client.create_order(
                symbol=SYMBOL,
                side=Client.SIDE_SELL,
                type=Client.ORDER_TYPE_MARKET,
                quantity=qty,
            )

        # kompaktes Order-Feedback
        return {
            "status": "ok",
            "action": text,
            "symbol": SYMBOL,
            "executedQty": order.get("executedQty"),
            "side": order.get("side"),
            "orderStatus": order.get("status"),
            "avgPrice": (order["fills"][0]["price"] if order.get("fills") else None),
        }

    except (BinanceAPIException, BinanceOrderException) as e:
        # Binance-Fehler sauber durchreichen
        raise HTTPException(status_code=400, detail=f"Binance Error: {str(e)}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")


if __name__ == "__main__":
    # Railway gibt PORT vor
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("app:app", host="0.0.0.0", port=port)
