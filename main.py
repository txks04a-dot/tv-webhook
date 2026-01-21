import os
import json
from datetime import datetime, timezone
from flask import Flask, request, jsonify

app = Flask(__name__)

# -----------------------------
# Configuration (env variables)
# -----------------------------
LOG_PATH = os.environ.get("LOG_PATH", "alerts.ndjson")
COOLDOWN_SECONDS = int(os.environ.get("COOLDOWN_SECONDS", "120"))
MIN_CONFIDENCE = int(os.environ.get("MIN_CONFIDENCE", "80"))


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

def last_alert_time_for_symbol(symbol):
    """Scan log file backwards and return last alert time for symbol."""
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

def calculate_confidence(payload, cooldown_ok):
    score = 0

    # Direction present
    if payload.get("direction") in ("CALL", "PUT"):
        score += 20

    # Timeframe alignment
    if str(payload.get("timeframe")) == "1":
        score += 20

    # Expiry alignment
    if payload.get("expiry_minutes") == 1:
        score += 20

    # Cooldown passed
    if cooldown_ok:
        score += 20

    # Symbol sanity check
    if payload.get("symbol"):
        score += 20

    return score


# -----------------------------
# Routes
# -----------------------------
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True)

    if data is None:
        return jsonify({"status": "error", "message": "Expected JSON body"}), 400

    symbol = data.get("symbol")
    now = utc_now()

    last_time = last_alert_time_for_symbol(symbol)
    cooldown_ok = True
    seconds_since_last = None

    if last_time:
        seconds_since_last = (now - last_time).total_seconds()
        if seconds_since_last < COOLDOWN_SECONDS:
            cooldown_ok = False

    confidence = calculate_confidence(data, cooldown_ok)
allowed = (confidence >= MIN_CONFIDENCE) and cooldown_ok

    record = {
    "received_at_utc": utc_now_iso(),
    "payload": safe_json(data),
    "cooldown": {
        "cooldown_seconds": COOLDOWN_SECONDS,
        "cooldown_ok": cooldown_ok,
        "seconds_since_last": seconds_since_last
    },
    "confidence": confidence,
    "allowed": allowed
}




    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

    direction = data.get("direction")
    tf = data.get("timeframe")
    exp = data.get("expiry_minutes")

    app.logger.warning(
    f"ALERT | {symbol} | {direction} | TF={tf} | "
    f"EXP={exp}m | cooldown_ok={cooldown_ok} | "
    f"confidence={confidence} | allowed={allowed}"
)




    return jsonify({
    "status": "ok",
    "cooldown_ok": cooldown_ok,
    "confidence": confidence,
    "allowed": allowed
}), 200


@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "tv-webhook"}), 200

@app.route("/count", methods=["GET"])
def count():
    try:
        if not os.path.exists(LOG_PATH):
            return jsonify({"count": 0}), 200
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            c = sum(1 for _ in f)
        return jsonify({"count": c}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)




