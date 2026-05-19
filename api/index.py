"""
This module implements a Flask server to provide an API that interacts with Xtream servers.
It includes endpoints for configuration, manifest generation, metadata, catalogs, and streams.
"""

import hashlib
import logging
import re
import time
import unicodedata
from base64 import b64decode, b64encode
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from functools import lru_cache
from json import dumps, loads
from threading import Lock
from urllib.parse import unquote, urlparse, urlunparse

import os
from cryptography.fernet import Fernet, InvalidToken
from flask import (
    Flask,
    jsonify,
    render_template,
    request,
    send_from_directory,
    url_for,
    session,
)
from flask_cors import CORS
from httpx import Client, RequestError
from idna import encode as idna_encode

# HTTP client with automatic redirect following
http = Client(follow_redirects=True)

# Initialize Flask application
app = Flask(__name__)
# Enable Cross-Origin Resource Sharing (CORS) for all routes
CORS(app)

# HTTP headers used for requests to Xtream servers
hraders = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3",
    "Connection": "keep-alive",
    "Accept-Encoding": "gzip",
}

# Basic logging configuration
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Read sensitive keys from environment (defined in docker-compose.yml)
FERNET_KEY = os.environ.get("FERNET_KEY")
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")

# Cache TTL in seconds. Override via ENV.
# CACHE_TTL_XTREAM  — full content lists (VOD, series, live)
# CACHE_TTL_DETAILS — per-item detail calls (series_info, vod_info)
# CACHE_TTL_TMDB    — TMDB metadata (changes very rarely)
CACHE_TTL_XTREAM  = int(os.environ.get("CACHE_TTL_XTREAM",  3600))   # 1 h
CACHE_TTL_DETAILS = int(os.environ.get("CACHE_TTL_DETAILS", 3600))   # 1 h
CACHE_TTL_TMDB    = int(os.environ.get("CACHE_TTL_TMDB",    86400))  # 24 h

# Comma-separated list of titles that should skip the release-year check.
# Example: YEAR_CHECK_BYPASS=Chiquititas,Rebelde,Carrossel
_bypass_raw = os.environ.get("YEAR_CHECK_BYPASS", "")

# If there is no FERNET_KEY defined, generate a temporary key (non-persistent)
if not FERNET_KEY:
    logging.warning("FERNET_KEY not found in environment. Generating temporary key (non-persistent).")
    FERNET_KEY = Fernet.generate_key().decode()

# Instantiate Fernet and handle invalid keys
try:
    fernet = Fernet(FERNET_KEY.encode() if isinstance(FERNET_KEY, str) else FERNET_KEY)
except Exception as e:
    logging.error("Invalid FERNET_KEY: %s. Generating temporary key.", e)
    FERNET_KEY = Fernet.generate_key()
    fernet = Fernet(FERNET_KEY)


# ---------------------------------------------------------------------------
# TTL-aware in-process cache
# Replaces bare lru_cache for HTTP calls so stale data is automatically
# refreshed without needing a container restart.
# ---------------------------------------------------------------------------

_cache: dict = {}
_cache_lock = Lock()


def _cache_get(key: str):
    """Return cached value if still fresh, else None."""
    with _cache_lock:
        entry = _cache.get(key)
        if entry and time.monotonic() < entry["expires"]:
            return entry["value"]
    return None


def _cache_set(key: str, value, ttl: int):
    with _cache_lock:
        _cache[key] = {"value": value, "expires": time.monotonic() + ttl}


def fetch_url(url: str, params: dict, timeout: int = 10, ttl: int = CACHE_TTL_XTREAM):
    """
    Fetches a URL with TTL-based caching.
    Returns the parsed JSON response or None on error.
    """
    cache_key = url + str(sorted(params.items()))
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    try:
        response = http.get(url, params=params, headers=hraders, timeout=timeout)
        logger.debug("Response URL: %s, Status: %s", response.url, response.status_code)
        response.raise_for_status()
        data = response.json()
        _cache_set(cache_key, data, ttl)
        return data
    except RequestError as e:
        logger.error("Request error for %s: %s", url, e)
        return None


def fetch_xtream(base_url: str, creds: dict, action: str, extra: dict = None, ttl: int = CACHE_TTL_XTREAM):
    """Fetch an Xtream player_api.php endpoint with caching."""
    params = {"username": creds["username"], "password": creds["password"], "action": action}
    if extra:
        params.update(extra)
    return fetch_url(f"{base_url}/player_api.php", params, ttl=ttl)


