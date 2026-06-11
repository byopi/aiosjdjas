"""
Bot de Telegram - Livescores Mundial FIFA 2026
Requiere: python-telegram-bot>=20.0, pyfotmob, python-dotenv (opcional para local)
Variables de entorno: TELEGRAM_TOKEN, ADMIN_ID
"""

import asyncio
import logging
import os
import re
from collections import defaultdict

from telegram import Bot, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes

try:
    import pyfotmob
except ImportError:
    pyfotmob = None

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("WorldCupBot")

# ─────────────────────────────────────────────
# CONFIGURACIÓN
# ─────────────────────────────────────────────
TELEGRAM_TOKEN: str = os.environ["TELEGRAM_TOKEN"]
ADMIN_ID: int = int(os.environ["ADMIN_ID"])

POLL_INTERVAL: int = 10  # segundos entre cada consulta a FotMob

# ID de la competición Copa del Mundo en FotMob (puede variar; se filtra también por nombre)
WORLD_CUP_LEAGUE_IDS = {77, 132}  # 77 = FIFA World Cup histórico, 132 puede ser 2026
WORLD_CUP_KEYWORDS = ["world cup", "mundial", "fifa world cup", "coupe du monde"]

# ─────────────────────────────────────────────
# ESTADO EN MEMORIA
# ─────────────────────────────────────────────

# { channel_id (int): { "link": "t.me/username | @username | chat_id_str" } }
registered_channels: dict[int, dict] = {}

# {
#   match_id: {
#     "message_ids": { channel_id: message_id },  ← mensaje principal por canal
#     "home": str, "away": str,
#     "home_score": int, "away_score": int,
#     "status": str,  ← "not_started" | "in_progress" | "half_time" | "second_half" | "finished"
#     "round": str,
#     "events": set(),  ← event_ids ya procesados
#     "scorers": list[dict],  ← lista de eventos gol conocidos
#     "channel_links": { channel_id: link_str }
#   }
# }
active_matches: dict[str, dict] = {}

# ─────────────────────────────────────────────
# HELPERS DE FORMATO (HTML – todo en negrita)
# ─────────────────────────────────────────────

