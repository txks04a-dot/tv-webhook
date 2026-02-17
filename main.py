import os
import json
import uuid
import tempfile
from datetime import datetime, timezone
from flask import Flask, request, jsonify

# Optional (recommended) for correct "trading day" logic.
try:
    from zoneinfo import ZoneInfo  # py3.9+
except Exception:
    ZoneInfo = None

app = Flask(__name__)

# ============================================================
# DURABILITY FIX (Render):
# - Local container FS is ephemeral across deploys/restarts.
# - Use a persistent disk mount and write NDJSON there.
#
# On Render: create a Persistent Disk and mount it, e.g. at /var/data
# Then set env:
#   DATA_DIR=/var/data
# ============================================================
DATA_DIR = os.environ.get("DATA_DIR", "/var/data").strip() or "/var/data"


def ensure_dir(path: str) -> None:
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        pass


ensure_dir(DATA_DIR)
if not os.path.isdir(DATA_DIR):
    DATA_DIR = os.getcwd()

# -----------------------------
# Configuration (env variables)
# -----------------------------
LOG_PATH = os.environ.get("LOG_PATH", os.path.join(DATA_DIR, "alerts.ndjson"))
TRADES_PATH = os.environ.get("TRADES_PATH", os.path.join(DATA_DIR, "trades.ndjson"))

COOLDOWN_SECONDS = int(os.environ.get("COOLDOWN_SECONDS", "300"))
MIN_CONFIDENCE = int(os.environ.get("MIN_CONFIDENCE", "80"))

EXECUTION_MODE = os.environ.get("EXECUTION_MODE", "paper").strip().lower()  # paper | off
SYMBOL_ALLOWLIST = os.environ.get("SYMBOL_ALLOWLIST", "").strip()

MAX_TRADES_PER_DAY = int(os.environ.get("MAX_TRADES_PER_DAY", "20"))
MAX_OPEN_TRADES_PER_SYMBOL = int(os.environ.get("MAX_OPEN_TRADES_PER_SYMBOL", "1"))

PAPER_STAKE = float(os.environ.get("PAPER_STAKE", "1"))
PAPER_PAYOUT = float(os.environ.get("PAPER_PAYOUT", "0.80"))

# Day boundary (recommended): align your "day" with NY session
DAY_TZ = os.environ.get("DAY_TZ", "America/New_York").strip()

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

def _fsync_file(f):
    try:
        f.flush()
        os.fsync(f.fileno())
    except Exception:
        pass

def ndjson_append(path: str, record: dict) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    line = json.dumps(record, ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)
        _fsync_file(f)

def ndjson_read_all(path: str):
    if not os.path.exists(path):
        return []
    out = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return []
    return out