def normalize_string(s):
    """
    Normalizes a string by removing accents, converting to lowercase, and removing special characters.
    """
    if not isinstance(s, str):
        return ""
    s = "".join(
        c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn"
    ).lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


# Build the bypass set now that normalize_string is available.
YEAR_CHECK_BYPASS: set = {
    normalize_string(t.strip())
    for t in _bypass_raw.split(",")
    if t.strip()
}


def extract_base_title(name):
    """
    Extracts the base title from an Xtream item name by stripping common
    provider suffixes such as quality tags, year annotations, and flags.

    Examples:
        "Witch Hat Atelier (2026)"  -> "Witch Hat Atelier"
        "Hells Paradise [L]"        -> "Hells Paradise"
        "Paradise"                  -> "Paradise"
        "The Office HD"             -> "The Office"
        "Avatar 4K [L]"             -> "Avatar"
    """
    if not isinstance(name, str):
        return ""
    name = re.sub(r"\[.*?\]", "", name)
    name = re.sub(r"\(\d{4}\)", "", name)
    name = re.sub(
        r"\b(SD|HD|FHD|UHD|4K|H265|H264|HEVC|DUB|LEG|DUAL|MULTI|ALT)\b",
        "",
        name,
        flags=re.IGNORECASE,
    )
    return name.strip()


def extract_year(date_str):
    """
    Extracts the 4-digit year from a date string (YYYY-MM-DD or YYYY).
    Returns an int, or None if the string is empty/unparseable.
    """
    if not date_str or not isinstance(date_str, str):
        return None
    match = re.match(r"(\d{4})", date_str.strip())
    return int(match.group(1)) if match else None


def year_matches(provider_info, tmdb_year, title=""):
    """
    Returns True when:
    - The title is in YEAR_CHECK_BYPASS -> skip year check entirely
    - The provider item has no release year (field absent or empty) -> pass through
    - tmdb_year is unknown (None) -> pass through
    - The provider year matches the TMDB year exactly

    Accepts both 'releasedate' and 'releaseDate' field names.
    """
    if normalize_string(title) in YEAR_CHECK_BYPASS:
        logger.info("Year check bypassed for title: %s", title)
        return True
    if tmdb_year is None:
        return True
    raw = provider_info.get("releasedate") or provider_info.get("releaseDate") or ""
    provider_year = extract_year(raw)
    if provider_year is None:
        return True
    return provider_year == tmdb_year


def names_match(xtream_name, tmdb_name):
    """
    Returns True only when the base title of an Xtream item is an *exact*
    match (after normalization) against the TMDB title.
    """
    base = normalize_string(extract_base_title(xtream_name))
    target = normalize_string(tmdb_name)
    return base == target


def format_date(date_str):
    """
    Formats a date string in ISO 8601 format with 'Z' suffix.
    If the string is empty, returns the current date/time.
    If conversion is not possible, returns the original string.
    """
    try:
        if not date_str or date_str.strip() == "":
            return datetime.now().isoformat() + "Z"
        return datetime.strptime(date_str, "%Y-%m-%d").isoformat() + "Z"
    except Exception as e:
        logger.warning("Invalid date '%s': %s", date_str, e)
        return date_str


@lru_cache(maxsize=256)
def convert_to_url(url):
    """
    Converts a URL to ensure the domain is in IDNA format (safe Unicode).
    Returns the converted URL or the original one in case of error.
    """
    try:
        parsed_url = urlparse(url)
        netloc = parsed_url.netloc.split(":")
        encoded_netloc = idna_encode(netloc[0]).decode("utf-8")
        if len(netloc) > 1:
            encoded_netloc += f":{netloc[1]}"
        return urlunparse(parsed_url._replace(netloc=encoded_netloc))
    except Exception as e:
        logger.warning(f"Erro ao converter URL '{url}': {e}")
        return url


