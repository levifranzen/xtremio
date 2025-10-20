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
# Mimics a standard browser to avoid potential blocking
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


def normalize_string(s):
    """
    Normalizes a string by removing accents, converting to lowercase, and removing special characters.
    """
    if not isinstance(s, str):
        return ""
    # Remove accents and normalize to lowercase
    s = "".join(
        c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn"
    ).lower()
    # Remove non-alphanumeric characters and replace multiple spaces with one
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


@lru_cache(maxsize=128)
def get_cached_url(url, params, timeout=10):
    """
    Performs a cached GET request for the provided URL and parameters.
    Returns the response JSON or None in case of error.
    """
    try:
        response = http.get(
            url,
            params=dict(params),
            headers=hraders,
            follow_redirects=True,
            timeout=timeout,
        )
        logger.debug(
            "Response URL: %s, Status Code: %s", response.url, response.status_code
        )
        response.raise_for_status()
        return response.json()
    except RequestError as e:
        logger.error("Erro ao requisitar %s: %s", url, e)
        return None


def format_date(date_str):
    """
    Formats a date string in ISO 8601 format with 'Z' suffix.
    If the string is empty, returns the current date/time.
    If conversion is not possible, returns the original string.
    """
    try:
        if not date_str or date_str.strip() == "":
            return datetime.now().isoformat() + "Z"
        # Accepts dates in YYYY-MM-DD format
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
        # Remove common quality suffixes from the name
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

        # Update the grouping
        grouped_names[group_key]["list"].append(i)
        grouped_names[group_key]["id"] = hashlib.md5(group_key.encode()).hexdigest()
        if not grouped_names[group_key]["logo"] and i.get("stream_icon"):
            grouped_names[group_key]["logo"] = i["stream_icon"]
        if not grouped_names[group_key]["name"]:
            grouped_names[group_key]["name"] = name
    return grouped_names


def decode_hash(hash_str):
    """
    Decodes the hash, detecting whether it's base64 or Fernet.
    """
    try:
        # Try to decode as Fernet
        decoded = fernet.decrypt(hash_str.encode())
        return loads(decoded.decode("utf-8"))
    except (InvalidToken, ValueError):
        # If it fails, try base64
        try:
            try:
                return loads(b64decode(hash_str).decode("utf-8"))
            except UnicodeDecodeError:
                # Try latin1 if utf-8 fails
                return loads(b64decode(hash_str).decode("latin1"))
        except Exception as exc:
            raise ValueError("Invalid hash") from exc


def encode_hash(data: dict, use_fernet=False):
    """
    Encodes the dictionary as base64 or Fernet hash.
    """
    raw = dumps(data).encode("utf-8")
    if use_fernet:
        return fernet.encrypt(raw).decode()
    else:
        return b64encode(raw).decode()


@app.route("/encrypt", methods=["POST"])
def encrypt():
    """
    Endpoint to encrypt configs using Fernet.
    Receives JSON in the body and returns the encrypted hash.
    
    Returns:
        JSON response with encrypted hash or error message
    """
    try:
        data = request.get_json(force=True)
        encrypted = encode_hash(data, use_fernet=True)
        return jsonify({"hash": encrypted})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/")
@app.route("/configure")
def index():
    """
    Main configuration page endpoint.
    Renders the configuration form with user session information.
    
    Returns:
        Rendered HTML template for configuration
    """
    return render_template("config.html", config={})



@app.route("/<hash>/configure")
def config(hash):
    """
    Configuration page with pre-filled data from hash.
    Decodes the hash parameter and displays stored configuration.
    
    Args:
        hash: Encoded configuration data (base64 or Fernet)
        
    Returns:
        Rendered configuration template or error message
    """
    try:
        config_data = decode_hash(hash)
    except Exception:
        return "Invalid hash", 400
    return render_template("config.html", config=config_data)


@app.route("/favicon.ico")
def favicon():
    """
    Serves the favicon.ico file from the static folder.
    
    Returns:
        Favicon file
    """
    return send_from_directory(app.static_folder, "favicon.ico")


