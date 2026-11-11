"""Text encodings for every market: detect the code page of raw bytes (DBF, CSV, DXF, TXT), repair text that
was decoded with the wrong one (mojibake), write the .cpg that tells GIS programs how to read a shapefile, and
repair the text of DXF drawings.

Detection decodes the bytes with every candidate code page and scores each result for how much it looks like
real writing: letters of one script per word, the common letters of that script, no control or replacement
characters, no stray symbols, no capitals inside words. Legacy *glyph* fonts (Vietnamese TCVN3/VNI, Bengali
Bijoy, Hindi Kruti Dev) are not code pages - see legacy_fonts.py.

Pure Python (no qgis import), so it also runs outside QGIS."""
import os
import re
import struct
import unicodedata

# ---- code pages ---------------------------------------------------------------------------------------------
# Single-byte and multi-byte candidates, in tie-break order (earlier wins a tie).
SINGLE_BYTE = ["cp1252", "cp1250", "cp1254", "cp1251", "koi8_r", "cp1256", "cp1255", "cp874", "cp1258"]
MULTI_BYTE = ["cp949", "gbk", "big5", "cp932"]
DOS = ["cp720", "cp864", "cp866", "cp437", "cp850"]  # only tried when a country or the DBF asks for them
ALL = ["utf-8"] + MULTI_BYTE + SINGLE_BYTE

NAMES = {  # what people call them -> Python codec
    "utf8": "utf-8", "utf-8": "utf-8", "latin1": "cp1252", "latin-1": "cp1252", "iso-8859-1": "cp1252",
    "iso8859-1": "cp1252", "windows-1252": "cp1252", "ansi": "cp1252", "1252": "cp1252",
    "tis-620": "cp874", "tis620": "cp874", "iso-8859-11": "cp874", "874": "cp874", "windows-874": "cp874",
    "euc-kr": "cp949", "euckr": "cp949", "uhc": "cp949", "ks_c_5601-1987": "cp949", "949": "cp949",
    "windows-949": "cp949", "gb2312": "gbk", "gb18030": "gbk", "cp936": "gbk", "936": "gbk", "big5": "big5",
    "cp950": "big5", "950": "big5", "shift_jis": "cp932", "sjis": "cp932", "shift-jis": "cp932", "932": "cp932",
    "koi8-r": "koi8_r", "koi8r": "koi8_r", "866": "cp866",
}
for _cp in ("1250", "1251", "1253", "1254", "1255", "1256", "1257", "1258", "720", "864", "437", "850", "852"):
    NAMES.setdefault(_cp, "cp" + _cp)
    NAMES.setdefault("windows-" + _cp, "cp" + _cp)

# .cpg text GDAL understands (a number means "CP<number>")
CPG = {"utf-8": "UTF-8", "cp1252": "1252", "cp1250": "1250", "cp1251": "1251", "cp1253": "1253",
       "cp1254": "1254", "cp1255": "1255", "cp1256": "1256", "cp1257": "1257", "cp1258": "1258", "cp874": "874",
       "cp949": "949", "gbk": "936", "big5": "950", "cp932": "932", "koi8_r": "KOI8-R", "cp866": "866",
       "cp720": "720", "cp864": "864", "cp437": "437", "cp850": "850", "cp852": "852"}

# DXF $DWGCODEPAGE values
DXF_CODEPAGES = {"utf-8": "UTF8", "cp1252": "ANSI_1252", "cp1250": "ANSI_1250", "cp1251": "ANSI_1251",
                 "cp1253": "ANSI_1253", "cp1254": "ANSI_1254", "cp1255": "ANSI_1255", "cp1256": "ANSI_1256",
                 "cp1257": "ANSI_1257", "cp1258": "ANSI_1258", "cp874": "ANSI_874", "cp932": "ANSI_932",
                 "gbk": "ANSI_936", "cp949": "ANSI_949", "big5": "ANSI_950"}

# dBASE language driver ids (byte 29 of the header), as GDAL reads them
LDID = {0x01: "cp437", 0x02: "cp850", 0x03: "cp1252", 0x13: "cp932", 0x26: "cp866", 0x4D: "gbk", 0x4E: "cp949",
        0x4F: "big5", 0x50: "cp874", 0x57: "cp1252", 0x58: "cp1252", 0x59: "cp1252", 0x64: "cp852",
        0x65: "cp866", 0x78: "big5", 0x79: "cp949", 0x7A: "gbk", 0x7B: "cp932", 0x7C: "cp874", 0x7D: "cp1255",
        0x7E: "cp1256", 0xC8: "cp1250", 0xC9: "cp1251", 0xCA: "cp1254", 0xCB: "cp1253", 0xCC: "cp1257"}