def agroup_channels(channels: list) -> dict:
    """
    Groups channels by normalized name (ignoring quality suffixes).
    Returns a dictionary with lists of grouped channels, id, logo, and name.
    """
    grouped_names = defaultdict(lambda: {"list": [], "id": "", "logo": "", "name": ""})
    for i in channels:
        name = (
            re.sub(
                r"\b(SD|FHD|HD|4K|H265|Alt)\b",
                "",
                i.get("name", ""),
                flags=re.IGNORECASE,
            )
            .strip()
            .replace("  ", " ")
            .replace("[]", "")
        )
        if name.endswith(" "):
            name = name[:-1]
        keywords = name.split()
        group_key = normalize_string(" ".join(keywords[:2])) if keywords else ""
        if not group_key:
            continue
        grouped_names[group_key]["list"].append(i)
        grouped_names[group_key]["id"] = hashlib.md5(group_key.encode()).hexdigest()
        if not grouped_names[group_key]["logo"] and i.get("stream_icon"):
            grouped_names[group_key]["logo"] = i["stream_icon"]
        if not grouped_names[group_key]["name"]:
            grouped_names[group_key]["name"] = name
    return grouped_names


def decode_hash(hash_str):
    """Decodes the hash, detecting whether it's base64 or Fernet."""
    try:
        decoded = fernet.decrypt(hash_str.encode())
        return loads(decoded.decode("utf-8"))
    except (InvalidToken, ValueError):
        try:
            try:
                return loads(b64decode(hash_str).decode("utf-8"))
            except UnicodeDecodeError:
                return loads(b64decode(hash_str).decode("latin1"))
        except Exception as exc:
            raise ValueError("Invalid hash") from exc


def encode_hash(data: dict, use_fernet=False):
    """Encodes the dictionary as base64 or Fernet hash."""
    raw = dumps(data).encode("utf-8")
    if use_fernet:
        return fernet.encrypt(raw).decode()
    else:
        return b64encode(raw).decode()


def _get_release_date(info: dict) -> str:
    """Returns the first non-empty release date string from an info dict."""
    return info.get("releasedate") or info.get("releaseDate") or ""


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------

@app.route("/encrypt", methods=["POST"])
def encrypt():
    """Endpoint to encrypt configs using Fernet."""
    try:
        data = request.get_json(force=True)
        encrypted = encode_hash(data, use_fernet=True)
        return jsonify({"hash": encrypted})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/")
@app.route("/configure")
def index():
    """Main configuration page endpoint."""
    return render_template("config.html", config={})


@app.route("/<hash>/configure")
def config(hash):
    """Configuration page with pre-filled data from hash."""
    try:
        config_data = decode_hash(hash)
    except Exception:
        return "Invalid hash", 400
    return render_template("config.html", config=config_data)


@app.route("/favicon.ico")
def favicon():
    return send_from_directory(app.static_folder, "favicon.ico")


@app.route("/manifest.json")
def manifest():
    """Returns the base manifest for unconfigured addon."""
    return jsonify(
        {
            "id": "org.xtremio.config",
            "version": "1.0.1",
            "name": "Xtremio",
            "description": "Watch movies and series from your Xtream server",
            "logo": url_for("static", filename="logo.png", _external=True),
            "resources": ["catalog", "meta", "stream"],
            "types": ["movie", "series", "tv"],
            "catalogs": [],
            "idPrefixes": ["tt"],
            "behaviorHints": {
                "configurable": True,
                "configurationRequired": True,
            },
        }
    )


