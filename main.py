import os
import json
from datetime import datetime, timezone
from flask import Flask, request, jsonify

app = Flask(__name__)

# File path for logs (NDJSON: 1 JSON object per line)
LOG_PATH = os.environ.get("LOG_PATH", "alerts.ndjson")

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def safe_json(obj):
    """Make sure we can always write something, even if obj isn't perfectly JSON serializable."""
    try:
        json.dumps(obj)
        return obj
    except TypeError:
        return {"_nonserializable": str(obj)}

@app.route("/webhook", methods=["POST"])
def webhook():
    # Accept JSON body
    data = request.get_json(silent=True)

    if data is None:
        return jsonify({"status": "error", "message": "Expected JSON body"}), 400

    # Build a normalized log record
    record = {
        "received_at_utc": utc_now_iso(),
        "remote_addr": request.headers.get("X-Forwarded-For", request.remote_addr),
        "user_agent": request.headers.get("User-Agent"),
        "payload": safe_json(data),
    }

    # Append to NDJSON file
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        # Still return error so you can see problems quickly
        return jsonify({"status": "error", "message": f"Failed to write log: {e}"}), 500

    # Also print a short line to Render logs (handy for live monitoring)
    direction = data.get("direction")
    symbol = data.get("symbol")
    tf = data.get("timeframe")
    expiry = data.get("expiry_minutes")
    print(f"ALERT OK | {symbol} | TF={tf} | {direction} | EXP={expiry}m")

    return jsonify({"status": "ok"}), 200

@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "message": "tv-webhook is running"}), 200

if __name__ == "__main__":
    # Render binds to port 10000 in your setup, keep it consistent.
    app.run(host="0.0.0.0", port=10000)