# likely encodings per country (ISO 3166 alpha-2), best first - used as a prior by the Region registry
COUNTRY = {
    "KR": ["cp949"], "KP": ["cp949"], "TH": ["cp874"], "VN": ["utf-8", "tcvn3", "vni", "cp1258"],
    "IR": ["cp1256"], "AF": ["cp1256"], "IQ": ["cp1256"], "SA": ["cp1256"], "EG": ["cp1256"], "AE": ["cp1256"],
    "KW": ["cp1256"], "QA": ["cp1256"], "BH": ["cp1256"], "OM": ["cp1256"], "YE": ["cp1256"], "JO": ["cp1256"],
    "SY": ["cp1256"], "LB": ["cp1256"], "LY": ["cp1256"], "SD": ["cp1256"], "PS": ["cp1256"],
    "DZ": ["cp1256", "cp1252"], "MA": ["cp1256", "cp1252"], "TN": ["cp1256", "cp1252"], "PK": ["cp1256"],
    "IL": ["cp1255"], "TR": ["cp1254"], "AZ": ["cp1254"], "RU": ["cp1251", "koi8_r", "cp866"],
    "UA": ["cp1251"], "BY": ["cp1251"], "KZ": ["cp1251"], "KG": ["cp1251"], "BG": ["cp1251"], "RS": ["cp1251"],
    "MK": ["cp1251"], "PL": ["cp1250"], "CZ": ["cp1250"], "SK": ["cp1250"], "HU": ["cp1250"], "SI": ["cp1250"],
    "HR": ["cp1250"], "RO": ["cp1250"], "AL": ["cp1250"], "CN": ["gbk"], "SG": ["gbk"], "TW": ["big5"],
    "HK": ["big5"], "MO": ["big5"], "JP": ["cp932"], "BD": ["bijoy"], "IN": ["krutidev"], "GR": ["cp1253"],
    "EE": ["cp1257"], "LV": ["cp1257"], "LT": ["cp1257"],
}


def codec(name):
    """The Python codec for an encoding name ('UTF-8', '1256', 'EUC-KR', 'ANSI_949', 'TIS-620' …)."""
    if not name:
        return None
    n = str(name).strip().lower().replace("ansi_", "")
    if n in NAMES:
        return NAMES[n]
    if n.isdigit():
        return "cp" + n
    try:
        import codecs

        return codecs.lookup(n).name.replace("-", "_") if n not in ("utf8",) else "utf-8"
    except LookupError:
        return None


def for_country(cc):
    """Likely encodings for a country code (best first); [] when nothing special is known."""
    return list(COUNTRY.get((cc or "").upper(), []))


def cpg_name(encoding):
    c = codec(encoding) or encoding
    return CPG.get(c, str(encoding).upper())


# ---- scripts and letter weights -----------------------------------------------------------------------------
_RU_FREQ = "оеаинтсрвлкмдпуяыьгзбчйхжшюцщэфъё"
_RU_W = {c: (1.0 if i < 16 else 0.8 if i < 26 else 0.55) for i, c in enumerate(_RU_FREQ)}
_RU_W.update({c: 0.8 for c in "ієїґў"})  # Ukrainian, Belarusian
_LATIN_COMMON = set("àáâãäåæçèéêëìíîïñòóôõöøùúûüýÿßœąćęłńśźżčďěňřšťůžőűğışăâîșțţđơưåæøąėįųūšž"
                    "ÀÁÂÃÄÅÆÇÈÉÊËÌÍÎÏÑÒÓÔÕÖØÙÚÛÜŸĄĆĘŁŃŚŹŻČĎĚŇŘŠŤŮŽŐŰĞİŞĂÂÎȘȚŢĐƠƯŒ")
_LATIN_RARE = set("ðþÐÞÝ")
_SYM_OK = set("°²³½¼¾×·’‘“”–—«»€…•№§©®™µ±†‡‰")
_HEB_POINTS = range(0x05B0, 0x05C8)