def _esc(text: str) -> str:
    """Escapa caracteres especiales de HTML para evitar rotura de entidades."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _b(text: str) -> str:
    """Envuelve texto en negrita HTML."""
    return f"<b>{_esc(text)}</b>"


def _build_header(round_label: str) -> str:
    return f"{_b('🏆 | Mundial FIFA 2026')}\n{_b(f'ℹ️ | {round_label}')}"


def _build_score_line(home: str, away: str, hs: int, as_: int) -> str:
    return _b(f"🏳️ {home} {hs}-{as_} {away} 🏳️")


def _build_footer(channel_link: str) -> str:
    return f"{_b('#️⃣ #FIFAWorldCup')}\n\n{_b(f'📲 Suscríbete en {channel_link}')}"


def build_kickoff_msg(home: str, away: str, round_label: str, channel_link: str) -> str:
    parts = [
        _build_header(round_label),
        "",
        _build_score_line(home, away, 0, 0),
        "",
        _b("▫️ ¡EMPIEZA EL PARTIDO!"),
        "",
        _build_footer(channel_link),
    ]
    return "\n".join(parts)


def build_halftime_msg(home: str, away: str, hs: int, as_: int, round_label: str, channel_link: str) -> str:
    parts = [
        _build_header(round_label),
        "",
        _build_score_line(home, away, hs, as_),
        "",
        _b("▫️ ¡DESCANSO!"),
        "",
        _build_footer(channel_link),
    ]
    return "\n".join(parts)


def build_second_half_msg(home: str, away: str, hs: int, as_: int, round_label: str, channel_link: str) -> str:
    parts = [
        _build_header(round_label),
        "",
        _build_score_line(home, away, hs, as_),
        "",
        _b("▫️ ¡EMPIEZA EL SEGUNDO TIEMPO!"),
        "",
        _build_footer(channel_link),
    ]
    return "\n".join(parts)


def build_finished_msg(home: str, away: str, hs: int, as_: int, round_label: str, channel_link: str) -> str:
    parts = [
        _build_header(round_label),
        "",
        _build_score_line(home, away, hs, as_),
        "",
        _b("▫️ ¡TERMINA EL PARTIDO!"),
        "",
        _build_footer(channel_link),
    ]
    return "\n".join(parts)


def build_goal_msg(home: str, away: str, hs: int, as_: int, round_label: str, scorer: str, channel_link: str) -> str:
    scorer_text = scorer if scorer else "-"
    parts = [
        _build_header(round_label),
        "",
        _build_score_line(home, away, hs, as_),
        "",
        _b(f"⚽️ {scorer_text}"),
        "",
        _build_footer(channel_link),
    ]
    return "\n".join(parts)


def build_var_goal_msg(home: str, away: str, hs: int, as_: int, round_label: str, scorer: str, channel_link: str) -> str:
    parts = [
        _build_header(round_label),
        "",
        _build_score_line(home, away, hs, as_),
        "",
        _b("🚩 Gol anulado"),
        _b(f"⚽️ {scorer}"),
        "",
        _build_footer(channel_link),
    ]
    return "\n".join(parts)


def build_red_card_msg(home: str, away: str, hs: int, as_: int, round_label: str, player: str, channel_link: str) -> str:
    parts = [
        _build_header(round_label),
        "",
        _build_score_line(home, away, hs, as_),
        "",
        _b("🟥 Tarjeta roja:"),
        _b(f"▪️ {player}"),
        "",
        _build_footer(channel_link),
    ]
    return "\n".join(parts)


def build_substitution_msg(team: str, ins: list[str], outs: list[str], channel_link: str) -> str:
    if len(ins) == 1:
        title = _b(f"🔄 CAMBIO EN {team.upper()}")
        in_line = _b(f"⬆️ Entra: {ins[0]}")
        out_line = _b(f"⬇️ Sale: {outs[0]}")
    else:
        title = _b(f"🔄 CAMBIOS EN {team.upper()}")
        in_line = _b(f"⬆️ Entran: {', '.join(ins)}")
        out_line = _b(f"⬇️ Salen: {', '.join(outs)}")
    parts = [
        title,
        "",
        in_line,
        out_line,
        "",
        _build_footer(channel_link),
    ]
    return "\n".join(parts)


# ─────────────────────────────────────────────
# HELPERS DE TELEGRAM
# ─────────────────────────────────────────────

async def safe_edit(bot: Bot, chat_id: int, message_id: int, text: str) -> None:
    """Edita un mensaje ignorando errores de 'mensaje no modificado'."""
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode=ParseMode.HTML,
        )
    except BadRequest as e:
        if "message is not modified" in str(e).lower():
            pass  # sin cambios, ignorar
        else:
            logger.warning("BadRequest al editar mensaje %s en %s: %s", message_id, chat_id, e)
    except TelegramError as e:
        logger.error("TelegramError al editar mensaje: %s", e)


async def safe_send(bot: Bot, chat_id: int, text: str) -> int | None:
    """Envía un mensaje y devuelve el message_id, o None si falla."""
    try:
        msg = await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
        )
        return msg.message_id
    except TelegramError as e:
        logger.error("Error al enviar mensaje a %s: %s", chat_id, e)
        return None


async def get_channel_link(bot: Bot, channel_id: int) -> str:
    """Obtiene dinámicamente el enlace público del canal via get_chat()."""
    try:
        chat = await bot.get_chat(channel_id)
        if chat.username:
            return f"t.me/{chat.username}"
        else:
            # Canal privado: usar el ID como referencia
            return str(channel_id)
    except TelegramError as e:
        logger.warning("No se pudo obtener info del canal %s: %s", channel_id, e)
        return str(channel_id)


# ─────────────────────────────────────────────
# HELPERS DE PYFOTMOB
# ─────────────────────────────────────────────

def is_world_cup_match(match_data: dict) -> bool:
    """Verifica si un partido pertenece a la Copa del Mundo."""
    league_id = match_data.get("leagueId") or match_data.get("tournament", {}).get("id")
    league_name = (
        match_data.get("leagueName", "")
        or match_data.get("tournament", {}).get("name", "")
        or match_data.get("parentLeagueName", "")
    ).lower()

    if league_id in WORLD_CUP_LEAGUE_IDS:
        return True
    for kw in WORLD_CUP_KEYWORDS:
        if kw in league_name:
            return True
    return False


def extract_round_label(match_data: dict) -> str:
    """Extrae la etiqueta de ronda/grupo del partido."""
    round_info = match_data.get("roundInfo") or {}
    round_name = round_info.get("name") or round_info.get("roundName") or ""
    tournament_round = match_data.get("tournamentRound", {}).get("round") or ""

    if round_name:
        return round_name
    if tournament_round:
        return f"Jornada {tournament_round}"
    return "Fase de Grupos"


def get_match_status_str(status_code: str | None, status_name: str | None) -> str:
    """Normaliza el estado del partido a una cadena interna."""
    code = (status_code or "").lower()
    name = (status_name or "").lower()
    combined = code + " " + name

    if any(x in combined for x in ["not started", "notstarted", "scheduled", "prematch", "fixture"]):
        return "not_started"
    if any(x in combined for x in ["halftime", "half time", "ht", "half_time"]):
        return "half_time"
    if any(x in combined for x in ["second half", "secondhalf", "2nd"]):
        return "second_half"
    if any(x in combined for x in ["in progress", "inprogress", "live", "1st", "first half"]):
        return "in_progress"
    if any(x in combined for x in ["finished", "fin", "full time", "fulltime", "ft", "ended", "complete"]):
        return "finished"
    return "unknown"


async def fetch_world_cup_matches() -> list[dict]:
    """
    Consulta FotMob y devuelve la lista de partidos del Mundial.
    Usa pyfotmob de forma asíncrona (ejecutándolo en un executor para no bloquear).
    """
    if pyfotmob is None:
        logger.error("pyfotmob no está instalado.")
        return []

    loop = asyncio.get_event_loop()

    def _sync_fetch():
        try:
            client = pyfotmob.FotMob()
            # Intentar obtener partidos de hoy
            matches_today = client.get_matches_by_date()  # fecha actual por defecto
            all_matches = []

            # La estructura de pyfotmob puede variar; manejamos las más comunes
            if hasattr(matches_today, "leagues"):
                for league in matches_today.leagues:
                    for match in (league.matches or []):
                        match_dict = {}
                        # Intentamos leer atributos comunes
                        try:
                            match_dict = {
                                "id": str(match.id),
                                "leagueId": getattr(league, "id", None),
                                "leagueName": getattr(league, "name", ""),
                                "parentLeagueName": getattr(league, "parentLeagueName", ""),
                                "home": getattr(match, "home", {}).get("name", "?") if isinstance(getattr(match, "home", {}), dict) else getattr(getattr(match, "home", None), "name", "?"),
                                "away": getattr(match, "away", {}).get("name", "?") if isinstance(getattr(match, "away", {}), dict) else getattr(getattr(match, "away", None), "name", "?"),
                                "homeScore": _safe_score(match, "home"),
                                "awayScore": _safe_score(match, "away"),
                                "statusCode": str(getattr(match, "status", {}).get("utcTime", "") if isinstance(getattr(match, "status", {}), dict) else ""),
                                "statusName": _safe_status_name(match),
                                "roundInfo": {"name": getattr(match, "roundInfo", {}).get("name", "") if isinstance(getattr(match, "roundInfo", {}), dict) else ""},
                                "_raw": match,
                            }
                            all_matches.append(match_dict)
                        except Exception as ex:
                            logger.debug("Error parseando partido: %s", ex)
            elif isinstance(matches_today, dict):
                # Formato dict raw
                for league_data in matches_today.get("leagues", []):
                    for m in league_data.get("matches", []):
                        m["leagueName"] = league_data.get("name", "")
                        m["leagueId"] = league_data.get("id")
                        m["parentLeagueName"] = league_data.get("parentLeagueName", "")
                        all_matches.append(m)

            return all_matches
        except Exception as e:
            logger.error("Error en fetch FotMob: %s", e)
            return []

    return await loop.run_in_executor(None, _sync_fetch)


def _safe_score(match, side: str) -> int:
    """Extrae el marcador de forma segura."""
    try:
        obj = getattr(match, side, None)
        if obj is None:
            return 0
        if isinstance(obj, dict):
            return int(obj.get("score", 0) or 0)
        score = getattr(obj, "score", None)
        if score is None:
            return 0
        return int(score)
    except Exception:
        return 0


def _safe_status_name(match) -> str:
    """Extrae el nombre de estado de forma segura."""
    try:
        status = getattr(match, "status", None)
        if status is None:
            return ""
        if isinstance(status, dict):
            return status.get("liveTime", {}).get("short", "") or status.get("reason", {}).get("short", "") or ""
        return getattr(status, "reason", {}).get("short", "") or getattr(status, "liveTime", {}).get("short", "") or ""
    except Exception:
        return ""


async def fetch_match_details(match_id: str) -> dict:
    """
    Obtiene detalles (eventos: goles, tarjetas, cambios) de un partido específico.
    """
    if pyfotmob is None:
        return {}

    loop = asyncio.get_event_loop()

    def _sync_details():
        try:
            client = pyfotmob.FotMob()
            details = client.get_match_details(match_id)

            result = {
                "events": [],
                "homeScore": 0,
                "awayScore": 0,
                "statusName": "",
                "statusCode": "",
            }

            if isinstance(details, dict):
                # Marcador
                header = details.get("header", {})
                teams = header.get("teams", [{}])
                if len(teams) >= 2:
                    result["homeScore"] = int((teams[0].get("score") or 0))
                    result["awayScore"] = int((teams[1].get("score") or 0))

                # Estado
                general = details.get("general", {})
                match_info = general.get("matchInfo", {})
                result["statusName"] = match_info.get("status", {}).get("short", "") or ""

                # Eventos
                event_data = details.get("content", {}).get("matchFacts", {}).get("events", {})
                events_list = event_data.get("events", []) if isinstance(event_data, dict) else []
                result["events"] = events_list

            else:
                # Objeto pyfotmob
                try:
                    result["homeScore"] = _safe_score(details, "home")
                    result["awayScore"] = _safe_score(details, "away")
                    result["statusName"] = _safe_status_name(details)
                except Exception:
                    pass

                try:
                    content = getattr(details, "content", None)
                    if content:
                        match_facts = getattr(content, "matchFacts", None)
                        if match_facts:
                            events_obj = getattr(match_facts, "events", None)
                            if events_obj:
                                evts = getattr(events_obj, "events", []) or []
                                result["events"] = evts
                except Exception:
                    pass

            return result
        except Exception as e:
            logger.error("Error obteniendo detalles del partido %s: %s", match_id, e)
            return {}

    return await loop.run_in_executor(None, _sync_details)


def parse_event(event) -> dict | None:
    """
    Parsea un evento (gol, tarjeta, cambio) de FotMob en un dict normalizado.
    Retorna None si no es un evento relevante.
    """
    try:
        if isinstance(event, dict):
            etype = (event.get("type", {}).get("id", "") or "").lower()
            subtype = (event.get("type", {}).get("value", "") or "").lower()
            event_id = str(event.get("id", ""))
            player = event.get("player", {}).get("name", "-") or "-"
            player_in = event.get("swap", {}).get("playerIn", {}).get("name", "-") if "swap" in event else None
            team_id = str(event.get("teamId", ""))
            is_home = event.get("isHome", None)
            minute = event.get("time", {}).get("minute", "") or event.get("minute", "")
        else:
            etype = (getattr(getattr(event, "type", None), "id", "") or "").lower()
            subtype = (getattr(getattr(event, "type", None), "value", "") or "").lower()
            event_id = str(getattr(event, "id", ""))
            _player_obj = getattr(event, "player", None)
            player = getattr(_player_obj, "name", "-") if _player_obj else "-"
            _swap = getattr(event, "swap", None)
            player_in = getattr(getattr(_swap, "playerIn", None), "name", None) if _swap else None
            team_id = str(getattr(event, "teamId", ""))
            is_home = getattr(event, "isHome", None)
            minute = getattr(getattr(event, "time", None), "minute", "") or getattr(event, "minute", "") or ""

        if not event_id:
            return None

        normalized_type = None

        if "goal" in etype:
            if "var" in subtype or "disallowed" in subtype or "cancelled" in subtype:
                normalized_type = "var_goal"
            elif "own" in subtype:
                normalized_type = "goal"
            else:
                normalized_type = "goal"
        elif "card" in etype:
            if "red" in subtype or "yellowred" in subtype:
                normalized_type = "red_card"
            else:
                return None  # amarillas no se notifican
        elif "substitution" in etype or "sub" in etype:
            normalized_type = "substitution"
        else:
            return None

        return {
            "id": event_id,
            "type": normalized_type,
            "player": player or "-",
            "player_in": player_in,
            "team_id": team_id,
            "is_home": is_home,
            "minute": str(minute),
            "raw_subtype": subtype,
        }
    except Exception as e:
        logger.debug("Error parseando evento: %s", e)
        return None


# ─────────────────────────────────────────────
# LOOP DE MONITOREO PRINCIPAL
# ─────────────────────────────────────────────

async def monitor_loop(bot: Bot) -> None:
    """Loop principal que consulta FotMob cada POLL_INTERVAL segundos."""
    logger.info("🟢 Monitor loop iniciado. Intervalo: %ds", POLL_INTERVAL)

    # Buffer de cambios pendientes por partido y equipo
    # { match_id: { team_id: { "ins": [...], "outs": [...] } } }
    pending_subs: dict[str, dict] = defaultdict(lambda: defaultdict(lambda: {"ins": [], "outs": []}))

    while True:
        try:
            if not registered_channels:
                await asyncio.sleep(POLL_INTERVAL)
                continue

            matches = await fetch_world_cup_matches()
            wc_matches = [m for m in matches if is_world_cup_match(m)]

            if not wc_matches:
                logger.info("Sin partidos del Mundial activos ahora mismo.")
                await asyncio.sleep(POLL_INTERVAL)
                continue

            logger.info("Partidos del Mundial encontrados: %d", len(wc_matches))

            for match_raw in wc_matches:
                match_id = str(match_raw.get("id", ""))
                if not match_id:
                    continue

                home = _esc_name(match_raw.get("home", "?"))
                away = _esc_name(match_raw.get("away", "?"))
                round_label = extract_round_label(match_raw)
                status_str = get_match_status_str(
                    match_raw.get("statusCode"),
                    match_raw.get("statusName"),
                )

                # Saltar partidos no iniciados aún
                if status_str == "not_started":
                    continue

                # Si es un partido terminado que ya procesamos, saltar
                if match_id in active_matches and active_matches[match_id]["status"] == "finished":
                    continue

                # ── OBTENER DETALLES DEL PARTIDO ──
                details = await fetch_match_details(match_id)
                home_score = details.get("homeScore", match_raw.get("homeScore", 0))
                away_score = details.get("awayScore", match_raw.get("awayScore", 0))
                detail_status = get_match_status_str(
                    details.get("statusCode"),
                    details.get("statusName"),
                ) if details else status_str
                if detail_status and detail_status != "unknown":
                    status_str = detail_status

                events_raw = details.get("events", [])

                # ── INICIALIZAR PARTIDO NUEVO ──
                if match_id not in active_matches:
                    active_matches[match_id] = {
                        "message_ids": {},
                        "home": home,
                        "away": away,
                        "home_score": 0,
                        "away_score": 0,
                        "status": "not_started",
                        "round": round_label,
                        "events": set(),
                        "scorers": [],
                        "channel_links": {},
                    }

                state = active_matches[match_id]
                prev_status = state["status"]

                # ── ENVIAR MENSAJE INICIAL (KICKOFF) ──
                if prev_status in ("not_started", "unknown") and status_str in ("in_progress", "second_half"):
                    logger.info("🏁 INICIO: %s vs %s", home, away)
                    for ch_id, ch_info in registered_channels.items():
                        link = await get_channel_link(bot, ch_id)
                        state["channel_links"][ch_id] = link
                        text = build_kickoff_msg(home, away, round_label, link)
                        msg_id = await safe_send(bot, ch_id, text)
                        if msg_id:
                            state["message_ids"][ch_id] = msg_id
                    state["status"] = status_str
                    state["home_score"] = home_score
                    state["away_score"] = away_score

                # ── ACTUALIZAR ENLACE CANAL SI HACE FALTA ──
                for ch_id in registered_channels:
                    if ch_id not in state["channel_links"]:
                        state["channel_links"][ch_id] = await get_channel_link(bot, ch_id)

                # ── CAMBIO DE ESTADO: DESCANSO ──
                if prev_status == "in_progress" and status_str == "half_time":
                    logger.info("⏸ DESCANSO: %s vs %s (%d-%d)", home, away, home_score, away_score)
                    state["home_score"] = home_score
                    state["away_score"] = away_score
                    state["status"] = "half_time"
                    for ch_id, msg_id in state["message_ids"].items():
                        link = state["channel_links"].get(ch_id, str(ch_id))
                        text = build_halftime_msg(home, away, home_score, away_score, round_label, link)
                        await safe_edit(bot, ch_id, msg_id, text)

                # ── CAMBIO DE ESTADO: SEGUNDO TIEMPO ──
                elif prev_status == "half_time" and status_str in ("second_half", "in_progress"):
                    logger.info("▶️ SEGUNDO TIEMPO: %s vs %s", home, away)
                    state["status"] = "second_half"
                    for ch_id, msg_id in state["message_ids"].items():
                        link = state["channel_links"].get(ch_id, str(ch_id))
                        text = build_second_half_msg(home, away, home_score, away_score, round_label, link)
                        await safe_edit(bot, ch_id, msg_id, text)

                # ── CAMBIO DE ESTADO: FIN DEL PARTIDO ──
                elif status_str == "finished" and prev_status != "finished":
                    logger.info("🏆 FIN: %s vs %s (%d-%d)", home, away, home_score, away_score)
                    state["home_score"] = home_score
                    state["away_score"] = away_score
                    state["status"] = "finished"
                    for ch_id, msg_id in state["message_ids"].items():
                        link = state["channel_links"].get(ch_id, str(ch_id))
                        text = build_finished_msg(home, away, home_score, away_score, round_label, link)
                        await safe_edit(bot, ch_id, msg_id, text)

                # ── PROCESAR EVENTOS (Goles, Tarjetas, Cambios) ──
                if state["message_ids"]:  # Solo si ya tenemos mensaje principal
                    sub_buffer: dict[str, dict] = {}  # team_id → {ins, outs}

                    for event_raw in events_raw:
                        evt = parse_event(event_raw)
                        if evt is None:
                            continue

                        evt_id = evt["id"]
                        if evt_id in state["events"]:
                            # Evento ya procesado – pero si es gol con scorer "-", revisar actualización
                            if evt["type"] == "goal":
                                for i, sc in enumerate(state["scorers"]):
                                    if sc["id"] == evt_id and sc["player"] == "-" and evt["player"] != "-":
                                        logger.info("✏️ Actualizando nombre de goleador: %s", evt["player"])
                                        state["scorers"][i]["player"] = evt["player"]
                                        hs = state["home_score"]
                                        as_ = state["away_score"]
                                        for ch_id, msg_id in state["message_ids"].items():
                                            link = state["channel_links"].get(ch_id, str(ch_id))
                                            text = build_goal_msg(home, away, hs, as_, round_label, evt["player"], link)
                                            await safe_edit(bot, ch_id, msg_id, text)
                            continue

                        # Evento nuevo
                        state["events"].add(evt_id)

                        if evt["type"] == "goal":
                            # Determinar a qué equipo pertenece el gol
                            is_home_goal = evt.get("is_home")
                            if is_home_goal is True:
                                state["home_score"] += 1
                            elif is_home_goal is False:
                                state["away_score"] += 1
                            else:
                                # Fallback: usar marcador de FotMob
                                state["home_score"] = home_score
                                state["away_score"] = away_score

                            scorer_name = evt["player"]
                            state["scorers"].append({"id": evt_id, "player": scorer_name})
                            hs = state["home_score"]
                            as_ = state["away_score"]
                            logger.info("⚽ GOL: %s (%s %d-%d %s)", scorer_name, home, hs, as_, away)
                            for ch_id, msg_id in state["message_ids"].items():
                                link = state["channel_links"].get(ch_id, str(ch_id))
                                text = build_goal_msg(home, away, hs, as_, round_label, scorer_name, link)
                                await safe_edit(bot, ch_id, msg_id, text)

                        elif evt["type"] == "var_goal":
                            # Gol anulado – restar marcador
                            is_home_goal = evt.get("is_home")
                            if is_home_goal is True and state["home_score"] > 0:
                                state["home_score"] -= 1
                            elif is_home_goal is False and state["away_score"] > 0:
                                state["away_score"] -= 1
                            else:
                                state["home_score"] = home_score
                                state["away_score"] = away_score

                            scorer_name = evt["player"]
                            hs = state["home_score"]
                            as_ = state["away_score"]
                            logger.info("🚩 GOL ANULADO: %s (%s %d-%d %s)", scorer_name, home, hs, as_, away)
                            for ch_id, msg_id in state["message_ids"].items():
                                link = state["channel_links"].get(ch_id, str(ch_id))
                                text = build_var_goal_msg(home, away, hs, as_, round_label, scorer_name, link)
                                await safe_edit(bot, ch_id, msg_id, text)

                        elif evt["type"] == "red_card":
                            hs = state["home_score"]
                            as_ = state["away_score"]
                            logger.info("🟥 TARJETA ROJA: %s (%s vs %s)", evt["player"], home, away)
                            for ch_id, msg_id in state["message_ids"].items():
                                link = state["channel_links"].get(ch_id, str(ch_id))
                                text = build_red_card_msg(home, away, hs, as_, round_label, evt["player"], link)
                                await safe_edit(bot, ch_id, msg_id, text)

                        elif evt["type"] == "substitution":
                            team_id = evt["team_id"]
                            player_out = evt["player"]
                            player_in = evt["player_in"] or "-"
                            is_home_team = evt.get("is_home")
                            team_name = home if is_home_team is True else (away if is_home_team is False else "Equipo")

                            if team_id not in sub_buffer:
                                sub_buffer[team_id] = {
                                    "ins": [],
                                    "outs": [],
                                    "team_name": team_name,
                                }
                            sub_buffer[team_id]["ins"].append(player_in)
                            sub_buffer[team_id]["outs"].append(player_out)

                    # Enviar cambios agrupados por equipo
                    for team_id, subs in sub_buffer.items():
                        ins = subs["ins"]
                        outs = subs["outs"]
                        team_name = subs["team_name"]
                        logger.info("🔄 CAMBIO(S) en %s: entra(n) %s, sale(n) %s", team_name, ins, outs)
                        for ch_id in state["message_ids"]:
                            link = state["channel_links"].get(ch_id, str(ch_id))
                            text = build_substitution_msg(team_name, ins, outs, link)
                            await safe_send(bot, ch_id, text)

                # Actualizar marcador si el partido está en curso
                if status_str in ("in_progress", "second_half", "half_time"):
                    state["home_score"] = home_score
                    state["away_score"] = away_score

        except Exception as e:
            logger.error("Error inesperado en monitor_loop: %s", e, exc_info=True)

        await asyncio.sleep(POLL_INTERVAL)


def _esc_name(name: str) -> str:
    """Limpia el nombre del equipo para uso en HTML."""
    return str(name).strip()


# ─────────────────────────────────────────────
# COMANDOS DEL BOT
# ─────────────────────────────────────────────

def admin_only(func):
    """Decorador que restringe el comando al ADMIN_ID."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or user.id != ADMIN_ID:
            await update.message.reply_text("❌ No tienes permiso para usar este comando.")
            return
        if update.effective_chat.type != "private":
            await update.message.reply_text("⚠️ Usa este comando en chat privado.")
            return
        return await func(update, context)
    wrapper.__name__ = func.__name__
    return wrapper