@app.route("/manifest.json")
def manifest():
    """
    Returns the base manifest for unconfigured addon.
    This manifest requires configuration before use.
    
    Returns:
        JSON manifest with basic addon information
    """
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
    Fetches server information and creates catalogs for movies, series, and TV channels.
    
    Args:
        hash: Encoded configuration containing server URL and credentials
        
    Returns:
        JSON manifest with server-specific catalogs and metadata
    """
    logger.info("Generating manifest for hash: %s", hash)
    hash = unquote(hash)
    try:
        b = decode_hash(hash)
    except Exception:
        return jsonify({"error": "Invalid hash"}), 400
    
    # Convert and validate the base URL
    base_url = convert_to_url(b["BaseURL"])
    xtr = base_url.split("//")[1].split(".")[0]
    name = b["name"] if b.get("name") else xtr + " - Xtremio"

    # Fetch server information to validate credentials
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
        logger.warning("Invalid credentials provided.")
        return jsonify({"error": "Invalid credentials"}), 401

    catalogs = []

    # Create movie catalog with categories
    films = get_cached_url(
        f"{base_url}/player_api.php",
        params=frozenset(
            {
                "username": b["username"],
                "password": b["password"],
                "action": "get_vod_categories",
            }.items()
        ),
    )

    cat = [film["category_name"] for film in films]

    catalogs.append(
        {
            "type": "movie",
            "id": xtr,
            "name": f"{name} - Movies",
            "extra": [
                {"name": "genre", "options": cat},
                {"name": "search"},
                {"name": "skip"},
            ],
        }
    )

    # Create series catalog with categories
    series = get_cached_url(
        f"{base_url}/player_api.php",
        params=frozenset(
            {
                "username": b["username"],
                "password": b["password"],
                "action": "get_series_categories",
            }.items()
        ),
    )

    cat = [serie["category_name"] for serie in series]

    catalogs.append(
        {
            "type": "series",
            "id": xtr,
            "name": f"{name} - Series",
            "extra": [
                {"name": "genre", "options": cat},
                {"name": "search"},
                {"name": "skip"},
            ],
        }
    )

    # Create TV channels catalog with categories
    tv = get_cached_url(
        f"{base_url}/player_api.php",
        params=frozenset(
            {
                "username": b["username"],
                "password": b["password"],
                "action": "get_live_categories",
            }.items()
        ),
    )

    cat = [tv["category_name"] for tv in tv]

    catalogs.append(
        {
            "type": "tv",
            "id": xtr,
            "name": f"{name} - TV",
            "extra": [
                {"name": "genre", "options": cat},
                {"name": "search"},
                {"name": "skip"},
            ],
        }
    )
    
    # Build description with account information
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
    Fetches detailed information including description, poster, rating, and episodes.
    
    Args:
        hash: Encoded server configuration
        type: Content type (movie, series, or tv)
        id: Content identifier (may include server prefix)
        
    Returns:
        JSON metadata object with content details
    """
    logger.info(
        "Processing meta request for hash: %s, type: %s, id: %s", hash, type, id
    )
    hash = unquote(hash)
    type = unquote(type)
    
    # Fix the split to get only the first ':'
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
    
    # Check if this is a new-style category ID
    if ":" in id:
        id = id.split(":")[1]
        cat_new = True

    # Return empty meta for IMDB IDs (external content)
    if "tt" in id:
        logger.debug("IMDB ID detected in meta request: %s", id)
        return jsonify({"meta": {}})
    elif xtr != base_url.split("//")[1].split(".")[0]:
        logger.warning(
            "Mismatch in XTR and BaseURL: XTR=%s, BaseURL=%s", xtr, b["BaseURL"]
        )
        return jsonify({"meta": {}})

    logger.info("Processing meta request for type: %s and id: %s", type, id)

    if type == "series":
        # Fetch series information including all episodes
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

        # Build video list with all episodes across all seasons
        videos = []
        for season in program["episodes"]:
            for episode in program["episodes"][season]:
                videos.append(
                    {
                        "id": f"{xtr}:{id}:{season}:{episode['episode_num']}",
                        "title": f"{episode['title']}",
                        "episode": episode["episode_num"],
                        "season": episode["season"],
                        "overview": episode["info"]["plot"]
                        if episode["info"].get("plot")
                        else "",
                        "released": format_date(
                            episode["info"]["releasedate"]
                            if episode["info"].get("releasedate")
                            else episode["info"]["releaseDate"]
                            if episode["info"].get("releaseDate")
                            else "",
                        ),
                        "thumbnail": episode["info"]["movie_image"]
                        if episode["info"].get("movie_image")
                        else program["info"]["cover"],
                    }
                )

        meta = {
            "id": f"{xtr}:{id}",
            "name": program["info"]["name"],
            "poster": program["info"]["cover"],
            "background": program["info"]["backdrop_path"][0]
            if program["info"]["backdrop_path"]
            else "",
            "description": program["info"]["plot"]
            if program["info"].get("plot")
            else "",
            "genre": program["info"]["genre"],
            "imdbRating": program["info"]["rating"],
            "released": format_date(
                program["info"]["releaseDate"]
                if program["info"].get("releaseDate")
                else program["info"]["releasedate"]
                if program["info"].get("releasedate")
                else "",
            ),
            "type": "series",
            "videos": videos,
        }

        return jsonify({"meta": meta})

    elif type == "movie":
        # Fetch movie information
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

        meta = {
            "id": f"{xtr}:{id}",
            "name": program["info"]["name"]
            if program["info"].get("name")
            else program["movie_data"]["name"],
            "poster": program["info"]["cover_big"]
            if program["info"].get("cover_big")
            else program["info"]["backdrop"]
            if program["info"].get("backdrop")
            else "",
            "background": program["info"]["backdrop_path"][0]
            if program["info"].get("backdrop_path") and program["info"]["backdrop_path"]
            else "",
            "description": program["info"]["plot"]
            if program["info"].get("plot")
            else "",
            "genre": program["info"]["genre"],
            "imdbRating": program["info"]["rating"],
            "released": format_date(
                program["info"]["releasedate"]
                if program["info"].get("releasedate")
                else program["info"]["releaseDate"]
                if program["info"].get("releaseDate")
                else "",
            ),
            "type": "movie",
        }
        
        # Add trailer if available
        if "youtube_trailer" in program["info"]:
            meta["trailer"] = {
                "source": program["info"]["youtube_trailer"],
                "type": "Trailer",
            }
        logger.info("Meta response generated for type: %s, id: %s", type, id)
        return jsonify({"meta": meta})

    elif type == "tv":
        # Fetch all live TV streams
        program = get_cached_url(
            f"{base_url}/player_api.php",
            params=frozenset(
                {
                    "username": b["username"],
                    "password": b["password"],
                    "action": "get_live_streams",
                }.items()
            ),
        )

        if cat_new:
            # Handle grouped channels (by normalized name)
            grouped_names = agroup_channels(program)
            for i in grouped_names:
                if grouped_names[i]["id"] == id:
                    meta = {
                        "id": f"{xtr}:ai:{grouped_names[i]['id']}",
                        "name": grouped_names[i]["name"],
                        "background": grouped_names[i]["logo"],
                        "type": "tv",
                    }
                    break
        else:
            # Handle individual channel
            id = id.replace("null", "")
            try:
                live_id = int(id)
            except ValueError:
                return jsonify({"meta": {}})

            # Build a quick lookup dictionary
            lives = {live["stream_id"]: live for live in program}

            if live_id not in lives:
                return jsonify({"meta": {}})

            live = lives[live_id]

            meta = {
                "id": f"{xtr}:{id}",
                "name": live["name"],
                "poster": live["stream_icon"],
                "background": live["stream_icon"],
                "type": "tv",
            }

        return jsonify({"meta": meta})