# The most frequent Chinese characters (simplified and traditional forms) and common Japanese kanji: a decoding
# with the wrong code page turns real text into rare characters, so the share of these separates the right one.
_HAN_COMMON = set(
    "的一是不了在人有我他这个们中来上大为和国地到以说时要就出会可也你对生能而子那得于着下自之年过发后作里用道行所然家种事"
    "成方多经么去法学如都同现当没动面起看定天分还进好小部其些主样理心她本前开但因只从想实日军者意无力它与长把机十民第公此"
    "已工使情明性知全三又关点正业外将两高间由问很最重并物手应战向头文体政美相见被利什二等产或新己制身果加西斯月话合回特代"
    "内信表化老给世位次度门任常先海通教儿原东声提立及比员解水名真论处走义各入几口认条平系气题活尔更别打女变四神总何电数安"
    "少报才结反受目太量再感建务做接必场件计管期市直德资命山金指克许统区保至队形社便空决治展马科司五基眼书非则听白却界达光"
    "放强即像难且权思王象完设式色路记南品住告类求据程北边死张该交规万取拉格望觉术领共确传师观清今切院让识候带导争运笑飞风"
    "步改收根干造言联持组每济车亲极林服快办议往元英士证近失转夫令准布始怎呢存未远叫台单影具罗字爱击流备兵连调深商算质团集"
    "百需价花党华城石级整府离况亚请技际约示复病息究线似官火断精满支视消越器容照须九增研写称企八功吗包片史委乎查轻易早曾除"
    "农找装广显吧阿李标谈吃图念六引历首医局突专费号尽另周较注语仅考落青随选列武红响虽推势参希古众构房半节土投某案黑维革划"
    "敌致陈律足态护七兴派孩验责营星够章音跟志底站严巴例防族供效续施留讲型料终答紧黄绝奇察母京段依批群项故按河米围江织害斗"
    "双境客纪采举杀攻父苏密低朝友诉止细愿千值仍男钱破网热助倒育属坐帝限船脸职速刻乐否刚威毛状率甚独球般普怕弹校苦创假久错"
    "承印晚兰试股拿脑预谁益阳若哪微尼继送急血惊伤素药适波夜省初喜卫源食险待述陆习置居劳财环排福纳欢雷警获模充负云停木游龙"
    "树疑层冷洲冲射略范竟句室异激汉村哈策演简卡罪判担州静退既衣您宗积余痛检差富灵协角占配征修皮挥胜降阶审沉坚善妈刘读啊超"
    "免压银买皇养伊怀执副乱抗犯追帮宣佛岁航优怪香著田铁控税左右份穿艺背阵草脚概恶块顿敢守酒岛托央户烈洋哥索胡款靠评版宝座"
    "释景顾弟登货互付伯慢欧换闻危忙核暗姐介坏讨丽良序升监临亮露永呼味野架域沙掉括舰鱼杂误湖镇乡县街道园桥港湾岭坡塘庄"
    "這個們來為國說時會對發後過裡麼還進現當沒動樣經從實關點業將兩間問題長門見與開機無軍學體電區號縣鄉鎮東華園臺灣車馬書"
    "讓認識變邊頭話員處義聲論條氣總數報結區權廣際黨歷醫網陽陸聯戰鐵導術縣灣橋龍廳館層務辦場齊師農區島嶺莊"
    "日本人東京都道府県市区町村郡丁目番地号駅線通北南西新上下中山川田島野本大小高橋宮崎岡福井沢松原池谷森林")
