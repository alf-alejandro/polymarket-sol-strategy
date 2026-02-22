#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys, io
# Forzar UTF-8 en Windows para que los caracteres Unicode funcionen
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
"""
===================================================================
  POLYMARKET CLOB - Solana Up or Down (5 Minutes) Strategy
  Estrategia por Volumen / Imbalance del Order Book (OBI)
===================================================================

Logica de la estrategia:
  - Encuentra automaticamente el mercado activo de SOL 5min
  - Descarga el order book en tiempo real via REST polling
  - Calcula Order Book Imbalance (OBI):
      OBI = (Bid Volume - Ask Volume) / (Bid Volume + Ask Volume)
  - OBI > +THRESHOLD  -> SIGNAL: UP   (presion compradora dominante)
  - OBI < -THRESHOLD  -> SIGNAL: DOWN (presion vendedora dominante)
  - Combina OBI instantaneo con promedio de ventana temporal
  - Muestra dashboard en tiempo real con historial visual
===================================================================
Uso:
  python strategy.py              # umbral 15% por default
  python strategy.py 0.20         # umbral 20%
  python strategy.py 0.10 2       # umbral 10%, intervalo 2s
===================================================================
"""

import time
import sys
import os
import math
import requests
from datetime import datetime, timezone
from collections import deque
from py_clob_client.client import ClobClient

# ─── Configuracion ────────────────────────────────────────────────────────────
CLOB_HOST     = "https://clob.polymarket.com"
GAMMA_API     = "https://gamma-api.polymarket.com"

POLL_INTERVAL = 3        # segundos entre snapshots (minimo recomendado: 2)
OBI_THRESHOLD = 0.15     # 15% de imbalance para generar señal
WINDOW_SIZE   = 8        # snapshots para calcular tendencia
TOP_LEVELS    = 15       # niveles de profundidad del order book a analizar

# Patron de slots: sol-updown-5m-{unix_timestamp cada 300s}
SLOT_ORIGIN   = 1771778100  # primer slot conocido (Feb 22, 11:35AM ET)
SLOT_STEP     = 300         # 5 minutos en segundos

# ─── ANSI Colors ──────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
WHITE  = "\033[97m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
RESET  = "\033[0m"


def clear():
    os.system("cls" if os.name == "nt" else "clear")


# ─── Busqueda del mercado activo ──────────────────────────────────────────────

def get_current_slot_ts(lookahead=1):
    """
    Retorna el unix timestamp del slot actual (o el proximo con lookahead).
    Los mercados se abren 1-2 minutos antes del cierre del anterior.
    """
    now = int(time.time())
    elapsed = (now - SLOT_ORIGIN) % SLOT_STEP
    current = now - elapsed
    return current + lookahead * SLOT_STEP


def fetch_market_by_slug(slug):
    """Busca un mercado en Gamma API por su slug."""
    try:
        resp = requests.get(
            f"{GAMMA_API}/markets",
            params={"slug": slug},
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list) and data:
            return data[0]
        return None
    except Exception:
        return None


