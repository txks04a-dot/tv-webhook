import os
import json
import uuid
from datetime import datetime, timezone
from flask import Flask, request, jsonify

app = Flask(__name__)

# -----------------------------
# Configuration (env variables)
# -----------------------------
LOG_PATH = os.environ.get("LOG_PATH", "alerts.ndjson")
TRADES_PATH = os.environ.get("TRADES_PATH", "trades.ndjson")

COOLDOWN_SECONDS = int(os.environ.get("COOLDOWN_SECONDS", "300"))
MIN_CONFIDENCE = int(os.environ.get("MIN_CONFIDENCE", "80"))

EXECUTION_MODE = os.environ.get("EXECUTION_MODE", "paper").strip().lower()  # paper | off
SYMBOL_ALLOWLIST = os.environ.get("SYMBOL_ALLOWLIST", "").strip()

MAX_TRADES_PER_DAY = int(os.environ.get("MAX_TRADES_PER_DAY", "20"))
MAX_OPEN_TRADES_PER_SYMBOL = int(os.environ.get("MAX_OPEN_TRADES_PER_SYMBOL", "1"))

PAPER_STAKE = float(os.environ.get("PAPER_STAKE", "1"))
PAPER_PAYOUT = float(os.environ.get("PAPER_PAYOUT", "0.80"))

# Parse allowlist
ALLOWLIST = set()
if SYMBOL_ALLOWLIST:
    ALLOWLIST = {s.strip().upper() for s in SYMBOL_ALLOWLIST.split(",") if s.strip()}

# -----------------------------
# Helpers
# -----------------------------
def utc_now() -> datetime:
    return datetime.now(timezone.utc)

def utc_now_iso() -> str:
    return utc_now().isoformat()

def parse_iso(ts: str):
    try:
        return datetime.fromisoformat(ts)
    except Exception:
        return None

def safe_json(obj):
    try:
        json.dumps(obj)
        return obj
    except TypeError:
        return {"_nonserializable": str(obj)}

