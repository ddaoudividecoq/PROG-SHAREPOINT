#!/usr/bin/env python3
"""
Recupere les concerts des 3 salles et ecrit docs/events.json.

Conserve l'historique : les evenements deja presents dans events.json
ne sont jamais supprimes, meme si l'API ne les renvoie plus (shows passes).
"""

import os
import json
import datetime as dt
import requests

# --- Cles / auth des API (secrets GitHub) ---
ACCOR_API_KEY  = os.environ["ACCOR_API_KEY"]     # header x-api-key
ADIDAS_API_KEY = os.environ["ADIDAS_API_KEY"]    # header x-api-key
BATACLAN_AUTH  = os.environ["BATACLAN_AUTH"]     # header hubber-authorization

# --- Endpoints ---
ACCOR_URL    = "https://services.groupe-pec.com/hospi/integration/sessions?venueId=796dc182-0131-4540-bcc8-889b5351cee0"
ADIDAS_URL   = "https://services.groupe-pec.com/hospi/integration/sessions?venueId=c8741b53-e7d8-4eb3-9aea-674050d7f062"
BATACLAN_URL = "https://billetterie.bataclan.fr/fr/hubber/catalog/resource/ticketing?_format=json"

# --- Lien "au clic" pour les salles Groupe PEC (leur JSON n'a pas de lien par evenement) ---
# >>> A CONFIRMER : remplace par la vraie page programmation de chaque salle.
ACCOR_LINK  = "https://www.accorarena.com"
ADIDAS_LINK = "https://www.adidas-arena.com"

OUT = "docs/events.json"


def fetch_groupe_pec(url, api_key, salle, lien):
    r = requests.get(url, headers={"x-api-key": api_key}, timeout=30)
    r.raise_for_status()
    out = []
    for e in r.json():
        if e.get("cancelled"):
            continue
        slug = salle.lower().replace(" ", "")
        out.append({
            "id": f"{slug}-{e['externalId']}",
            "title": (e.get("name") or "").strip(),
            "date": e.get("start"),               # deja ISO 8601
            "venue": salle,
            "image": e.get("image") or "",
            "link": lien,
        })
    return out


def fetch_bataclan(url, auth):
    # Ajuste le nom du header si besoin (l'utilisateur a indique "hubber authorization").
    r = requests.get(url, headers={"hubber-authorization": auth}, timeout=30)
    r.raise_for_status()
    out = []
    for e in r.json().get("manifestations", []):
        if not e.get("published", False):
            continue
        ts = e.get("date")
        date_iso = (dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat()
                    if ts else None)
        out.append({
            "id": f"bataclan-{e['id']}",
            "title": (e.get("title") or "").strip(),
            "date": date_iso,
            "venue": "Bataclan",
            "image": e.get("visuel") or "",
            "link": e.get("url") or "",
        })
    return out


def load_history():
    if os.path.exists(OUT):
        with open(OUT, encoding="utf-8") as f:
            return {e["id"]: e for e in json.load(f)}
    return {}


def main():
    events = load_history()          # historique existant (shows passes conserves)

    fresh = []
    fresh += fetch_groupe_pec(ACCOR_URL, ACCOR_API_KEY, "Accor Arena", ACCOR_LINK)
    fresh += fetch_groupe_pec(ADIDAS_URL, ADIDAS_API_KEY, "adidas arena", ADIDAS_LINK)
    fresh += fetch_bataclan(BATACLAN_URL, BATACLAN_AUTH)

    for e in fresh:                  # ajoute les nouveaux, met a jour les existants
        if e.get("date") and e.get("title"):
            events[e["id"]] = e

    result = sorted(events.values(), key=lambda e: e["date"] or "")
    os.makedirs("docs", exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"{len(fresh)} evenements recuperes, {len(result)} au total dans {OUT}.")


if __name__ == "__main__":
    main()
