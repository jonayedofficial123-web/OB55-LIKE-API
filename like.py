# -*- coding: utf-8 -*-
"""
FF LIKE API — unlimited requests per account, all tokens used per call,
only the total DELIVERED likes is capped at 300/day (resets 4 AM IST).
"""
from flask import Flask, request, jsonify
import asyncio, sqlite3, time, binascii, json, os, threading
from datetime import datetime, timedelta, timezone
from collections import OrderedDict
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
import aiohttp, requests

try:
    import like_pb2, uid_generator_pb2, visit_count_pb2
    _PB_OK = True
except Exception:
    _PB_OK = False

app = Flask(__name__)

# ─── CONFIG ──────────────────────────────────────────────────
VALID_API_KEYS       = {"mehedi"}

DAILY_LIKE_LIMIT     = 300            # only thing capped: total likes DELIVERED per day
IST                  = timezone(timedelta(hours=5, minutes=30))
DAILY_RESET_HOUR_IST = 4

DB_PATH              = os.path.join(os.path.dirname(__file__), "like_usage.db")
MAX_CONCURRENT_TOKENS= 30             # burst size cap (all tokens still used, just throttled in-flight)
AES_KEY              = b'Yg&tc%DEuh6%Zc^8'
AES_IV               = b'6oyZDr22E3ychjM%'

# ─── DB (daily delivered-like counter only) ──────────────────
_DB_LOCK = threading.RLock()

