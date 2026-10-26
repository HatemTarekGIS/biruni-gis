"""One-off helper: add Arabic translations for new strings, matched by a unique
prefix of the English text (avoids retyping long help texts), and drop unused
keys. usage: python add_ar_strings.py <plugin dir>"""
import json
import os
import subprocess
import sys

plugin = sys.argv[1]
ar_path = os.path.join(plugin, "i18n", "ar.json")
out = subprocess.run([sys.executable, os.path.join(os.path.dirname(__file__), "..", "extract_strings.py"), plugin],
                     capture_output=True, text=True, encoding="utf-8").stdout
all_strings = json.loads(out)

AR = {
    "Accuracy": "الدقة", "Altitude": "الارتفاع", "Bluetooth / USB (COM port)": "بلوتوث / USB (منفذ COM)",
    "Center": "توسيط", "Connect": "اتصال", "Connect to the phone (Wi-Fi)": "الاتصال بالهاتف (Wi-Fi)",
    "Connected - waiting for a position": "متصل - في انتظار الموقع…",
    "Connection lost - retrying": "انقطع الاتصال - جارٍ إعادة المحاولة…",
    "Connects a phone or GNSS receiver": "يتصل بهاتف أو جهاز GNSS عبر Wi-Fi أو بلوتوث أو USB، ويعرض الموقع الحي على الخريطة، ويسجّل المسارات - النقاط وخط المسار بالمسافة والسرعة وأماكن التوقف - داخل المشروع.",
    "Could not read {p}": "تعذّرت قراءة {p}", "Disconnect": "قطع الاتصال",
    "Enter the phone's address.": "أدخل عنوان الهاتف.", "Fix": "التثبيت", "Follow on the map": "تتبّع على الخريطة",
    "For a phone paired over Bluetooth": "لهاتف مقترن عبر البلوتوث بتطبيق مشاركة GPS، أو جهاز GNSS خارجي متصل بالبلوتوث أو USB.",
    "GPS": "GPS", "GPS & field": "GPS والعمل الميداني", "GPS connected": "اتصال GPS",
    "GPS tracks recorded": "مسارات GPS المسجّلة", "GPS points": "نقاط GPS", "GPS tracks": "مسارات GPS",
    "GPS stops": "توقفات GPS", "Heading": "الاتجاه",
    "Import GPS Track": "استيراد مسار GPS", "Import a recorded track (GPX)": "استيراد مسار مسجّل (GPX)…",
    "Import and analyse a GPS track": "استيراد مسار GPS وتحليله",
    "In the phone app, choose to send NMEA": "في تطبيق الهاتف اختر إرسال NMEA إلى هذا العنوان (UDP أو TCP). أندرويد: GPSd Forwarder. آيفون: GPS2IP. يجب أن يكون الهاتف والكمبيوتر على نفس شبكة Wi-Fi، أو الكمبيوتر متصلًا بنقطة اتصال الهاتف.",
    "Last update": "آخر تحديث",
    "No COM port found.": "لا يوجد منفذ COM. اقرن الهاتف أو وصّل الجهاز.",
    "No data for {s} s": "لا توجد بيانات منذ {s} ث - راجع تطبيق الهاتف.", "No fix": "بلا تثبيت",
    "No network found": "لا توجد شبكة", "Not connected": "غير متصل", "Nothing was recorded.": "لم يُسجَّل شيء.",
    "Phone\nGPS": "GPS\nالهاتف", "Phone GPS": "GPS الهاتف", "Phone address": "عنوان الهاتف",
    "Phone connected - waiting for a position": "الهاتف متصل - في انتظار الموقع…",
    "Phone not reachable - retrying": "تعذّر الوصول للهاتف - جارٍ إعادة المحاولة…", "Port": "المنفذ",
    "Position": "الموقع",
    "Reads a track recorded with a phone": "يقرأ مسارًا مسجّلًا بهاتف أو جهاز GPS (GPX، أو نقاط KML/GeoJSON بوقت) وينتج خط المسار بإحصاءاته - المسافة والمدة ووقت الحركة ومتوسط السرعة وأقصاها - والنقاط بسرعاتها، والأماكن التي توقف فيها الجهاز دقيقة أو أكثر.",
    "Receiving positions": "يستقبل المواقع", "Recording started": "بدء التسجيل",
    "Recording… {n} points · {km} km · {min} min": "جارٍ التسجيل… {n} نقطة · {km} كم · {min} دقيقة",
    "Recording… {n} points": "جارٍ التسجيل… {n} نقطة",
    "Satellites": "الأقمار", "Saved: {name} - {summary}": "تم الحفظ: {name} - {summary}", "Send to": "أرسِل إلى",
    "Speed": "السرعة", "Speed (baud)": "السرعة (baud)", "Start recording": "بدء التسجيل",
    "Stop recording": "إيقاف التسجيل", "Stops": "التوقفات",
    "The file contains no track points.": "الملف لا يحتوي على نقاط مسار.",
    "The phone sends to this PC (Wi-Fi)": "الهاتف يرسل إلى هذا الكمبيوتر (Wi-Fi)",
    "The points have no time": "النقاط بلا وقت: يمكن قياس المسافة فقط.",
    "Track": "المسار", "Track a phone (live GPS)": "تتبّع هاتف (GPS مباشر)", "Track file": "ملف المسار",
    "Track points": "نقاط المسار", "Track recorded": "تسجيل مسار", "Track {d}": "مسار {d}",
    "Use this when the phone app works as a server": "استخدم هذا عندما يعمل تطبيق الهاتف كخادم ويعرض عنوانه ومنفذه (مثل GPS2IP في وضع socket).",
    "Waiting for the phone": "في انتظار الهاتف…", "now": "الآن",
    "{km} km in {min} min, {stops} stops": "{km} كم في {min} دقيقة، {stops} توقف",
    "{n} points, {km} km, {stops} stops.": "{n} نقطة، {km} كم، {stops} توقف.", "{s} s ago": "منذ {s} ث",
}

ar = json.load(open(ar_path, encoding="utf-8"))
ar = {k: v for k, v in ar.items() if k in all_strings}
unmatched = []
for s in all_strings:
    if s in ar:
        continue
    hits = [p for p in AR if s.startswith(p)]
    if len(hits) == 1 or (hits and max(hits, key=len) == s):
        ar[s] = AR[max(hits, key=len)]
    elif len(hits) > 1:
        ar[s] = AR[max(hits, key=len)]
    else:
        unmatched.append(s)
with open(ar_path, "w", encoding="utf-8", newline="\n") as f:
    json.dump(dict(sorted(ar.items())), f, ensure_ascii=False, indent=1)
    f.write("\n")
print("unmatched:", unmatched)
