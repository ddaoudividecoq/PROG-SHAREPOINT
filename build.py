#!/usr/bin/env python3
"""
Recupere les concerts des 3 salles et ecrit docs/events.json.

Sources :
- Accor Arena ET adidas arena : le CMS commun Directus (accorarena.com/api-cms).
  La salle est determinee par le champ "room" de chaque evenement.
  -> titre, seances horodatees, image de presentation.
- adidas arena, surcouche depuis l'API front adidas (adidasarena.directus.app) :
    * image : remplacee par custom_presentation_image UNIQUEMENT s'il existe,
      sinon on garde l'image du CMS commun.
    * lien : le generated_url par evenement (absent du CMS commun).
- Bataclan : son API (visuels et liens deja bons).
Images manquantes : Spotify (si configure) -> iTunes -> Deezer -> vignette de repli.
L'historique est conserve : rien n'est jamais supprime.
"""

import os
import re
import json
import unicodedata
import datetime as dt
import requests

# --- Auth ---
BATACLAN_AUTH = os.environ["BATACLAN_AUTH"]            # header hubber-authorization
SPOTIFY_CLIENT_ID     = os.environ.get("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")

# --- CMS commun (Accor + adidas) ---
CMS_EVENTS = "https://www.accorarena.com/api-cms/items/event"
CMS_ASSETS = "https://www.accorarena.com/api-cms/assets/"
ACCOR_PROG  = "https://www.accorarena.com/fr/programmation"
ADIDAS_PROG = "https://www.adidasarena.com/programmation"
# Pages par evenement (hors billetterie)
ACCOR_FRONT    = "https://www.accorarena.com/fr/programmation/"   # + {display_url}
BATACLAN_FRONT = "https://www.bataclan.fr/evenement/"            # + {slug}_{AAAA-MM-JJ}

# --- Surcouche adidas (API front : image custom + lien par evenement) ---
ADIDAS_OVERLAY        = "https://adidasarena.directus.app/items/Programmations"
ADIDAS_OVERLAY_ASSETS = "https://adidasarena.directus.app/assets/"

# --- Bataclan ---
BATACLAN_URL = "https://billetterie.bataclan.fr/fr/hubber/catalog/resource/ticketing?_format=json"

OUT = "docs/events.json"
UA = {"User-Agent": "concerts-sync/1.0"}

MONTHS = {"janvier": 1, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
          "juillet": 7, "aout": 8, "septembre": 9, "octobre": 10,
          "novembre": 11, "decembre": 12}


def norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s.lower()).strip()


def clean_query(title):
    t = title or ""
    for sep in [" - ", " \u2013 ", " \u2014 ", " | ", " : "]:
        if sep in t:
            t = t.split(sep)[0]
    return t.strip()


def iso_ymd(iso):
    try:
        y, m, d = iso[:10].split("-")
        return (int(y), int(m), int(d))
    except Exception:
        return None


MONTHS_SORTED = {"".join(sorted(k)): v for k, v in MONTHS.items()}


def cms_ymd(s):
    m = re.search(r"(\d{1,2})\s+([a-z]+)\s+(\d{4})", norm(s))
    if not m:
        return None
    word = m.group(2)
    # tolerant aux fautes de frappe par interversion (ex. "ocotbre" -> "octobre")
    mon = MONTHS.get(word) or MONTHS_SORTED.get("".join(sorted(word)))
    if not mon:
        return None
    return (int(m.group(3)), mon, int(m.group(1)))


# ===========================================================================
# CMS commun : Accor + adidas
# ===========================================================================
def _cms_asset(f):
    if not isinstance(f, dict):
        return ""
    if f.get("private_hash"):
        return CMS_ASSETS + f["private_hash"]
    data = f.get("data") or {}
    if data.get("asset_url"):
        return "https://www.accorarena.com" + data["asset_url"]
    return ""


def _iso(s):
    return s.replace(" ", "T") if s else None