def _init_db():
    with _DB_LOCK, sqlite3.connect(DB_PATH) as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS daily_usage (
            day  TEXT PRIMARY KEY,
            used INTEGER NOT NULL DEFAULT 0
        );
        """)
        c.commit()

def _current_ist_day_key() -> str:
    now = datetime.now(IST)
    if now.hour < DAILY_RESET_HOUR_IST:
        now = now - timedelta(days=1)
    return now.strftime("%Y-%m-%d")

def _seconds_until_reset() -> int:
    now = datetime.now(IST)
    reset = now.replace(hour=DAILY_RESET_HOUR_IST, minute=0, second=0, microsecond=0)
    if now >= reset:
        reset += timedelta(days=1)
    return int((reset - now).total_seconds())

def _get_used() -> int:
    day = _current_ist_day_key()
    with _DB_LOCK, sqlite3.connect(DB_PATH) as c:
        r = c.execute("SELECT used FROM daily_usage WHERE day=?", (day,)).fetchone()
        return int(r[0]) if r else 0

def _add_used(n: int):
    if n <= 0: return
    day = _current_ist_day_key()
    with _DB_LOCK, sqlite3.connect(DB_PATH) as c:
        c.execute("""INSERT INTO daily_usage (day, used) VALUES (?, ?)
                     ON CONFLICT(day) DO UPDATE SET used = used + excluded.used""",
                  (day, int(n)))
        c.commit()

_init_db()

# ─── CRYPTO ──────────────────────────────────────────────────
def encrypt_message(plaintext):
    try:
        cipher = AES.new(AES_KEY, AES.MODE_CBC, AES_IV)
        return binascii.hexlify(cipher.encrypt(pad(plaintext, AES.block_size))).decode("utf-8")
    except Exception as e:
        app.logger.error(f"encrypt: {e}"); return None

def _protobuf_like(uid, region):
    m = like_pb2.like(); m.uid = int(uid); m.region = region
    return m.SerializeToString()

def _protobuf_uid(uid):
    m = uid_generator_pb2.uid_generator()
    m.saturn_ = int(uid); m.garena = 1
    return m.SerializeToString()

def enc(uid):
    return encrypt_message(_protobuf_uid(uid))

# ─── TOKENS ──────────────────────────────────────────────────
def load_tokens(region):
    try:
        path = ("token_ind.json" if region == "IND"
                else "token_br.json" if region in {"BR","US","SAC","NA"}
                else "token_bd.json")
        with open(path) as f:
            return json.load(f) or []
    except Exception as e:
        app.logger.error(f"load_tokens({region}): {e}"); return []

def _like_url(region):
    if region == "IND": return "https://client.ind.freefiremobile.com/LikeProfile"
    if region in {"BR","US","SAC","NA"}: return "https://client.us.freefiremobile.com/LikeProfile"
    return "https://clientbp.ggpolarbear.com/LikeProfile"

def _show_url(region):
    if region == "IND": return "https://client.ind.freefiremobile.com/GetPlayerPersonalShow"
    if region in {"BR","US","SAC","NA"}: return "https://client.us.freefiremobile.com/GetPlayerPersonalShow"
    return "https://clientbp.ggpolarbear.com/GetPlayerPersonalShow"

# ─── HTTP ────────────────────────────────────────────────────
async def _send_one(session, encrypted_uid, token, url):
    try:
        async with session.post(
            url, data=bytes.fromhex(encrypted_uid),
            headers={
                "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 9; ASUS_Z01QD Build/PI)",
                "Connection": "Keep-Alive",
                "Accept-Encoding": "gzip",
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Unity-Version": "2018.4.11f1",
                "X-GA": "v1 1",
                "ReleaseVersion": "OB55",
            },
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            return await resp.text()
    except Exception:
        return None

async def _send_burst(encrypted_uid, tokens, url):
    """
    Every token in the pool is used, round-robin across bursts.
    Concurrency cap only prevents OS socket exhaustion —
    each token still fires once per request call.
    """
    if not tokens: return []
    connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT_TOKENS, ttl_dns_cache=300)
    async with aiohttp.ClientSession(connector=connector) as session:
        # Every single token fires — the concurrency cap just queues them
        tasks = [_send_one(session, encrypted_uid, t["token"], url) for t in tokens]
        return await asyncio.gather(*tasks, return_exceptions=True)

def _show_player(encrypted_uid, region, token):
    try:
        r = requests.post(_show_url(region), data=bytes.fromhex(encrypted_uid),
            headers={
                "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 9; ASUS_Z01QD Build/PI)",
                "Connection": "Keep-Alive",
                "Accept-Encoding": "gzip",
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Unity-Version": "2018.4.11f1",
                "X-GA": "v1 1",
                "ReleaseVersion": "OB55",
            }, verify=False, timeout=15)
        pb = visit_count_pb2.Info(); pb.ParseFromString(r.content)
        return pb
    except Exception as e:
        app.logger.error(f"show_player: {e}"); return None

# ─── ROUTES ──────────────────────────────────────────────────
@app.route("/like", methods=["GET"])
def handle_like():
    if request.args.get("key") not in VALID_API_KEYS:
        return app.response_class(
            response=json.dumps({"error": "Invalid API key", "status": 3},
                                separators=(",", ":")),
            status=401, mimetype="application/json")

    uid    = request.args.get("uid")
    region = (request.args.get("region") or "").upper()
    if not uid or not region:
        return {"error": "uid and region required"}, 400
    if not _PB_OK:
        return {"error": "protobuf modules missing"}, 500

    # ── daily delivered-likes gate ───────────────────────
    used = _get_used()
    if used >= DAILY_LIKE_LIMIT:
        return app.response_class(
            response=json.dumps({
                "error": "Daily like limit reached",
                "daily_limit": DAILY_LIKE_LIMIT,
                "used": used,
                "remaining": 0,
                "reset_in_seconds": _seconds_until_reset(),
                "status": 3,
            }, separators=(",", ":")),
            status=429, mimetype="application/json")

    try:
        tokens = load_tokens(region)
        if not tokens:
            raise Exception("no tokens for region")

        master_token = tokens[0]["token"]
        encrypted_uid = enc(uid)
        if not encrypted_uid:
            raise Exception("encrypt failed")

        before = _show_player(encrypted_uid, region, master_token)
        if not before:
            raise Exception("pre-fetch failed")
        before_like = before.AccountInfo.Likes

        # Send to EVERY token in the pool
        asyncio.run(_send_burst(encrypted_uid, tokens, _like_url(region)))

        after = _show_player(encrypted_uid, region, master_token)
        if not after:
            raise Exception("post-fetch failed")
        after_like = after.AccountInfo.Likes

        delivered = max(0, after_like - before_like)
        _add_used(delivered)

        new_used = _get_used()
        remaining = max(DAILY_LIKE_LIMIT - new_used, 0)

        return app.response_class(
            response=json.dumps(OrderedDict([
                ("LikesGivenByAPI", delivered),
                ("LikesafterCommand", after_like),
                ("LikesbeforeCommand", before_like),
                ("PlayerNickname", after.AccountInfo.PlayerNickname),
                ("Level", after.AccountInfo.Levels),
                ("Region", after.AccountInfo.PlayerRegion),
                ("UID", after.AccountInfo.UID),
                ("status", 1 if delivered > 0 else 2),
                ("daily_limit", DAILY_LIKE_LIMIT),
                ("used", new_used),
                ("remaining", remaining),
                ("reset_in_seconds", _seconds_until_reset()),
            ]), separators=(",", ":")),
            status=200, mimetype="application/json")

    except Exception as e:
        app.logger.error(f"like error: {e}")
        return {"error": str(e)}, 500

@app.route("/remain", methods=["GET"])
def remain_info():
    used = _get_used()
    return jsonify({
        "daily_limit": DAILY_LIKE_LIMIT,
        "remaining": max(DAILY_LIKE_LIMIT - used, 0),
        "used": used,
        "reset_info": f"Daily at {DAILY_RESET_HOUR_IST}:00 AM IST",
        "reset_in_seconds": _seconds_until_reset(),
    })

if __name__ == "__main__":
    app.run(debug=False, use_reloader=False, host="0.0.0.0", port=5000)