_HANGUL_COMMON = set(
    "이의다에는가하고지서기사로를도리아대시자정수나동구일인한전상어보해제주성부국원장경문위면군읍산강남북중길번호"
    "파트마을학교청우리것들그수있없되는요니까습니다했았었세계생활방법회사업무관련정보시설지역공원역로터미널병원센터"
    "서울부산대구인천광주대전울산세종경기강원충청전라경상제주특별광역도시군구읍면동리가로길번지호아파트빌라단지"
    "신중앙중구용산성동광진동대문중랑성북강북도봉노원은평서대문마포양천강서구로금천영등포동작관악서초강남송파강동"
    "김이박최정강조윤장임한오신권황안송류홍전고문양손배백허유남심노하곽성차주우구민진나지엄채원천방공현함변염여추도석소선설마길연위표명기반왕금옥육인맹제모탁국어은편용예"
    "특별테헤란평택춘천원주강릉포항경주김해창원진주목포여수순천익산군산전주청주천안아산당진서산홍성논산공주보령수원성남"
    "고양용인부천안산안양남양주화성의정부시흥파주김포광명하남오산이천양주구리포천양평여주동두천과천가평연천분당일산판교"
    "관내외앞뒤옆새큰작은높낮넓좁좋나쁜많적같다른모든어떤무엇누구언제어디왜어떻게그리고하지만그래서또는및등을를이가은는의"
    "에서으로로와과도만까지부터보다처럼에게한테께서께조차마저밖에야말로든지라도이나나마요죠네까데군걸게세결권대학병의원"
    "점역앞쪽측번째층호실동관건물주택상가공장창고농지임야전답대지도로하천구거유지제방묘지공원체육유원지종교사적철도수도"
    "광장주차장주유소창고시장학교관공서은행우체국경찰서소방서보건소도서관미술관박물관문화회관운동장골프장스키장해수욕장")
_THAI_RANGE = (0x0E01, 0x0E5B)


def _kind(ch):
    o = ord(ch)
    if o < 0x80:
        return "ascii"
    if 0x80 <= o <= 0x9F or o == 0xFFFD or 0xE000 <= o <= 0xF8FF or unicodedata.category(ch) == "Cn":
        return "bad"
    if 0x0300 <= o <= 0x036F:
        return "comb"
    if 0x00C0 <= o <= 0x024F or 0x1E00 <= o <= 0x1EFF:
        return "latin"
    if 0x0400 <= o <= 0x04FF:
        return "cyr"
    if 0x0370 <= o <= 0x03FF:
        return "greek"
    if 0x0590 <= o <= 0x05FF:
        return "hebr"
    if 0x0600 <= o <= 0x06FF or 0x0750 <= o <= 0x077F or 0xFB50 <= o <= 0xFDFF or 0xFE70 <= o <= 0xFEFF:
        return "arab"
    if _THAI_RANGE[0] <= o <= _THAI_RANGE[1]:
        return "thai"
    if 0x0900 <= o <= 0x097F:
        return "deva"
    if 0x0980 <= o <= 0x09FF:
        return "beng"
    if 0xAC00 <= o <= 0xD7A3 or 0x1100 <= o <= 0x11FF or 0x3130 <= o <= 0x318F:
        return "hangul"
    if 0x3040 <= o <= 0x30FF:
        return "kana"
    if 0xFF61 <= o <= 0xFF9F:
        return "halfkana"
    if 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF or 0xF900 <= o <= 0xFAFF:
        return "han"
    if 0x3000 <= o <= 0x303F or 0xFF00 <= o <= 0xFF60:
        return "cjkpunct"
    return "letter" if ch.isalpha() else "sym"


_THAI_RARE = set("ฃฅฆฌฎฏฐฑฒณฬฮ")
_THAI_OBSOLETE = set("ฤฦ๏๚๛ฯ")
_THAI_MARKS = set(chr(c) for c in [0x0E31] + list(range(0x0E34, 0x0E3B)) + list(range(0x0E47, 0x0E4F)))
_THAI_LEADING = set("เแโใไ")


def _thai_weight(ch, prev, prev_kind):
    """Thai syllables: vowel and tone marks sit on a consonant (or on another mark); a leading vowel is never
    followed by a mark. Byte soup read as cp874 breaks these rules."""
    if ch in _THAI_MARKS:
        if prev_kind != "thai" or prev in _THAI_LEADING or not (0x0E01 <= ord(prev) <= 0x0E2E or prev in _THAI_MARKS):
            return -1.0
        return 1.0
    if ch in _THAI_OBSOLETE:
        return -0.5
    if ch in _THAI_RARE:
        return 0.5
    if 0x0E50 <= ord(ch) <= 0x0E59:
        return 0.6
    return 1.0


_BICAMERAL = {"latin", "cyr", "greek", "ascii"}
_VOWELS_X = set("àáâãäåèéêëìíîïòóôõöùúûüýÿ")