@app.route("/<hash>/manifest.json")
def manifesth(hash):
    """
    Generates a configured Stremio manifest for a specific Xtream server.
    Fetches server info and all category lists in parallel.
    """
    logger.info("Generating manifest for hash: %s", hash)
    hash = unquote(hash)
    try:
        b = decode_hash(hash)
    except Exception:
        return jsonify({"error": "Invalid hash"}), 400

    base_url = convert_to_url(b["BaseURL"])
    xtr = base_url.split("//")[1].split(".")[0]
    name = b["name"] if b.get("name") else xtr + " - Xtremio"

    # Fetch server info and all three category lists in parallel
    with ThreadPoolExecutor(max_workers=4) as pool:
        f_info    = pool.submit(fetch_url, f"{base_url}/player_api.php",
                                {"username": b["username"], "password": b["password"]})
        f_vod_cat = pool.submit(fetch_xtream, base_url, b, "get_vod_categories")
        f_ser_cat = pool.submit(fetch_xtream, base_url, b, "get_series_categories")
        f_tv_cat  = pool.submit(fetch_xtream, base_url, b, "get_live_categories")

    info     = f_info.result()
    vod_cats = f_vod_cat.result()
    ser_cats = f_ser_cat.result()
    tv_cats  = f_tv_cat.result()

    if not info:
        logger.warning("Invalid credentials provided.")
        return jsonify({"error": "Invalid credentials"}), 401

    catalogs = [
        {
            "type": "movie",
            "id": xtr,
            "name": f"{name} - Movies",
            "extra": [
                {"name": "genre", "options": [c["category_name"] for c in vod_cats]},
                {"name": "search"},
                {"name": "skip"},
            ],
        },
        {
            "type": "series",
            "id": xtr,
            "name": f"{name} - Series",
            "extra": [
                {"name": "genre", "options": [c["category_name"] for c in ser_cats]},
                {"name": "search"},
                {"name": "skip"},
            ],
        },
        {
            "type": "tv",
            "id": xtr,
            "name": f"{name} - TV",
            "extra": [
                {"name": "genre", "options": [c["category_name"] for c in tv_cats]},
                {"name": "search"},
                {"name": "skip"},
            ],
        },
    ]

    description = (
        f"Hello {b['username']}!\n",
        "You will be able to watch movies and series from your Xtream server\n\n",
        "Server info:\n",
        f"Server URL: {base_url}\n",
        f"Max connections: {info['user_info']['max_connections']}\n",
        f"This account is trial: {info['user_info']['is_trial']}\n",
        f"Account status: {info['user_info']['status']}\n",
        f"Account expiracy date: {datetime.fromtimestamp(int(info['user_info']['exp_date']))}\n"
        if "exp_date" in info["user_info"] and info["user_info"]["exp_date"]
        else "Account expiracy date: Not available\n",
        f"Account created at: {datetime.fromtimestamp(int(info['user_info']['created_at']))}\n\n",
        "This addon is not a official addon from Stremio. It was made by the community.\n",
        "If you have any problem, please contact the developer of this addon.\n",
        "Enjoy it!",
    )

    response = jsonify(
        {
            "id": f"org.xtremio.{xtr}",
            "version": "1.0.1",
            "name": name,
            "description": "".join(description),
            "logo": url_for("static", filename="logo.png", _external=True),
            "resources": ["catalog", "meta", "stream"],
            "types": ["movie", "series", "tv"],
            "catalogs": catalogs,
            "idPrefixes": ["tt", xtr],
            "behaviorHints": {
                "configurable": False if b.get("sell") else True,
                "configurationRequired": False,
            },
        }
    )
    response.headers.add("Access-Control-Allow-Origin", "*")
    return response