def get_clob_market(condition_id):
    """Obtiene tokens y datos de trading desde el CLOB."""
    try:
        resp = requests.get(
            f"{CLOB_HOST}/markets/{condition_id}",
            timeout=8,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


def find_active_sol_market():
    """
    Busca el mercado de SOL Up/Down 5min activo y aceptando ordenes.
    Prueba el slot actual, anterior y siguiente para encontrar uno aceptable.
    """
    print(f"{CYAN}[*] Buscando mercado activo SOL Up/Down 5min...{RESET}")

    current_ts = get_current_slot_ts(0)
    candidates = []

    # Probar slots: -1 (anterior), 0 (actual), +1 (siguiente)
    for offset in [-1, 0, 1, 2]:
        ts   = current_ts + offset * SLOT_STEP
        slug = f"sol-updown-5m-{ts}"
        gm   = fetch_market_by_slug(slug)
        if gm:
            candidates.append((ts, slug, gm))

    if not candidates:
        return None

    # Preferir el mercado aceptando ordenes mas cercano al momento actual
    for ts, slug, gm in candidates:
        cid = gm.get("conditionId")
        if not cid:
            continue
        cm = get_clob_market(cid)
        if cm and cm.get("accepting_orders"):
            return _build_market_info(gm, cm)

    # Si ninguno acepta ordenes, tomar cualquier activo
    for ts, slug, gm in candidates:
        cid = gm.get("conditionId")
        if not cid:
            continue
        cm = get_clob_market(cid)
        if cm:
            return _build_market_info(gm, cm)

    return None


def _build_market_info(gamma_market, clob_market):
    """Construye el dict de info del mercado unificando datos Gamma + CLOB."""
    tokens = clob_market.get("tokens", [])
    if len(tokens) < 2:
        return None

    up_token   = None
    down_token = None
    for t in tokens:
        outcome = (t.get("outcome", "") or "").lower()
        if "up" in outcome:
            up_token = t
        elif "down" in outcome:
            down_token = t

    if not up_token:
        up_token   = tokens[0]
        down_token = tokens[1]

    end_date = gamma_market.get("endDate") or clob_market.get("end_date_iso", "")

    return {
        "condition_id":       clob_market.get("condition_id"),
        "question":           clob_market.get("question", "Solana Up or Down - 5min"),
        "end_date":           end_date,
        "market_slug":        clob_market.get("market_slug", ""),
        "accepting_orders":   clob_market.get("accepting_orders", False),
        "up_token_id":        up_token["token_id"],
        "up_outcome":         up_token.get("outcome", "Up"),
        "up_price":           float(up_token.get("price", 0.5)),
        "down_token_id":      down_token["token_id"],
        "down_outcome":       down_token.get("outcome", "Down"),
        "down_price":         float(down_token.get("price", 0.5)),
    }


def seconds_to_market_end(market_info):
    """Calcula segundos restantes hasta el cierre del mercado."""
    end_raw = market_info.get("end_date", "")
    if not end_raw:
        return None
    try:
        end_dt = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
        now_dt = datetime.now(timezone.utc)
        diff   = (end_dt - now_dt).total_seconds()
        return max(0, diff)
    except Exception:
        return None


# ─── Metricas del Order Book ──────────────────────────────────────────────────

def get_order_book_metrics(client, token_id, top_n=TOP_LEVELS):
    """
    Descarga el order book del token y calcula:
      bid_volume, ask_volume, OBI, spread, VWAP mid, profundidad por nivel.
    """
    try:
        ob = client.get_order_book(token_id)
    except Exception as e:
        return None, str(e)

    bids = ob.bids or []
    asks = ob.asks or []

    # Ordenar: bids de mayor a menor precio, asks de menor a mayor
    top_bids = sorted(bids, key=lambda x: float(x.price), reverse=True)[:top_n]
    top_asks = sorted(asks, key=lambda x: float(x.price))[:top_n]

    bid_volume = sum(float(b.size) for b in top_bids)
    ask_volume = sum(float(a.size) for a in top_asks)
    total_vol  = bid_volume + ask_volume

    obi = (bid_volume - ask_volume) / total_vol if total_vol > 0 else 0.0

    best_bid = float(top_bids[0].price) if top_bids else 0.0
    best_ask = float(top_asks[0].price) if top_asks else 0.0
    spread   = round(best_ask - best_bid, 4) if (best_bid and best_ask) else 0.0

    # VWAP mid (precio medio ponderado por volumen)
    if total_vol > 0:
        bvwap = (sum(float(b.price) * float(b.size) for b in top_bids) / bid_volume) if bid_volume > 0 else 0
        avwap = (sum(float(a.price) * float(a.size) for a in top_asks) / ask_volume) if ask_volume > 0 else 0
        vwap_mid = (bvwap * bid_volume + avwap * ask_volume) / total_vol
    else:
        vwap_mid = (best_bid + best_ask) / 2

    return {
        "bid_volume":  bid_volume,
        "ask_volume":  ask_volume,
        "total_vol":   total_vol,
        "obi":         obi,
        "best_bid":    best_bid,
        "best_ask":    best_ask,
        "spread":      spread,
        "vwap_mid":    vwap_mid,
        "num_bids":    len(bids),
        "num_asks":    len(asks),
        "top_bids":    [(round(float(b.price),4), round(float(b.size),2)) for b in top_bids[:6]],
        "top_asks":    [(round(float(a.price),4), round(float(a.size),2)) for a in top_asks[:6]],
    }, None


# ─── Señal y dashboard ────────────────────────────────────────────────────────

def compute_signal(obi_now, obi_window, threshold):
    """
    Señal basada en OBI actual (60%) + promedio ventana (40%).
    Retorna (label, color, confidence_pct, combined_obi)
    """
    avg_obi  = sum(obi_window) / len(obi_window) if obi_window else obi_now
    combined = 0.6 * obi_now + 0.4 * avg_obi

    abs_c = abs(combined)
    if combined > threshold:
        conf = min(int(50 + (abs_c / 0.5) * 50), 99)
        label = "STRONG UP  ▲▲" if combined > threshold * 2 else "UP  ▲"
        return label, GREEN, conf, combined
    elif combined < -threshold:
        conf = min(int(50 + (abs_c / 0.5) * 50), 99)
        label = "STRONG DOWN  ▼▼" if combined < -threshold * 2 else "DOWN  ▼"
        return label, RED, conf, combined
    else:
        return "NEUTRAL  ──", YELLOW, 50, combined


def obi_bar(obi, width=36):
    """Barra visual del OBI: verde = bids dominan, rojo = asks dominan."""
    half   = width // 2
    filled = min(int(abs(obi) * half), half)
    center = "│"
    if obi >= 0:
        left  = " " * half
        right = GREEN + "█" * filled + RESET + " " * (half - filled)
    else:
        left  = " " * (half - filled) + RED + "█" * filled + RESET
        right = " " * half
    return f"[{left}{center}{right}]"


def size_bar(size, max_size, width=18):
    if max_size == 0:
        return ""
    n = int((size / max_size) * width)
    return "█" * n


def fmt_time(seconds):
    if seconds is None:
        return "N/A"
    m = int(seconds) // 60
    s = int(seconds) % 60
    return f"{m}m {s:02d}s"


def render_dashboard(market_info, up_m, obi_history, snap, threshold):
    """Renderiza el dashboard completo."""
    clear()
    now_str   = datetime.now().strftime("%H:%M:%S")
    remaining = seconds_to_market_end(market_info)
    q         = market_info["question"]
    acc       = market_info["accepting_orders"]

    print(f"{BOLD}{CYAN}{'═'*68}{RESET}")
    print(f"{BOLD}{CYAN}  POLYMARKET - SOL Up/Down 5min | Estrategia OBI (Order Book Imbalance){RESET}")
    print(f"{BOLD}{CYAN}{'═'*68}{RESET}")
    print(f"  {BOLD}Mercado:{RESET} {WHITE}{q}{RESET}")
    print(f"  Snapshot #{snap}  |  {WHITE}{now_str}{RESET}  |  "
          f"Acepta ordenes: {''+GREEN+'SI'+RESET if acc else RED+'NO'+RESET}")
    if remaining is not None:
        rem_color = GREEN if remaining > 120 else YELLOW if remaining > 30 else RED
        print(f"  Cierra en: {rem_color}{BOLD}{fmt_time(remaining)}{RESET}  |  "
              f"Umbral: {YELLOW}{threshold:.0%}{RESET}  |  "
              f"Ventana: {WINDOW_SIZE} snapshots  |  Profundidad: TOP {TOP_LEVELS}")

    print(f"{DIM}{'─'*68}{RESET}")

    if not up_m:
        print(f"\n  {YELLOW}[!] Sin datos del order book...{RESET}")
        return

    obi      = up_m["obi"]
    sig, sig_color, conf, combined = compute_signal(obi, list(obi_history), threshold)
    avg_obi  = sum(obi_history) / len(obi_history) if obi_history else obi

    # ── Precios actuales ──────────────────────────────────────────────────────
    print(f"\n  {BOLD}Precios de mercado:{RESET}")
    up_p   = market_info["up_price"]
    down_p = market_info["down_price"]
    print(f"  {GREEN}UP (Yes)  {up_p:.4f} USDC{RESET}   "
          f"{RED}DOWN (No)  {down_p:.4f} USDC{RESET}   "
          f"{DIM}VWAP: {up_m['vwap_mid']:.4f}{RESET}")

    # ── Volumenes ─────────────────────────────────────────────────────────────
    print(f"\n  {BOLD}Volumenes (Token UP - top {TOP_LEVELS} niveles):{RESET}")
    bid_bar_w = int((up_m["bid_volume"] / max(up_m["total_vol"], 0.01)) * 30)
    ask_bar_w = 30 - bid_bar_w
    print(f"  {GREEN}Bids (comprar UP):  {up_m['bid_volume']:>9.2f} USDC  "
          f"{'█'*bid_bar_w}{RESET}  ({up_m['num_bids']} ordenes)")
    print(f"  {RED}Asks (vender UP):   {up_m['ask_volume']:>9.2f} USDC  "
          f"{'█'*ask_bar_w}{RESET}  ({up_m['num_asks']} ordenes)")
    print(f"  {DIM}Total vol:          {up_m['total_vol']:>9.2f} USDC  |  "
          f"Spread: {up_m['spread']:.4f}{RESET}")

    # ── OBI ───────────────────────────────────────────────────────────────────
    print(f"\n  {BOLD}Order Book Imbalance (OBI):{RESET}")
    print(f"  {RED}◄ SELL (asks){RESET}  {obi_bar(obi)}  {GREEN}BUY (bids) ►{RESET}")
    print(f"  OBI actual  = {BOLD}{WHITE}{obi:+.4f}{RESET}  ({obi:+.1%})")
    print(f"  OBI ventana = {WHITE}{avg_obi:+.4f}{RESET}  ({avg_obi:+.1%})")
    print(f"  OBI combined= {WHITE}{combined:+.4f}{RESET}  (60% actual + 40% ventana)")

    # ── SEÑAL ─────────────────────────────────────────────────────────────────
    print(f"\n  {DIM}{'─'*68}{RESET}")
    print(f"  {BOLD}SEÑAL:{RESET}  {sig_color}{BOLD}  {sig}  {RESET}   "
          f"{BOLD}Confianza: {WHITE}{conf}%{RESET}")
    interpret = (
        "Presion compradora dominante → mercado espera que SOL SUBA" if combined > threshold else
        "Presion vendedora dominante → mercado espera que SOL BAJE" if combined < -threshold else
        "Presion equilibrada → señal no definitiva"
    )
    print(f"  {DIM}{interpret}{RESET}")
    print(f"  {DIM}{'─'*68}{RESET}")

    # ── Historial OBI ─────────────────────────────────────────────────────────
    print(f"\n  {BOLD}Historial OBI (ultimos {WINDOW_SIZE} snapshots):{RESET}")
    hist = list(obi_history)
    hist_line = "  "
    for o in hist:
        if o > threshold:
            hist_line += f"{GREEN}▲{RESET}"
        elif o < -threshold:
            hist_line += f"{RED}▼{RESET}"
        else:
            hist_line += f"{YELLOW}─{RESET}"
    # Padding
    hist_line += f"{DIM}" + "·" * (WINDOW_SIZE - len(hist)) + f"{RESET}"
    print(hist_line + f"  (▲=UP | ▼=DOWN | ─=NEUTRAL, umbral {threshold:.0%})")

    # ── Top del order book ────────────────────────────────────────────────────
    if up_m["top_bids"] or up_m["top_asks"]:
        max_b = max((s for _, s in up_m["top_bids"]), default=1)
        max_a = max((s for _, s in up_m["top_asks"]), default=1)

        print(f"\n  {'Top Bids (UP)':^35}  {'Top Asks (UP)':^35}")
        print(f"  {DIM}{'Precio':>8}  {'Volumen':>8}  {'':18}  {'Precio':>8}  {'Volumen':>8}  {'':18}{RESET}")
        rows = max(len(up_m["top_bids"]), len(up_m["top_asks"]))
        for i in range(rows):
            b_str = a_str = ""
            if i < len(up_m["top_bids"]):
                p, s = up_m["top_bids"][i]
                b_str = f"{GREEN}{p:>8.4f}  {s:>8.2f}  {size_bar(s,max_b):<18}{RESET}"
            else:
                b_str = " " * 36
            if i < len(up_m["top_asks"]):
                p, s = up_m["top_asks"][i]
                a_str = f"{RED}{p:>8.4f}  {s:>8.2f}  {size_bar(s,max_a):<18}{RESET}"
            print(f"  {b_str}  {a_str}")

    print(f"\n{DIM}  Ctrl+C para salir  |  Interval: {POLL_INTERVAL}s  |  OBI Strategy v2.0{RESET}")
    print(f"{BOLD}{CYAN}{'═'*68}{RESET}")


# ─── Main loop ────────────────────────────────────────────────────────────────

def run_strategy(market_info, threshold):
    """Loop principal: polling del order book y calculo de señales."""
    client      = ClobClient(CLOB_HOST)
    obi_history = deque(maxlen=WINDOW_SIZE)
    snap        = 0

    print(f"\n{GREEN}[+] Iniciando monitoreo...{RESET}")
    print(f"    UP   token: {market_info['up_token_id'][:30]}...")
    print(f"    DOWN token: {market_info['down_token_id'][:30]}...")
    time.sleep(1.5)

    try:
        while True:
            snap += 1

            up_m, up_err = get_order_book_metrics(
                client, market_info["up_token_id"]
            )

            if up_m:
                obi_history.append(up_m["obi"])
                market_info["up_price"]   = up_m["vwap_mid"]
                market_info["down_price"] = round(1 - up_m["vwap_mid"], 4)

            render_dashboard(market_info, up_m, obi_history, snap, threshold)

            if up_err:
                print(f"\n  {YELLOW}[!] Error book: {up_err}{RESET}")

            # Si el mercado cerro, buscar el siguiente
            remaining = seconds_to_market_end(market_info)
            if remaining is not None and remaining < 5:
                print(f"\n  {YELLOW}[*] Mercado cerrando. Buscando siguiente slot...{RESET}")
                time.sleep(8)
                new_market = find_active_sol_market()
                if new_market:
                    market_info = new_market
                    obi_history.clear()
                    snap = 0
                    print(f"\n  {GREEN}[+] Nuevo mercado: {new_market['question']}{RESET}")
                    time.sleep(2)

            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print(f"\n\n{YELLOW}[*] Estrategia detenida por el usuario.{RESET}")
        if obi_history:
            avg = sum(obi_history) / len(obi_history)
            print(f"    OBI promedio sesion: {avg:+.4f} ({avg:+.1%})")
        print(f"{CYAN}[*] Hasta la proxima.{RESET}\n")


def main():
    print(f"{BOLD}{CYAN}")
    print("  ╔══════════════════════════════════════════════════════════════╗")
    print("  ║     POLYMARKET CLOB - SOL Up/Down 5min | OBI Strategy       ║")
    print("  ╚══════════════════════════════════════════════════════════════╝")
    print(f"{RESET}")

    # Parsear argumentos opcionales
    threshold = OBI_THRESHOLD
    poll_int  = POLL_INTERVAL

    if len(sys.argv) > 1:
        try:
            threshold = float(sys.argv[1])
            print(f"{CYAN}[*] Umbral: {threshold:.0%}{RESET}")
        except ValueError:
            print(f"{YELLOW}[!] Umbral invalido, usando {threshold:.0%}{RESET}")

    if len(sys.argv) > 2:
        try:
            poll_int = max(1, int(sys.argv[2]))
            globals()["POLL_INTERVAL"] = poll_int
            print(f"{CYAN}[*] Intervalo: {poll_int}s{RESET}")
        except ValueError:
            pass

    # Buscar mercado activo
    market_info = find_active_sol_market()
    if not market_info:
        print(f"\n{YELLOW}[!] No se encontro mercado automaticamente.{RESET}")
        print(f"    Ingresa el Condition ID manualmente:")
        print(f"    {DIM}(URL del mercado en polymarket.com){RESET}")
        cid = input("  Condition ID: ").strip()
        if not cid:
            print(f"{RED}[!] Saliendo.{RESET}")
            sys.exit(1)
        cm = get_clob_market(cid)
        if not cm:
            print(f"{RED}[!] No se pudo obtener el mercado. Saliendo.{RESET}")
            sys.exit(1)
        market_info = _build_market_info({"conditionId": cid, "endDate": cm.get("end_date_iso","")}, cm)

    if not market_info:
        print(f"{RED}[!] Error construyendo info del mercado.{RESET}")
        sys.exit(1)

    print(f"\n{GREEN}[+] Mercado encontrado:{RESET}")
    print(f"    {WHITE}{market_info['question']}{RESET}")
    acc = market_info["accepting_orders"]
    print(f"    Acepta ordenes: {''+GREEN+'SI'+RESET if acc else YELLOW+'NO (solo lectura)'+RESET}")
    print(f"    UP token:   {market_info['up_outcome']}  @ {market_info['up_price']:.4f}")
    print(f"    DOWN token: {market_info['down_outcome']} @ {market_info['down_price']:.4f}")
    print(f"    Cierra: {market_info['end_date']}")

    run_strategy(market_info, threshold)


if __name__ == "__main__":
    main()