def atomic_write_ndjson(path: str, records: list[dict]) -> None:
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
            _fsync_file(f)

        os.replace(tmp_path, path)

        # Best-effort fsync directory entry (Linux)
        try:
            dir_fd = os.open(parent, os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except Exception:
            pass
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass

def normalize_symbol(sym):
    if not sym:
        return None
    return str(sym).strip().upper()

def today_date_str() -> str:
    if ZoneInfo is None:
        return utc_now().date().isoformat()
    try:
        tz = ZoneInfo(DAY_TZ)
        return datetime.now(tz).date().isoformat()
    except Exception:
        return utc_now().date().isoformat()

# -----------------------------
# Cooldown / Limits
# -----------------------------
def last_signal_time_for_symbol(symbol: str):
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
    if not os.path.exists(TRADES_PATH):
        return 0
    day_key = today_date_str()
    c = 0
    for t in ndjson_read_all(TRADES_PATH):
        if t.get("created_date_key") == day_key:
            c += 1
    return c

def open_trades_for_symbol(symbol: str):
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
# Metrics
# -----------------------------
def compute_metrics(trades: list[dict], last_n: int | None = None) -> dict:
    if last_n is not None and last_n > 0:
        trades = trades[-last_n:]

    total = len(trades)
    open_trades = [t for t in trades if t.get("status") == "OPEN"]
    closed = [t for t in trades if t.get("status") == "CLOSED"]

    def _ts(t):
        return t.get("closed_at_utc") or t.get("created_at_utc") or ""

    closed_sorted = sorted(closed, key=_ts)

    wins = sum(1 for t in closed_sorted if t.get("result") == "WIN")
    losses = sum(1 for t in closed_sorted if t.get("result") == "LOSS")
    ties = sum(1 for t in closed_sorted if t.get("result") == "TIE")
    unknown = sum(1 for t in closed_sorted if t.get("result") == "UNKNOWN")

    decided = wins + losses
    closed_count = len(closed_sorted)

    pnl_sum = 0.0
    win_pnl = 0.0
    loss_pnl = 0.0
    for t in closed_sorted:
        try:
            p = float(t.get("pnl") or 0.0)
        except Exception:
            p = 0.0
        pnl_sum += p
        if p > 0:
            win_pnl += p
        elif p < 0:
            loss_pnl += (-p)

    avg_pnl_closed = (pnl_sum / closed_count) if closed_count else 0.0
    expectancy_per_decided = (pnl_sum / decided) if decided else 0.0

    win_rate_decided = (wins / decided) if decided else 0.0
    win_rate_closed = (wins / closed_count) if closed_count else 0.0
    tie_rate_closed = (ties / closed_count) if closed_count else 0.0

    profit_factor = (win_pnl / loss_pnl) if loss_pnl > 0 else (float("inf") if win_pnl > 0 else 0.0)

    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in closed_sorted:
        try:
            equity += float(t.get("pnl") or 0.0)
        except Exception:
            pass
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd

    current_streak_type = None
    current_streak_len = 0
    max_win_streak = 0
    max_loss_streak = 0
    win_streak = 0
    loss_streak = 0

    for t in closed_sorted:
        r = t.get("result") or "UNKNOWN"

        if r == current_streak_type:
            current_streak_len += 1
        else:
            current_streak_type = r
            current_streak_len = 1

        if r == "WIN":
            win_streak += 1
            loss_streak = 0
        elif r == "LOSS":
            loss_streak += 1
            win_streak = 0
        else:
            win_streak = 0
            loss_streak = 0

        if win_streak > max_win_streak:
            max_win_streak = win_streak
        if loss_streak > max_loss_streak:
            max_loss_streak = loss_streak

    last_closed = closed_sorted[-1] if closed_sorted else None

    return {
        "counts": {
            "total_records": total,
            "open": len(open_trades),
            "closed": closed_count,
            "wins": wins,
            "losses": losses,
            "ties": ties,
            "unknown": unknown,
            "decided": decided,
        },
        "rates": {
            "win_rate_decided": round(win_rate_decided, 6),
            "win_rate_closed": round(win_rate_closed, 6),
            "tie_rate_closed": round(tie_rate_closed, 6),
        },
        "pnl": {
            "sum": round(pnl_sum, 6),
            "avg_per_closed_trade": round(avg_pnl_closed, 6),
            "expectancy_per_decided_trade": round(expectancy_per_decided, 6),
            "gross_profit": round(win_pnl, 6),
            "gross_loss": round(loss_pnl, 6),
            "profit_factor": (profit_factor if profit_factor in (0.0, float("inf")) else round(profit_factor, 6)),
            "max_drawdown": round(max_dd, 6),
            "ending_equity": round(equity, 6),
        },
        "streaks": {
            "current": {"type": current_streak_type, "len": current_streak_len if closed_count else 0},
            "max_win_streak": max_win_streak,
            "max_loss_streak": max_loss_streak,
        },
        "last_closed_trade": {
            "id": last_closed.get("id") if last_closed else None,
            "symbol": last_closed.get("symbol") if last_closed else None,
            "direction": last_closed.get("direction") if last_closed else None,
            "result": last_closed.get("result") if last_closed else None,
            "pnl": last_closed.get("pnl") if last_closed else None,
            "closed_at_utc": last_closed.get("closed_at_utc") if last_closed else None,
            "confidence": last_closed.get("confidence") if last_closed else None,
        } if last_closed else None,
    }

# -----------------------------
# Paper trade lifecycle
# -----------------------------
def timedelta_minutes(m: int):
    from datetime import timedelta
    return timedelta(minutes=int(m))

def create_paper_trade(symbol: str, direction: str, expiry_minutes: int, payload: dict, confidence: int, breakdown: dict):
    trade_id = str(uuid.uuid4())
    now = utc_now()
    created_iso = now.isoformat()

    entry_price = payload.get("close")
    try:
        entry_price = float(entry_price) if entry_price is not None else None
    except Exception:
        entry_price = None

    tv_time_ms = payload.get("tv_time_ms")
    try:
        tv_time_ms = int(tv_time_ms) if tv_time_ms is not None else None
    except Exception:
        tv_time_ms = None

    day_key = today_date_str()

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
        "created_date_key": day_key,
        "source_alert_received_at_utc": created_iso,
        "source_tv_time_ms": tv_time_ms,
        "entry_price": entry_price,
        "exit_price": None,
        "result": None,
        "pnl": None,
        "expires_at_utc": (now + timedelta_minutes(expiry_minutes)).isoformat(),
    }
    ndjson_append(TRADES_PATH, trade)
    return trade