@app.route("/<hash>/meta/<type>/<id>.json")
def meta(hash, type, id):
    """
    Provides metadata for a specific content item (movie, series, or TV channel).
    """
    logger.info("Processing meta request for hash: %s, type: %s, id: %s", hash, type, id)
    hash = unquote(hash)
    type = unquote(type)

    id_decoded = unquote(id)
    if ":" in id_decoded:
        xtr, id = id_decoded.split(":", 1)
    else:
        xtr, id = id_decoded, id_decoded
    try:
        b = decode_hash(hash)
    except Exception:
        return jsonify({"meta": {}})
    base_url = convert_to_url(b["BaseURL"])
    cat_new = False

    if ":" in id:
        id = id.split(":")[1]
        cat_new = True

    if "tt" in id:
        logger.debug("IMDB ID detected in meta request: %s", id)
        return jsonify({"meta": {}})
    elif xtr != base_url.split("//")[1].split(".")[0]:
        logger.warning("Mismatch in XTR and BaseURL: XTR=%s, BaseURL=%s", xtr, b["BaseURL"])
        return jsonify({"meta": {}})

    logger.info("Processing meta request for type: %s and id: %s", type, id)

    if type == "series":
        program = fetch_xtream(base_url, b, "get_series_info", {"series_id": id},
                               ttl=CACHE_TTL_DETAILS)

        videos = []
        for season in program["episodes"]:
            for episode in program["episodes"][season]:
                videos.append(
                    {
                        "id": f"{xtr}:{id}:{season}:{episode['episode_num']}",
                        "title": episode["title"],
                        "episode": episode["episode_num"],
                        "season": episode["season"],
                        "overview": episode["info"].get("plot", ""),
                        "released": format_date(_get_release_date(episode["info"])),
                        "thumbnail": episode["info"].get("movie_image") or program["info"]["cover"],
                    }
                )

        meta_obj = {
            "id": f"{xtr}:{id}",
            "name": program["info"]["name"],
            "poster": program["info"]["cover"],
            "background": program["info"]["backdrop_path"][0]
            if program["info"]["backdrop_path"]
            else "",
            "description": program["info"].get("plot", ""),
            "genre": program["info"]["genre"],
            "imdbRating": program["info"]["rating"],
            "released": format_date(_get_release_date(program["info"])),
            "type": "series",
            "videos": videos,
        }
        return jsonify({"meta": meta_obj})

    elif type == "movie":
        program = fetch_xtream(base_url, b, "get_vod_info", {"vod_id": id},
                               ttl=CACHE_TTL_DETAILS)

        meta_obj = {
            "id": f"{xtr}:{id}",
            "name": program["info"].get("name") or program["movie_data"]["name"],
            "poster": program["info"].get("cover_big") or program["info"].get("backdrop") or "",
            "background": (program["info"].get("backdrop_path") or [""])[0],
            "description": program["info"].get("plot", ""),
            "genre": program["info"]["genre"],
            "imdbRating": program["info"]["rating"],
            "released": format_date(_get_release_date(program["info"])),
            "type": "movie",
        }
        if "youtube_trailer" in program["info"]:
            meta_obj["trailer"] = {
                "source": program["info"]["youtube_trailer"],
                "type": "Trailer",
            }
        logger.info("Meta response generated for type: %s, id: %s", type, id)
        return jsonify({"meta": meta_obj})

    elif type == "tv":
        program = fetch_xtream(base_url, b, "get_live_streams")

        if cat_new:
            grouped_names = agroup_channels(program)
            for i in grouped_names:
                if grouped_names[i]["id"] == id:
                    meta_obj = {
                        "id": f"{xtr}:ai:{grouped_names[i]['id']}",
                        "name": grouped_names[i]["name"],
                        "background": grouped_names[i]["logo"],
                        "type": "tv",
                    }
                    break
        else:
            id = id.replace("null", "")
            try:
                live_id = int(id)
            except ValueError:
                return jsonify({"meta": {}})

            lives = {live["stream_id"]: live for live in program}
            if live_id not in lives:
                return jsonify({"meta": {}})

            live = lives[live_id]
            meta_obj = {
                "id": f"{xtr}:{id}",
                "name": live["name"],
                "poster": live["stream_icon"],
                "background": live["stream_icon"],
                "type": "tv",
            }
        return jsonify({"meta": meta_obj})


@app.route("/<hash>/catalog/<type>/<xtr>/search=<search>.json")
@app.route("/<hash>/catalog/<type>/<xtr>/genre=<genre>.json")
@app.route("/<hash>/catalog/<type>/<xtr>.json")
def catalog(hash, type, xtr, genre=None, search=None):
    """
    Returns a catalog of content items (movies, series, or TV channels).
    Supports filtering by genre and searching by name.
    """
    logger.info(
        "Catalog request: hash=%s, type=%s, xtr=%s, genre=%s, search=%s",
        hash, type, xtr, genre, search,
    )
    hash   = unquote(hash)
    type   = unquote(type)
    xtr    = unquote(xtr)
    genre  = unquote(genre).replace("genre=", "") if genre else None
    search = unquote(search).replace("search=", "") if search else None
    try:
        b = decode_hash(hash)
    except Exception:
        return jsonify({"metas": []})
    base_url = convert_to_url(b["BaseURL"])

    if xtr != base_url.split("//")[1].split(".")[0]:
        logger.warning("XTR mismatch: %s != %s", xtr, base_url.split("//")[1].split(".")[0])
        return jsonify({"metas": []})

    types  = {"movie": "vod", "series": "series", "tv": "live"}
    action = f"get_{types[type]}" if type == "series" else f"get_{types[type]}_streams"

    if genre:
        catalog_data = fetch_xtream(base_url, b, f"get_{types[type]}_categories")
        category_id  = next(
            item["category_id"] for item in catalog_data if item["category_name"] == genre
        )
        all_content = fetch_xtream(base_url, b, action, {"category_id": category_id})
    elif search:
        series_data = fetch_xtream(base_url, b, action)
        all_content = [
            item for item in series_data if re.search(search, normalize_string(item["name"]))
        ]
    else:
        try:
            all_content = fetch_xtream(base_url, b, action)
        except Exception:
            all_content = []

    metas = []

    if type != "tv":
        all_content = all_content[:50]
        for item in all_content:
            metas.append(
                {
                    "id": f"{xtr}:{item['series_id']}" if type == "series" else f"{xtr}:{item['stream_id']}",
                    "name": item["name"],
                    "poster": item.get("cover") or item.get("stream_icon"),
                    "posterShape": "poster",
                    "type": type,
                    "releaseInfo": format_date(item["releasedate"]) if "releasedate" in item else None,
                    "imdbRating": item["rating"],
                }
            )
    else:
        grouped_names = agroup_channels(all_content)
        for itens in grouped_names:
            metas.append(
                {
                    "id": f"{xtr}:ai:{grouped_names[itens]['id']}",
                    "name": grouped_names[itens]["name"],
                    "poster": grouped_names[itens]["logo"],
                    "posterShape": "square",
                    "type": "tv",
                    "description": "\n".join([i["name"] for i in grouped_names[itens]["list"]]),
                }
            )

    logger.info("Catalog response generated with %d items.", len(metas))
    return jsonify({"metas": metas})