def _weight(ch, kind, prev, prev_kind):
    """How much one non-ASCII character looks like part of real text (-2 … 1)."""
    if kind == "bad":
        return -2.0
    if kind == "sym":
        return 0.0 if ch in _SYM_OK else -0.7
    if kind == "comb":  # combining marks: Vietnamese tones after a vowel (cp1258)
        return 1.0 if prev_kind in ("latin", "ascii") and prev.isalpha() else -1.0
    if kind == "latin":
        if ch in _LATIN_RARE:
            return 0.2
        w = 1.0 if ch in _LATIN_COMMON or 0x1EA0 <= ord(ch) <= 0x1EF9 else 0.45
        if prev.lower() in _VOWELS_X and ch.lower() in _VOWELS_X:
            w -= 0.4  # two accented vowels in a row: typical of a wrong code page
        return w
    if kind == "cyr":
        return _RU_W.get(ch.lower(), 0.45)
    if kind == "hebr":
        if ord(ch) in _HEB_POINTS:
            return 0.5 if prev_kind == "hebr" and 0x05D0 <= ord(prev) <= 0x05EA else -1.0
        return 1.0 if 0x05D0 <= ord(ch) <= 0x05EA else -0.5
    if kind == "arab":
        o = ord(ch)
        if 0x0621 <= o <= 0x064A or ch in "پچژکگیۆێڵڕەڤٹڈڑںےھ،؛؟٠١٢٣٤٥٦٧٨٩":
            return 1.0
        if 0x064B <= o <= 0x0652:  # harakat after a letter
            return 0.6 if prev_kind == "arab" else -1.0
        return 0.3
    if kind == "thai":
        return _thai_weight(ch, prev, prev_kind)
    if kind in ("deva", "beng"):
        return 1.0
    if kind == "hangul":
        return 1.0 if ch in _HANGUL_COMMON else 0.5 if 0xAC00 <= ord(ch) <= 0xD7A3 else 0.1
    if kind == "kana":
        return 1.0
    if kind == "halfkana":
        return -0.3
    if kind == "han":
        return 1.0 if ch in _HAN_COMMON else 0.3 if ord(ch) <= 0x9FFF else 0.05
    if kind == "cjkpunct":
        return 0.6
    if kind == "greek":
        return 0.6
    return 0.2


_COMPATIBLE = ({"han", "kana", "halfkana"}, {"han", "hangul"})


def _mixed(scripts):
    """Letters of scripts that never share a word (Cyrillic with Arabic, Latin with Hebrew …)."""
    s = set(scripts) - {"comb"}
    if len(s) <= 1:
        return False
    if any(s <= group for group in _COMPATIBLE):
        return False
    return True


def score_text(text):
    """How much a text looks like real writing: 1 = clean, 0 or less = garbage. Pure ASCII returns 1.0."""
    if not text:
        return 1.0
    total, n = 0.0, 0
    prev, prev_kind = " ", "ascii"
    scripts, weights = set(), []
    run = 0  # accented Latin letters in a row: "ÔÇÑÚ" is a code page error, no language writes that

    def close_word():
        nonlocal total, n
        if weights:
            penalty = 1.2 if _mixed(scripts) else 0.0
            total += sum(weights) - penalty * len(weights)
            n += len(weights)

    for ch in text:
        kind = _kind(ch)
        if ch.isalpha() or kind == "comb":
            script = "latin" if kind in ("ascii", "latin") else kind
            if kind != "comb":
                scripts.add(script)
            run = run + 1 if kind == "latin" else 0
            if kind != "ascii":
                w = _weight(ch, kind, prev, prev_kind)
                # a capital right after a small letter inside a word (KOI8 read as cp1251 …)
                if kind in _BICAMERAL and ch.isupper() and prev.isalpha() and prev.islower():
                    w -= 0.7
                if run >= 3:
                    w -= 0.8
                weights.append(w)
            elif ch.isupper() and prev.isalpha() and prev.islower() and prev_kind != "ascii":
                weights.append(-0.5)
        else:
            close_word()
            scripts, weights, run = set(), [], 0
            if kind != "ascii" and not (kind == "sym" and ch in _SYM_OK):  # ° ½ † … say nothing either way
                total += _weight(ch, kind, prev, prev_kind)
                n += 1
        prev, prev_kind = ch, kind
    close_word()
    return 1.0 if n == 0 else total / n