@admin_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🏆 <b>Bot Livescores – Mundial FIFA 2026</b>\n\n"
        "Comandos disponibles:\n"
        "• /addchannel <id_o_username> – Vincular canal\n"
        "• /channels – Ver canales registrados\n"
        "• /removechannel <id> – Eliminar canal\n\n"
        "El bot comenzará a transmitir partidos automáticamente en los canales registrados.",
        parse_mode=ParseMode.HTML,
    )


@admin_only
async def cmd_addchannel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Uso: /addchannel <id_del_canal_o_@username>")
        return

    raw = context.args[0].strip()

    # Determinar si es un ID numérico o un username
    try:
        channel_id = int(raw)
    except ValueError:
        # Puede ser @username o username sin @
        username = raw if raw.startswith("@") else f"@{raw}"
        channel_id = username  # type: ignore[assignment]

    try:
        chat = await context.bot.get_chat(channel_id)
        real_id = chat.id
        link = f"t.me/{chat.username}" if chat.username else str(real_id)
        title = chat.title or str(real_id)

        registered_channels[real_id] = {"link": link, "title": title}
        logger.info("Canal registrado: %s (%s) → %s", title, real_id, link)
        await update.message.reply_text(
            f"✅ Canal vinculado:\n"
            f"<b>{_esc(title)}</b>\n"
            f"ID: <code>{real_id}</code>\n"
            f"Enlace: {_esc(link)}",
            parse_mode=ParseMode.HTML,
        )
    except TelegramError as e:
        await update.message.reply_text(f"❌ No se pudo obtener info del canal: {e}\n\n"
                                        "Asegúrate de que el bot es administrador del canal.")