def ndjson_append(path: str, record: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(path) else None
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

def ndjson_read_all(path: str):
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

def normalize_symbol(sym):
    if not sym:
        return None
    return str(sym).strip().upper()

def today_utc_date_str() -> str:
    return utc_now().date().isoformat()

# -----------------------------
# Cooldown / Limits
# -----------------------------
def last_signal_time_for_symbol(symbol: str):
    """Scan alerts log backwards and return last SIGNAL time for symbol."""
    if not os.path.exists(LOG_PATH):
        return None
    try:
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return None

    for line in reversed(lines):
        try:
            rec = json.loads(line)
            if rec.get("event_type") != "signal":
                continue
            payload = rec.get("payload", {})
            if normalize_symbol(payload.get("symbol")) == symbol:
                ts = rec.get("received_at_utc")
                return parse_iso(ts)
        except Exception:
            continue
    return None

def trades_today_count():
    """Count number of paper trades created today (UTC)."""
    if not os.path.exists(TRADES_PATH):
        return 0
    c = 0
    for t in ndjson_read_all(TRADES_PATH):
        if t.get("created_date_utc") == today_utc_date_str():
            c += 1
    return c

def open_trades_for_symbol(symbol: str):
    """Return OPEN trades for a given symbol."""
    symbol = normalize_symbol(symbol)
    if not os.path.exists(TRADES_PATH):
        return []
    open_ts = []
    for t in ndjson_read_all(TRADES_PATH):
        if t.get("status") == "OPEN" and normalize_symbol(t.get("symbol")) == symbol:
            open_ts.append(t)
    return open_ts

# -----------------------------
# Confidence Scoring
# -----------------------------
def calculate_confidence(payload: dict, cooldown_ok: bool):
    """
    Returns: (confidence:int, breakdown:dict[str,int], reasons:list[str])
    Score out of 100.
    We score what we can from the payload we receive.
    """
    breakdown = {"supertrend": 0, "adx": 0, "stoch": 0, "keltner": 0, "cooldown": 0}
    reasons = []

    direction = payload.get("direction")
    supertrend_dir = payload.get("supertrend_dir")
    adx = payload.get("adx")
    stoch_k = payload.get("stoch_k")
    stoch_d = payload.get("stoch_d")
    kpos = payload.get("keltner_pos")

    # Supertrend alignment (25)
    if direction in ("CALL", "PUT") and supertrend_dir in ("CALL", "PUT"):
        if direction == supertrend_dir:
            breakdown["supertrend"] = 25
        else:
            reasons.append("direction != supertrend_dir")

    # ADX sanity (up to 25)
    try:
        adx_f = float(adx)
        # If payload includes adx_min you could compare; otherwise, tier it.
        if adx_f >= 25:
            breakdown["adx"] = 25
        elif adx_f >= 20:
            breakdown["adx"] = 18
        elif adx_f >= 15:
            breakdown["adx"] = 10
        else:
            reasons.append("adx low")
    except Exception:
        reasons.append("adx missing/unparseable")

    # Stoch alignment (20)
    try:
        kf = float(stoch_k)
        df = float(stoch_d)
        if direction == "CALL" and kf > df:
            breakdown["stoch"] = 20
        elif direction == "PUT" and kf < df:
            breakdown["stoch"] = 20
        else:
            reasons.append("stoch not aligned")
    except Exception:
        reasons.append("stoch missing/unparseable")

    # Keltner position (20)
    # Your strategy is mean-reversion: CALL near lower, PUT near upper.
    if kpos in ("upper", "middle", "lower"):
        if direction == "CALL" and kpos == "lower":
            breakdown["keltner"] = 20
        elif direction == "PUT" and kpos == "upper":
            breakdown["keltner"] = 20
        else:
            reasons.append("keltner_pos not ideal")

    # Cooldown (10)
    if cooldown_ok:
        breakdown["cooldown"] = 10
    else:
        reasons.append("cooldown not ok")

    confidence = sum(breakdown.values())
    return confidence, breakdown, reasons

# -----------------------------
# Paper trade lifecycle
# -----------------------------
def create_paper_trade(symbol: str, direction: str, expiry_minutes: int, payload: dict, confidence: int, breakdown: dict):
    trade_id = str(uuid.uuid4())
    now = utc_now()
    created_iso = now.isoformat()

    # Entry price: if you include close in SIGNAL payload, great; otherwise None.
    entry_price = payload.get("close")
    try:
        entry_price = float(entry_price) if entry_price is not None else None
    except Exception:
        entry_price = None

    # Use tv_time_ms if present (more “chart-true”), else server time.
    tv_time_ms = payload.get("tv_time_ms")
    try:
        tv_time_ms = int(tv_time_ms) if tv_time_ms is not None else None
    except Exception:
        tv_time_ms = None

    trade = {
        "id": trade_id,
        "mode": EXECUTION_MODE,
        "status": "OPEN",

        "symbol": symbol,
        "direction": direction,
        "timeframe": str(payload.get("timeframe", "1")),
        "expiry_minutes": int(expiry_minutes),

        "stake": PAPER_STAKE,
        "payout": PAPER_PAYOUT,

        "confidence": confidence,
        "breakdown": breakdown,

        "created_at_utc": created_iso,
        "created_date_utc": now.date().isoformat(),
        "source_alert_received_at_utc": created_iso,
        "source_tv_time_ms": tv_time_ms,

        "entry_price": entry_price,   # may be None unless you send close on signal
        "exit_price": None,
        "result": None,               # WIN / LOSS / TIE
        "pnl": None,                  # +stake*payout, -stake, or 0

        # When we should settle this trade (server-based)
        "expires_at_utc": (now + timedelta_minutes(expiry_minutes)).isoformat(),
    }
    ndjson_append(TRADES_PATH, trade)
    return trade

def timedelta_minutes(m: int):
    from datetime import timedelta
    return timedelta(minutes=int(m))

def resolve_expired_trades_for_symbol(symbol: str, bar_close: float):
    """
    Resolve OPEN trades for symbol that have expired, using bar_close as exit_price.
    Returns: number_resolved
    """
    if not os.path.exists(TRADES_PATH):
        return 0

    symbol = normalize_symbol(symbol)
    now = utc_now()

    trades = ndjson_read_all(TRADES_PATH)
    changed = False
    resolved_count = 0

    for t in trades:
        if t.get("status") != "OPEN":
            continue
        if normalize_symbol(t.get("symbol")) != symbol:
            continue

        exp = parse_iso(t.get("expires_at_utc"))
        if not exp:
            continue
        if now < exp:
            continue

        entry = t.get("entry_price")
        # If you never sent entry_price on signal, we can’t evaluate outcome accurately.
        # We still mark it CLOSED but as "UNKNOWN" unless entry exists.
        try:
            entry_f = float(entry) if entry is not None else None
        except Exception:
            entry_f = None

        exit_f = float(bar_close)
        t["exit_price"] = exit_f
        t["closed_at_utc"] = now.isoformat()
        t["status"] = "CLOSED"

        if entry_f is None:
            t["result"] = "UNKNOWN"
            t["pnl"] = 0.0
        else:
            if t.get("direction") == "CALL":
                if exit_f > entry_f:
                    t["result"] = "WIN"
                    t["pnl"] = round(float(t.get("stake", PAPER_STAKE)) * float(t.get("payout", PAPER_PAYOUT)), 6)
                elif exit_f < entry_f:
                    t["result"] = "LOSS"
                    t["pnl"] = -round(float(t.get("stake", PAPER_STAKE)), 6)
                else:
                    t["result"] = "TIE"
                    t["pnl"] = 0.0
            elif t.get("direction") == "PUT":
                if exit_f < entry_f:
                    t["result"] = "WIN"
                    t["pnl"] = round(float(t.get("stake", PAPER_STAKE)) * float(t.get("payout", PAPER_PAYOUT)), 6)
                elif exit_f > entry_f:
                    t["result"] = "LOSS"
                    t["pnl"] = -round(float(t.get("stake", PAPER_STAKE)), 6)
                else:
                    t["result"] = "TIE"
                    t["pnl"] = 0.0
            else:
                t["result"] = "UNKNOWN"
                t["pnl"] = 0.0

        changed = True
        resolved_count += 1

    if changed:
        # Rewrite file safely (small file expected in demo)
        tmp = TRADES_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for t in trades:
                f.write(json.dumps(t, ensure_ascii=False) + "\n")
        os.replace(tmp, TRADES_PATH)

    return resolved_count

# -----------------------------
# Routes
# -----------------------------
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True)
    if data is None:
        return jsonify({"status": "error", "message": "Expected JSON body"}), 400

    event_type = str(data.get("type", "signal")).strip().lower()  # signal | bar
    symbol = normalize_symbol(data.get("symbol"))
    direction = data.get("direction")

    received_at = utc_now_iso()

    # Always log raw inbound first (but keep it lightweight)
    base_record = {
        "received_at_utc": received_at,
        "event_type": event_type,
        "payload": safe_json(data),
    }

    # Handle BAR heartbeat
    if event_type == "bar":
        # Optional: close from heartbeat; needed to resolve paper trades
        close_val = data.get("close")
        resolved = 0
        close_float = None
        if close_val is not None and symbol:
            try:
                close_float = float(close_val)
                resolved = resolve_expired_trades_for_symbol(symbol, close_float)
            except Exception:
                pass

        base_record["bar"] = {
            "symbol": symbol,
            "close": close_float,
            "resolved_trades": resolved,
        }
        ndjson_append(LOG_PATH, base_record)

        app.logger.warning(f"BAR | {symbol} | close={close_float} | resolved={resolved}")
        return jsonify({"status": "ok", "type": "bar", "symbol": symbol, "resolved_trades": resolved}), 200

    # Handle SIGNAL
    if not symbol:
        base_record["rejected"] = {"reason": "missing symbol"}
        ndjson_append(LOG_PATH, base_record)
        return jsonify({"status": "error", "message": "Missing symbol"}), 400

    if ALLOWLIST and symbol not in ALLOWLIST:
        base_record["decision"] = {"allowed": False, "reason": "symbol_not_allowlisted"}
        ndjson_append(LOG_PATH, base_record)
        return jsonify({"status": "ok", "allowed": False, "reason": "symbol_not_allowlisted"}), 200

    # Cooldown check (signals only)
    now_dt = utc_now()
    last_time = last_signal_time_for_symbol(symbol)
    cooldown_ok = True
    seconds_since_last = None
    if last_time:
        seconds_since_last = (now_dt - last_time).total_seconds()
        if seconds_since_last < COOLDOWN_SECONDS:
            cooldown_ok = False

    confidence, breakdown, reasons = calculate_confidence(data, cooldown_ok)
    confidence_ok = confidence >= MIN_CONFIDENCE

    # Daily limit check
    day_count = trades_today_count()
    daily_ok = day_count < MAX_TRADES_PER_DAY
    if not daily_ok:
        reasons.append("max_trades_per_day reached")

    # Max open per symbol check
    open_for_symbol = open_trades_for_symbol(symbol)
    per_symbol_ok = len(open_for_symbol) < MAX_OPEN_TRADES_PER_SYMBOL
    if not per_symbol_ok:
        reasons.append("max_open_trades_per_symbol reached")

    allowed = cooldown_ok and confidence_ok and daily_ok and per_symbol_ok

    # Log the signal evaluation
    base_record["cooldown"] = {
        "cooldown_seconds": COOLDOWN_SECONDS,
        "cooldown_ok": cooldown_ok,
        "seconds_since_last": seconds_since_last,
    }
    base_record["confidence"] = confidence
    base_record["breakdown"] = breakdown
    base_record["allowed"] = allowed
    base_record["reasons"] = reasons
    ndjson_append(LOG_PATH, base_record)

    # If not allowed, stop here
    if not allowed:
        app.logger.warning(
            f"ALERT | {symbol} | {direction} | TF={data.get('timeframe')} | "
            f"EXP={data.get('expiry_minutes')}m | cooldown_ok={cooldown_ok} | "
            f"confidence={confidence} | allowed={allowed} | reasons={reasons}"
        )
        return jsonify({
            "status": "ok",
            "type": "signal",
            "allowed": False,
            "cooldown_ok": cooldown_ok,
            "confidence": confidence,
            "breakdown": breakdown,
            "reasons": reasons,
        }), 200

    # EXECUTION
    trade = None
    if EXECUTION_MODE == "paper":
        expiry = int(data.get("expiry_minutes", 1))
        trade = create_paper_trade(symbol, direction, expiry, data, confidence, breakdown)
    else:
        # mode off -> allowed but no execution
        trade = None

    app.logger.warning(
        f"ALERT | {symbol} | {direction} | TF={data.get('timeframe')} | "
        f"EXP={data.get('expiry_minutes')}m | cooldown_ok={cooldown_ok} | "
        f"confidence={confidence} | allowed={allowed} | breakdown={breakdown}"
    )

    return jsonify({
        "status": "ok",
        "type": "signal",
        "allowed": True,
        "cooldown_ok": cooldown_ok,
        "confidence": confidence,
        "breakdown": breakdown,
        "trade": trade,
    }), 200