@app.route("/<hash>/stream/<type>/<id>.json")
def stream(hash, type, id):
    """
    Provides stream URLs for playback of movies, series episodes, or TV channels.
    Handles both Xtream-native content and IMDB-based lookups via TMDB.
    """
    hash = unquote(hash)
    type = unquote(type)
    id   = unquote(id)
    result = {}

    try:
        b = decode_hash(hash)
    except Exception:
        return jsonify({"streams": []})
    base_url = convert_to_url(b["BaseURL"])

    # ------------------------------------------------------------------
    # Xtream-native content (id does NOT start with "tt")
    # ------------------------------------------------------------------
    if not id.startswith("tt"):
        xtr, id = id.split(":", 1)

        if type == "series":
            id, season, episode = id.split(":")
            # Separamos a busca para conseguir pegar o nome da série em ['info']['name']
            series_data = fetch_xtream(base_url, b, "get_series_info", {"series_id": id}, ttl=CACHE_TTL_DETAILS)
            ep = series_data["episodes"][season][int(episode) - 1]
            series_name = series_data.get("info", {}).get("name", "Série")
            
            result = {
                "streams": [
                    {
                        "name": f"IPTV | {series_name}",
                        "url": f"{base_url}/series/{b['username']}/{b['password']}/{ep['id']}.{ep['container_extension']}",
                        "description": ep["info"].get("plot", ""),
                        "released": format_date(_get_release_date(ep["info"])),
                    }
                ]
            }

        elif type == "movie":
            film = fetch_xtream(base_url, b, "get_vod_info", {"vod_id": id},
                                ttl=CACHE_TTL_DETAILS)
            result = {
                "streams": [
                    {
                        "name": film["info"].get("name") or film["movie_data"].get("name", ""),
                        "url": f"{base_url}/movie/{b['username']}/{b['password']}/{id}.{film['movie_data']['container_extension']}",
                        "description": f"Ano: {film['info'].get('year', 'Desconhecido')} | {film['info'].get('plot', '')}",
                        "released": format_date(_get_release_date(film["info"])),
                    }
                ]
            }

        elif type == "tv":
            lives = fetch_xtream(base_url, b, "get_live_streams")

            if ":" in id:
                group_id = id.split(":")[1]
                group = agroup_channels(lives)
                for i in group:
                    if group[i]["id"] == group_id:
                        lives = group[i]["list"]
                        break
                result = {
                    "streams": [
                        {
                            "name": i["name"],
                            "url": f"{base_url}/live/{b['username']}/{b['password']}/{i['stream_id']}.m3u8",
                        }
                        for i in lives
                    ]
                }
            else:
                live = next(l for l in lives if l["stream_id"] == int(id))
                result = {
                    "streams": [
                        {
                            "name": live["name"],
                            "url": f"{base_url}/live/{b['username']}/{b['password']}/{id}.m3u8",
                        }
                    ]
                }

        response = jsonify(result)
        response.headers.add("Access-Control-Allow-Origin", "*")
        return response

    # ------------------------------------------------------------------
    # IMDB content — resolve via TMDB then match to Xtream
    # ------------------------------------------------------------------
    if type == "series":
        id, season, episode = id.split(":")

    lang = b.get("lang") or "pt-BR"

    # Fetch TMDB in configured language AND English simultaneously
    def _tmdb(language):
        return fetch_url(
            f"https://api.themoviedb.org/3/find/{id}",
            {"api_key": TMDB_API_KEY, "external_source": "imdb_id", "language": language},
            ttl=CACHE_TTL_TMDB,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_prog    = pool.submit(_tmdb, lang)
        f_prog_en = pool.submit(_tmdb, "en-US")

    program    = f_prog.result()
    program_en = f_prog_en.result()

    result = {"streams": []}

    if type == "series":
        name    = program["tv_results"][0]["name"]
        name_en = ((program_en or {}).get("tv_results") or [{}])[0].get("name", "")
        candidate_names = list(dict.fromkeys(filter(None, [name, name_en])))
        tmdb_year = extract_year(program["tv_results"][0].get("first_air_date", ""))

        all_series = fetch_xtream(base_url, b, "get_series")
        similar_items = [
            item for item in all_series
            if any(names_match(item["name"], c) for c in candidate_names)
        ]

        xtr = base_url.split("//")[1].split(".")[0]

        # Fetch all matching series details in parallel
        def _fetch_series_info(item):
            return item, fetch_xtream(base_url, b, "get_series_info",
                                      {"series_id": item["series_id"]}, ttl=CACHE_TTL_DETAILS)

        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {pool.submit(_fetch_series_info, item): item for item in similar_items}
            for future in as_completed(futures):
                item, sessions = future.result()
                if not (
                    year_matches(sessions.get("info", {}), tmdb_year, title=name)
                    and len(sessions["episodes"]) >= int(season)
                    and len(sessions["episodes"][season]) >= int(episode) - 1
                ):
                    continue
                pattern = re.compile(
                    rf"S0?{int(season)}E0?{int(episode)}(?!\d)", re.IGNORECASE
                )
                found = next(
                    (ep for ep in sessions["episodes"][season]
                     if pattern.search(ep.get("title", ""))),
                    None,
                )
                if not found:
                    found = sessions["episodes"][season][int(episode) - 1]
                result["streams"].append(
                    {
                        "name": found["title"],
                        "url": f"{base_url}/series/{b['username']}/{b['password']}/{found['id']}.{found['container_extension']}",
                        "description": found["info"].get("plot", ""),
                        "released": format_date(_get_release_date(found["info"])),
                        "behaviorHints": {"bingeGroup": f"{xtr}-{id}"},
                    }
                )

    else:
        name    = program["movie_results"][0]["title"]
        name_en = ((program_en or {}).get("movie_results") or [{}])[0].get("title", "")
        candidate_names = list(dict.fromkeys(filter(None, [name, name_en])))
        tmdb_year = extract_year(program["movie_results"][0].get("release_date", ""))
        tmdb_id   = str(program["movie_results"][0]["id"])

        all_vod = fetch_xtream(base_url, b, "get_vod_streams")
        similar_items = [
            item for item in all_vod
            if any(names_match(item["name"], c) for c in candidate_names)
        ]

        # Fetch all matching VOD details in parallel
        def _fetch_vod_info(item):
            return item, fetch_xtream(base_url, b, "get_vod_info",
                                      {"vod_id": item["stream_id"]}, ttl=CACHE_TTL_DETAILS)

        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {pool.submit(_fetch_vod_info, item): item for item in similar_items}
            for future in as_completed(futures):
                item, film = future.result()
                if (
                    year_matches(film["info"], tmdb_year, title=name)
                    and (not film["info"].get("tmdb_id") or str(film["info"]["tmdb_id"]) == tmdb_id)
                ):
                    result["streams"].append(
                        {
                            "name": item["name"],
                            "url": f"{base_url}/movie/{b['username']}/{b['password']}/{item['stream_id']}.{item['container_extension']}",
                            "description": film["info"].get("plot", ""),
                            "released": format_date(_get_release_date(film["info"])),
                        }
                    )

    logger.info("Stream result: %d streams for id=%s type=%s", len(result["streams"]), id, type)
    response = jsonify(result)
    response.headers.add("Access-Control-Allow-Origin", "*")
    return response


@app.route("/<hash>/data")
def show_data(hash):
    """Displays decoded configuration data for debugging purposes."""
    try:
        config_data = decode_hash(hash)
    except Exception:
        return "Invalid hash", 400
    return render_template("show_data.html", config=config_data)


@app.errorhandler(404)
def page_not_found(e):
    return render_template("404.html"), 404


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5002)