def _venue_from_room(ev):
    room = ev.get("room") if isinstance(ev.get("room"), dict) else {}
    loc = ev.get("location") if isinstance(ev.get("location"), dict) else {}
    r = norm(room.get("full_name") or ev.get("room_display_title")
             or room.get("short_name") or loc.get("full_name") or "")
    if "adidas" in r:
        return "adidas arena", "adidas"
    if "accor" in r:
        return "Accor Arena", "accor"
    return None, None


def is_parking(title):
    return "parking" in norm(title).split()


def parse_cms_event(ev):
    venue, slug = _venue_from_room(ev)
    if not venue:
        return []
    title = (ev.get("display_title") or ev.get("artist_reference") or "").strip()
    if not title or is_parking(title):        # on exclut les emplacements de parking
        return []
    image = (_cms_asset(ev.get("presentation_event"))
             or _cms_asset(ev.get("list_image"))
             or _cms_asset(ev.get("logo")))
    cms_slug = ev.get("display_url") or ""        # slug canonique de la fiche editee
    if venue == "Accor Arena":
        # URL = {slug}--{5 premiers caracteres de l'uuid} ; la surcouche adidas
        # posera, elle, son propre lien.
        uid = (ev.get("uuid") or "")
        if cms_slug and uid:
            link = f"{ACCOR_FRONT}{cms_slug}--{uid[:5]}"
        elif cms_slug:
            link = ACCOR_FRONT + cms_slug
        else:
            link = ACCOR_PROG
    else:
        link = ADIDAS_PROG
    sessions = [s for s in (ev.get("sessions") or [])
                if s.get("status") == "published" and s.get("date")]
    if not sessions and ev.get("start"):
        sessions = [{"id": 0, "date": ev.get("start")}]
    out = []
    for s in sessions:
        out.append({
            "id": f"{slug}-{ev.get('id')}-{s.get('id')}",
            "title": title,
            "date": _iso(s.get("date")),
            "venue": venue,
            "image": image,
            "link": link,
            "cms_slug": cms_slug,
            "cancelled": _cms_session_cancelled(s),
        })
    return out


def _cms_session_cancelled(s):
    """Une seance est annulee si sa date d'annonce d'annulation est renseignee.
    (champ 'cancelled_date_announcement' du CMS ; A CONFIRMER sur un cas reel)."""
    return bool(s.get("cancelled_date_announcement"))


def get_accor_token():
    """Jeton pour le CMS commun : statique si fourni, sinon via email/mot de passe."""
    tok = os.environ.get("ACCOR_CMS_TOKEN", "")
    if tok:
        return tok
    email = os.environ.get("ACCOR_CMS_EMAIL", "")
    pwd = os.environ.get("ACCOR_CMS_PASSWORD", "")
    if not (email and pwd):
        return ""
    base = "https://www.accorarena.com/api-cms"
    # Directus 8 : /auth/authenticate -> data.token ; Directus 9+ : /auth/login -> data.access_token
    for path in ("/auth/authenticate", "/auth/login"):
        try:
            r = requests.post(base + path, json={"email": email, "password": pwd},
                              headers=UA, timeout=15)
            if r.status_code >= 400:
                continue
            data = r.json().get("data", {}) or {}
            t = data.get("token") or data.get("access_token")
            if t:
                return t
        except Exception as e:
            print(f"[Accor auth] {path} echec: {e}")
    print("[Accor auth] aucun jeton obtenu depuis les identifiants")
    return ""


def fetch_cms_events():
    params = {
        "limit": -1,
        "fields": ("id,uuid,status,display_title,artist_reference,display_url,start,"
                   "room.full_name,room.short_name,room_display_title,location.full_name,"
                   "presentation_event.*,list_image.*,logo.*,sessions.*"),
    }
    headers = dict(UA)
    token = get_accor_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    else:
        print("[CMS commun] aucun jeton fourni (ACCOR_CMS_TOKEN ou ACCOR_CMS_EMAIL/PASSWORD)")
    try:
        r = requests.get(CMS_EVENTS, params=params, headers=headers, timeout=90)
        r.raise_for_status()
        rows = r.json().get("data", [])
    except Exception as e:
        print(f"[CMS commun] echec: {e}")
        return []
    out = []
    for ev in rows:
        if ev.get("status") and ev.get("status") != "published":
            continue
        out += parse_cms_event(ev)
    return out