@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "tv-webhook", "mode": EXECUTION_MODE}), 200


@app.route("/count", methods=["GET"])
def count():
    # Alert log line count
    try:
        if not os.path.exists(LOG_PATH):
            return jsonify({"count": 0}), 200
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            c = sum(1 for _ in f)
        return jsonify({"count": c}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/open_trades", methods=["GET"])
def open_trades():
    trades = ndjson_read_all(TRADES_PATH)
    open_ts = [t for t in trades if t.get("status") == "OPEN"]
    return jsonify({"count": len(open_ts), "open_trades": open_ts}), 200


@app.route("/trades", methods=["GET"])
def trades():
    trades = ndjson_read_all(TRADES_PATH)
    return jsonify({"count": len(trades), "trades": trades}), 200


@app.route("/summary", methods=["GET"])
def summary():
    trades = ndjson_read_all(TRADES_PATH)
    wins = sum(1 for t in trades if t.get("result") == "WIN")
    losses = sum(1 for t in trades if t.get("result") == "LOSS")
    ties = sum(1 for t in trades if t.get("result") == "TIE")
    unknown = sum(1 for t in trades if t.get("result") == "UNKNOWN")
    open_count = sum(1 for t in trades if t.get("status") == "OPEN")
    pnl = 0.0
    for t in trades:
        try:
            pnl += float(t.get("pnl") or 0.0)
        except Exception:
            continue
    return jsonify({
        "mode": EXECUTION_MODE,
        "open": open_count,
        "wins": wins,
        "losses": losses,
        "ties": ties,
        "unknown": unknown,
        "pnl": round(pnl, 6),
        "today_trades": trades_today_count(),
    }), 200


if __name__ == "__main__":
    # Render sets PORT; fall back to 10000
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