@admin_only
async def cmd_channels(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not registered_channels:
        await update.message.reply_text("No hay canales registrados aún.")
        return

    lines = ["<b>Canales registrados:</b>\n"]
    for ch_id, info in registered_channels.items():
        lines.append(f"• {_esc(info.get('title', '?'))} — <code>{ch_id}</code> — {_esc(info.get('link', ''))}")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


@admin_only
async def cmd_removechannel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Uso: /removechannel <id_del_canal>")
        return
    try:
        ch_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("El ID del canal debe ser numérico.")
        return

    if ch_id in registered_channels:
        info = registered_channels.pop(ch_id)
        await update.message.reply_text(f"✅ Canal <b>{_esc(info.get('title', str(ch_id)))}</b> eliminado.", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("❌ Ese canal no está registrado.")


# ─────────────────────────────────────────────
# PUNTO DE ENTRADA
# ─────────────────────────────────────────────

async def post_init(application: Application) -> None:
    """Se ejecuta después de que la aplicación está lista; arranca el monitor loop."""
    logger.info("Bot iniciado. Arrancando monitor loop...")
    asyncio.create_task(monitor_loop(application.bot))


def main() -> None:
    if pyfotmob is None:
        logger.error("❌ pyfotmob no está instalado. Instálalo con: pip install pyfotmob")
        return

    logger.info("Iniciando Bot Mundial FIFA 2026...")
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("addchannel", cmd_addchannel))
    app.add_handler(CommandHandler("channels", cmd_channels))
    app.add_handler(CommandHandler("removechannel", cmd_removechannel))

    logger.info("Bot en ejecución. Esperando partidos...")
    app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()