@app.route("/<hash>/catalog/<type>/<xtr>/search=<search>.json")
@app.route("/<hash>/catalog/<type>/<xtr>/genre=<genre>.json")
@app.route("/<hash>/catalog/<type>/<xtr>.json")
def catalog(hash, type, xtr, genre=None, search=None):
    """
    Returns a catalog of content items (movies, series, or TV channels).
    Supports filtering by genre and searching by name.
    
    Args:
        hash: Encoded server configuration
        type: Content type (movie, series, or tv)
        xtr: Server identifier
        genre: Optional genre filter
        search: Optional search query
        
    Returns:
        JSON catalog with list of content metadata
    """
    logger.info(
        "Catalog request: hash=%s, type=%s, xtr=%s, genre=%s, search=%s",
        hash,
        type,
        xtr,
        genre,
        search,
    )
    hash = unquote(hash)
    type = unquote(type)
    xtr = unquote(xtr)
    genre = unquote(genre).replace("genre=", "") if genre else None
    search = unquote(search).replace("search=", "") if search else None
    try:
        b = decode_hash(hash)
    except Exception:
        return jsonify({"metas": []})
    base_url = convert_to_url(b["BaseURL"])

    # Validate server identifier matches
    if xtr != base_url.split("//")[1].split(".")[0]:
        logger.warning(
            "XTR mismatch: %s != %s", xtr, base_url.split("//")[1].split(".")[0]
        )
        return jsonify({"metas": []})

    # Map content types to API actions
    types = {
        "movie": "vod",
        "series": "series",
        "tv": "live",
    }

    if genre:
        # Filter by category/genre
        catalog_data = get_cached_url(
            f"{base_url}/player_api.php",
            params=frozenset(
                {
                    "username": b["username"],
                    "password": b["password"],
                    "action": f"get_{types[type]}_categories",
                }.items()
            ),
        )

        # Find the category ID for the selected genre
        category_id = [item for item in catalog_data if item["category_name"] == genre][0][
            "category_id"
        ]

        # Fetch content for this category
        all_content = get_cached_url(
            f"{base_url}/player_api.php",
            params=frozenset(
                {
                    "username": b["username"],
                    "password": b["password"],
                    "action": f"get_{types[type]}"
                    if type == "series"
                    else f"get_{types[type]}_streams",
                    "category_id": category_id,
                }.items()
            ),
        )

    elif search:
        # Search across all content
        series_data = get_cached_url(
            f"{base_url}/player_api.php",
            params=frozenset(
                {
                    "username": b["username"],
                    "password": b["password"],
                    "action": f"get_{types[type]}"
                    if type == "series"
                    else f"get_{types[type]}_streams",
                }.items()
            ),
        )

        # Filter by normalized search term
        all_content = [
            item for item in series_data if re.search(search, normalize_string(item["name"]))
        ]

    else:
        # Get all content (no filters)
        try:
            all_content = get_cached_url(
                f"{base_url}/player_api.php",
                params=frozenset(
                    {
                        "username": b["username"],
                        "password": b["password"],
                        "action": f"get_{types[type]}"
                        if type == "series"
                        else f"get_{types[type]}_streams",
                    }.items()
                ),
            )
        except Exception:
            all_content = []
            
    metas = []
    
    if type != "tv":
        # Limit movies and series to 50 items for performance
        all_content = all_content[:50]
        for item in all_content:
            metas.append(
                {
                    "id": f"{xtr}:{item['series_id']}"
                    if type == "series"
                    else f"{xtr}:{item['stream_id']}",
                    "name": item["name"],
                    "poster": item["cover"] if "cover" in item else item["stream_icon"],
                    "posterShape": "poster",
                    "type": type,
                    "releaseInfo": format_date(item["releasedate"])
                    if "releasedate" in item
                    else None,
                    "imdbRating": item["rating"],
                }
            )
    else:
        # For TV channels, group by normalized name
        grouped_names = agroup_channels(all_content)
        for itens in grouped_names:
            metas.append(
                {
                    "id": f"{xtr}:ai:{grouped_names[itens]['id']}",
                    "name": grouped_names[itens]["name"],
                    "poster": grouped_names[itens]["logo"],
                    "posterShape": "square",
                    "type": "tv",
                    "description": "\n".join(
                        [i["name"] for i in grouped_names[itens]["list"]]
                    ),
                }
            )
    logger.info("Catalog response generated with %d items.", len(metas))
    print("Catalog:", metas)

    return jsonify({"metas": metas})


