import requests
import logging
from datetime import datetime, timezone, timedelta

from config.settings import settings

logger = logging.getLogger(__name__)

# Deriv's API symbols are opaque (frxXAUUSD, OTC_DJI). These are the names
# DTrader shows in its asset picker, so the alert can be matched to the
# platform by eye. Unknown symbols fall through to the raw code.
SYMBOL_DISPLAY_NAMES = {
    "frxXAUUSD": "Gold/USD",
    "frxXAGUSD": "Silver/USD",
    "frxEURUSD": "EUR/USD",
    "frxGBPUSD": "GBP/USD",
    "frxUSDJPY": "USD/JPY",
    "frxAUDUSD": "AUD/USD",
    "OTC_DJI": "Wall Street 30",
    "OTC_SPC": "US 500",
    "OTC_NDX": "US Tech 100",
    "OTC_FTSE": "UK 100",
    "OTC_GDAXI": "Germany 40",
}

_DURATION_UNITS = {"t": "tick", "s": "second", "m": "minute", "h": "hour", "d": "day"}


def _fmt_duration(amount, unit):
    """'15', 'm' -> '15 minutes'."""
    word = _DURATION_UNITS.get(str(unit).lower(), str(unit))
    return f"{amount} {word}{'s' if amount != 1 else ''}"


def _fmt_clock(dt):
    """Render a UTC datetime in the machine's local timezone — the clock the
    user is actually looking at on the DTrader screen."""
    if dt is None:
        return None
    local = dt.astimezone()
    return local.strftime("%H:%M:%S %Z").strip()


def _contract_fields(display_name, side_upper, c):
    """Build the 'what do I actually click, and when does it settle' block.

    Every value is optional — the engine fills in what Deriv returned, and a
    missing field is omitted rather than guessed at.
    """
    fields = []

    # BUY/SELL is MACS' internal vocabulary; CALL/PUT is the API's; Rise/Fall
    # is what the DTrader buttons say. Show the last one — it's the only one
    # that maps to a thing on screen.
    ctype = (c.get("contract_type") or "").upper()
    if ctype == "CALL":
        fields.append({"name": "Trade", "value": "📈 **RISE** (Call)", "inline": True})
    elif ctype == "PUT":
        fields.append({"name": "Trade", "value": "📉 **FALL** (Put)", "inline": True})

    if display_name:
        fields.append({"name": "Asset", "value": display_name, "inline": True})

    if c.get("duration") is not None:
        fields.append({
            "name": "Duration",
            "value": _fmt_duration(c["duration"], c.get("duration_unit", "m")),
            "inline": True,
        })

    entry, expiry = c.get("entry_time"), c.get("expiry_time")
    if entry:
        fields.append({"name": "Entered", "value": _fmt_clock(entry), "inline": True})
    if expiry:
        # Discord renders <t:epoch:R> as a live-updating "in 14 minutes",
        # which beats a static timestamp for something that settles on a clock.
        rel = f" (<t:{int(expiry.timestamp())}:R>)"
        fields.append({"name": "Settles at", "value": f"{_fmt_clock(expiry)}{rel}", "inline": True})

    if c.get("entry_spot") is not None:
        fields.append({"name": "Entry spot", "value": f"{c['entry_spot']}", "inline": True})

    stake, payout = c.get("stake"), c.get("payout")
    if stake is not None:
        fields.append({"name": "Stake", "value": f"${float(stake):.2f}", "inline": True})
    if payout is not None:
        payout = float(payout)
        line = f"${payout:.2f}"
        if stake:
            line += f" (+${payout - float(stake):.2f})"
        fields.append({"name": "Payout if won", "value": line, "inline": True})
        # The number that decides whether any of this can work: a fixed-payout
        # binary needs stake/payout accuracy just to break even, not 50%.
        if stake and payout > 0:
            fields.append({
                "name": "Breakeven WR",
                "value": f"{(float(stake) / payout) * 100:.1f}%",
                "inline": True,
            })

    # Deriv's own plain-English statement of the contract terms. Authoritative
    # in a way our reconstruction isn't — if it disagrees with the fields
    # above, it wins.
    if c.get("longcode"):
        fields.append({"name": "Contract", "value": c["longcode"], "inline": False})

    return fields


def send_discord_signal(symbol: str, side: str, price: float, strategy: str, ai_score: float = None, notes: str = "",
                        contract: dict = None):
    """Send a trade signal to Discord via webhook.

    `contract` is optional execution detail from the engine that placed the
    trade. When present the alert becomes actionable — it reports the Rise/Fall
    direction as DTrader labels it, the duration, and the wall-clock expiry —
    rather than just naming a side that already happened.
    """
    webhook_url = getattr(settings, "DISCORD_WEBHOOK_URL", None)
    if not webhook_url:
        logger.warning("DISCORD_WEBHOOK_URL is not set. Skipping Discord notification.")
        return

    side_upper = side.upper()
    color = 0x00FF00 if side_upper == "BUY" else 0xFF0000
    display_name = SYMBOL_DISPLAY_NAMES.get(symbol, symbol)

    embed = {
        "title": f"🚨 MACS-V2 Trade Signal: {side_upper} {symbol}",
        "color": color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "fields": [
            {"name": "Symbol", "value": symbol, "inline": True},
            {"name": "Action", "value": side_upper, "inline": True},
            {"name": "Price", "value": f"${price:.2f}" if price else "MKT", "inline": True},
            {"name": "Strategy", "value": strategy, "inline": True},
        ]
    }

    if ai_score is not None:
        embed["fields"].append({"name": "AI Score", "value": f"{ai_score:.2f}", "inline": True})

    if contract:
        embed["fields"].extend(_contract_fields(display_name, side_upper, contract))

    if notes:
        embed["fields"].append({"name": "Notes", "value": notes, "inline": False})

    payload = {
        "username": "MACS-V2 Bot",
        "embeds": [embed]
    }

    try:
        response = requests.post(webhook_url, json=payload, timeout=5.0)
        response.raise_for_status()
        logger.info(f"Discord webhook sent successfully. Status code: {response.status_code}")
    except requests.exceptions.RequestException:
        logger.error("Failed to send Discord notification: HTTP Request Exception. Check webhook validity.")
    except Exception:
        logger.error("Failed to send Discord notification: Unexpected Error.")

def send_heartbeat(status: str):
    """Sends a lightweight heartbeat to Discord."""
    webhook_url = getattr(settings, "DISCORD_WEBHOOK_URL", None)
    if not webhook_url:
        logger.warning("DISCORD_WEBHOOK_URL is not set. Skipping Discord heartbeat.")
        return
        
    payload = {
        "embeds": [{
            "title": "💓 MACS-V2 Heartbeat",
            "description": f"Cycle completed. Status: {status}",
            "color": 0x00FF00 if "Success" in status else 0xFFA500
        }]
    }
    
    try:
        response = requests.post(webhook_url, json=payload, timeout=5.0)
        response.raise_for_status()
        logger.info(f"Heartbeat sent successfully. Status code: {response.status_code}")
    except requests.exceptions.RequestException:
        logger.error("Failed to send heartbeat: HTTP Request Exception. Check network or webhook validity.")
    except Exception:
        logger.error("Failed to send heartbeat: Unexpected Error.")