def dominant_script(text):
    counts = {}
    for ch in text or "":
        if ch.isalpha():
            k = _kind(ch)
            k = "latin" if k in ("ascii", "latin") else k
            counts[k] = counts.get(k, 0) + 1
    return max(counts, key=counts.get) if counts else ""


# ---- detection ------------------------------------------------------------------------------------------------
def _decode(raw, enc):
    try:
        text = raw.decode(enc)
    except (UnicodeDecodeError, LookupError):
        return None
    return unicodedata.normalize("NFC", text) if enc == "cp1258" else text


def decode(raw, encoding):
    """Bytes decoded with an encoding (any name), or None when they are not valid in it."""
    return _decode(raw, codec(encoding) or encoding)


def rank_encodings(samples, candidates=None, prefer=()):
    """[(encoding, score, script)] best first, for raw byte strings. Samples without 8-bit bytes are ignored.
    An encoding that cannot decode a sample is dropped. `prefer` (e.g. for_country(cc) or the DBF language
    driver) breaks near ties."""
    samples = [s for s in samples if s and any(b >= 0x80 for b in s)]
    if not samples:
        return []
    prefer = [codec(p) for p in prefer if p and codec(p)]
    candidates = [codec(c) or c for c in (candidates or ALL + [p for p in prefer if p in DOS])]
    ranked = []
    for order, enc in enumerate(candidates):
        total, weight, texts, ok = 0.0, 0, [], True
        for s in samples:
            t = _decode(s, enc)
            if t is None:
                ok = False
                break
            k = sum(1 for c in t if ord(c) > 0x7F) or 1
            total += score_text(t) * k
            weight += k
            if len(texts) < 50:
                texts.append(t)
        if not ok:
            continue
        sc = total / max(weight, 1)
        if enc == "utf-8":
            sc += 0.25  # valid UTF-8 with 8-bit text almost never happens by chance
        if enc in prefer:
            sc += 0.06 * (len(prefer) - prefer.index(enc)) / len(prefer)
        ranked.append((enc, sc, dominant_script(" ".join(texts)), -order))
    ranked.sort(key=lambda r: (round(r[1], 6), r[3]), reverse=True)
    return [(e, s, sc) for e, s, sc, _o in ranked]


def detect(samples, candidates=None, prefer=()):
    """(encoding, score) of raw byte strings; (None, 0.0) when there is no 8-bit text."""
    r = rank_encodings(samples, candidates, prefer)
    return (r[0][0], r[0][1]) if r else (None, 0.0)


# ---- mojibake (text decoded with the wrong code page) ----------------------------------------------------------
_MOJIBAKE_SOURCES = ["cp1252+", "cp1256", "cp1251", "cp850", "cp437", "cp949", "gbk"]


def _encode_back(text, src):
    """The bytes a wrongly decoded text came from. 'cp1252+' is cp1252 where the five bytes it leaves
    undefined (0x81, 0x8D …) came through as the same code points, as Windows and .NET decode them."""
    if src != "cp1252+":
        return text.encode(src)
    out = bytearray()
    for ch in text:
        try:
            out += ch.encode("cp1252")
        except UnicodeEncodeError:
            if ord(ch) > 0xFF:
                raise
            out.append(ord(ch))
    return bytes(out)


def repair_mojibake(text, candidates=None, prefer=()):
    """Text that was decoded with the wrong code page, back to what it was. Returns (text, how) - how is ''
    when nothing was changed. Real accented Latin text is left alone."""
    if not text or all(ord(c) < 0x80 for c in text):
        return text, ""
    base = score_text(text)
    if base >= 0.95:
        return text, ""
    best, best_s, how = text, base, ""
    targets = [codec(c) or c for c in (candidates or ALL)]
    for src in _MOJIBAKE_SOURCES:
        try:
            raw = _encode_back(text, src)
        except UnicodeEncodeError:
            continue
        for r in rank_encodings([raw], targets, prefer)[:1]:
            enc, s, _script = r
            if enc == src.rstrip("+"):
                continue
            cand = _decode(raw, enc)
            s = score_text(cand) + (0.1 if enc == "utf-8" else 0.0)
            if cand != text and s >= 0.6 and s > best_s + 0.25:
                best, best_s, how = cand, s, f"{src}→{enc}"
    return best, how


