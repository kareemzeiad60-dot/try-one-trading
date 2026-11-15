import os
import json
import time
from datetime import datetime, timedelta
import pytz
import requests
import pandas as pd

# ================= الإعدادات من GitHub Secrets =================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_CHAT_ID = os.environ.get("CHAT_ID")  # رقم حسابك الشخصي لتشغيل البوت لأول مرة

CAIRO_TZ = pytz.timezone("Africa/Cairo")
DB_FILE = "egx_pro_saas_db.json"
EGX_API_BASE = "https://beta.egx.com.eg/api/bff/egx"
EGX_HEADERS = {
    "Accept": "application/json",
    "Referer": "https://beta.egx.com.eg/en",
    "User-Agent": "Mozilla/5.0",
    "x-egx-bff-request": "1",
}
EGX_SESSION = requests.Session()
EGX_SESSION.headers.update(EGX_HEADERS)
FORECAST_HORIZON = 5

WATCHLIST = [
    "COMI.CA", "TMGH.CA", "EAST.CA", "HRHO.CA", "ETEL.CA",
    "SWDY.CA", "ABUK.CA", "FWRY.CA", "ESRS.CA", "MFPC.CA",
    "ORAS.CA", "EFIH.CA", "EKHO.CA", "HELI.CA", "PHDC.CA",
    "ADIB.CA", "CIEB.CA", "AMOC.CA", "SKPC.CA", "JUFO.CA"
]

# ================= إدارة قاعدة البيانات =================
def load_db():
    data = {}
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r", encoding="utf-8") as f:
            try:
                data = json.load(f)
            except json.JSONDecodeError:
                data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("last_update_id", 0)
    data.setdefault("users", {})
    return data

def save_db(db):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=4)

def init_user(db, chat_id):
    uid = str(chat_id)
    today_str = datetime.now(CAIRO_TZ).strftime("%Y-%m-%d")
    if uid not in db["users"]:
        db["users"][uid] = {
            "balance": 10000.0,
            "available_cash": 10000.0,
            "risk_pct": 0.015,
            "max_open_trades": 3,
            "daily_loss": 0.0,
            "last_trade_date": today_str,
            "report_sent_today": False,
            "active_trades": {},
            "history": [],
            "last_signals": {}
        }
    elif db["users"][uid].get("last_trade_date") != today_str:
        db["users"][uid]["daily_loss"] = 0.0
        db["users"][uid]["last_trade_date"] = today_str
        db["users"][uid]["report_sent_today"] = False
    db["users"][uid].setdefault("last_signals", {})
    return db["users"][uid]
