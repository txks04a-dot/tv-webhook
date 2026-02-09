import os
import json
import uuid
from datetime import datetime, timezone, date
from flask import Flask, request, jsonify

app = Flask(__name__)

# -----------------------------
# Configuration (env variables)
# -----------------------------
LOG_PATH = os.environ.get("LOG_PATH", "alerts.ndjson")
TRADES_PATH = os.environ.get("TRADES_PATH", "trades.ndjson")

COOLDOWN_SECONDS = int(os.environ.get("COOLDOWN_SECONDS", "300"))
MIN_CONFIDENCE = int(os.environ.get("MIN_CONFIDENCE", "80"))

EXECUTION_MODE = os.environ.get("EXECUTION_MODE", "paper").lower()  # paper | browser (later)
SYMBOL_ALLOWLIST = set(
    s.strip().upper()
    for s in os.environ.get("SYMBOL_ALLOWLIST", "").split(",")
    if s.strip()
)

MAX_TRADES_PER_DAY = int(os.environ.get("MAX_TRADES_PER_DAY", "20"))
MAX_OPEN_TRADES_PER_SYMBOL = int(os.environ.get("MAX_OPEN_TRADES_PER_SYMBOL", "1"))

PAPER_STAKE = float(os.environ.get("PAPER_STAKE", "1"))
PAPER_PAYOUT = float(os.environ.get("PAPER_PAYOUT", "0.80"))  # 0.80 = 80% payout


# -----------------------------
# Helpers
# -----------------------------
def utc_now():
    return datetime.now(timezone.utc)

def utc_now_iso():
    return utc_now().isoformat()

def safe_json(obj):
    try:
        json.dumps(obj)
        return obj
    except TypeError:
        return {"_nonserializable": str(obj)}

def parse_iso(ts):
    try:
        return datetime.fromisoformat(ts)
    except Exception:
        return None

def append_ndjson(path, record: dict):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

def read_ndjson(path):
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out

def last_alert_time_for_symbol(symbol):
    if not os.path.exists(LOG_PATH):
        return None
    try:
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return None

    for line in reversed(lines):
        try:
            record = json.loads(line)
            payload = record.get("payload", {})
            if payload.get("symbol") == symbol:
                ts = record.get("received_at_utc")
                return parse_iso(ts)
        except Exception:
            continue

    return None

def today_utc_str():
    return date.today().isoformat()  # OK for demo; UTC date boundary is close enough for now

def count_trades_today():
    trades = read_ndjson(TRADES_PATH)
    t = today_utc_str()
    return sum(1 for tr in trades if (tr.get("created_date_utc") == t))

def open_trades_for_symbol(symbol):
    trades = read_ndjson(TRADES_PATH)
    return [
        tr for tr in trades
        if tr.get("symbol") == symbol and tr.get("status") == "OPEN"
    ]

def calculate_confidence_breakdown(payload, cooldown_ok):
    """
    Confidence is driven by what Pine sent + our safety gates.
    Weights here assume Pine already applied your confluence logic.
    """
    breakdown = {"supertrend": 0, "adx": 0, "stoch": 0, "keltner": 0, "cooldown": 0}

    # Supertrend alignment (Pine sends supertrend_dir + direction)
    direction = payload.get("direction")
    st_dir = payload.get("supertrend_dir")
    if direction in ("CALL", "PUT") and st_dir in ("CALL", "PUT"):
        breakdown["supertrend"] = 25 if direction == st_dir else 10

    # ADX quality
    try:
        adx = float(payload.get("adx"))
    except Exception:
        adx = None

    if adx is not None:
        if adx >= 25:
            breakdown["adx"] = 25
        elif adx >= 20:
            breakdown["adx"] = 18
        elif adx >= 15:
            breakdown["adx"] = 10
        else:
            breakdown["adx"] = 0

    # Stoch sanity (Pine sends stoch_k / stoch_d)
    try:
        k = float(payload.get("stoch_k"))
        d = float(payload.get("stoch_d"))
        if direction == "CALL" and k > d:
            breakdown["stoch"] = 20
        elif direction == "PUT" and k < d:
            breakdown["stoch"] = 20
        else:
            breakdown["stoch"] = 5
    except Exception:
        breakdown["stoch"] = 0

    # Keltner position
    kp = payload.get("keltner_pos")
    if direction == "CALL" and kp == "lower":
        breakdown["keltner"] = 20
    elif direction == "PUT" and kp == "upper":
        breakdown["keltner"] = 20
    else:
        breakdown["keltner"] = 5

    breakdown["cooldown"] = 10 if cooldown_ok else 0

    confidence = sum(breakdown.values())
    return confidence, breakdown


