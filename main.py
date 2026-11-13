import os
import json
import time
from datetime import datetime
import pytz
import requests
import yfinance as yf
import pandas as pd

# ================= الإعدادات من GitHub Secrets =================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_CHAT_ID = os.environ.get("CHAT_ID")  # رقم حسابك الشخصي لتشغيل البوت لأول مرة

CAIRO_TZ = pytz.timezone("Africa/Cairo")
DB_FILE = "egx_pro_saas_db.json"

WATCHLIST = [
    "COMI.CA", "TMGH.CA", "EAST.CA", "HRHO.CA", "ETEL.CA",
    "SWDY.CA", "ABUK.CA", "FWRY.CA", "ESRS.CA", "MFPC.CA",
    "ORAS.CA", "EFIH.CA", "EKHO.CA", "HELI.CA", "PHDC.CA",
    "ADIB.CA", "CIEB.CA", "AMOC.CA", "SKPC.CA", "JUFO.CA"
]

# ================= إدارة قاعدة البيانات =================
def load_db():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {"last_update_id": 0, "users": {}}
    return {"last_update_id": 0, "users": {}}

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
            "history": []
        }
    elif db["users"][uid].get("last_trade_date") != today_str:
        db["users"][uid]["daily_loss"] = 0.0
        db["users"][uid]["last_trade_date"] = today_str
        db["users"][uid]["report_sent_today"] = False
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
    tg_request("sendMessage", payload)

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
            text = upd["message"]["text"].strip()
            user = init_user(db, chat_id)

            if text == "/start":
                send_msg(
                    chat_id,
                    "🤖 *أهلاً بك في EGX Autonomous Trader (GitHub Cloud)*\n"
                    f"💰 محفظتك: *{user['balance']:,.2f} ج.م* | 💵 الكاش: *{user['available_cash']:,.2f} ج.م*\n\n"
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
            user = init_user(db, chat_id)
            parts = cb["data"].split("|")
            action = parts[0]

            tg_request("answerCallbackQuery", {"callback_query_id": cb["id"]})

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

# ================= محرك التحليل الفني =================
def normalize_egx_price(price, df):
    if price > 400 and df["Close"].median() > 400:
        return round(price / 100.0, 2)
    return round(price, 2)

def analyze_stock(ticker):
    try:
        df = yf.download(ticker, period="4mo", interval="1d", progress=False)
        if df.empty or len(df) < 55:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df["SMA20"] = df["Close"].rolling(20).mean()
        df["SMA50"] = df["Close"].rolling(50).mean()
        delta = df["Close"].diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        df["RSI"] = 100 - (100 / (1 + (gain / loss)))

        exp1 = df["Close"].ewm(span=12, adjust=False).mean()
        exp2 = df["Close"].ewm(span=26, adjust=False).mean()
        df["MACD"] = exp1 - exp2
        df["Signal"] = df["MACD"].ewm(span=9, adjust=False).mean()
        df["Vol_Avg"] = df["Volume"].rolling(20).mean()

        last, prev = df.iloc[-1], df.iloc[-2]
        price = normalize_egx_price(float(last["Close"]), df)
        prev_price = normalize_egx_price(float(prev["Close"]), df)
        sma20 = normalize_egx_price(float(last["SMA20"]), df)
        sma50 = normalize_egx_price(float(last["SMA50"]), df)
        rsi = float(last["RSI"])

        score, reasons = 0, []
        if price > sma20 > sma50:
            score += 30
            reasons.append("اتجاه صاعد مؤكد فوق المتوسطات")
        if 42 <= rsi <= 62:
            score += 25
            reasons.append(f"زخم شرائي صحي (RSI: {rsi:.1f})")
        if float(last["MACD"]) > float(last["Signal"]) and float(last["MACD"]) > 0:
            score += 20
            reasons.append("تقاطع إيجابي لمؤشر MACD")
        if float(last["Volume"]) > (float(last["Vol_Avg"]) * 1.2):
            score += 25
            reasons.append("سيولة أعلى من المتوسط بـ 20%")

        sell_reasons = []
        if rsi > 72 and price < prev_price:
            sell_reasons.append(f"تشبع شرائي وبداية انعكاس (RSI: {rsi:.1f})")
        if price < sma20 and prev_price >= sma20:
            sell_reasons.append("كسر الدعم المتحرك SMA20 للأسفل")

        return {
            "symbol": ticker.replace(".CA", ""),
            "price": price,
            "score": min(score, 100),
            "reasons": reasons,
            "sell_reasons": sell_reasons
        }
    except Exception:
        return None

# ================= التشغيل الرئيسي =================
def main():
    db = load_db()
    if ADMIN_CHAT_ID:
        init_user(db, ADMIN_CHAT_ID)

    # 1. قراءة وتنفيذ أي أوامر أو أزرار ضغطت عليها مسبقاً
    force_scan_users = process_telegram_updates(db)

    now = datetime.now(CAIRO_TZ)
    is_trading_day = now.weekday() in [6, 0, 1, 2, 3]
    is_session_time = (10 <= now.hour < 14) or (now.hour == 14 and now.minute <= 30)
    is_closing_time = (now.hour == 14 and now.minute >= 35)

    # إذا لم يكن وقت جلسة ولم يطلب أحد /scan يدوياً، نحفظ التحديثات ونخرج لتوفير الموارد
    if not (is_trading_day and (is_session_time or is_closing_time)) and not force_scan_users:
        save_db(db)
        print("خارج أوقات التداول — تم تحديث الأوامر فقط.")
        return

    # 2. إرسال التقرير اليومي بعد الإغلاق (2:35 ظهراً)
    if is_trading_day and is_closing_time and not force_scan_users:
        for uid, user in db["users"].items():
            if not user.get("report_sent_today") and user["history"]:
                send_user_report(uid, user)
                user["report_sent_today"] = True
        save_db(db)
        return

    # 3. تحليل الأسهم
    results = [r for t in WATCHLIST if (r := analyze_stock(t)) is not None]
    top_opps = sorted([r for r in results if r["score"] >= 75], key=lambda x: x["score"], reverse=True)[:3]
    is_opening_chaos = (now.hour == 10 and now.minute < 30)

    for uid, user in db["users"].items():
        if not (is_trading_day and is_session_time) and uid not in force_scan_users:
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
                    send_msg(uid, f"🔴 *إشارة بيع عاجلة — متوقع ينخفض*\n• السهم: `{sym}` | السعر: {price} ج.م\n• السبب: {sell_reason}", btns)

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
            sl, tp = round(price * 0.96, 2), round(price * 1.08, 2)
            shares = min(int((user["balance"] * user["risk_pct"]) / (price - sl)), int((user["balance"] * 0.25) / price))
            cost = round(shares * price, 2)

            if shares > 0 and user["available_cash"] >= cost:
                reasons_txt = "\n".join([f"  ✓ {r}" for r in opp["reasons"]])
                btns = [[
                    {"text": "✅ نفذت الشراء في تراديفاي", "callback_data": f"BUY|{sym}|{price}|{shares}|{sl}|{tp}"},
                    {"text": "❌ تجاهل", "callback_data": "IGNORE"}
                ]]
                send_msg(
                    uid,
                    f"🟢 *فرصة شراء ذكية — متوقع يرتفع*\n"
                    f"• السهم: `{sym}` | الثقة: {opp['score']}%\n"
                    f"• السعر: {price} ج.م | الكمية: {shares} سهم ({cost:,.2f} ج.م)\n"
                    f"• الهدف: {tp} ج.م | الوقف: {sl} ج.م\n\n*الأسباب:*\n{reasons_txt}",
                    btns
                )

    save_db(db)

if __name__ == "__main__":
    main()