# ---- shapefiles ------------------------------------------------------------------------------------------------
def dbf_ldid(dbf_path):
    """The code page named by a DBF's language driver byte, or None."""
    try:
        with open(dbf_path, "rb") as f:
            head = f.read(32)
    except OSError:
        return None
    return LDID.get(head[29]) if len(head) >= 30 else None


def read_dbf_strings(dbf_path, max_records=None):
    """{field name: {record number: raw bytes}} of the text (C) fields of a dBASE file, in field order.
    Record numbers are the shapefile feature ids; deleted records are skipped."""
    with open(dbf_path, "rb") as f:
        head = f.read(32)
        if len(head) < 32:
            raise ValueError("not a dBASE file")
        n_rec, head_len, rec_len = struct.unpack("<IHH", head[4:12])
        fields = []
        while True:
            d = f.read(32)
            if not d or d[0] == 0x0D:
                break
            fields.append((d[:11].split(b"\x00")[0].decode("latin-1"), chr(d[11]), d[16]))
        f.seek(head_len)
        out = {name: {} for name, t, _l in fields if t == "C"}
        limit = n_rec if max_records is None else min(n_rec, max_records)
        for r in range(limit):
            rec = f.read(rec_len)
            if len(rec) < rec_len:
                break
            if rec[:1] == b"*":
                continue
            pos = 1
            for name, t, length in fields:
                if t == "C":
                    out[name][r] = rec[pos:pos + length].rstrip(b" \x00")
                pos += length
        return out


def detect_dbf(dbf_path, prefer=(), sample=3000):
    """(encoding, score, {field: (encoding, score)}) of a dBASE file's text."""
    strings = read_dbf_strings(dbf_path, sample)
    ld = dbf_ldid(dbf_path)
    pref = list(prefer) + ([ld] if ld and ld not in ("cp1252",) else [])
    per_field = {name: detect(list(vals.values()), prefer=pref) for name, vals in strings.items()}
    enc, sc = detect([v for vals in strings.values() for v in vals.values()], prefer=pref)
    return enc, sc, per_field


def write_cpg(shp_path, encoding):
    """Write the .cpg next to a shapefile so GIS programs read its text with `encoding`; returns its path."""
    path = os.path.splitext(shp_path)[0] + ".cpg"
    with open(path, "w", encoding="ascii") as f:
        f.write(cpg_name(encoding))
    return path


# ---- DXF -------------------------------------------------------------------------------------------------------
_TEXT_CODES = {"1", "3"}  # TEXT/MTEXT/ATTRIB/ATTDEF/DIMENSION text values
_TEXT_ENTITIES = {"TEXT", "MTEXT", "ATTRIB", "ATTDEF", "DIMENSION", "MULTILEADER", "TOLERANCE"}
_U_ESC = re.compile(r"\\U\+([0-9A-Fa-f]{4})")


def _dxf_pairs(raw):
    lines = raw.split(b"\n")
    lines = [ln[:-1] if ln.endswith(b"\r") else ln for ln in lines]
    for i in range(0, len(lines) - 1, 2):
        yield lines[i], lines[i + 1]


def _dxf_header(raw):
    """($ACADVER, $DWGCODEPAGE) of a DXF."""
    ver = cp = None
    want = None
    for code, val in _dxf_pairs(raw[:200000]):
        c = code.strip()
        if c == b"9":
            want = val.strip()
        elif want == b"$ACADVER" and c == b"1":
            ver, want = val.strip().decode("ascii", "replace"), None
        elif want == b"$DWGCODEPAGE" and c == b"3":
            cp, want = val.strip().decode("ascii", "replace"), None
        elif c == b"0" and val.strip() == b"ENDSEC" and ver:
            break
    return ver, cp


def _unescape(text):
    return _U_ESC.sub(lambda m: chr(int(m.group(1), 16)), text)


def _dxf_utf8_version(ver):
    try:
        return int((ver or "AC1009")[2:]) >= 1021
    except ValueError:
        return False