# -----------------------------
# Routes
# -----------------------------
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True)
    if data is None:
        return jsonify({"status": "error", "message": "Expected JSON body"}), 400

    # Normalize / extract
    symbol = str(data.get("symbol", "")).upper().strip()
    direction = data.get("direction")

    now = utc_now()

    # Cooldown check (per symbol)
    last_time = last_alert_time_for_symbol(symbol)
    cooldown_ok = True
    seconds_since_last = None
    if last_time:
        seconds_since_last = (now - last_time).total_seconds()
        if seconds_since_last < COOLDOWN_SECONDS:
            cooldown_ok = False

    # Confidence scoring (uses Pine fields + cooldown)
    confidence, breakdown = calculate_confidence_breakdown(data, cooldown_ok)

    # Core allow decision
    allowed_by_conf = confidence >= MIN_CONFIDENCE
    allowed = allowed_by_conf and cooldown_ok

    # Additional demo bot safety rules
    reason = []
    if SYMBOL_ALLOWLIST and symbol not in SYMBOL_ALLOWLIST:
        allowed = False
        reason.append("symbol_not_allowed")

    if direction not in ("CALL", "PUT"):
        allowed = False
        reason.append("bad_direction")

    trades_today = count_trades_today()
    if trades_today >= MAX_TRADES_PER_DAY:
        allowed = False
        reason.append("max_trades_per_day_hit")

    if len(open_trades_for_symbol(symbol)) >= MAX_OPEN_TRADES_PER_SYMBOL:
        allowed = False
        reason.append("open_trade_limit_hit")

    # Log alert record
    record = {
        "received_at_utc": utc_now_iso(),
        "payload": safe_json(data),
        "cooldown": {
            "cooldown_seconds": COOLDOWN_SECONDS,
            "cooldown_ok": cooldown_ok,
            "seconds_since_last": seconds_since_last
        },
        "confidence": confidence,
        "allowed": allowed,
        "breakdown": breakdown,
        "blocked_reasons": reason
    }

    try:
        append_ndjson(LOG_PATH, record)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

    # If allowed, create a paper trade record (OPEN)
    trade_id = None
    if allowed and EXECUTION_MODE == "paper":
        trade_id = str(uuid.uuid4())
        trade = {
            "id": trade_id,
            "created_at_utc": utc_now_iso(),
            "created_date_utc": today_utc_str(),
            "mode": "paper",
            "symbol": symbol,
            "direction": direction,
            "timeframe": data.get("timeframe"),
            "expiry_minutes": data.get("expiry_minutes"),
            "stake": PAPER_STAKE,
            "payout": PAPER_PAYOUT,
            "confidence": confidence,
            "breakdown": breakdown,
            "status": "OPEN",
            "source_alert_received_at_utc": record["received_at_utc"]
        }
        try:
            append_ndjson(TRADES_PATH, trade)
        except Exception as e:
            # Still OK to return; alert was logged.
            app.logger.warning(f"Trade log write failed: {e}")

    # Log line (Render)
    app.logger.warning(
        f"ALERT | {symbol} | {direction} | TF={data.get('timeframe')} | "
        f"EXP={data.get('expiry_minutes')}m | cooldown_ok={cooldown_ok} | "
        f"confidence={confidence} | allowed={allowed} | breakdown={breakdown} | reasons={reason}"
    )

    return jsonify({
        "status": "ok",
        "symbol": symbol,
        "cooldown_ok": cooldown_ok,
        "confidence": confidence,
        "allowed": allowed,
        "breakdown": breakdown,
        "blocked_reasons": reason,
        "trade_id": trade_id
    }), 200


@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "tv-webhook", "mode": EXECUTION_MODE}), 200


@app.route("/open_trades", methods=["GET"])
def open_trades():
    trades = read_ndjson(TRADES_PATH)
    opens = [t for t in trades if t.get("status") == "OPEN"]
    return jsonify({"open_trades": opens, "count": len(opens)}), 200


@app.route("/close_trade", methods=["POST"])
def close_trade():
    """
    Demo helper: manually close a paper trade.
    Body: {"id":"...","result":"WIN"|"LOSS"|"PUSH","notes":"optional"}
    """
    body = request.get_json(silent=True) or {}
    trade_id = body.get("id")
    result = str(body.get("result", "")).upper().strip()
    notes = body.get("notes")

    if not trade_id or result not in ("WIN", "LOSS", "PUSH"):
        return jsonify({"status": "error", "message": "Provide id and result WIN/LOSS/PUSH"}), 400

    trades = read_ndjson(TRADES_PATH)
    updated = False
    for t in trades:
        if t.get("id") == trade_id and t.get("status") == "OPEN":
            t["status"] = "CLOSED"
            t["closed_at_utc"] = utc_now_iso()
            t["result"] = result
            if notes:
                t["notes"] = notes
            updated = True
            break

    if not updated:
        return jsonify({"status": "error", "message": "Open trade not found"}), 404

    # Rewrite file (simple + safe for small demo volume)
    try:
        with open(TRADES_PATH, "w", encoding="utf-8") as f:
            for t in trades:
                f.write(json.dumps(t, ensure_ascii=False) + "\n")
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

    return jsonify({"status": "ok", "closed_id": trade_id, "result": result}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