# ===========================================================================
# Surcouche adidas (API front : custom_presentation_image + generated_url)
# ===========================================================================
def _overlay_asset(field):
    if not field:
        return ""
    if isinstance(field, dict):
        fid = field.get("id") or field.get("filename_disk") or field.get("filename_download")
    else:
        fid = field
    return (ADIDAS_OVERLAY_ASSETS + str(fid)) if fid else ""


ADIDAS_FRONT = "https://www.adidasarena.com/programmation/"


def slugify(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("&", " and ")           # "&" -> "and"
    s = s.replace("'", "").replace("’", "")   # apostrophes supprimees (pas de tiret)
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def _adidas_front_link(name, id_event, generated_url):
    """Lien de la page programmation adidas.

    Source principale : generated_url stocke (c'est l'URL reelle de la page, avec
    le slug choisi par l'editeur, ex. 'ovo', 'ares'), nettoye du '/' final.
    Jamais la billetterie. En dernier recours seulement, on reconstruit via le nom.
    """
    g = (generated_url or "").strip().rstrip("/")
    if "/programmation/" in g:
        return g
    if id_event and name:
        return f"{ADIDAS_FRONT}{slugify(name)}--{id_event}"
    return ""


def fetch_adidas_overlay():
    params = {"limit": -1,
              "fields": "name,date,id_event,generated_url,custom_presentation_image.*"}
    try:
        r = requests.get(ADIDAS_OVERLAY, params=params, headers=UA, timeout=30)
        r.raise_for_status()
        rows = r.json().get("data", [])
    except Exception:
        return []
    entries = []
    for row in rows:
        g = (row.get("generated_url") or "").strip().rstrip("/")
        entries.append({
            "name": row.get("name") or "",
            "id_event": row.get("id_event"),
            "has_page": "/programmation/" in g,   # la fiche a-t-elle une page live ?
            "ymd": cms_ymd(row.get("date")) or iso_ymd(str(row.get("date") or "")),
            "presentation": _overlay_asset(row.get("custom_presentation_image")),
            "link": _adidas_front_link(row.get("name"), row.get("id_event"),
                                       row.get("generated_url")),
        })
    return entries


FILLER = {"tour", "presents", "present", "presente", "presentent", "concert",
          "show", "live", "festival", "nouvelle", "nouveau", "generation",
          "deluxe", "edition", "paris", "france", "arena", "adidas", "feat",
          "featuring", "with", "avec", "date", "dates"}


def _keywords(s):
    return {w for w in norm(s).split(" ") if len(w) >= 4 and w not in FILLER}


def _title_compatible(a, b):
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    return na.split(" ")[0] == nb.split(" ")[0]


def _title_strict(a, b):
    na, nb = norm(a), norm(b)
    return bool(na and nb and (na == nb or na in nb or nb in na))


def _best(cands, ev_ymd):
    """Parmi plusieurs fiches candidates (evenements recurrents), choisit la bonne
    edition : d'abord la date exacte, puis la meme annee, sinon l'id le plus recent
    (une date d'une autre annee n'est pas un bon indice, l'id recent si)."""
    y = ev_ymd[0] if ev_ymd else None

    def keyf(c):
        cy = c["ymd"][0] if c.get("ymd") else None
        exact = (c.get("ymd") == ev_ymd and ev_ymd is not None)
        same_year = bool(cy and y and cy == y)
        idv = int(c["id_event"]) if str(c.get("id_event") or "").isdigit() else -1
        return (0 if exact else 1, 0 if same_year else 1, -idv)
    return sorted(cands, key=keyf)[0]


def match_overlay(title, ev_ymd, entries):
    nt = norm(title)
    if not ev_ymd:
        cands = [c for c in entries if nt and nt == norm(c["name"])]
        return _best(cands, ev_ymd) if cands else None
    y, m, _ = ev_ymd
    # 1) meme jour + titre compatible
    c1 = [c for c in entries if c["ymd"] == ev_ymd and _title_compatible(title, c["name"])]
    if c1:
        return _best(c1, ev_ymd)
    # 2) meme jour + mot-cle significatif partage
    c2 = [c for c in entries if c["ymd"] == ev_ymd and (_keywords(title) & _keywords(c["name"]))]
    if c2:
        return _best(c2, ev_ymd)
    # 3) meme mois + titre strict
    c3 = [c for c in entries if c["ymd"] and c["ymd"][0] == y and c["ymd"][1] == m
          and _title_strict(title, c["name"])]
    if c3:
        return _best(c3, ev_ymd)
    # 4) dernier recours : titre strictement identique, quelle que soit la date
    #    (fiche front sans date lisible) -> edition la plus proche / la plus recente
    c4 = [c for c in entries if nt and nt == norm(c["name"])]
    if c4:
        return _best(c4, ev_ymd)
    return None


# ===========================================================================
# Images de secours (artistes)
# ===========================================================================
def get_spotify_token():
    if not (SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET):
        return None
    try:
        r = requests.post("https://accounts.spotify.com/api/token",
                          data={"grant_type": "client_credentials"},
                          auth=(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET), timeout=15)
        r.raise_for_status()
        return r.json().get("access_token")
    except Exception:
        return None


def _from_spotify(q, token):
    if not token:
        return ""
    try:
        r = requests.get("https://api.spotify.com/v1/search",
                         headers={"Authorization": f"Bearer {token}"},
                         params={"q": q, "type": "artist", "limit": 1}, timeout=15)
        r.raise_for_status()
        items = r.json().get("artists", {}).get("items", [])
        if items and items[0].get("images"):
            return items[0]["images"][0]["url"]
    except Exception:
        pass
    return ""


def _from_itunes(q):
    try:
        r = requests.get("https://itunes.apple.com/search",
                         params={"term": q, "entity": "album",
                                 "attribute": "artistTerm", "limit": 1, "country": "FR"},
                         headers=UA, timeout=15)
        r.raise_for_status()
        res = r.json().get("results", [])
        if res and res[0].get("artworkUrl100"):
            return res[0]["artworkUrl100"].replace("100x100bb", "600x600bb")
    except Exception:
        pass
    return ""


def _from_deezer(q):
    try:
        r = requests.get("https://api.deezer.com/search/artist",
                         params={"q": q, "limit": 1}, headers=UA, timeout=15)
        r.raise_for_status()
        data = r.json().get("data", [])
        if data:
            return data[0].get("picture_xl") or data[0].get("picture_big") or ""
    except Exception:
        pass
    return ""


def lookup_image(title, sp_token):
    q = clean_query(title)
    if not q:
        return ""
    for src in (lambda: _from_spotify(q, sp_token),
                lambda: _from_itunes(q),
                lambda: _from_deezer(q)):
        img = src()
        if img:
            return img
    return ""


# ===========================================================================
# Bataclan
# ===========================================================================
def _bataclan_cancelled(e):
    """Annulation cote Bataclan. L'API n'expose pas de booleen dedie confirme :
    on detecte via le libelle de vente ('Annule') et d'eventuels champs de statut.
    A CONFIRMER sur un cas reel."""
    label = norm((e.get("on_sale") or {}).get("label") or "")
    if "annul" in label:
        return True
    status = norm(str(e.get("status") or e.get("state") or ""))
    if "annul" in status or "cancel" in status:
        return True
    return bool(e.get("cancelled") or e.get("canceled"))


def fetch_bataclan(url, auth):
    r = requests.get(url, headers={"hubber-authorization": auth}, timeout=30)
    r.raise_for_status()
    out = []
    for e in r.json().get("manifestations", []):
        if not e.get("published", False):
            continue
        if is_parking((e.get("title") or "")):
            continue
        ts = e.get("date")
        ts_end = e.get("date_end")
        date_iso = (dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat()
                    if ts else None)
        title = (e.get("title") or "").strip()
        day = date_iso[:10] if date_iso else ""
        # page programmation (bataclan.fr) plutot que la billetterie ; repli sur l'URL billetterie
        link = (f"{BATACLAN_FRONT}{slugify(title)}_{day}"
                if (title and day) else (e.get("url") or ""))
        ev = {
            "id": f"bataclan-{e['id']}",
            "title": title,
            "date": date_iso,
            "venue": "Bataclan",
            "image": e.get("visuel") or "",
            "link": link,
            "cancelled": _bataclan_cancelled(e),
        }
        if ts_end and ts_end != ts:
            ev["dateEnd"] = dt.datetime.fromtimestamp(ts_end, dt.timezone.utc).isoformat()
        out.append(ev)
    return out


def load_history():
    if os.path.exists(OUT):
        with open(OUT, encoding="utf-8") as f:
            return {e["id"]: e for e in json.load(f)}
    return {}


def main():
    history = load_history()
    # Transition : retirer les anciennes entrees billetterie (Accor & adidas),
    # remplacees par le CMS commun.
    history = {k: v for k, v in history.items()
               if not (k.startswith("accorarena-") or k.startswith("adidasarena-"))}

    sp_token = get_spotify_token()
    overlay = fetch_adidas_overlay()

    fresh = []
    fresh += fetch_cms_events()                         # Accor + adidas (CMS commun)
    fresh += fetch_bataclan(BATACLAN_URL, BATACLAN_AUTH)
    n_accor  = sum(1 for e in fresh if e["venue"] == "Accor Arena")
    n_adidas = sum(1 for e in fresh if e["venue"] == "adidas arena")

    # 1) Fusion : conserver une image deja connue si la synchro n'en a pas
    fresh_ids = set()
    for e in fresh:
        if not (e.get("date") and e.get("title")):
            continue
        prev = history.get(e["id"])
        if not e.get("image") and prev and prev.get("image"):
            e["image"] = prev["image"]
        history[e["id"]] = e
        fresh_ids.add(e["id"])

    # 2) Surcouche adidas : image remplacee seulement si custom_presentation_image
    #    existe ; lien par evenement depuis l'API front.
    overlay_applied = 0
    for ev in history.values():
        if ev.get("venue") != "adidas arena":
            continue
        c = match_overlay(ev.get("title", ""), iso_ymd(ev.get("date") or ""), overlay)
        if not c:
            continue
        changed = False
        if c["presentation"] and ev.get("image") != c["presentation"]:
            ev["image"] = c["presentation"]
            changed = True
        # Lien par evenement : slug canonique du CMS commun (display_url) + id_event
        # du front (le generated_url n'est PAS un indicateur fiable d'existence de page,
        # il est souvent vide alors que la page existe).
        slug = ev.get("cms_slug")
        idev = c.get("id_event")
        link = (f"{ADIDAS_FRONT}{slug}--{idev}" if (slug and idev) else c.get("link", ""))
        if link and ev.get("link") != link:
            ev["link"] = link
            changed = True
        if changed:
            overlay_applied += 1

    # 3) Images encore manquantes -> sources musicales (flux courant)
    found = 0
    for ev in history.values():
        if ev["id"] in fresh_ids and not ev.get("image"):
            ev["image"] = lookup_image(ev.get("title", ""), sp_token)
            if ev.get("image"):
                found += 1

    # Filtre final : retire les parkings, y compris ceux deja presents dans l'historique
    kept = [e for e in history.values() if not is_parking(e.get("title", ""))]
    result = sorted(kept, key=lambda e: e.get("date") or "")
    n_cancelled = sum(1 for e in result if e.get("cancelled"))
    os.makedirs("docs", exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"CMS commun : {n_accor} Accor, {n_adidas} adidas | {len(result)} au total | "
          f"{overlay_applied} adidas ajuste(s) via l'API front | "
          f"{found} image(s) de secours | {n_cancelled} annule(s) -> {OUT}.")


if __name__ == "__main__":
    main()
