"""
This module implements a Flask server to provide an API that interacts with Xtream servers.
It includes endpoints for configuration, manifest generation, metadata, catalogs, and streams.
"""

import hashlib
import logging
import re
import unicodedata
from base64 import b64decode, b64encode
from collections import defaultdict
from datetime import datetime
from functools import lru_cache
from json import dumps, loads
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

# Read sensitive keys from environment
FERNET_KEY = os.environ.get("FERNET_KEY")
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")

if not FERNET_KEY:
    logging.warning("FERNET_KEY not found in environment. Generating temporary key.")
    FERNET_KEY = Fernet.generate_key().decode()

try:
    fernet = Fernet(FERNET_KEY.encode() if isinstance(FERNET_KEY, str) else FERNET_KEY)
except Exception as e:
    logging.error("Invalid FERNET_KEY: %s. Generating temporary key.", e)
    FERNET_KEY = Fernet.generate_key()
    fernet = Fernet(FERNET_KEY)


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


@lru_cache(maxsize=128)
def get_cached_url(url, params, timeout=10):
    try:
        response = http.get(
            url,
            params=dict(params),
            headers=hraders,
            follow_redirects=True,
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()
    except RequestError as e:
        logger.error("Erro ao requisitar %s: %s", url, e)
        return None


def format_date(date_str):
    try:
        if not date_str or date_str.strip() == "":
            return datetime.now().isoformat() + "Z"
        return datetime.strptime(date_str, "%Y-%m-%d").isoformat() + "Z"
    except Exception as e:
        return date_str


@lru_cache(maxsize=256)
def convert_to_url(url):
    try:
        parsed_url = urlparse(url)
        netloc = parsed_url.netloc.split(":")
        encoded_netloc = idna_encode(netloc[0]).decode("utf-8")
        if len(netloc) > 1:
            encoded_netloc += f":{netloc[1]}"
        return urlunparse(parsed_url._replace(netloc=encoded_netloc))
    except Exception as e:
        return url


def agroup_channels(channels: list) -> dict:
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
    raw = dumps(data).encode("utf-8")
    if use_fernet:
        return fernet.encrypt(raw).decode()
    else:
        return b64encode(raw).decode()


@app.route("/encrypt", methods=["POST"])
def encrypt():
    try:
        data = request.get_json(force=True)
        encrypted = encode_hash(data, use_fernet=True)
        return jsonify({"hash": encrypted})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/")
@app.route("/configure")
def index():
    return render_template("config.html", config={})


@app.route("/<hash>/configure")
def config(hash):
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
    hash = unquote(hash)
    try:
        b = decode_hash(hash)
    except Exception:
        return jsonify({"error": "Invalid hash"}), 400
    
    base_url = convert_to_url(b["BaseURL"])
    xtr = base_url.split("//")[1].split(".")[0]
    name = b["name"] if b.get("name") else xtr + " - Xtremio"

    info = get_cached_url(
        f"{base_url}/player_api.php",
        params=frozenset(
            {
                "username": b["username"],
                "password": b["password"],
            }.items()
        ),
    )

    if not info:
        return jsonify({"error": "Invalid credentials"}), 401

    catalogs = []
    for c_type, action in [("movie", "vod"), ("series", "series"), ("tv", "live")]:
        cats = get_cached_url(
            f"{base_url}/player_api.php",
            params=frozenset(
                {
                    "username": b["username"],
                    "password": b["password"],
                    "action": f"get_{action}_categories",
                }.items()
            ),
        )
        genre_options = [c["category_name"] for c in cats] if cats else []
        catalogs.append(
            {
                "type": c_type,
                "id": xtr,
                "name": f"{name} - {c_type.capitalize()}",
                "extra": [
                    {"name": "genre", "options": genre_options},
                    {"name": "search"},
                    {"name": "skip"},
                ],
            }
        )
    
    description = (
        f"Hello {b['username']}!\n",
        "Server info:\n",
        f"Server URL: {base_url}\n",
        f"Max connections: {info['user_info']['max_connections']}\n",
        f"Account status: {info['user_info']['status']}\n",
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
    
    if "tt" in id:
        return jsonify({"meta": {}})

    if type == "series":
        program = get_cached_url(
            f"{base_url}/player_api.php",
            params=frozenset(
                {
                    "username": b["username"],
                    "password": b["password"],
                    "action": "get_series_info",
                    "series_id": id,
                }.items()
            ),
        )

        videos = []
        for season in program.get("episodes", {}):
            for episode in program["episodes"][season]:
                videos.append(
                    {
                        "id": f"{xtr}:{id}:{season}:{episode['episode_num']}",
                        "title": f"{episode['title']}",
                        "episode": episode["episode_num"],
                        "season": episode["season"],
                        "released": format_date(episode["info"].get("releasedate") or episode["info"].get("releaseDate") or ""),
                        "thumbnail": episode["info"].get("movie_image") or program["info"].get("cover"),
                    }
                )

        meta_data = {
            "id": f"{xtr}:{id}",
            "name": program["info"]["name"],
            "poster": program["info"]["cover"],
            "type": "series",
            "videos": videos,
        }
        return jsonify({"meta": meta_data})

    elif type == "movie":
        program = get_cached_url(
            f"{base_url}/player_api.php",
            params=frozenset(
                {
                    "username": b["username"],
                    "password": b["password"],
                    "action": "get_vod_info",
                    "vod_id": id,
                }.items()
            ),
        )
        meta_data = {
            "id": f"{xtr}:{id}",
            "name": program["info"].get("name") or program["movie_data"]["name"],
            "poster": program["info"].get("cover_big") or program["info"].get("backdrop") or "",
            "type": "movie",
        }
        return jsonify({"meta": meta_data})
    
    return jsonify({"meta": {}})


@app.route("/<hash>/catalog/<type>/<xtr>/search=<search>.json")
@app.route("/<hash>/catalog/<type>/<xtr>/genre=<genre>.json")
@app.route("/<hash>/catalog/<type>/<xtr>.json")
def catalog(hash, type, xtr, genre=None, search=None):
    hash = unquote(hash)
    type = unquote(type)
    genre = unquote(genre).replace("genre=", "") if genre else None
    search = unquote(search).replace("search=", "") if search else None
    
    try:
        b = decode_hash(hash)
    except Exception:
        return jsonify({"metas": []})
        
    base_url = convert_to_url(b["BaseURL"])
    types_map = {"movie": "vod", "series": "series", "tv": "live"}

    params = {"username": b["username"], "password": b["password"]}
    
    if genre:
        cats = get_cached_url(f"{base_url}/player_api.php", params=frozenset({**params, "action": f"get_{types_map[type]}_categories"}.items()))
        category_id = next((item["category_id"] for item in cats if item["category_name"] == genre), None)
        action = f"get_{types_map[type]}" if type == "series" else f"get_{types_map[type]}_streams"
        all_content = get_cached_url(f"{base_url}/player_api.php", params=frozenset({**params, "action": action, "category_id": category_id}.items()))
    elif search:
        action = f"get_{types_map[type]}" if type == "series" else f"get_{types_map[type]}_streams"
        data = get_cached_url(f"{base_url}/player_api.php", params=frozenset({**params, "action": action}.items()))
        all_content = [item for item in data if re.search(search, normalize_string(item["name"]))]
    else:
        action = f"get_{types_map[type]}" if type == "series" else f"get_{types_map[type]}_streams"
        all_content = get_cached_url(f"{base_url}/player_api.php", params=frozenset({**params, "action": action}.items())) or []

    metas = []
    if type != "tv":
        for item in all_content[:60]:
            metas.append({
                "id": f"{xtr}:{item.get('series_id') or item.get('stream_id')}",
                "name": item["name"],
                "poster": item.get("cover") or item.get("stream_icon"),
                "type": type,
                "releaseInfo": item.get("year") or (item.get("releasedate")[:4] if item.get("releasedate") else None)
            })
    else:
        grouped = agroup_channels(all_content)
        for g in grouped:
            metas.append({
                "id": f"{xtr}:ai:{grouped[g]['id']}",
                "name": grouped[g]["name"],
                "poster": grouped[g]["logo"],
                "type": "tv"
            })
            
    return jsonify({"metas": metas})


@app.route("/<hash>/stream/<type>/<id>.json")
def stream(hash, type, id):
    hash = unquote(hash)
    type = unquote(type)
    id = unquote(id)
    
    try:
        b = decode_hash(hash)
    except Exception:
        return jsonify({"streams": []})
        
    base_url = convert_to_url(b["BaseURL"])

    # 1. LÓGICA NATIVA XTREAM (ID que não é IMDB)
    if not id.startswith("tt"):
        xtr, content_id = id.split(":", 1)
        if type == "series":
            cid, season, episode = content_id.split(":")
            info = get_cached_url(f"{base_url}/player_api.php", params=frozenset({"username": b["username"], "password": b["password"], "action": "get_series_info", "series_id": cid}.items()))
            if info and "episodes" in info and season in info["episodes"]:
                eps = info["episodes"][season]
                if len(eps) >= int(episode):
                    ep = eps[int(episode) - 1]
                    return jsonify({"streams": [{"name": ep["title"], "url": f"{base_url}/series/{b['username']}/{b['password']}/{ep['id']}.{ep['container_extension']}"}]})
        
        elif type == "movie":
            film = get_cached_url(f"{base_url}/player_api.php", params=frozenset({"username": b["username"], "password": b["password"], "action": "get_vod_info", "vod_id": content_id}.items()))
            if film and "info" in film:
                return jsonify({"streams": [{"name": film["info"].get("name") or film["movie_data"].get("name"), "url": f"{base_url}/movie/{b['username']}/{b['password']}/{content_id}.{film['movie_data']['container_extension']}"}]})
        return jsonify({"streams": []})

    # 2. LÓGICA DE BUSCA INTELIGENTE POR IMDB (tt...)
    if type == "series":
        try:
            imdb_id, season, episode = id.split(":")
        except ValueError:
            return jsonify({"streams": []})
    else:
        imdb_id = id

    # Obter Metadados do TMDB para coletar Nome (PT/Original) e Ano
    program = get_cached_url(
        f"https://api.themoviedb.org/3/find/{imdb_id}",
        params=frozenset({"api_key": TMDB_API_KEY, "external_source": "imdb_id", "language": b.get("lang", "pt-BR")}.items()),
    )

    if not program:
        return jsonify({"streams": []})

    target_name = ""
    target_original_name = ""
    target_year = ""
    
    if type == "series" and program.get("tv_results"):
        res = program["tv_results"][0]
        target_name = res.get("name", "")
        target_original_name = res.get("original_name", "")
        target_year = res.get("first_air_date", "")[:4]
    elif type == "movie" and program.get("movie_results"):
        res = program["movie_results"][0]
        target_name = res.get("title", "")
        target_original_name = res.get("original_title", "")
        target_year = res.get("release_date", "")[:4]

    # Normalizamos os alvos de busca
    norm_target = normalize_string(target_name)
    norm_target_orig = normalize_string(target_original_name)
    
    result = {"streams": []}

    # Busca no Provider (Series ou Movies)
    if type == "series":
        all_items = get_cached_url(f"{base_url}/player_api.php", params=frozenset({"username": b["username"], "password": b["password"], "action": "get_series"}.items())) or []
        
        for item in all_items:
            item_name_norm = normalize_string(item.get("name", ""))
            
            # FILTRO 1: Nome (Traduzido ou Original)
            name_match = (norm_target and norm_target in item_name_norm) or \
                         (norm_target_orig and norm_target_orig in item_name_norm)
            
            if name_match:
                # FILTRO 2: Ano (Metadado Secundário)
                item_year = str(item.get("year", ""))
                if target_year and item_year and item_year != "None":
                    if item_year != target_year:
                        continue # Pula se os anos forem divergentes

                # Se passou pelos filtros, busca as temporadas
                sessions = get_cached_url(f"{base_url}/player_api.php", params=frozenset({"username": b["username"], "password": b["password"], "action": "get_series_info", "series_id": item["series_id"]}.items()))
                if sessions and "episodes" in sessions and season in sessions["episodes"]:
                    eps = sessions["episodes"][season]
                    # Busca por padrão SxxExx no título ou por índice
                    pattern = re.compile(rf"S0?{int(season)}E0?{int(episode)}(?!\d)", re.IGNORECASE)
                    found = next((e for e in eps if pattern.search(e.get("title", ""))), None)
                    if not found and len(eps) >= int(episode):
                        found = eps[int(episode) - 1]
                    
                    if found:
                        result["streams"].append({
                            "name": f"ST | {item['name']}",
                            "url": f"{base_url}/series/{b['username']}/{b['password']}/{found['id']}.{found['container_extension']}",
                            "description": f"Ano: {item_year}" if item_year else ""
                        })

    else: # Movies
        all_items = get_cached_url(f"{base_url}/player_api.php", params=frozenset({"username": b["username"], "password": b["password"], "action": "get_vod_streams"}.items())) or []
        
        for item in all_items:
            item_name_norm = normalize_string(item.get("name", ""))
            
            # FILTRO 1: Nome (Traduzido ou Original)
            name_match = (norm_target and norm_target in item_name_norm) or \
                         (norm_target_orig and norm_target_orig in item_name_norm)
            
            if name_match:
                # FILTRO 2: Ano (Metadado Secundário)
                item_year = str(item.get("year", ""))
                if target_year and item_year and item_year != "None":
                    if item_year != target_year:
                        continue
                
                result["streams"].append({
                    "name": f"ST | {item['name']}",
                    "url": f"{base_url}/movie/{b['username']}/{b['password']}/{item['stream_id']}.{item['container_extension']}",
                    "description": f"Ano: {item_year}" if item_year else ""
                })

    response = jsonify(result)
    response.headers.add("Access-Control-Allow-Origin", "*")
    return response


@app.route("/<hash>/data")
def show_data(hash):
    try:
        config_data = decode_hash(hash)
        return render_template("show_data.html", config=config_data)
    except Exception:
        return "Invalid hash", 400


@app.errorhandler(404)
def page_not_found(e):
    return render_template("404.html"), 404


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5002)