@app.route("/<hash>/stream/<type>/<id>.json")
def stream(hash, type, id):
    """
    Provides stream URLs for playback of movies, series episodes, or TV channels.
    Handles both Xtream content and IMDB-based external lookups via TMDB.
    
    Args:
        hash: Encoded server configuration
        type: Content type (movie, series, or tv)
        id: Content identifier (may include season/episode for series)
        
    Returns:
        JSON with stream URLs and metadata
    """
    hash = unquote(hash)
    type = unquote(type)
    id = unquote(id)
    result = {}

    try:
        b = decode_hash(hash)
    except Exception:
        return jsonify({"streams": []})
    base_url = convert_to_url(b["BaseURL"])

    # Handle Xtream-native content (not IMDB)
    if not id.startswith("tt"):
        xtr, id = id.split(":", 1)
        
        if type == "series":
            # Parse series ID, season, and episode number
            id, season, episode = id.split(":")
            session = get_cached_url(
                f"{base_url}/player_api.php",
                params=frozenset(
                    {
                        "username": b["username"],
                        "password": b["password"],
                        "action": "get_series_info",
                        "series_id": id,
                    }.items()
                ),
            )["episodes"][season][int(episode) - 1]
            
            result = {
                "streams": [
                    {
                        "name": session["title"],
                        "url": f"{base_url}/series/{b['username']}/{b['password']}/{session['id']}.{session['container_extension']}",
                        "description": session["info"]["plot"]
                        if session["info"].get("plot")
                        else "",
                        "released": format_date(
                            session["info"]["releasedate"]
                            if session["info"].get("releasedate")
                            else session["info"]["releaseDate"]
                            if session["info"].get("releaseDate")
                            else "",
                        ),
                    }
                ]
            }
            
        elif type == "movie":
            # Fetch movie information and build stream URL
            film = get_cached_url(
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
            result = {
                "streams": [
                    {
                        "name": film["info"]["name"]
                        if film["info"].get("name")
                        else film["movie_data"]["name"]
                        if film["movie_data"].get("name")
                        else "",
                        "url": f"{base_url}/movie/{b['username']}/{b['password']}/{id}.{film['movie_data']['container_extension']}",
                        "description": film["info"]["plot"]
                        if film["info"].get("plot")
                        else "",
                        "released": format_date(
                            film["info"]["releasedate"]
                            if film["info"].get("releasedate")
                            else film["info"]["releaseDate"]
                            if film["info"].get("releaseDate")
                            else "",
                        ),
                    }
                ]
            }
            
        elif type == "tv":
            # Fetch all live streams
            lives = get_cached_url(
                f"{base_url}/player_api.php",
                params=frozenset(
                    {
                        "username": b["username"],
                        "password": b["password"],
                        "action": "get_live_streams",
                    }.items()
                ),
            )
            print(id)
            
            if ":" in id:
                # Handle grouped channels
                id = id.split(":")[1]
                group = agroup_channels(lives)
                for i in group:
                    if group[i]["id"] == id:
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
                # Handle single channel
                live = [live for live in lives if live["stream_id"] == int(id)][0]
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

    # Handle IMDB content - lookup via TMDB and match to Xtream content
    if type == "series":
        id, season, episode = id.split(":")

    # Query TMDB to find the content details
    program = get_cached_url(
        f"https://api.themoviedb.org/3/find/{id}",
        params=frozenset(
            {
                "api_key": TMDB_API_KEY,
                "external_source": "imdb_id",
                "language": b["lang"] if b.get("lang") else "pt-BR",
            }.items()
        ),
    )

    print(program)

    result = {"streams": []}

    if type == "series":
        # Get series name from TMDB
        name = program["tv_results"][0]["name"]
        
        # Fetch all series from Xtream
        all_series = get_cached_url(
            f"{base_url}/player_api.php",
            params=frozenset(
                {
                    "username": b["username"],
                    "password": b["password"],
                    "action": "get_series",
                }.items()
            ),
        )

        # Find series with similar names
        normalized_name = normalize_string(name)
        similar_items = [
            item
            for item in all_series
            if re.search(normalized_name, normalize_string(item["name"]))
        ]

        # For each similar series, check if it has the requested episode
        for item in similar_items:
            sessions = get_cached_url(
                f"{base_url}/player_api.php",
                params=frozenset(
                    {
                        "username": b["username"],
                        "password": b["password"],
                        "action": "get_series_info",
                        "series_id": item["series_id"],
                    }.items()
                ),
            )

            # Check if this series has the requested season and episode
            if (
                len(sessions["episodes"]) >= int(season)
                and len(sessions["episodes"][season]) >= int(episode) - 1
            ):
                # Try to match episode by S##E## pattern
                pattern = re.compile(
                    rf"S0?{int(season)}E0?{int(episode)}(?!\d)", re.IGNORECASE
                )
                found = None
                for ep in sessions["episodes"][season]:
                    if pattern.search(ep.get("title", "")):
                        found = ep
                        break
                if not found:
                    # Fall back to index-based matching
                    found = sessions["episodes"][season][int(episode) - 1]
                session = found
                xtr = base_url.split("//")[1].split(".")[0]
                result["streams"].append(
                    {
                        "name": session["title"],
                        "url": f"{base_url}/series/{b['username']}/{b['password']}/{session['id']}.{session['container_extension']}",
                        "description": session["info"]["plot"]
                        if session["info"].get("plot")
                        else "",
                        "released": format_date(
                            session["info"]["releasedate"]
                            if session["info"].get("releasedate")
                            else session["info"]["releaseDate"]
                            if session["info"].get("releaseDate")
                            else "",
                        ),
                        "behaviorHints": {
                            "bingeGroup": f"{xtr}-{id}",
                        },
                    }
                )

    else:
        # Handle IMDB movies
        name = program["movie_results"][0]["title"]
        
        # Fetch all VOD from Xtream
        all_vod = get_cached_url(
            f"{base_url}/player_api.php",
            params=frozenset(
                {
                    "username": b["username"],
                    "password": b["password"],
                    "action": "get_vod_streams",
                }.items()
            ),
        )

        # Find movies with similar names
        normalized_name = normalize_string(name)
        similar_items = [
            item
            for item in all_vod
            if re.search(normalized_name, normalize_string(item["name"]))
        ]

        # For each similar movie, verify TMDB ID match
        for item in similar_items:
            film = get_cached_url(
                f"{base_url}/player_api.php",
                params=frozenset(
                    {
                        "username": b["username"],
                        "password": b["password"],
                        "action": "get_vod_info",
                        "vod_id": item["stream_id"],
                    }.items()
                ),
            )

            # Match by TMDB ID if available
            if not film["info"].get("tmdb_id") or str(film["info"]["tmdb_id"]) == str(
                program["movie_results"][0]["id"]
            ):
                result["streams"].append(
                    {
                        "name": item["name"],
                        "url": f"{base_url}/movie/{b['username']}/{b['password']}/{item['stream_id']}.{item['container_extension']}",
                        "description": film["info"]["plot"]
                        if film["info"].get("plot")
                        else "",
                        "released": format_date(
                            film["info"]["releasedate"]
                            if film["info"].get("releasedate")
                            else film["info"]["releaseDate"]
                            if film["info"].get("releaseDate")
                            else "",
                        ),
                    }
                )

    print(result)
    response = jsonify(result)
    response.headers.add("Access-Control-Allow-Origin", "*")
    return response


@app.route("/<hash>/data")
def show_data(hash):
    """
    Displays decoded configuration data for debugging purposes.
    Shows the raw configuration stored in the hash.
    
    Args:
        hash: Encoded configuration data
        
    Returns:
        Rendered HTML template with configuration details or error
    """
    try:
        config_data = decode_hash(hash)
    except Exception:
        return "Invalid hash", 400

    return render_template("show_data.html", config=config_data)


# Handler for 404 error
@app.errorhandler(404)
def page_not_found(e):
    """
    Custom 404 error handler.
    Renders a custom template for page not found errors.
    
    Args:
        e: The error object
        
    Returns:
        Rendered 404 template with 404 status code
    """
    return render_template("404.html"), 404


if __name__ == "__main__":
    # Run Flask development server
    # Listen on all interfaces (0.0.0.0) on port 5002
    app.run(host="0.0.0.0", port=5002)