def resolve_expired_trades_for_symbol(symbol: str, bar_close: float):
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
        if not exp or now < exp:
            continue

        entry = t.get("entry_price")
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
            stake = float(t.get("stake", PAPER_STAKE))
            payout = float(t.get("payout", PAPER_PAYOUT))

            if t.get("direction") == "CALL":
                if exit_f > entry_f:
                    t["result"] = "WIN"
                    t["pnl"] = round(stake * payout, 6)
                elif exit_f < entry_f:
                    t["result"] = "LOSS"
                    t["pnl"] = -round(stake, 6)
                else:
                    t["result"] = "TIE"
                    t["pnl"] = 0.0
            elif t.get("direction") == "PUT":
                if exit_f < entry_f:
                    t["result"] = "WIN"
                    t["pnl"] = round(stake * payout, 6)
                elif exit_f > entry_f:
                    t["result"] = "LOSS"
                    t["pnl"] = -round(stake, 6)
                else:
                    t["result"] = "TIE"
                    t["pnl"] = 0.0
            else:
                t["result"] = "UNKNOWN"
                t["pnl"] = 0.0

        changed = True
        resolved_count += 1

    if changed:
        atomic_write_ndjson(TRADES_PATH, trades)

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

    base_record = {
        "received_at_utc": received_at,
        "event_type": event_type,
        "payload": safe_json(data),
    }

    # -----------------------------
    # Handle BAR heartbeat
    # -----------------------------
    if event_type == "bar":
        prices = data.get("prices")

        # master bar
        if isinstance(prices, dict) and prices:
            resolved_total = 0
            symbols_seen = 0

            for sym, close_val in prices.items():
                sym_norm = normalize_symbol(sym)
                if not sym_norm:
                    continue
                try:
                    close_float = float(close_val)
                except Exception:
                    continue

                symbols_seen += 1
                resolved_total += resolve_expired_trades_for_symbol(sym_norm, close_float)

            base_record["bar"] = {
                "mode": "master_prices",
                "symbols_in_payload": symbols_seen,
                "resolved_trades": resolved_total,
            }
            ndjson_append(LOG_PATH, base_record)

            app.logger.warning(f"BAR(master) | symbols={symbols_seen} | resolved={resolved_total}")
            return jsonify({
                "status": "ok",
                "type": "bar",
                "mode": "master_prices",
                "symbols": symbols_seen,
                "resolved_trades": resolved_total,
            }), 200

        # single symbol bar
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
            "mode": "single_symbol",
            "symbol": symbol,
            "close": close_float,
            "resolved_trades": resolved,
        }
        ndjson_append(LOG_PATH, base_record)

        app.logger.warning(f"BAR | {symbol} | close={close_float} | resolved={resolved}")
        return jsonify({
            "status": "ok",
            "type": "bar",
            "mode": "single_symbol",
            "symbol": symbol,
            "resolved_trades": resolved
        }), 200

    # -----------------------------
    # Handle SIGNAL
    # -----------------------------
    if not symbol:
        base_record["rejected"] = {"reason": "missing symbol"}
        ndjson_append(LOG_PATH, base_record)
        return jsonify({"status": "error", "message": "Missing symbol"}), 400

    if ALLOWLIST and symbol not in ALLOWLIST:
        base_record["decision"] = {"allowed": False, "reason": "symbol_not_allowlisted"}
        ndjson_append(LOG_PATH, base_record)
        return jsonify({"status": "ok", "allowed": False, "reason": "symbol_not_allowlisted"}), 200

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

    day_count = trades_today_count()
    daily_ok = day_count < MAX_TRADES_PER_DAY
    if not daily_ok:
        reasons.append("max_trades_per_day reached")

    open_for_symbol = open_trades_for_symbol(symbol)
    per_symbol_ok = len(open_for_symbol) < MAX_OPEN_TRADES_PER_SYMBOL
    if not per_symbol_ok:
        reasons.append("max_open_trades_per_symbol reached")

    allowed = cooldown_ok and confidence_ok and daily_ok and per_symbol_ok

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

    trade = None
    if EXECUTION_MODE == "paper":
        expiry = int(data.get("expiry_minutes", 1))
        trade = create_paper_trade(symbol, direction, expiry, data, confidence, breakdown)

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
    return jsonify({
        "status": "ok",
        "service": "tv-webhook",
        "mode": EXECUTION_MODE,
        "data_dir": DATA_DIR,
        "trades_path": TRADES_PATH,
        "alerts_path": LOG_PATH,
        "day_tz": DAY_TZ,
    }), 200


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
        "day_tz": DAY_TZ,
        "data_dir": DATA_DIR,
    }), 200


@app.route("/metrics", methods=["GET"])
def metrics():
    """
    /metrics
      - default: compute over all trades in file
      - optional: ?last_n=200 to compute over last N records
    """
    trades = ndjson_read_all(TRADES_PATH)

    last_n = request.args.get("last_n", default=None, type=int)
    if last_n is not None and last_n <= 0:
        last_n = None

    out = compute_metrics(trades, last_n=last_n)
    out["meta"] = {
        "mode": EXECUTION_MODE,
        "data_dir": DATA_DIR,
        "trades_path": TRADES_PATH,
        "day_tz": DAY_TZ,
        "today_trades": trades_today_count(),
        "window_last_n": last_n,
        "generated_at_utc": utc_now_iso(),
    }
    return jsonify(out), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