def dxf_texts(path):
    """[(raw bytes, decoded text)] of the text values of a DXF, decoded the way the file really is (detected,
    not trusted from the header). Returns (texts, declared code page, detected encoding)."""
    raw = open(path, "rb").read()
    ver, cp = _dxf_header(raw)
    values, entity = [], None
    for code, val in _dxf_pairs(raw):
        c = code.strip()
        if c == b"0":
            entity = val.strip().decode("ascii", "replace")
        elif entity in _TEXT_ENTITIES and c.decode("ascii", "replace") in _TEXT_CODES:
            values.append(val)
    declared = "utf-8" if _dxf_utf8_version(ver) else codec(cp) or "cp1252"
    enc, _s = detect(values, prefer=[declared])
    enc = enc or declared
    return [(v, _unescape(_decode(v, enc) or v.decode(declared, "replace"))) for v in values], cp, enc


def fix_dxf_encoding(path_in, path_out, target="auto"):
    """Rewrite a DXF so every program reads its texts correctly.

    The text values are decoded the way they really are - QGIS, for example, writes UTF-8 MTEXT while the
    header says ANSI_949 (issue #67573) - and written back consistently:
      target 'auto'      the declared code page when it holds every text, else the first ANSI code page that
                         does (Arabic text in an ANSI_1252 drawing → ANSI_1256), else \\U+XXXX escapes,
      target 'utf8'      UTF-8 for R2007+ files; older files as 'auto',
      target 'ansi_949'  (or any ANSI_xxx / encoding name) that code page; characters it lacks become \\U+XXXX
                         (AutoCAD shows them; GDAL/QGIS keep them as escapes).
    Returns a summary dict."""
    raw = open(path_in, "rb").read()
    ver, cp = _dxf_header(raw)
    texts, _cp, real = dxf_texts(path_in)
    decoded = [t for _r, t in texts]

    def holds(enc):
        try:
            for t in decoded:
                t.encode(enc)
            return True
        except UnicodeEncodeError:
            return False

    def best_ansi():
        first = codec(cp) or ("cp1252" if real == "utf-8" else real) or "cp1252"
        for enc in [first] + [c for c in DXF_CODEPAGES if c not in ("utf-8", first)]:
            if holds(enc):
                return enc
        return first

    if target in ("auto", None, "") or (str(target).lower() in ("utf8", "utf-8") and not _dxf_utf8_version(ver)):
        out_enc = best_ansi()
    elif str(target).lower() in ("utf8", "utf-8"):
        out_enc = "utf-8" if _dxf_utf8_version(ver) else (codec(cp) or real or "cp1252")
    else:
        out_enc = codec(target) or "cp1252"
    utf8_file = _dxf_utf8_version(ver)
    if utf8_file:
        out_enc = "utf-8"

    def encode(text):
        if out_enc == "utf-8":
            return text.encode("utf-8")
        out = []
        for ch in text:
            try:
                out.append(ch.encode(out_enc))
            except UnicodeEncodeError:
                out.append(f"\\U+{ord(ch):04X}".encode("ascii"))
        return b"".join(out)

    lines = raw.split(b"\n")
    crlf = lines and lines[0].endswith(b"\r")
    lines = [ln[:-1] if ln.endswith(b"\r") else ln for ln in lines]
    entity, want, changed = None, None, 0
    for i in range(0, len(lines) - 1, 2):
        c = lines[i].strip()
        v = lines[i + 1]
        if c == b"0":
            entity = v.strip().decode("ascii", "replace")
        elif c == b"9":
            want = v.strip()
        elif want == b"$DWGCODEPAGE" and c == b"3" and not utf8_file:
            lines[i + 1] = DXF_CODEPAGES.get(out_enc, "ANSI_1252").encode("ascii")
            want = None
        elif entity in _TEXT_ENTITIES and c.decode("ascii", "replace") in _TEXT_CODES:
            dec = _decode(v, real)
            if dec is None:
                continue
            new = encode(dec)
            if new != v:
                lines[i + 1] = new
                changed += 1
    sep = b"\r\n" if crlf else b"\n"
    with open(path_out, "wb") as f:
        f.write(sep.join(lines))
    return {"declared": cp, "real": real, "written": "UTF8" if out_enc == "utf-8" else DXF_CODEPAGES.get(out_enc),
            "changed": changed, "version": ver}


# ---- Persian / Kurdish letters ---------------------------------------------------------------------------------
_PERSIAN = str.maketrans({"ي": "ی", "ى": "ی", "ك": "ک"})


def persian_letters(text):
    """Arabic ي/ى/ك written as the Persian/Kurdish ی/ک: mixed data otherwise breaks searches and joins."""
    return text.translate(_PERSIAN) if text else text
