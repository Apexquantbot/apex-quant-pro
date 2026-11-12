#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Apex Quant Pro - motor de análisis (gratis, datos de Yahoo Finance).

Calcula para cada acción de tickers.txt:
  - Valor razonable (media de varios modelos: DCF, EPV, Graham, múltiplos, dividendos)
  - Etiqueta (Ganga, Infravalorado, Razonable, Sobrevalorado, Caro) y riesgo
  - Salud de la empresa (5 categorías de 1 a 5, comparada con su sector)
  - ProTips (frases automáticas positivas y negativas)
  - Indicadores técnicos
  - Consenso de analistas y próximos resultados
  - Carteras automáticas que se renuevan cada mes
  - Notificaciones al móvil (Web Push) según los avisos elegidos en la app (opcional)

Genera: datos.json, series.json y carteras.json
"""
import json
import math
import os
import statistics
import time
import datetime as dt

import numpy as np
import pandas as pd
import requests
import yfinance as yf

RF = 0.0425      # tipo sin riesgo (bono EE. UU. 10 años aprox.)
ERP = 0.055      # prima de riesgo del mercado
TAX = 0.21       # impuesto de sociedades
G_TERM = 0.025   # crecimiento a perpetuidad
KD = 0.055       # coste de la deuda antes de impuestos

BASE = os.path.dirname(os.path.abspath(__file__))
F_DATOS = os.path.join(BASE, "datos.json")
F_SERIES = os.path.join(BASE, "series.json")
F_CART = os.path.join(BASE, "carteras.json")
F_TICK = os.path.join(BASE, "tickers.txt")
F_AVISOS = os.path.join(BASE, "avisos.json")
F_NOTICIAS = os.path.join(BASE, "noticias.json")
D_LOGOS = os.path.join(BASE, "logos")

INDICES = [("^GSPC", "S&P 500"), ("^IXIC", "Nasdaq"), ("^DJI", "Dow Jones"), ("^IBEX", "IBEX 35")]

SECTORES_ES = {
    "Technology": "Tecnología", "Communication Services": "Comunicación",
    "Consumer Cyclical": "Consumo cíclico", "Consumer Defensive": "Consumo defensivo",
    "Financial Services": "Financiero", "Healthcare": "Salud", "Industrials": "Industria",
    "Energy": "Energía", "Utilities": "Eléctricas", "Real Estate": "Inmobiliario",
    "Basic Materials": "Materiales",
}


# ----------------------------------------------------------------- utilidades
def num(x):
    try:
        if x is None:
            return None
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return None
        return v
    except Exception:
        return None


def rd(x, n=4):
    v = num(x)
    return None if v is None else round(v, n)


def intentar(f, defecto=None, n=3):
    for i in range(n):
        try:
            r = f()
            if r is not None:
                return r
        except Exception as e:  # noqa
            print("   aviso:", type(e).__name__, str(e)[:120])
        time.sleep(1.5 * (i + 1))
    return defecto


def ordenar(df):
    """Columnas de más reciente a más antigua."""
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()
    try:
        return df.reindex(sorted(df.columns, reverse=True), axis=1)
    except Exception:
        return df


def fila(df, *nombres):
    if df is None or df.empty:
        return []
    for n in nombres:
        if n in df.index:
            s = df.loc[n]
            if isinstance(s, pd.DataFrame):
                s = s.iloc[0]
            return [num(v) for v in s.tolist()]
    return []


def el(lst, i):
    return lst[i] if lst is not None and len(lst) > i else None


def cagr(vals, años=3):
    vals = [v for v in vals if v is not None]
    n = min(años, len(vals) - 1)
    if n < 1:
        return None
    nuevo, viejo = vals[0], vals[n]
    if nuevo is None or viejo is None or nuevo <= 0 or viejo <= 0:
        return None
    return (nuevo / viejo) ** (1 / n) - 1


def acotar(v, lo, hi):
    return max(lo, min(hi, v))


def mediana(vals):
    vals = [v for v in vals if v is not None]
    return statistics.median(vals) if vals else None


def div(a, b):
    a, b = num(a), num(b)
    if a is None or b is None or b == 0:
        return None
    return a / b


# ------------------------------------------------------------------ descarga
NOTICIAS_RAW = {}


def descargar(sym):
    t = yf.Ticker(sym)
    info = intentar(lambda: t.info, {}) or {}
    hist = intentar(lambda: t.history(period="2y", interval="1d", auto_adjust=True), pd.DataFrame())
    inc = ordenar(intentar(lambda: t.income_stmt, pd.DataFrame()))
    cf = ordenar(intentar(lambda: t.cashflow, pd.DataFrame()))
    bs = ordenar(intentar(lambda: t.balance_sheet, pd.DataFrame()))
    rec = intentar(lambda: t.recommendations, pd.DataFrame(), n=2)
    cal = intentar(lambda: t.calendar, {}, n=2)
    divs = intentar(lambda: t.dividends, pd.Series(dtype=float), n=2)
    try:
        NOTICIAS_RAW[sym] = t.news or []
    except Exception:
        NOTICIAS_RAW[sym] = []
    return info, hist, inc, cf, bs, rec, cal, divs


# ------------------------------------------------------------- tipo de cambio
_FX = {}


def tipo_cambio(de, a):
    """Cuántas unidades de «a» vale 1 unidad de «de» (p. ej. TWD → USD)."""
    clave = de + a
    if clave not in _FX:
        h = intentar(lambda: yf.Ticker(f"{de}{a}=X").history(period="5d"), pd.DataFrame(), n=2)
        v = None
        if isinstance(h, pd.DataFrame) and len(h):
            v = num(h["Close"].dropna().iloc[-1])
        _FX[clave] = v
        print(f"   tipo de cambio {de}/{a}: {v}")
    return _FX[clave]


# ---------------------------------------------------------------- noticias
def _fecha_iso(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return dt.datetime.fromtimestamp(v, dt.timezone.utc).isoformat(timespec="minutes")
    try:
        return pd.Timestamp(v).tz_convert("UTC").isoformat(timespec="minutes") if pd.Timestamp(v).tzinfo else pd.Timestamp(v).tz_localize("UTC").isoformat(timespec="minutes")
    except Exception:
        return None


def leer_noticias(sym, raw):
    """Convierte las noticias de Yahoo Finance (formato antiguo o nuevo) a un formato sencillo."""
    out = []
    for it in raw or []:
        if not isinstance(it, dict):
            continue
        c = it.get("content") if isinstance(it.get("content"), dict) else it
        tipo = c.get("contentType") or c.get("type") or "STORY"
        if str(tipo).upper() not in ("STORY", "ARTICLE", "VIDEO"):
            continue
        titulo = (c.get("title") or "").strip()
        url = ((c.get("canonicalUrl") or {}).get("url") or (c.get("clickThroughUrl") or {}).get("url")
               or c.get("link") or "")
        if not titulo or not url:
            continue
        fecha = _fecha_iso(c.get("pubDate") or c.get("displayTime") or c.get("providerPublishTime"))
        fuente = (c.get("provider") or {}).get("displayName") if isinstance(c.get("provider"), dict) else c.get("publisher")
        img = None
        th = c.get("thumbnail") or {}
        res = th.get("resolutions") or []
        if res:
            res = sorted([r for r in res if r.get("url")], key=lambda r: abs((r.get("width") or 0) - 240))
            img = res[0]["url"] if res else None
        if not img and th.get("originalUrl"):
            img = th["originalUrl"]
        resumen = (c.get("summary") or c.get("description") or "").strip()
        if len(resumen) > 280:
            resumen = resumen[:277].rsplit(" ", 1)[0] + "…"
        rel = [sym] + [x for x in (c.get("relatedTickers") or []) if isinstance(x, str)]
        out.append({"t": titulo, "r": resumen, "f": fecha, "fuente": fuente or "", "url": url, "img": img, "s": rel})
    return out[:8]


# ------------------------------------------------- traducción (Gemini, opcional)
def gemini_modelos(key):
    try:
        r = requests.get(f"https://generativelanguage.googleapis.com/v1beta/models?pageSize=200&key={key}", timeout=20)
        mods = []
        for m in r.json().get("models", []):
            n = m.get("name", "").replace("models/", "")
            if "generateContent" in (m.get("supportedGenerationMethods") or []) and n.startswith("gemini-") and "flash" in n \
                    and not any(x in n for x in ("image", "tts", "audio", "live", "embedding", "vision", "thinking")):
                mods.append(n)

        def punt(n):
            import re
            v = re.search(r"gemini-(\d+(?:\.\d+)?)", n)
            p = float(v.group(1)) * 100 if v else 0
            return p - (5 if "lite" in n else 0) - (20 if ("preview" in n or "exp" in n) else 0)
        mods.sort(key=punt, reverse=True)
        return mods[:4] + ["gemini-flash-latest", "gemini-flash-lite-latest"]
    except Exception as e:
        print("Gemini: no se han podido listar los modelos:", e)
        return ["gemini-flash-latest", "gemini-flash-lite-latest"]


def gemini_json(key, modelos, prompt):
    cuerpo = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
              "generationConfig": {"temperature": 0.2, "maxOutputTokens": 8192, "responseMimeType": "application/json"}}
    for m in modelos:
        try:
            r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent?key={key}",
                              json=cuerpo, timeout=90)
            if r.status_code != 200:
                continue
            partes = r.json().get("candidates", [{}])[0].get("content", {}).get("parts", [])
            txt = "".join(p.get("text", "") for p in partes).strip().strip("`")
            if txt.startswith("json"):
                txt = txt[4:]
            return json.loads(txt)
        except Exception:
            continue
    return None


def traducir_noticias(noticias, anteriores):
    """Pone en español los titulares y resúmenes. Reutiliza las traducciones ya hechas."""
    hechas = {n["url"]: n for n in anteriores if n.get("te")}
    for n in noticias:
        v = hechas.get(n["url"])
        if v:
            n["te"], n["re"] = v.get("te"), v.get("re", "")
    key = os.environ.get("GEMINI_KEY", "").strip()
    pend = [n for n in noticias if not n.get("te")][:240]
    if not key or not pend:
        if not key:
            print("Traducción: no hay secreto GEMINI_KEY, las noticias quedan en inglés")
        return
    modelos = gemini_modelos(key)
    ok = 0
    for i in range(0, len(pend), 12):
        lote = pend[i:i + 12]
        entrada = [{"t": n["t"], "r": n.get("r", "")} for n in lote]
        res = gemini_json(key, modelos,
                          "Traduce al español de España estos titulares y resúmenes de noticias financieras. "
                          "Mantén nombres de empresas, cifras y símbolos bursátiles. Devuelve SOLO un array JSON con el mismo "
                          "número de elementos y en el mismo orden, cada uno con la forma {\"t\": \"titular\", \"r\": \"resumen\"}.\n\n"
                          + json.dumps(entrada, ensure_ascii=False))
        if isinstance(res, list) and len(res) == len(lote):
            for n, x in zip(lote, res):
                if isinstance(x, dict) and x.get("t"):
                    n["te"], n["re"] = str(x["t"]), str(x.get("r") or "")
                    ok += 1
        time.sleep(1)
    print(f"Traducción: {ok} noticias nuevas traducidas")


# ------------------------------------------------------------------ técnico
def tecnico(closes):
    c = closes.dropna()
    res = {}
    if len(c) < 30:
        return res
    precio = float(c.iloc[-1])

    def sma(n):
        return float(c.rolling(n).mean().iloc[-1]) if len(c) >= n else None

    delta = c.diff()
    gan = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    per = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rsi = 100 - 100 / (1 + gan / per.replace(0, np.nan))
    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    señal = macd.ewm(span=9, adjust=False).mean()
    m20 = c.rolling(20).mean()
    sd20 = c.rolling(20).std()

    def ret(n):
        return float(c.iloc[-1] / c.iloc[-1 - n] - 1) if len(c) > n else None

    ult = c.tail(252)
    res = {
        "sma20": sma(20), "sma50": sma(50), "sma200": sma(200),
        "rsi": num(rsi.iloc[-1]),
        "macd": num(macd.iloc[-1]), "macd_senal": num(señal.iloc[-1]),
        "boll_sup": num(m20.iloc[-1] + 2 * sd20.iloc[-1]), "boll_inf": num(m20.iloc[-1] - 2 * sd20.iloc[-1]),
        "max52": float(ult.max()), "min52": float(ult.min()),
        "soporte": float(c.tail(60).min()), "resistencia": float(c.tail(60).max()),
        "r1m": ret(21), "r3m": ret(63), "r6m": ret(126), "r12m": ret(252),
        "vol": num(c.pct_change().tail(252).std() * math.sqrt(252)),
    }
    año = c.index[-1].year
    ytd = c[c.index.year < año]
    res["ytd"] = float(precio / ytd.iloc[-1] - 1) if len(ytd) else None

    # Resumen de señales
    p = 0
    total = 0
    for cond in [
        res["sma50"] and precio > res["sma50"],
        res["sma200"] and precio > res["sma200"],
        res["sma50"] and res["sma200"] and res["sma50"] > res["sma200"],
        res["macd"] is not None and res["macd_senal"] is not None and res["macd"] > res["macd_senal"],
        res["sma20"] and precio > res["sma20"],
    ]:
        total += 1
        p += 1 if cond else -1
    if res["rsi"] is not None:
        total += 1
        if res["rsi"] < 30:
            p += 1
        elif res["rsi"] > 70:
            p -= 1
        elif res["rsi"] >= 50:
            p += 0.5
        else:
            p -= 0.5
    sc = p / total if total else 0
    res["senal_valor"] = round(sc, 2)
    res["senal"] = ("Compra fuerte" if sc >= 0.6 else "Compra" if sc >= 0.2 else
                    "Neutral" if sc > -0.2 else "Venta" if sc > -0.6 else "Venta fuerte")
    return res


# ------------------------------------------------------------ dividendos
def rachas_dividendo(divs):
    try:
        if divs is None or len(divs) == 0:
            return 0, 0
        por_año = divs.groupby(divs.index.year).sum()
        año_actual = dt.date.today().year
        años = [a for a in sorted(por_año.index) if a < año_actual]
        if not años or años[-1] < año_actual - 1:
            return 0, 0
        pagos = 0
        for a in reversed(años):
            if por_año.get(a, 0) > 0 and (pagos == 0 or a == años[-1] - pagos):
                pagos += 1
            else:
                break
        subidas = 0
        for i in range(len(años) - 1, 0, -1):
            a, b = años[i], años[i - 1]
            if a - b == 1 and por_año[a] > por_año[b] * 1.001:
                subidas += 1
            else:
                break
        return pagos, subidas
    except Exception:
        return 0, 0


# --------------------------------------------------------------- piotroski
def piotroski(inc, cf, bs):
    ni = fila(inc, "Net Income", "Net Income Common Stockholders")
    ta = fila(bs, "Total Assets")
    ocf = fila(cf, "Operating Cash Flow")
    ltd = fila(bs, "Long Term Debt")
    ca = fila(bs, "Current Assets")
    cl = fila(bs, "Current Liabilities")
    sh = fila(bs, "Ordinary Shares Number", "Share Issued")
    gp = fila(inc, "Gross Profit")
    rev = fila(inc, "Total Revenue")
    pruebas = []

    def ok(f):
        try:
            r = f()
            pruebas.append(bool(r) if r is not None else None)
        except Exception:
            pruebas.append(None)

    ok(lambda: el(ni, 0) > 0 if el(ni, 0) is not None else None)
    ok(lambda: el(ni, 0) / el(ta, 0) > 0 if el(ni, 0) is not None and el(ta, 0) else None)
    ok(lambda: el(ocf, 0) > 0 if el(ocf, 0) is not None else None)
    ok(lambda: el(ocf, 0) > el(ni, 0) if el(ocf, 0) is not None and el(ni, 0) is not None else None)
    ok(lambda: (el(ltd, 0) / el(ta, 0)) <= (el(ltd, 1) / el(ta, 1))
       if None not in (el(ltd, 0), el(ta, 0), el(ltd, 1), el(ta, 1)) else None)
    ok(lambda: (el(ca, 0) / el(cl, 0)) > (el(ca, 1) / el(cl, 1))
       if None not in (el(ca, 0), el(cl, 0), el(ca, 1), el(cl, 1)) and el(cl, 0) and el(cl, 1) else None)
    ok(lambda: el(sh, 0) <= el(sh, 1) * 1.005 if el(sh, 0) and el(sh, 1) else None)
    ok(lambda: (el(gp, 0) / el(rev, 0)) > (el(gp, 1) / el(rev, 1))
       if None not in (el(gp, 0), el(rev, 0), el(gp, 1), el(rev, 1)) and el(rev, 0) and el(rev, 1) else None)
    ok(lambda: (el(rev, 0) / el(ta, 0)) > (el(rev, 1) / el(ta, 1))
       if None not in (el(rev, 0), el(ta, 0), el(rev, 1), el(ta, 1)) and el(ta, 0) and el(ta, 1) else None)
    validas = [p for p in pruebas if p is not None]
    if len(validas) < 5:
        return None, None
    return sum(validas), len(validas)


# ----------------------------------------------------------- datos en bruto
def preparar(sym, info, hist, inc, cf, bs, rec, cal, divs):
    closes = hist["Close"].dropna() if isinstance(hist, pd.DataFrame) and "Close" in hist else pd.Series(dtype=float)
    precio = num(info.get("currentPrice")) or num(info.get("regularMarketPrice"))
    if precio is None and len(closes):
        precio = float(closes.iloc[-1])
    if precio is None:
        return None, None
    prev = float(closes.iloc[-2]) if len(closes) >= 2 else None
    ultimo = float(closes.iloc[-1]) if len(closes) else precio
    cambio = div(ultimo, prev)
    cambio = cambio - 1 if cambio is not None else num(info.get("regularMarketChangePercent"))

    mcap = num(info.get("marketCap"))
    if mcap:
        # Acciones "equivalentes" al precio que cotiza (sirve también para ADR y acciones clase A/B)
        acciones = mcap / precio
    else:
        acciones = num(info.get("sharesOutstanding")) or num(info.get("impliedSharesOutstanding"))
        mcap = acciones * precio if acciones else None

    # Empresas que presentan cuentas en otra moneda (p. ej. TSMC en dólares taiwaneses)
    moneda = info.get("currency") or "USD"
    moneda_fin = info.get("financialCurrency") or moneda
    fx = 1.0 if moneda_fin == moneda else tipo_cambio(moneda_fin, moneda)

    def conv(v):
        v = num(v)
        if v is None:
            return None
        return v * fx if fx else None

    rev_h = fila(inc, "Total Revenue")
    eps_h = fila(inc, "Diluted EPS", "Basic EPS")
    ebit_h = fila(inc, "EBIT", "Operating Income")
    fcf_h = fila(cf, "Free Cash Flow")
    ni_h = fila(inc, "Net Income", "Net Income Common Stockholders")
    sh_h = fila(bs, "Ordinary Shares Number", "Share Issued")

    revenue = conv(num(info.get("totalRevenue")) or el(rev_h, 0))
    ebitda = conv(info.get("ebitda"))
    fcf = num(info.get("freeCashflow"))
    if fcf is None:
        fcf = el(fcf_h, 0)
    fcf = conv(fcf)
    ocf = conv(num(info.get("operatingCashflow")) or el(fila(cf, "Operating Cash Flow"), 0))
    cash = conv(info.get("totalCash")) or 0.0
    deuda = conv(info.get("totalDebt")) or 0.0
    ni = conv(num(info.get("netIncomeToCommon")) or el(ni_h, 0))
    ebit_h = [conv(v) for v in ebit_h]
    fcf_h = [conv(v) for v in fcf_h]
    eps = num(info.get("trailingEps"))
    eps_f = num(info.get("forwardEps"))
    bvps = num(info.get("bookValue"))
    if moneda_fin != moneda:
        # Beneficio y valor contable por acción recalculados en la moneda de cotización
        t_eps, f_eps = eps, eps_f
        eps = (ni / acciones) if ni is not None and acciones else None
        eps_f = eps * (f_eps / t_eps) if eps and t_eps and f_eps and t_eps > 0 and f_eps > 0 else None
        patrimonio = conv(el(fila(bs, "Stockholders Equity", "Common Stock Equity"), 0))
        bvps = (patrimonio / acciones) if patrimonio and acciones else None
    div_ps = num(info.get("dividendRate")) or num(info.get("trailingAnnualDividendRate")) or 0.0
    payout = num(info.get("payoutRatio"))
    beta = num(info.get("beta"))

    t = tecnico(closes) if len(closes) else {}
    pagos, subidas = rachas_dividendo(divs)
    pf, pf_max = piotroski(inc, cf, bs)

    # Analistas
    an = {"compra": None, "mantener": None, "venta": None}
    try:
        if isinstance(rec, pd.DataFrame) and not rec.empty:
            r0 = rec[rec["period"] == "0m"].iloc[0] if "period" in rec else rec.iloc[0]
            an = {
                "compra": int(r0.get("strongBuy", 0) + r0.get("buy", 0)),
                "mantener": int(r0.get("hold", 0)),
                "venta": int(r0.get("sell", 0) + r0.get("strongSell", 0)),
            }
    except Exception:
        pass
    claves = {"strong_buy": "Compra fuerte", "buy": "Compra", "hold": "Mantener",
              "underperform": "Venta", "sell": "Venta fuerte"}
    an.update({
        "consenso": claves.get(str(info.get("recommendationKey", "")).lower()),
        "objetivo": num(info.get("targetMeanPrice")),
        "obj_alto": num(info.get("targetHighPrice")),
        "obj_bajo": num(info.get("targetLowPrice")),
        "num": num(info.get("numberOfAnalystOpinions")),
    })

    # Próximos resultados
    res = {}
    try:
        if isinstance(cal, dict):
            fe = cal.get("Earnings Date")
            if isinstance(fe, (list, tuple)) and fe:
                fe = fe[0]
            if fe is not None:
                res["fecha"] = str(fe)[:10]
            res["bpa_est"] = num(cal.get("Earnings Average"))
            res["ingresos_est"] = num(cal.get("Revenue Average"))
    except Exception:
        pass

    ev = (mcap or 0) + deuda - cash if mcap else None
    m = {
        # rentabilidad
        "margen_bruto": num(info.get("grossMargins")),
        "margen_operativo": num(info.get("operatingMargins")),
        "margen_neto": num(info.get("profitMargins")),
        "roe": num(info.get("returnOnEquity")),
        "roa": num(info.get("returnOnAssets")),
        # crecimiento
        "crec_ventas": num(info.get("revenueGrowth")),
        "crec_beneficio": num(info.get("earningsGrowth")),
        "cagr_ventas_3a": cagr(rev_h),
        "cagr_bpa_3a": cagr(eps_h),
        "crec_bpa_previsto": (eps_f / eps - 1) if eps and eps > 0 and eps_f else None,
        # flujo de caja
        "margen_fcf": div(fcf, revenue),
        "rent_fcf": div(fcf, mcap),
        "ocf_beneficio": div(ocf, ni) if ni and ni > 0 else None,
        "deuda_ebitda": div(deuda - cash, ebitda) if ebitda and ebitda > 0 else None,
        "liquidez": num(info.get("currentRatio")),
        # impulso
        "r1m": t.get("r1m"), "r3m": t.get("r3m"), "r6m": t.get("r6m"), "r12m": t.get("r12m"),
        "dist_sma200": (precio / t["sma200"] - 1) if t.get("sma200") else None,
        # valor relativo
        "per": (precio / eps) if eps and eps > 0 else None,
        "per_previsto": (precio / eps_f) if eps_f and eps_f > 0 else None,
        "ev_ebitda": div(ev, ebitda) if ebitda and ebitda > 0 and ev and ev > 0 else None,
        "pvc": (precio / bvps) if bvps and bvps > 0 else None,
        "pv": div(mcap, revenue) if revenue and revenue > 0 else None,
        "rent_div": (div_ps / precio) if div_ps else 0.0,
    }
    for k in ("roe", "crec_beneficio", "ocf_beneficio"):
        if m[k] is not None:
            m[k] = acotar(m[k], -3, 3)

    a = {
        "s": sym,
        "n": info.get("longName") or info.get("shortName") or sym,
        "sector_en": info.get("sector") or "Otros",
        "industria": info.get("industry") or "",
        "pais": info.get("country") or "",
        "web": info.get("website") or "",
        "empresa": {
            "desc": info.get("longBusinessSummary") or "",
            "empleados": num(info.get("fullTimeEmployees")),
            "ciudad": ", ".join([x for x in [info.get("city"), info.get("state")] if x]),
            "web": info.get("website") or "",
            "bolsa": info.get("fullExchangeName") or info.get("exchange") or "",
            "desde": (dt.datetime.fromtimestamp(info["firstTradeDateEpochUtc"], dt.timezone.utc).year
                      if num(info.get("firstTradeDateEpochUtc")) else None),
            "moneda_cuentas": moneda_fin,
            "directivos": [{"n": o.get("name"), "c": o.get("title")} for o in (info.get("companyOfficers") or [])[:4] if o.get("name")],
        },
        "moneda": info.get("currency") or "USD",
        "precio": precio, "cambio": cambio, "mcap": mcap,
        "beta": beta, "eps": eps, "eps_f": eps_f, "bvps": bvps,
        "div": div_ps, "payout": payout, "acciones_n": acciones,
        "revenue": revenue, "ebitda": ebitda, "fcf": fcf, "ocf": ocf, "ni": ni,
        "cash": cash, "deuda": deuda,
        "ebit_h": [v for v in ebit_h[:4] if v is not None],
        "fcf_h": [v for v in fcf_h[:3] if v is not None],
        "rev_h": rev_h[:4], "sh_h": sh_h[:2],
        "tec": t, "m": m, "an": an, "res": res,
        "div_anos": pagos, "div_subidas": subidas,
        "piotroski": pf, "piotroski_max": pf_max,
    }
    a["sector"] = SECTORES_ES.get(a["sector_en"], a["sector_en"])

    serie = None
    if len(closes):
        c = closes.tail(520)
        fechas = [d.date() for d in c.index]
        serie = {
            "f0": str(fechas[0]),
            "dd": [0] + [(fechas[i] - fechas[i - 1]).days for i in range(1, len(fechas))],
            "c": [round(float(v), 4 if v < 10 else 2) for v in c.tolist()],
        }
    return a, serie


# ------------------------------------------------------------- valoración
def wacc(a):
    beta = acotar(a["beta"] if a["beta"] is not None else 1.0, 0.6, 2.0)
    ke = acotar(RF + beta * ERP, 0.07, 0.13)
    e = a["mcap"] or 0
    d = a["deuda"] or 0
    if e + d <= 0:
        return ke, ke
    w = (e * ke + d * KD * (1 - TAX)) / (e + d)
    return acotar(w, 0.065, 0.12), ke


def crecimiento(a):
    m = a["m"]
    cand = [m.get("crec_bpa_previsto"), m.get("crec_ventas"), m.get("cagr_ventas_3a")]
    cand = [c for c in cand if c is not None]
    g = statistics.median(cand) if cand else 0.04
    return acotar(g, -0.05, 0.30)


def dcf(base, g0, w, años, acciones, cash, deuda, salida=None, ebitda=None):
    if not base or base <= 0 or not acciones:
        return None
    vp = 0.0
    flujo = base
    eb = ebitda
    for i in range(1, años + 1):
        g = g0 + (G_TERM - g0) * (i - 1) / max(1, años - 1)
        flujo *= 1 + g
        vp += flujo / (1 + w) ** i
        if eb:
            eb *= 1 + g
    if salida and eb:
        tv = eb * salida
    else:
        tv = flujo * (1 + G_TERM) / (w - G_TERM)
    vp += tv / (1 + w) ** años
    return (vp + cash - deuda) / acciones


def valorar(a, sec):
    precio = a["precio"]
    w, ke = wacc(a)
    g0 = crecimiento(a)
    acc = a["acciones_n"]
    cash, deuda = a["cash"] or 0, a["deuda"] or 0
    financiero = a["sector_en"] == "Financial Services"
    inmob = a["sector_en"] == "Real Estate"
    mod = []

    def add(clave, nombre, tipo, v, nota=""):
        mod.append({"id": clave, "n": nombre, "tipo": tipo, "v": num(v), "nota": nota})

    fcf_vals = [v for v in a["fcf_h"] if v is not None]
    fcf_base = statistics.mean(fcf_vals[:2]) if len(fcf_vals) >= 2 else a["fcf"]
    if not financiero:
        add("dcf5", "DCF flujo de caja 5 años", "DCF", dcf(fcf_base, g0, w, 5, acc, cash, deuda))
        add("dcf10", "DCF flujo de caja 10 años", "DCF", dcf(fcf_base, g0, w, 10, acc, cash, deuda))
        mult = sec.get("ev_ebitda")
        if mult and a["ebitda"] and a["ebitda"] > 0:
            add("dcf_salida", "DCF con múltiplo de salida", "DCF",
                dcf(fcf_base, g0, w, 5, acc, cash, deuda, salida=min(mult, 20), ebitda=a["ebitda"]))
        ebits = [e for e in a["ebit_h"] if e is not None]
        if ebits and statistics.mean(ebits) > 0 and acc:
            epv = statistics.mean(ebits) * (1 - TAX) / w
            add("epv", "Earnings Power Value", "DCF", (epv + cash - deuda) / acc)
    if not inmob and a["eps"] and a["eps"] > 0 and a["bvps"] and a["bvps"] > 0:
        add("graham", "Número de Graham", "Múltiplos", math.sqrt(22.5 * a["eps"] * a["bvps"]))
    eps_base = a["eps_f"] if a["eps_f"] and a["eps_f"] > 0 else a["eps"]
    if sec.get("per") and eps_base and eps_base > 0:
        add("per", "PER del sector", "Múltiplos", eps_base * sec["per"])
    if not financiero and sec.get("ev_ebitda") and a["ebitda"] and a["ebitda"] > 0 and acc:
        add("ev_ebitda", "EV/EBITDA del sector", "Múltiplos", (a["ebitda"] * sec["ev_ebitda"] + cash - deuda) / acc)
    if sec.get("pvc") and a["bvps"] and a["bvps"] > 0 and (financiero or inmob or a["sector_en"] == "Utilities"):
        add("pvc", "Precio/valor contable del sector", "Múltiplos", a["bvps"] * sec["pvc"])
    if not financiero and sec.get("pv") and a["revenue"] and acc:
        add("pv", "Precio/ventas del sector", "Múltiplos", a["revenue"] / acc * sec["pv"])
    if a["div"] and a["div"] > 0 and (a["payout"] is None or a["payout"] < 1.2):
        g = acotar(g0, 0, 0.05)
        if ke - g > 0.015:
            add("ddm", "Descuento de dividendos (Gordon)", "Dividendos", a["div"] * (1 + g) / (ke - g))

    validos = []
    for mo in mod:
        v = mo["v"]
        if v is None or v <= 0:
            mo["ok"] = False
            mo["nota"] = "Sin datos suficientes"
            mo["v"] = None
            continue
        x = v / precio - 1
        mo["v"] = round(v, 2)
        mo["pot"] = round(x, 4)
        if x > 1.0 or x < -0.6:
            mo["ok"] = False
            mo["nota"] = "Descartado: resultado extremo"
        else:
            mo["ok"] = True
            validos.append(v)
    mod = [mo for mo in mod if mo["v"] is not None]

    vr = statistics.mean(validos) if validos else None
    pot = vr / precio - 1 if vr else None
    if pot is None:
        etiqueta = "Sin datos"
    elif pot >= 0.30:
        etiqueta = "Ganga"
    elif pot >= 0.10:
        etiqueta = "Infravalorado"
    elif pot > -0.10:
        etiqueta = "Razonable"
    elif pot > -0.30:
        etiqueta = "Sobrevalorado"
    else:
        etiqueta = "Caro"

    pts = 0
    vol = a["tec"].get("vol")
    pts += 0 if vol is not None and vol < 0.25 else 1 if vol is not None and vol < 0.4 else 2 if vol is not None and vol < 0.6 else 3
    mn = a["m"].get("margen_neto")
    pts += 0 if mn is not None and mn > 0.10 else 1 if mn is not None and mn > 0 else 2
    pts += 0 if len(validos) >= 6 else 1 if len(validos) >= 4 else 2
    riesgo = "Bajo" if pts <= 1 else "Medio" if pts <= 3 else "Alto" if pts <= 5 else "Muy alto"

    return {
        "valor": rd(vr, 2), "pot": rd(pot), "etiqueta": etiqueta, "riesgo": riesgo,
        "modelos": mod, "supuestos": {"wacc": round(w, 4), "ke": round(ke, 4), "g": round(g0, 4)},
    }


# ------------------------------------------------------------------- salud
CATEGORIAS = {
    "rentabilidad": ("Rentabilidad", [("margen_bruto", 1), ("margen_operativo", 1), ("margen_neto", 1), ("roe", 1), ("roa", 1)]),
    "crecimiento": ("Crecimiento", [("crec_ventas", 1), ("crec_beneficio", 1), ("cagr_ventas_3a", 1), ("cagr_bpa_3a", 1), ("crec_bpa_previsto", 1)]),
    "flujo": ("Flujo de caja", [("margen_fcf", 1), ("rent_fcf", 1), ("ocf_beneficio", 1), ("deuda_ebitda", -1), ("liquidez", 1)]),
    "impulso": ("Impulso del precio", [("r1m", 1), ("r3m", 1), ("r6m", 1), ("r12m", 1), ("dist_sma200", 1)]),
    "valor": ("Valor relativo", [("per", -1), ("ev_ebitda", -1), ("pvc", -1), ("pv", -1), ("rent_fcf", 1), ("pot_vr", 1)]),
}


def etiqueta_salud(x):
    if x is None:
        return "Sin datos"
    return ("Excelente" if x >= 4.25 else "Genial" if x >= 3.5 else "Bien" if x >= 2.75 else
            "Justo" if x >= 2.0 else "Débil" if x >= 1.5 else "Pobre")


def percentil(v, valores):
    if v is None or not valores:
        return None
    menos = sum(1 for x in valores if x < v)
    igual = sum(1 for x in valores if x == v)
    return (menos + 0.5 * igual) / len(valores)


def salud(acciones):
    por_sector = {}
    for a in acciones:
        por_sector.setdefault(a["sector"], []).append(a)
    for a in acciones:
        grupo = por_sector[a["sector"]]
        cats = {}
        for clave, (nombre, metricas) in CATEGORIAS.items():
            ps = []
            for k, dirn in metricas:
                v = a["m"].get(k)
                if v is None:
                    continue
                pares = [b["m"].get(k) for b in grupo if b["m"].get(k) is not None]
                if len(pares) < 5:
                    pares = [b["m"].get(k) for b in acciones if b["m"].get(k) is not None]
                p = percentil(v, pares)
                if p is None:
                    continue
                ps.append(p if dirn > 0 else 1 - p)
            cats[clave] = round(1 + 4 * statistics.mean(ps), 2) if len(ps) >= 2 else None
        vals = [v for v in cats.values() if v is not None]
        total = round(statistics.mean(vals), 2) if len(vals) >= 3 else None
        a["salud"] = {"total": total, "etiqueta": etiqueta_salud(total),
                      "cat": {k: {"v": v, "e": etiqueta_salud(v)} for k, v in cats.items()}}


# ----------------------------------------------------------------- ProTips
def protips(a, sec):
    p, n = [], []
    m, t, an = a["m"], a["tec"], a["an"]
    pot = a["vr"]["pot"]
    if pot is not None and pot >= 0.20:
        p.append(f"Cotiza un {pot*100:.0f}% por debajo de su valor razonable")
    if pot is not None and pot <= -0.15:
        n.append(f"Cotiza un {-pot*100:.0f}% por encima de su valor razonable")
    if m["roe"] is not None and m["roe"] > 0.20:
        p.append(f"Alta rentabilidad sobre sus fondos propios (ROE {m['roe']*100:.0f}%)")
    if m["margen_bruto"] is not None and m["margen_bruto"] > 0.55:
        p.append(f"Márgenes brutos muy altos ({m['margen_bruto']*100:.0f}%)")
    if m["margen_neto"] is not None and m["margen_neto"] < 0:
        n.append("No ha sido rentable en los últimos 12 meses")
    if m["crec_ventas"] is not None and m["crec_ventas"] > 0.15:
        p.append(f"Las ventas crecen con fuerza (+{m['crec_ventas']*100:.0f}% interanual)")
    if m["crec_ventas"] is not None and m["crec_ventas"] < 0:
        n.append(f"Las ventas están cayendo ({m['crec_ventas']*100:.0f}% interanual)")
    if m["crec_bpa_previsto"] is not None and m["crec_bpa_previsto"] > 0.10:
        p.append("Los analistas esperan que el beneficio por acción siga creciendo")
    if m["crec_bpa_previsto"] is not None and m["crec_bpa_previsto"] < -0.05:
        n.append("Los analistas esperan que el beneficio por acción baje")
    if a["cash"] and a["deuda"] is not None and a["cash"] > a["deuda"]:
        p.append("Tiene más dinero en caja que deuda")
    if m["deuda_ebitda"] is not None and m["deuda_ebitda"] > 3:
        n.append(f"Deuda alta: {m['deuda_ebitda']:.1f} veces su EBITDA")
    if m["liquidez"] is not None and m["liquidez"] < 1:
        n.append("Sus deudas a corto plazo superan a sus activos a corto plazo")
    if a["div_anos"] >= 10:
        p.append(f"Ha pagado dividendo {a['div_anos']} años seguidos")
    if a["div_subidas"] >= 5:
        p.append(f"Ha subido el dividendo {a['div_subidas']} años seguidos")
    if a["payout"] is not None and a["payout"] > 0.9 and a["div"]:
        n.append("Reparte más del 90% del beneficio: el dividendo podría estar en riesgo")
    if m["rent_fcf"] is not None and m["rent_fcf"] > 0.05:
        p.append(f"Genera mucho flujo de caja libre ({m['rent_fcf']*100:.1f}% de su valor en bolsa)")
    if m["rent_fcf"] is not None and m["rent_fcf"] < 0:
        n.append("Quema caja: su flujo de caja libre es negativo")
    if m["per"] and sec.get("per"):
        if m["per"] < sec["per"] * 0.8:
            p.append(f"PER por debajo de su sector ({m['per']:.1f} frente a {sec['per']:.1f})")
        elif m["per"] > sec["per"] * 1.5:
            n.append(f"PER alto frente a su sector ({m['per']:.1f} frente a {sec['per']:.1f})")
    sh = a["sh_h"]
    if el(sh, 0) and el(sh, 1):
        if sh[0] < sh[1] * 0.98:
            p.append("Está recomprando acciones propias")
        elif sh[0] > sh[1] * 1.03:
            n.append("Ha emitido acciones nuevas y diluye a los accionistas")
    tot = (an.get("compra") or 0) + (an.get("mantener") or 0) + (an.get("venta") or 0)
    if tot >= 5 and (an.get("compra") or 0) / tot >= 0.7:
        p.append(f"El {an['compra']/tot*100:.0f}% de los analistas recomienda comprar")
    if tot >= 5 and (an.get("venta") or 0) / tot >= 0.3:
        n.append("Bastantes analistas recomiendan vender")
    if t.get("r12m") is not None and t["r12m"] > 0.30:
        p.append(f"Fuerte subida en el último año (+{t['r12m']*100:.0f}%)")
    if t.get("r12m") is not None and t["r12m"] < -0.25:
        n.append(f"Fuerte caída en el último año ({t['r12m']*100:.0f}%)")
    if t.get("rsi") is not None and t["rsi"] > 70:
        n.append(f"Sobrecompra técnica (RSI {t['rsi']:.0f}): podría corregir")
    if t.get("rsi") is not None and t["rsi"] < 30:
        p.append(f"Sobreventa técnica (RSI {t['rsi']:.0f}): posible rebote")
    if t.get("vol") is not None and t["vol"] > 0.5:
        n.append("Su precio es muy volátil")
    if t.get("min52") and a["precio"] <= t["min52"] * 1.05:
        n.append("Cotiza cerca de su mínimo de 52 semanas")
    if t.get("max52") and a["precio"] >= t["max52"] * 0.97:
        p.append("Cotiza cerca de su máximo de 52 semanas")
    if a["piotroski"] is not None:
        if a["piotroski"] >= 7:
            p.append(f"Fundamentales sólidos: Piotroski {a['piotroski']} de {a['piotroski_max']}")
        elif a["piotroski"] <= 3:
            n.append(f"Fundamentales débiles: Piotroski {a['piotroski']} de {a['piotroski_max']}")
    return {"pos": p, "neg": n}


# ---------------------------------------------------------------- carteras
def h(a, k):
    v = a["salud"]["cat"].get(k, {}).get("v")
    return v if v is not None else 0


ESTRATEGIAS = [
    {"id": "top", "n": "Lo mejor de Apex",
     "d": "Las 10 con mejor combinación de salud financiera y potencial frente a su valor razonable.",
     "f": lambda a: a["salud"]["total"] is not None and (a["vr"]["pot"] or -1) > 0,
     "p": lambda a: a["salud"]["total"] + 3 * min(a["vr"]["pot"], 0.8)},
    {"id": "gangas", "n": "Gigantes infravalorados",
     "d": "Grandes empresas sanas que cotizan claramente por debajo de su valor razonable.",
     "f": lambda a: (a["vr"]["pot"] or -1) >= 0.15 and (a["salud"]["total"] or 0) >= 2.75,
     "p": lambda a: a["vr"]["pot"]},
    {"id": "calidad", "n": "Calidad a buen precio",
     "d": "Empresas muy rentables y con buen flujo de caja que no cotizan caras.",
     "f": lambda a: (a["vr"]["pot"] or -1) > -0.05,
     "p": lambda a: 0.5 * h(a, "rentabilidad") + 0.3 * h(a, "flujo") + 0.2 * h(a, "valor")},
    {"id": "crecimiento", "n": "Crecimiento imparable",
     "d": "Las que más crecen en ventas y beneficios, con el precio acompañando.",
     "f": lambda a: (a["m"]["crec_ventas"] or 0) > 0.05,
     "p": lambda a: 0.6 * h(a, "crecimiento") + 0.4 * h(a, "impulso")},
    {"id": "dividendos", "n": "Dividendos sólidos",
     "d": "Buen dividendo, pago sostenible y empresa sana.",
     "f": lambda a: (a["m"]["rent_div"] or 0) >= 0.02 and (a["payout"] is None or a["payout"] < 0.8) and (a["salud"]["total"] or 0) >= 2.5,
     "p": lambda a: a["m"]["rent_div"] * 100 + a["salud"]["total"] + a["div_subidas"] * 0.1},
    {"id": "impulso", "n": "Impulso alcista",
     "d": "Las acciones con la tendencia más fuerte en los últimos meses.",
     "f": lambda a: (a["tec"].get("r6m") or -1) > 0,
     "p": lambda a: h(a, "impulso")},
]


def carteras(acciones, sp):
    hoy = dt.date.today()
    mes = hoy.strftime("%Y-%m")
    precios = {a["s"]: a["precio"] for a in acciones}
    try:
        with open(F_CART, encoding="utf-8") as f:
            prev = json.load(f)
    except Exception:
        prev = {"mes": None, "estrategias": {}}

    def rent(pos):
        rs = [precios[x["s"]] / x["p"] - 1 for x in pos if x["s"] in precios and x["p"]]
        return statistics.mean(rs) if rs else None

    nuevo = {"mes": mes, "estrategias": {}}
    salida = []
    for e in ESTRATEGIAS:
        ant = prev.get("estrategias", {}).get(e["id"])
        hist = ant.get("historial", []) if ant else []
        if ant and prev.get("mes") == mes and ant.get("posiciones"):
            est = ant
        else:
            if ant and ant.get("posiciones"):
                r = rent(ant["posiciones"])
                rsp = (sp / ant["sp_inicio"] - 1) if sp and ant.get("sp_inicio") else None
                hist.append({"mes": prev.get("mes"), "r": rd(r), "sp": rd(rsp)})
            cand = []
            for a in acciones:
                try:
                    if e["f"](a):
                        cand.append((e["p"](a), a))
                except Exception:
                    pass
            cand.sort(key=lambda x: x[0], reverse=True)
            est = {"inicio": str(hoy), "sp_inicio": sp,
                   "posiciones": [{"s": a["s"], "p": round(a["precio"], 4)} for _, a in cand[:10]],
                   "historial": hist[-24:]}
        nuevo["estrategias"][e["id"]] = est
        r_act = rent(est["posiciones"])
        sp_act = (sp / est["sp_inicio"] - 1) if sp and est.get("sp_inicio") else None
        acum = 1.0
        acum_sp = 1.0
        for x in est["historial"]:
            acum *= 1 + (x.get("r") or 0)
            acum_sp *= 1 + (x.get("sp") or 0)
        acum *= 1 + (r_act or 0)
        acum_sp *= 1 + (sp_act or 0)
        salida.append({
            "id": e["id"], "n": e["n"], "d": e["d"], "inicio": est["inicio"],
            "r": rd(r_act), "sp": rd(sp_act), "acum": rd(acum - 1), "acum_sp": rd(acum_sp - 1),
            "historial": est["historial"],
            "pos": [{"s": x["s"], "p": x["p"], "r": rd(precios[x["s"]] / x["p"] - 1) if x["s"] in precios and x["p"] else None}
                    for x in est["posiciones"]],
        })
    with open(F_CART, "w", encoding="utf-8") as f:
        json.dump(nuevo, f, ensure_ascii=False, indent=1)
    return salida, (prev.get("mes") != mes and prev.get("mes") is not None)


# ------------------------------------------------------------------- logos
def nombre_logo(sym):
    return sym.replace(".", "_").replace("^", "") + ".png"


def descargar_logos(acciones):
    """Descarga el logo de cada empresa una sola vez y lo guarda en la carpeta logos/."""
    from urllib.parse import urlparse
    os.makedirs(D_LOGOS, exist_ok=True)
    cab = {"User-Agent": "Mozilla/5.0 (ApexQuantPro)"}
    nuevos = 0
    for a in acciones:
        ruta = os.path.join(D_LOGOS, nombre_logo(a["s"]))
        if os.path.exists(ruta) and os.path.getsize(ruta) > 0:
            continue
        dominio = urlparse(a.get("web") or "").netloc.replace("www.", "")
        base = a["s"].split(".")[0]
        urls = [f"https://financialmodelingprep.com/image-stock/{a['s']}.png"]
        if base != a["s"]:
            urls.append(f"https://financialmodelingprep.com/image-stock/{base}.png")
        if dominio:
            urls.append(f"https://www.google.com/s2/favicons?domain={dominio}&sz=128")
        for u in urls:
            try:
                r = requests.get(u, headers=cab, timeout=15)
                tipo = r.headers.get("content-type", "")
                if r.status_code == 200 and tipo.startswith("image") and len(r.content) > 900:
                    with open(ruta, "wb") as f:
                        f.write(r.content)
                    nuevos += 1
                    break
            except Exception:
                pass
    print(f"Logos: {nuevos} nuevos")


# ------------------------------------------------------- notificaciones push
def leer_config_push():
    """Lee el código que la app genera en Ajustes > Notificaciones (secreto PUSH_CONFIG)."""
    raw = os.environ.get("PUSH_CONFIG", "").strip()
    if not raw:
        return None
    try:
        cfg = json.loads(raw)
    except Exception:
        print("AVISO: el secreto PUSH_CONFIG no es un código válido. Cópialo otra vez desde la app.")
        return None
    devs = cfg.get("dispositivos") or ([cfg["dispositivo"]] if cfg.get("dispositivo") else [])
    cfg["_devs"] = [d for d in devs if d.get("sub") and d.get("vapid_priv")]
    # Si la app guarda tus estrellas y avisos en avisos.json, mandan sobre los del código
    try:
        with open(F_AVISOS, encoding="utf-8") as f:
            av = json.load(f)
        if isinstance(av.get("favs"), list):
            cfg["favs"] = av["favs"]
        if isinstance(av.get("avisos"), dict):
            cfg["avisos"] = av["avisos"]
        print(f"Avisos leídos de avisos.json ({len(cfg.get('favs') or [])} acciones seguidas)")
    except FileNotFoundError:
        pass
    except Exception as e:
        print("AVISO: avisos.json no se ha podido leer:", e)
    return cfg if cfg["_devs"] else None


def enviar_push(cfg, avisos):
    if not cfg or not avisos:
        return
    try:
        import base64
        from cryptography.hazmat.primitives.asymmetric import ec
        from py_vapid import Vapid
        from pywebpush import webpush, WebPushException
    except ImportError as e:
        print("Faltan librerías de notificaciones:", e)
        return
    if len(avisos) > 6:
        resto = len(avisos) - 5
        avisos = avisos[:5] + [{"titulo": "Apex Quant Pro", "texto": f"Y {resto} avisos más. Ábrela para verlos en Seguimiento.",
                                 "url": "./?v=seguimiento", "tag": "resumen"}]
    # El remitente tiene que ser solo el dominio (https://apexquantbot.github.io), sin rutas
    import re
    m = re.match(r"https://[^/]+", cfg.get("web") or "")
    contacto = m.group(0) if m else "mailto:avisos@apexquantpro.app"
    for dev in cfg["_devs"]:
        try:
            d = dev["vapid_priv"]
            n = int.from_bytes(base64.urlsafe_b64decode(d + "=" * (-len(d) % 4)), "big")
            vapid = Vapid(private_key=ec.derive_private_key(n, ec.SECP256R1()))
        except Exception as e:
            print("Clave de notificaciones no válida:", e)
            continue
        for a in avisos:
            try:
                webpush(subscription_info=dev["sub"], data=json.dumps(a, ensure_ascii=False),
                        vapid_private_key=vapid, vapid_claims={"sub": contacto}, ttl=43200)
                print("Notificación enviada:", a["titulo"])
            except WebPushException as e:
                cod = getattr(getattr(e, "response", None), "status_code", None)
                if cod in (404, 410):
                    print("AVISO: el móvil ya no acepta notificaciones. Vuelve a activarlas en la app y copia el código nuevo.")
                    break
                print("Error al enviar notificación:", cod, str(e)[:200])
            except Exception as e:
                print("Error al enviar notificación:", str(e)[:200])


def calcular_avisos(cfg, out, anterior, carts, mes_nuevo):
    av = {"ganga": True, "fav_etq": True, "fav_tec": True, "fav_mov": True, "fav_res": True, "carteras": True}
    av.update(cfg.get("avisos") or {})
    favs = set(cfg.get("favs") or [])
    tarde = dt.datetime.now(dt.timezone.utc).hour >= 19      # ejecución de después del cierre
    mañana = str(dt.date.today() + dt.timedelta(days=1))
    lista = []
    for a in out:
        s, fav = a["s"], a["s"] in favs
        url, ant = f"./?s={s}", anterior.get(a["s"])
        if ant:
            e0, e1 = ant["vr"]["etiqueta"], a["vr"]["etiqueta"]
            if e0 != e1 and e1 != "Sin datos" and ((fav and av["fav_etq"]) or (e1 == "Ganga" and av["ganga"])):
                pot = a["vr"]["pot"]
                lista.append({"titulo": f"{'★ ' if fav else ''}{s}: {e0} → {e1}",
                              "texto": f"{a['n']}. " + (f"Potencial frente al valor razonable: {pot*100:+.0f}%." if pot is not None else ""),
                              "url": url, "tag": f"etq-{s}"})
            t0, t1 = (ant.get("tec") or {}).get("senal"), a["tec"].get("senal")
            if fav and av["fav_tec"] and t0 and t1 and t0 != t1:
                lista.append({"titulo": f"★ {s}: señal técnica {t1}", "texto": f"{a['n']}. Antes: {t0}.", "url": url + "&t=tecnico", "tag": f"tec-{s}"})
        if fav and tarde and av["fav_mov"] and a["cambio"] is not None and abs(a["cambio"]) >= 0.05:
            lista.append({"titulo": f"★ {s} {'sube' if a['cambio'] > 0 else 'cae'} un {abs(a['cambio'])*100:.1f}% hoy",
                          "texto": f"{a['n']} cierra en {a['precio']:.2f} {a['moneda']}.", "url": url, "tag": f"mov-{s}"})
        if fav and tarde and av["fav_res"] and a["res"].get("fecha") == mañana:
            lista.append({"titulo": f"★ {s} presenta resultados mañana", "texto": f"{a['n']}. Revisa la ficha antes de la presentación.",
                          "url": url, "tag": f"res-{s}"})
    if mes_nuevo and av["carteras"]:
        lista.append({"titulo": "Carteras renovadas", "texto": "Ya están las carteras de " + dt.date.today().strftime("%m/%Y") + ": " +
                      ", ".join(f"{c['n']} ({len(c['pos'])})" for c in carts[:3]) + "…", "url": "./?v=carteras", "tag": "carteras"})
    # Primero las de las acciones que sigues
    lista.sort(key=lambda x: not x["titulo"].startswith("★"))
    return lista


# -------------------------------------------------------------------- main
def main():
    cfg_push = leer_config_push()
    if os.environ.get("PRUEBA", "").lower() == "true":
        if not cfg_push:
            raise SystemExit("No hay código de notificaciones. Crea el secreto PUSH_CONFIG con el código que copia la app.")
        enviar_push(cfg_push, [{"titulo": "Apex Quant Pro", "texto": "✅ Las notificaciones funcionan correctamente.", "url": "./", "tag": "prueba"}])
        print("Prueba terminada (no se han actualizado los datos).")
        return

    with open(F_TICK, encoding="utf-8") as f:
        tickers = [l.split("#")[0].strip().upper() for l in f]
    tickers = list(dict.fromkeys([t for t in tickers if t]))
    print(f"Analizando {len(tickers)} acciones…")

    try:
        with open(F_DATOS, encoding="utf-8") as f:
            anterior = {x["s"]: x for x in json.load(f).get("acciones", [])}
    except Exception:
        anterior = {}

    acciones, series, fallos = [], {}, []
    for i, sym in enumerate(tickers, 1):
        print(f"[{i}/{len(tickers)}] {sym}")
        try:
            a, serie = preparar(sym, *descargar(sym))
            if a is None:
                fallos.append(sym)
                continue
            acciones.append(a)
            if serie:
                series[sym] = serie
        except Exception as e:
            print("   ERROR:", e)
            fallos.append(sym)
        time.sleep(0.4)

    if len(acciones) < 5:
        raise SystemExit("Yahoo Finance no ha devuelto datos suficientes. Vuelve a ejecutarlo en un rato.")

    # Medianas de múltiplos por sector (para comparables y ProTips)
    sec_med = {}
    grupos = {}
    for a in acciones:
        grupos.setdefault(a["sector"], []).append(a)
    claves = ["per", "ev_ebitda", "pvc", "pv"]
    limites = {"per": 100, "ev_ebitda": 60, "pvc": 40, "pv": 40}
    universo = {k: mediana([a["m"][k] for a in acciones if a["m"][k] and a["m"][k] < limites[k]]) for k in claves}
    for s, g in grupos.items():
        sec_med[s] = {}
        for k in claves:
            vals = [a["m"][k] for a in g if a["m"][k] and a["m"][k] < limites[k]]
            sec_med[s][k] = mediana(vals) if len(vals) >= 3 else universo[k]

    for a in acciones:
        a["vr"] = valorar(a, sec_med[a["sector"]])
        a["m"]["pot_vr"] = a["vr"]["pot"]
    salud(acciones)

    # Medianas de todas las métricas por sector (para mostrarlas en la app)
    metricas = sorted({k for a in acciones for k in a["m"]})
    sectores = {}
    for s, g in grupos.items():
        sectores[s] = {k: rd(mediana([a["m"][k] for a in g])) for k in metricas}

    for a in acciones:
        a["tips"] = protips(a, sec_med[a["sector"]])

    # Índices
    indices = []
    sp = None
    for sym, nombre in INDICES:
        hst = intentar(lambda: yf.Ticker(sym).history(period="5d", interval="1d"), pd.DataFrame(), n=2)
        if isinstance(hst, pd.DataFrame) and len(hst) >= 2:
            c = hst["Close"].dropna()
            indices.append({"s": sym, "n": nombre, "v": round(float(c.iloc[-1]), 2), "c": rd(c.iloc[-1] / c.iloc[-2] - 1)})
            if sym == "^GSPC":
                sp = float(c.iloc[-1])

    carts, mes_nuevo = carteras(acciones, sp)
    try:
        descargar_logos(acciones)
    except Exception as e:
        print("Logos:", e)

    # Salida
    out = []
    for a in acciones:
        out.append({
            "s": a["s"], "n": a["n"], "sector": a["sector"], "industria": a["industria"], "pais": a["pais"],
            "empresa": a["empresa"],
            "logo": ("logos/" + nombre_logo(a["s"])) if os.path.exists(os.path.join(D_LOGOS, nombre_logo(a["s"]))) else None,
            "moneda": a["moneda"], "precio": rd(a["precio"], 4), "cambio": rd(a["cambio"]), "mcap": a["mcap"],
            "beta": rd(a["beta"], 2), "eps": rd(a["eps"]), "eps_f": rd(a["eps_f"]), "div": rd(a["div"]),
            "payout": rd(a["payout"]), "div_anos": a["div_anos"], "div_subidas": a["div_subidas"],
            "piotroski": a["piotroski"], "piotroski_max": a["piotroski_max"],
            "vr": a["vr"], "salud": a["salud"], "tips": a["tips"], "an": a["an"], "res": a["res"],
            "tec": {k: rd(v) for k, v in a["tec"].items() if not isinstance(v, str)} | {"senal": a["tec"].get("senal")},
            "m": {k: rd(v) for k, v in a["m"].items()},
        })
    datos = {
        "actualizado": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
        "indices": indices, "sectores": sectores, "acciones": out, "carteras": carts, "fallos": fallos,
    }
    with open(F_DATOS, "w", encoding="utf-8") as f:
        json.dump(datos, f, ensure_ascii=False, separators=(",", ":"))
    with open(F_SERIES, "w", encoding="utf-8") as f:
        json.dump(series, f, separators=(",", ":"))

    # Noticias de todas las acciones, sin repetir y de los últimos 10 días
    validos = {a["s"] for a in out}
    por_url = {}
    limite = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=10)).isoformat()
    for sym, raw in NOTICIAS_RAW.items():
        for n in leer_noticias(sym, raw):
            if n["f"] and n["f"] < limite:
                continue
            n["s"] = [x for x in dict.fromkeys(n["s"]) if x in validos]
            clave = n["url"].split("?")[0]
            if clave in por_url:
                por_url[clave]["s"] = list(dict.fromkeys(por_url[clave]["s"] + n["s"]))
            else:
                por_url[clave] = n
    noticias = sorted(por_url.values(), key=lambda n: n["f"] or "", reverse=True)[:500]
    try:
        with open(F_NOTICIAS, encoding="utf-8") as f:
            anteriores = json.load(f).get("n", [])
    except Exception:
        anteriores = []
    try:
        traducir_noticias(noticias, anteriores)
    except Exception as e:
        print("Traducción:", e)
    with open(F_NOTICIAS, "w", encoding="utf-8") as f:
        json.dump({"actualizado": datos["actualizado"], "n": noticias}, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Noticias: {len(noticias)}")
    print(f"Listo: {len(out)} acciones, {len(fallos)} fallos: {fallos}")

    # Notificaciones
    if cfg_push and anterior:
        enviar_push(cfg_push, calcular_avisos(cfg_push, out, anterior, carts, mes_nuevo))
    elif cfg_push and mes_nuevo:
        enviar_push(cfg_push, calcular_avisos(cfg_push, out, {}, carts, mes_nuevo))


if __name__ == "__main__":
    main()