# ================= دوال تيليجرام المباشرة (REST API) =================
def tg_request(method, payload=None, files=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    try:
        if files:
            r = requests.post(url, data=payload, files=files, timeout=20)
        else:
            r = requests.post(url, json=payload, timeout=15)
        return r.json()
    except Exception as e:
        print(f"Telegram API Error ({method}): {e}")
        return {}

def send_msg(chat_id, text, buttons=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    return tg_request("sendMessage", payload)

def send_signal_once(user, signal_key, data_date, chat_id, text, buttons):
    last_signals = user.setdefault("last_signals", {})
    if last_signals.get(signal_key) == data_date:
        return
    response = send_msg(chat_id, text, buttons)
    if response.get("ok"):
        last_signals[signal_key] = data_date

def is_authorized_telegram_user(chat_id, user_id):
    if not ADMIN_CHAT_ID:
        return False
    expected_id = str(ADMIN_CHAT_ID)
    return str(chat_id) == expected_id and str(user_id) == expected_id

def send_user_report(chat_id, user):
    history = user["history"]
    if not history:
        send_msg(chat_id, "📉 لا توجد صفقات مغلقة بعد لإنشاء تقرير Excel.")
        return

    df = pd.DataFrame(history)
    file_path = f"EGX_Report_{chat_id}.xlsx"
    df.to_excel(file_path, index=False)

    wins = len([x for x in history if x["الربح_جنيه"] > 0])
    win_rate = (wins / len(history)) * 100
    total_profit = sum(x["الربح_جنيه"] for x in history)

    caption = (
        f"📑 *تقرير الأداء اليومي — EGX SaaS*\n"
        f"• إجمالي الصفقات: {len(history)} | نسبة النجاح: {win_rate:.1f}%\n"
        f"• صافي الربح/الخسارة: *{total_profit:+,.2f} ج.م*\n"
        f"• الرصيد الحالي: *{user['balance']:,.2f} ج.م*"
    )
    with open(file_path, "rb") as f:
        tg_request("sendDocument", {"chat_id": chat_id, "caption": caption, "parse_mode": "Markdown"}, {"document": f})
    os.remove(file_path)

# ================= معالجة ضغطات الأزرار والأوامر المتراكمة =================
def process_telegram_updates(db):
    offset = db.get("last_update_id", 0) + 1
    res = tg_request("getUpdates", {"offset": offset, "timeout": 5})
    updates = res.get("result", [])
    force_scan_users = []

    for upd in updates:
        db["last_update_id"] = upd["update_id"]

        # 1. معالجة الرسائل النصية (/start, /scan, /portfolio, /report)
        if "message" in upd and "text" in upd["message"]:
            chat_id = str(upd["message"]["chat"]["id"])
            user_id = upd["message"].get("from", {}).get("id")
            if not is_authorized_telegram_user(chat_id, user_id):
                send_msg(chat_id, "⛔ هذا البوت خاص وغير مصرح لك باستخدامه.")
                continue

            text = upd["message"]["text"].strip()
            user = init_user(db, chat_id)

            if text == "/start":
                send_msg(
                    chat_id,
                    "🤖 *أهلاً بك في مساعد إشارات EGX (GitHub Actions)*\n"
                    f"💰 محفظتك: *{user['balance']:,.2f} ج.م* | 💵 الكاش: *{user['available_cash']:,.2f} ج.م*\n\n"
                    "الإشارات مبنية على بيانات البورصة المصرية ومؤشرات فنية، وليست نصيحة استثمارية أو تنفيذًا تلقائيًا للأوامر.\n\n"
                    "⏱️ يعمل عبر فحوصات GitHub Actions كل 20 دقيقة تقريباً، وليس خادماً مستمراً؛ قد يتأخر التشغيل أو يفوته. خارج الجلسة يعتمد على آخر بيانات متاحة.\n\n"
                    "الأوامر:\n• `/scan` فحص السوق فوراً\n• `/portfolio` صفقاتك المفتوحة\n• `/report` تقرير Excel"
                )
            elif text == "/scan":
                send_msg(chat_id, "⏳ جاري فحص السوق الآن...")
                force_scan_users.append(chat_id)
            elif text == "/portfolio":
                trades = user["active_trades"]
                if not trades:
                    send_msg(chat_id, f"📭 لا توجد صفقات مفتوحة.\n💵 الكاش المتاح: *{user['available_cash']:,.2f} ج.م*")
                else:
                    msg = "📊 *صفقاتك المفتوحة:*\n\n"
                    for s, t in trades.items():
                        msg += f"🔹 *{s}* ({t['shares']} سهم) | شراء: {t['entry']} | هدف: {t['tp']} | وقف: {t['sl']}\n"
                    send_msg(chat_id, msg)
            elif text == "/report":
                send_user_report(chat_id, user)

        # 2. معالجة الأزرار التفاعلية (BUY / SELL / IGNORE)
        elif "callback_query" in upd:
            cb = upd["callback_query"]
            chat_id = str(cb["message"]["chat"]["id"])
            msg_id = cb["message"]["message_id"]
            user_id = cb.get("from", {}).get("id")
            authorized = is_authorized_telegram_user(chat_id, user_id)
            answer_payload = {"callback_query_id": cb["id"]}
            if not authorized:
                answer_payload.update({
                    "text": "هذا البوت خاص وغير مصرح لك باستخدامه.",
                    "show_alert": True,
                })
            tg_request("answerCallbackQuery", answer_payload)
            if not authorized:
                continue

            user = init_user(db, chat_id)
            parts = cb["data"].split("|")
            action = parts[0]

            if action == "BUY":
                sym, price, shares, sl, tp = parts[1], float(parts[2]), int(parts[3]), float(parts[4]), float(parts[5])
                cost = round(price * shares, 2)
                if sym not in user["active_trades"] and user["available_cash"] >= cost and len(user["active_trades"]) < user["max_open_trades"]:
                    user["available_cash"] = round(user["available_cash"] - cost, 2)
                    user["active_trades"][sym] = {
                        "entry": price, "shares": shares, "sl": sl, "tp": tp,
                        "date": datetime.now(CAIRO_TZ).strftime("%Y-%m-%d %H:%M")
                    }
                    tg_request("editMessageText", {
                        "chat_id": chat_id, "message_id": msg_id, "parse_mode": "Markdown",
                        "text": f"✅ *تم تسجيل شراء {shares} سهم من `{sym}` بسعر {price} ج.م*\n💵 الكاش المتبقي: {user['available_cash']:,.2f} ج.م"
                    })

            elif action == "SELL":
                sym, exit_price = parts[1], float(parts[2])
                if sym in user["active_trades"]:
                    trade = user["active_trades"].pop(sym)
                    revenue = round(exit_price * trade["shares"], 2)
                    profit_egp = round(revenue - (trade["entry"] * trade["shares"]), 2)
                    profit_pct = round(((exit_price - trade["entry"]) / trade["entry"]) * 100, 2)

                    user["available_cash"] = round(user["available_cash"] + revenue, 2)
                    user["balance"] = round(user["balance"] + profit_egp, 2)
                    if profit_egp < 0:
                        user["daily_loss"] += abs(profit_egp)

                    user["history"].append({
                        "السهم": sym, "تاريخ_الدخول": trade["date"],
                        "تاريخ_الخروج": datetime.now(CAIRO_TZ).strftime("%Y-%m-%d %H:%M"),
                        "سعر_الشراء": trade["entry"], "سعر_البيع": exit_price,
                        "الكمية": trade["shares"], "الربح_جنيه": profit_egp, "النسبة": f"{profit_pct}%"
                    })
                    tg_request("editMessageText", {
                        "chat_id": chat_id, "message_id": msg_id, "parse_mode": "Markdown",
                        "text": f"💰 *تم إغلاق صفقة `{sym}`*\n• النتيجة: *{profit_egp:+,.2f} ج.م ({profit_pct:+}%)*\n• الرصيد الجديد: *{user['balance']:,.2f} ج.م*"
                    })

            elif action == "IGNORE":
                tg_request("deleteMessage", {"chat_id": chat_id, "message_id": msg_id})

    return force_scan_users

# ================= بيانات البورصة المصرية =================
def egx_get(endpoint, params=None):
    response = EGX_SESSION.get(
        f"{EGX_API_BASE}/{endpoint}",
        params=params,
        timeout=20
    )
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError as exc:
        raise ValueError(f"EGX returned a non-JSON response for {endpoint}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"EGX returned an invalid response for {endpoint}")
    if payload.get("success") is False:
        raise RuntimeError(f"EGX {endpoint}: {payload.get('message', 'request failed')}")
    return payload

def fetch_egx_symbols():
    market_payload = egx_get(
        "market-watch",
        {"Page": 1, "PageSize": 500, "SortBy": "value", "SortDescending": "true"}
    )
    market_data = market_payload.get("data")
    market_rows = market_data.get("data") if isinstance(market_data, dict) else None
    if not isinstance(market_rows, list):
        raise ValueError("EGX market-watch response did not contain a stock list")

    info_payload = egx_get("stock-info")
    info_rows = info_payload.get("data")
    if not isinstance(info_rows, list):
        raise ValueError("EGX stock-info response did not contain a stock list")

    quotes = {row.get("reuters"): row for row in market_rows if row.get("reuters")}
    metadata = {row.get("reuters"): row for row in info_rows if row.get("reuters")}
    return quotes, metadata

def fetch_egx_history(isin):
    for days in (730, 365, 180):
        try:
            payload = egx_get("price-volume-points", {"isin": isin, "date": days})
        except RuntimeError as exc:
            if days == 180:
                raise
            print(f"EGX {days}-day history unavailable for {isin}; trying a shorter range: {exc}")
            continue
        rows = payload.get("data")
        if not isinstance(rows, list):
            raise ValueError(f"EGX history for {isin} did not contain price points")
        return rows

    raise RuntimeError(f"EGX did not return usable history for {isin}")

def build_egx_price_frame(rows, quote):
    if not isinstance(rows, list) or not rows:
        raise ValueError("EGX returned no historical price points")

    frame = pd.DataFrame(rows)
    required = {"tradeDate", "closePrice", "tradeVolume"}
    if not required.issubset(frame.columns):
        raise ValueError("EGX historical data is missing date, price, or volume fields")

    frame = frame.rename(columns={
        "tradeDate": "Date",
        "closePrice": "Close",
        "tradeVolume": "Volume",
    })
    frame["Date"] = pd.to_datetime(frame["Date"], errors="coerce").dt.normalize()
    frame["Close"] = pd.to_numeric(frame["Close"], errors="coerce")
    frame["Volume"] = pd.to_numeric(frame["Volume"], errors="coerce").fillna(0)
    frame = frame.dropna(subset=["Date", "Close"])
    frame = frame[(frame["Close"] > 0) & (frame["Volume"] >= 0)]
    frame = frame.sort_values("Date").drop_duplicates("Date", keep="last").reset_index(drop=True)
    if frame.empty:
        raise ValueError("EGX historical data contained no usable price points")

    quote_price = quote.get("lastPrice") or quote.get("closePrice") or quote.get("lasT_CP")
    quote_date = quote.get("lastTradeDate") or quote.get("lasT_TRADE_DATE")
    quote_volume = quote.get("volume")
    if quote_volume is None:
        quote_volume = quote.get("lasT_VOL")

    quote_price = pd.to_numeric(quote_price, errors="coerce")
    quote_date = pd.to_datetime(quote_date, errors="coerce")
    if pd.notna(quote_price) and quote_price > 0 and pd.notna(quote_date):
        quote_date = quote_date.normalize()
        quote_volume = pd.to_numeric(quote_volume, errors="coerce")
        if quote_date == frame.iloc[-1]["Date"]:
            frame.loc[frame.index[-1], "Close"] = quote_price
            if pd.notna(quote_volume) and quote_volume > 0:
                frame.loc[frame.index[-1], "Volume"] = quote_volume
        elif quote_date > frame.iloc[-1]["Date"]:
            frame.loc[len(frame)] = {
                "Date": quote_date,
                "Close": quote_price,
                "Volume": quote_volume if pd.notna(quote_volume) and quote_volume >= 0 else 0,
            }

    return frame.sort_values("Date").reset_index(drop=True)

# ================= محرك التحليل الفني =================
def add_technical_indicators(frame):
    df = frame.copy()
    df["SMA20"] = df["Close"].rolling(20).mean()
    df["SMA50"] = df["Close"].rolling(50).mean()
    delta = df["Close"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    relative_strength = gain / loss
    df["RSI"] = 100 - (100 / (1 + relative_strength))
    df.loc[(loss == 0) & (gain > 0), "RSI"] = 100
    df.loc[(loss == 0) & (gain == 0), "RSI"] = 50

    exp1 = df["Close"].ewm(span=12, adjust=False).mean()
    exp2 = df["Close"].ewm(span=26, adjust=False).mean()
    df["MACD"] = exp1 - exp2
    df["Signal"] = df["MACD"].ewm(span=9, adjust=False).mean()
    df["Vol_Avg"] = df["Volume"].rolling(20).mean()
    return df

def forecast_similar_patterns(ticker, quote, history_rows):
    df = add_technical_indicators(build_egx_price_frame(history_rows, quote))
    if len(df) < 85:
        print(f"EGX history for {ticker} is too short for a five-session pattern forecast.")
        return None

    close = df["Close"]
    average_volume = df["Vol_Avg"].where(df["Vol_Avg"] > 0)
    features = pd.DataFrame({
        "momentum_5": close / close.shift(5) - 1,
        "sma20_distance": close / df["SMA20"] - 1,
        "sma50_distance": close / df["SMA50"] - 1,
        "rsi": df["RSI"] / 100,
        "macd_gap": (df["MACD"] - df["Signal"]) / close,
        "volume_ratio": (df["Volume"] / average_volume).clip(lower=0, upper=5),
    }).replace([float("inf"), float("-inf")], float("nan"))

    current_features = features.iloc[-1]
    if current_features.isna().any():
        print(f"EGX indicators for {ticker} are incomplete; skipping its pattern forecast.")
        return None

    forward_returns = close.shift(-FORECAST_HORIZON) / close - 1
    candidate_end = len(df) - FORECAST_HORIZON - 20
    historical_features = features.iloc[50:candidate_end + 1].dropna()
    valid_indices = historical_features.index.intersection(forward_returns.dropna().index)
    historical_features = historical_features.loc[valid_indices]
    outcomes = forward_returns.loc[valid_indices]
    if len(historical_features) < 10:
        print(f"EGX history for {ticker} has too few comparable patterns.")
        return None

    mean = historical_features.mean()
    scale = historical_features.std(ddof=0).replace(0, 1)
    normalized_history = (historical_features - mean) / scale
    normalized_current = (current_features - mean) / scale
    distances = (
        (normalized_history - normalized_current) ** 2
    ).mean(axis=1) ** 0.5
    nearest = []
    for index in distances.sort_values(kind="mergesort").index:
        if all(abs(index - selected) >= FORECAST_HORIZON for selected in nearest):
            nearest.append(index)
        if len(nearest) == 15:
            break
    if len(nearest) < 10:
        print(f"EGX history for {ticker} has too few independent pattern outcomes.")
        return None
    similar_returns = outcomes.loc[nearest]

    up_rate = float((similar_returns > 0).mean())
    down_rate = float((similar_returns < 0).mean())
    mean_return = float(similar_returns.mean())
    if up_rate >= 0.60 and mean_return > 0:
        direction = "up"
    elif up_rate <= 0.40 and mean_return < 0:
        direction = "down"
    else:
        direction = "neutral"

    return {
        "direction": direction,
        "up_rate": up_rate,
        "down_rate": down_rate,
        "mean_return": mean_return,
        "samples": len(similar_returns),
        "data_date": df.iloc[-1]["Date"].strftime("%Y-%m-%d"),
        "horizon": FORECAST_HORIZON,
    }

def format_pattern_forecast(forecast):
    if not forecast:
        return "التوقع التاريخي غير متاح لعدم كفاية البيانات."
    direction = {
        "up": "ميل صاعد",
        "down": "ميل هابط",
        "neutral": "الاتجاه غير حاسم",
    }[forecast["direction"]]
    outcome_name = "هبطت" if forecast["direction"] == "down" else "صعدت"
    outcome_rate = forecast["down_rate"] if forecast["direction"] == "down" else forecast["up_rate"]
    start_date, end_date = forecast_session_window(
        forecast["data_date"],
        forecast["horizon"],
    )
    return (
        f"{direction} خلال {forecast['horizon']} جلسات "
        f"(تقريباً من {start_date} إلى {end_date})؛ "
        f"{outcome_name} {outcome_rate:.0%} من {forecast['samples']} حالات مشابهة، "
        f"ومتوسط تغيرها {forecast['mean_return'] * 100:+.2f}%"
    )

def forecast_session_window(data_date, horizon=FORECAST_HORIZON):
    session_date = datetime.strptime(data_date, "%Y-%m-%d").date()
    session_dates = []
    while len(session_dates) < horizon:
        session_date += timedelta(days=1)
        if session_date.weekday() in [6, 0, 1, 2, 3]:
            session_dates.append(session_date.strftime("%Y-%m-%d"))
    return session_dates[0], session_dates[-1]

def send_forecast_summary(user, chat_id, results):
    forecasts = [result for result in results if result.get("forecast")]
    if not forecasts:
        return

    up = sorted(
        [result for result in forecasts if result["forecast"]["direction"] == "up"],
        key=lambda result: result["forecast"]["mean_return"],
        reverse=True,
    )[:3]
    down = sorted(
        [result for result in forecasts if result["forecast"]["direction"] == "down"],
        key=lambda result: result["forecast"]["mean_return"],
    )[:3]
    data_date = max(result["forecast"]["data_date"] for result in forecasts)
    lines = [
        "🔎 *حالة EGX الآن والتوقع للخمس جلسات القادمة*",
        f"آخر تاريخ بيانات: {data_date}",
        "نافذة التوقع تقريبية وتتجاهل العطلات الرسمية؛ لا يمكن تحديد يوم بعينه.",
    ]

    for result in up:
        forecast = result["forecast"]
        lines.append(
            f"🟢 `{result['symbol']}` — الحالة حسب EGX: {result['current_status']}؛ "
            f"آخر سعر {result['price']:,.3f} ج.م. التوقع: "
            f"{format_pattern_forecast(forecast)}"
        )
    for result in down:
        forecast = result["forecast"]
        lines.append(
            f"🔴 `{result['symbol']}` — الحالة حسب EGX: {result['current_status']}؛ "
            f"آخر سعر {result['price']:,.3f} ج.م. التوقع: "
            f"{format_pattern_forecast(forecast)}"
        )
    if not up and not down:
        lines.append("لا تظهر أنماط تاريخية مشابهة ذات اتجاه واضح حالياً.")
    lines.append("هذه إحصاءات تاريخية احتمالية وليست ضماناً أو نصيحة استثمارية.")

    send_signal_once(
        user,
        "FORECAST_STATUS_SUMMARY",
        data_date,
        chat_id,
        "\n".join(lines),
        None,
    )

def analyze_stock(ticker, quote, history_rows):
    df = build_egx_price_frame(history_rows, quote)
    if len(df) < 55:
        print(f"EGX history for {ticker} has only {len(df)} usable points; skipping.")
        return None

    df = add_technical_indicators(df)

    last, prev = df.iloc[-1], df.iloc[-2]
    price = float(last["Close"])
    prev_price = float(prev["Close"])
    sma20 = float(last["SMA20"])
    sma50 = float(last["SMA50"])
    rsi = float(last["RSI"])
    if pd.isna(rsi):
        print(f"EGX indicators for {ticker} are incomplete; skipping.")
        return None

    if price > sma20 and price > sma50:
        trend_status = "السعر فوق متوسطي 20 و50 جلسة"
    elif price < sma20 and price < sma50:
        trend_status = "السعر تحت متوسطي 20 و50 جلسة"
    else:
        trend_status = "السعر بين المتوسطات وإشارات الاتجاه مختلطة"
    if rsi >= 70:
        rsi_status = f"RSI {rsi:.1f} في منطقة تشبع شرائي"
    elif rsi <= 30:
        rsi_status = f"RSI {rsi:.1f} في منطقة تشبع بيعي"
    else:
        rsi_status = f"RSI {rsi:.1f} في النطاق المعتاد"
    change_pct = ((price / prev_price) - 1) * 100 if prev_price > 0 else 0.0
    current_status = (
        f"{trend_status}؛ {rsi_status}؛ "
        f"التغير عن الجلسة السابقة {change_pct:+.2f}%"
    )

    score, reasons = 0, []
    if price > sma20 > sma50:
        score += 30
        reasons.append("اتجاه صاعد فوق المتوسطات")
    if 42 <= rsi <= 62:
        score += 25
        reasons.append(f"زخم شرائي صحي (RSI: {rsi:.1f})")
    if float(last["MACD"]) > float(last["Signal"]) and float(last["MACD"]) > 0:
        score += 20
        reasons.append("تقاطع إيجابي لمؤشر MACD")
    if float(last["Vol_Avg"]) > 0 and float(last["Volume"]) > (float(last["Vol_Avg"]) * 1.2):
        score += 25
        reasons.append("حجم تداول أعلى من المتوسط بـ 20%")

    sell_reasons = []
    if rsi > 72 and price < prev_price:
        sell_reasons.append(f"تشبع شرائي وبداية انعكاس (RSI: {rsi:.1f})")
    if price < sma20 and prev_price >= sma20:
        sell_reasons.append("كسر المتوسط المتحرك SMA20 للأسفل")

    return {
        "symbol": ticker.replace(".CA", ""),
        "price": price,
        "data_date": last["Date"].strftime("%Y-%m-%d"),
        "current_status": current_status,
        "score": min(score, 100),
        "reasons": reasons,
        "sell_reasons": sell_reasons
    }

# ================= التشغيل الرئيسي =================
def main():
    tg_request("deleteWebhook")  # مسح أي ربط قديم يعطل قراءة الرسائل
    db = load_db()
    if ADMIN_CHAT_ID:
        is_new_user = str(ADMIN_CHAT_ID) not in db["users"]
        init_user(db, ADMIN_CHAT_ID)
        if is_new_user:
            send_msg(ADMIN_CHAT_ID, "🟢 تم الاتصال بنجاح! ابعت /start لعرض أوامر البوت.")

    # 1. قراءة وتنفيذ أي أوامر أو أزرار ضغطت عليها مسبقاً
    force_scan_users = process_telegram_updates(db)

    now = datetime.now(CAIRO_TZ)
    is_trading_day = now.weekday() in [6, 0, 1, 2, 3]
    is_session_time = (10 <= now.hour < 14) or (now.hour == 14 and now.minute <= 30)
    is_market_session = is_trading_day and is_session_time
    is_closing_time = (now.hour == 14 and now.minute >= 35)
    today_str = now.strftime("%Y-%m-%d")

    # التقرير بعد الإغلاق لا يحتاج إلى طلب بيانات السوق.
    if is_trading_day and is_closing_time and not force_scan_users:
        for uid, user in db["users"].items():
            if not user.get("report_sent_today") and user["history"]:
                send_user_report(uid, user)
                user["report_sent_today"] = True
        save_db(db)
        return

    needs_summary_upgrade = any(
        user.get("last_signals", {}).get("FORECAST_SUMMARY")
        and not user.get("last_signals", {}).get("FORECAST_STATUS_SUMMARY")
        for user in db["users"].values()
    )
    summary_upgrade_due = (
        needs_summary_upgrade
        and db.get("last_forecast_summary_upgrade_attempt_date") != today_str
    )
    forecast_due = (
        not is_market_session
        and now.hour >= 15
        and (
            db.get("last_forecast_attempt_date") != today_str
            or summary_upgrade_due
        )
    )
    if not (is_market_session or forecast_due or force_scan_users):
        save_db(db)
        print("خارج وقت تحليل السوق — تم تحديث الأوامر فقط.")
        return

    if forecast_due:
        db["last_forecast_attempt_date"] = today_str
        if summary_upgrade_due:
            db["last_forecast_summary_upgrade_attempt_date"] = today_str

    # 2. تحميل بيانات EGX وتحليل الأسهم
    try:
        market_quotes, stock_metadata = fetch_egx_symbols()
    except (requests.RequestException, ValueError, RuntimeError) as exc:
        print(f"EGX market data unavailable; skipping scan: {exc}")
        save_db(db)
        return

    results = []
    for ticker in WATCHLIST:
        if ticker not in market_quotes and ticker in stock_metadata:
            print(f"EGX market-watch has no current quote for {ticker}; using its latest listed-stock record.")
        quote = {
            **stock_metadata.get(ticker, {}),
            **market_quotes.get(ticker, {}),
        }
        isin = quote.get("isin")
        if not isin:
            print(f"EGX has no listed-stock record for {ticker}; skipping.")
            continue

        try:
            history = fetch_egx_history(isin)
            result = analyze_stock(ticker, quote, history)
            if result is not None:
                result["forecast"] = forecast_similar_patterns(ticker, quote, history)
                results.append(result)
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            print(f"EGX data error for {ticker}: {exc}")
        finally:
            time.sleep(0.25)

    if forecast_due and not results:
        print("No EGX stocks could be analyzed for today's forecast.")

    top_opps = sorted([r for r in results if r["score"] >= 75], key=lambda x: x["score"], reverse=True)[:3]
    is_opening_chaos = (now.hour == 10 and now.minute < 30)

    for uid, user in db["users"].items():
        if forecast_due or uid in force_scan_users:
            send_forecast_summary(user, uid, results)

        if not is_market_session and uid not in force_scan_users:
            continue

        # مراقبة صفقات البيع المفتوحة
        for res in results:
            sym, price = res["symbol"], res["price"]
            if sym in user["active_trades"]:
                trade = user["active_trades"][sym]
                sell_reason = None
                if price >= trade["tp"]:
                    sell_reason = "🎯 حقق السهم الهدف الربحي (+8%)"
                elif price <= trade["sl"]:
                    sell_reason = "🛑 كسر وقف الخسارة (-4%) — بيع لحماية رأس المال"
                elif res["sell_reasons"]:
                    sell_reason = "📉 " + " + ".join(res["sell_reasons"])

                if sell_reason:
                    btns = [[
                        {"text": "✅ نفذت البيع في تراديفاي", "callback_data": f"SELL|{sym}|{price}"},
                        {"text": "⏳ احتفاظ", "callback_data": "IGNORE"}
                    ]]
                    send_signal_once(
                        user,
                        f"SELL:{sym}",
                        res["data_date"],
                        uid,
                        f"🔴 *إشارة فنية للمراجعة — بيع محتمل*\n"
                        f"• السهم: `{sym}` | السعر: {price} ج.م\n"
                        f"• الحالة حسب EGX بتاريخ {res['data_date']}: {res['current_status']}\n"
                        f"• التوقع والتوقيت: {format_pattern_forecast(res['forecast'])}\n"
                        f"• السبب: {sell_reason}",
                        btns
                    )

        # شروط الأمان للشراء الجديد
        if is_opening_chaos and uid not in force_scan_users:
            continue
        if user["daily_loss"] >= (user["balance"] * 0.03) or len(user["active_trades"]) >= user["max_open_trades"]:
            continue

        for opp in top_opps:
            sym = opp["symbol"]
            if sym in user["active_trades"]:
                continue
            price = opp["price"]
            sl, tp = round(price * 0.96, 3), round(price * 1.08, 3)
            risk_per_share = price - sl
            if risk_per_share <= 0:
                print(f"Cannot size a position for {sym}: stop price rounds to the entry price.")
                continue
            shares = min(int((user["balance"] * user["risk_pct"]) / risk_per_share), int((user["balance"] * 0.25) / price))
            cost = round(shares * price, 2)

            if shares > 0 and user["available_cash"] >= cost:
                reasons_txt = "\n".join([f"  ✓ {r}" for r in opp["reasons"]])
                btns = [[
                    {"text": "✅ نفذت الشراء في تراديفاي", "callback_data": f"BUY|{sym}|{price}|{shares}|{sl}|{tp}"},
                    {"text": "❌ تجاهل", "callback_data": "IGNORE"}
                ]]
                send_signal_once(
                    user,
                    f"BUY:{sym}",
                    opp["data_date"],
                    uid,
                    f"🟢 *إشارة فنية محتملة للشراء — راجعها قبل القرار*\n"
                    f"• السهم: `{sym}` | الدرجة الفنية: {opp['score']}/100\n"
                    f"• الحالة حسب EGX بتاريخ {opp['data_date']}: {opp['current_status']}\n"
                    f"• التوقع والتوقيت: {format_pattern_forecast(opp['forecast'])}\n"
                    f"• السعر: {price} ج.م | الكمية: {shares} سهم ({cost:,.2f} ج.م)\n"
                    f"• الهدف: {tp} ج.م | الوقف: {sl} ج.م\n\n*الأسباب:*\n{reasons_txt}",
                    btns
                )

    save_db(db)

if __name__ == "__main__":
    main()
