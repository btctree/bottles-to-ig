#!/usr/bin/env python3
"""
Bottles-to-IG daily pipeline.

iCloud shared album ("Bottles") -> qualify (single bottle at ~45 degrees)
-> dedup (perceptual hash vs already-posted + existing IG media)
-> identify bottle via Gemini vision -> hashtag caption (sake/wine templates)
-> fixed filter preset -> commit JPEG to this repo -> publish via Instagram API.

Runs on GitHub Actions daily. Exits 0 with a clear message when secrets are
missing so the cron is safe to enable before setup is complete.
"""

import base64
import io
import json
import os
import re
import subprocess
import sys
import time

import requests
from PIL import Image, ImageEnhance, ImageOps

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)

CONFIG = json.load(open("config.json", encoding="utf-8"))
STATE_PATH = "state/posted.json"
IG_HASH_PATH = "state/ig_hashes.json"
PHOTOS_DIR = "photos"

GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
IG_TOKEN = os.environ.get("IG_ACCESS_TOKEN", "")
GIT_PUSH = os.environ.get("GIT_PUSH", "") == "1"

GRAPH = "https://graph.instagram.com/v23.0"
RAW_BASE = f"https://raw.githubusercontent.com/{CONFIG['repo']}/{CONFIG['branch']}"


# ---------------------------------------------------------------- state

def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def git(*args, check=True):
    return subprocess.run(["git", *args], check=check, capture_output=True, text=True)


def commit_push(paths, msg):
    if not GIT_PUSH:
        print(f"[dry] would commit: {msg}")
        return
    git("add", *paths)
    r = git("commit", "-m", msg, check=False)
    if r.returncode == 0:
        git("push")


# ---------------------------------------------------------------- iCloud album

def album_base():
    token = CONFIG["album_token"]
    url = f"https://p01-sharedstreams.icloud.com/{token}/sharedstreams/webstream"
    r = requests.post(url, json={"streamCtag": None}, timeout=30)
    if r.status_code == 330:
        host = r.json()["X-Apple-MMe-Host"]
        return f"https://{host}/{token}/sharedstreams"
    return f"https://p01-sharedstreams.icloud.com/{token}/sharedstreams"


def fetch_album(base):
    r = requests.post(f"{base}/webstream", json={"streamCtag": None}, timeout=60)
    r.raise_for_status()
    photos = r.json().get("photos", [])
    out = []
    for p in photos:
        if p.get("mediaAssetType", "").lower() == "video":
            continue
        derivs = {k: v for k, v in p.get("derivatives", {}).items() if k.isdigit()}
        if not derivs:
            continue
        best = max(derivs.items(), key=lambda kv: int(kv[0]))
        out.append({
            "guid": p["photoGuid"],
            "checksum": best[1]["checksum"],
            "date": p.get("batchDateCreated", ""),
        })
    return out


def asset_url(base, guid, checksum):
    r = requests.post(f"{base}/webasseturls", json={"photoGuids": [guid]}, timeout=60)
    r.raise_for_status()
    items = r.json().get("items", {})
    loc = items.get(checksum)
    if not loc:  # fall back to any derivative returned
        if not items:
            return None
        loc = list(items.values())[0]
    return f"https://{loc['url_location']}{loc['url_path']}"


def download(url):
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    return r.content


# ---------------------------------------------------------------- image work

def phash(img_bytes):
    import imagehash
    return str(imagehash.phash(Image.open(io.BytesIO(img_bytes)).convert("RGB")))


def hash_distance(h1, h2):
    import imagehash
    return imagehash.hex_to_hash(h1) - imagehash.hex_to_hash(h2)


def phash_variants(img_bytes):
    """Hashes of the photo as-is, square-cropped, and 4:5-padded — so a match is
    found even when the IG copy was cropped or padded differently."""
    import imagehash
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    img = ImageOps.exif_transpose(img)
    w, h = img.size
    side = min(w, h)
    square = img.crop(((w - side) // 2, (h - side) // 2,
                       (w + side) // 2, (h + side) // 2))
    if w / h < 0.8:
        canvas = Image.new("RGB", (int(h * 0.8), h), (250, 249, 246))
        canvas.paste(img, ((int(h * 0.8) - w) // 2, 0))
    else:
        canvas = img
    return [str(imagehash.phash(v)) for v in (img, square, canvas)]


def is_duplicate(variants, ig_hashes, threshold):
    for k, v in ig_hashes.items():
        if min(hash_distance(hv, v["phash"]) for hv in variants) <= threshold:
            return k
    return None


def apply_preset(img_bytes):
    """One consistent look for the whole feed: slight warmth, lift, punch."""
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    img = ImageOps.exif_transpose(img)
    img = ImageEnhance.Brightness(img).enhance(1.04)
    img = ImageEnhance.Contrast(img).enhance(1.08)
    img = ImageEnhance.Color(img).enhance(1.10)
    # gentle warmth: scale channels
    r, g, b = img.split()
    r = r.point(lambda v: min(255, int(v * 1.03)))
    b = b.point(lambda v: int(v * 0.97))
    img = Image.merge("RGB", (r, g, b))
    # Instagram feed accepts aspect ratios 4:5 .. 1.91:1 -> pad onto white
    w, h = img.size
    ratio = w / h
    if ratio < 0.8:
        new_w = int(h * 0.8)
        canvas = Image.new("RGB", (new_w, h), (250, 249, 246))
        canvas.paste(img, ((new_w - w) // 2, 0))
        img = canvas
    elif ratio > 1.91:
        new_h = int(w / 1.91)
        canvas = Image.new("RGB", (w, new_h), (250, 249, 246))
        canvas.paste(img, (0, (new_h - h) // 2))
        img = canvas
    img.thumbnail((1440, 1800))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return buf.getvalue()


# ---------------------------------------------------------------- gemini

def gemini(prompt, img_bytes=None, retries=5):
    models = [CONFIG["gemini_model"], "gemini-3.1-flash-lite"]
    parts = [{"text": prompt}]
    if img_bytes:
        parts.append({"inline_data": {
            "mime_type": "image/jpeg",
            "data": base64.b64encode(img_bytes).decode(),
        }})
    body = {
        "contents": [{"parts": parts}],
        "generationConfig": {"temperature": 0.2, "response_mime_type": "application/json"},
    }
    last = ""
    for i in range(retries):
        model = models[min(i // 2, len(models) - 1)]  # fall back after 2 tries
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{model}:generateContent?key={GEMINI_KEY}")
        r = requests.post(url, json=body, timeout=120)
        if r.status_code in (429, 500, 502, 503):
            last = f"{model} HTTP {r.status_code}"
            print(f"  {last}, retry {i + 1}")
            time.sleep(20 * (i + 1))
            continue
        r.raise_for_status()
        txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        txt = re.sub(r"^```(?:json)?|```$", "", txt.strip(), flags=re.M).strip()
        try:
            return json.loads(txt)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", txt, re.S)
            if m:
                try:
                    return json.loads(m.group(0).replace("“", '\\"').replace("”", '\\"'))
                except json.JSONDecodeError:
                    pass
            print(f"  bad JSON from model (attempt {i + 1}), retrying")
            last = "bad JSON"
            continue
    raise RuntimeError(f"Gemini failed after retries ({last})")


QUALIFY_PROMPT = """You check photos for an Instagram feed about wine and sake bottles.
Answer in JSON: {"qualified": true/false, "reason": "..."}
qualified=true ONLY if: the photo clearly shows ONE wine or sake bottle as the main
subject, AND the bottle is displayed/held tilted at roughly 45 degrees (diagonal),
AND the label is readable enough to identify the drink."""

IDENTIFY_PROMPT = """Identify this bottle precisely from its label. Answer in JSON only.
If it is SAKE (nihonshu):
{"kind":"sake","name_en":"","name_ja":"","type_en":"e.g. Junmai Daiginjo","type_ja":"e.g. 純米大吟醸",
"polish_en":"e.g. polished to 50 percent -> write: ricepolishingratio50","polish_ja":"e.g. 精米歩合50",
"rice_en":"e.g. yamadanishiki","rice_ja":"e.g. 山田錦","brewery_en":"","brewery_ja":"",
"city_en":"","city_ja":"","flavor_en":"one word e.g. fruity","flavor_ja":"e.g. フルーティー"}
If it is WINE:
{"kind":"wine","name":"","country":"","region":"","village":"","grapes":["",""],"vintage":"e.g. 2019","flavor":"one word"}
If you cannot identify it at all: {"kind":"unknown"}
Rules: read fields from the label when visible (especially rice polishing ratio
精米歩合 and rice variety). If you have confidently identified the exact product,
you may fill remaining fields (grapes, flavor, brewery, city, rice, polish) from
well-established knowledge of that specific product. Leave a field as empty
string only when it cannot be determined either way — never fabricate."""


def tagify(s):
    s = "".join(ch for ch in s.strip() if ch.isalnum())
    return f"#{s}" if s else ""


def build_caption(info):
    tags = []
    if info.get("kind") == "sake":
        order = ["name_en", "name_ja", "type_ja", "type_en", "polish_ja", "polish_en",
                 "rice_ja", "rice_en", "brewery_ja", "brewery_en", "city_ja", "city_en",
                 "flavor_ja", "flavor_en"]
        for k in order:
            tags.append(tagify(str(info.get(k, ""))))
        tags += ["#japan", "#日本", "#日本酒", "#日本酒好き", "#日本酒好きな人と繋がりたい", "#sake", "#sakelover"]
        title = info.get("name_ja") or info.get("name_en") or ""
    elif info.get("kind") == "wine":
        for k in ["name", "country", "region", "village"]:
            tags.append(tagify(str(info.get(k, ""))))
        for g in info.get("grapes", []):
            tags.append(tagify(str(g)))
        v = str(info.get("vintage", "")).strip()
        if v:
            tags.append(tagify(f"vintage{v}"))
        tags.append(tagify(str(info.get("flavor", ""))))
        tags += ["#wine", "#winelover"]
        title = info.get("name") or ""
    else:
        return None
    tags = [t for t in dict.fromkeys(tags) if t and t != "#"]
    return (title + "\n.\n" + " ".join(tags)).strip()


# ---------------------------------------------------------------- instagram

def ig_user_id():
    r = requests.get(f"{GRAPH}/me", params={
        "fields": "user_id,username", "access_token": IG_TOKEN}, timeout=30)
    r.raise_for_status()
    d = r.json()
    print(f"IG account: {d.get('username')}")
    return d.get("user_id") or d.get("id")


def ig_media_pages(uid):
    url = f"{GRAPH}/{uid}/media"
    params = {"fields": "id,media_type,media_url,caption,timestamp",
              "limit": 50, "access_token": IG_TOKEN}
    while url:
        r = requests.get(url, params=params, timeout=60)
        r.raise_for_status()
        d = r.json()
        yield from d.get("data", [])
        url = d.get("paging", {}).get("next")
        params = None


def sync_ig_hashes(uid, ig_hashes):
    """One-time (then incremental) perceptual-hash index of everything already on IG."""
    known = {m["media_id"] for m in ig_hashes.values() if m.get("media_id")}
    added = 0
    for m in ig_media_pages(uid):
        if m["id"] in known or m.get("media_type") == "VIDEO" or not m.get("media_url"):
            continue
        try:
            h = phash(download(m["media_url"]))
            ig_hashes[m["id"]] = {"media_id": m["id"], "phash": h,
                                  "ts": m.get("timestamp", "")}
            added += 1
        except Exception as e:
            print(f"  hash skip {m['id']}: {e}")
    if added:
        print(f"Indexed {added} existing IG posts")
    return added


def ig_publish(uid, image_url, caption):
    r = requests.post(f"{GRAPH}/{uid}/media", data={
        "image_url": image_url, "caption": caption,
        "access_token": IG_TOKEN}, timeout=120)
    r.raise_for_status()
    container = r.json()["id"]
    for _ in range(20):
        s = requests.get(f"{GRAPH}/{container}", params={
            "fields": "status_code", "access_token": IG_TOKEN}, timeout=30).json()
        if s.get("status_code") == "FINISHED":
            break
        if s.get("status_code") == "ERROR":
            raise RuntimeError(f"IG container error: {s}")
        time.sleep(5)
    r = requests.post(f"{GRAPH}/{uid}/media_publish", data={
        "creation_id": container, "access_token": IG_TOKEN}, timeout=120)
    r.raise_for_status()
    return r.json()["id"]


def maybe_refresh_token(state):
    """IG long-lived tokens last 60 days; refresh monthly and store back as secret."""
    admin_pat = os.environ.get("ADMIN_PAT", "")
    last = state.get("token_refreshed_at", 0)
    if time.time() - last < 30 * 86400 or not admin_pat or not IG_TOKEN:
        return
    r = requests.get("https://graph.instagram.com/refresh_access_token", params={
        "grant_type": "ig_refresh_token", "access_token": IG_TOKEN}, timeout=60)
    if r.status_code != 200:
        print(f"token refresh failed: {r.text[:200]}")
        return
    new_token = r.json()["access_token"]
    from nacl import encoding, public  # PyNaCl
    repo = CONFIG["repo"]
    hdr = {"Authorization": f"Bearer {admin_pat}",
           "X-GitHub-Api-Version": "2022-11-28"}
    key = requests.get(f"https://api.github.com/repos/{repo}/actions/secrets/public-key",
                       headers=hdr, timeout=30).json()
    sealed = public.SealedBox(public.PublicKey(key["key"].encode(), encoding.Base64Encoder))
    enc = base64.b64encode(sealed.encrypt(new_token.encode())).decode()
    pr = requests.put(f"https://api.github.com/repos/{repo}/actions/secrets/IG_ACCESS_TOKEN",
                      headers=hdr, json={"encrypted_value": enc, "key_id": key["key_id"]},
                      timeout=30)
    if pr.status_code in (201, 204):
        state["token_refreshed_at"] = int(time.time())
        print("IG token refreshed and secret updated")


# ---------------------------------------------------------------- main

def main():
    if not GEMINI_KEY or not IG_TOKEN:
        missing = [n for n, v in [("GEMINI_API_KEY", GEMINI_KEY),
                                  ("IG_ACCESS_TOKEN", IG_TOKEN)] if not v]
        print(f"Setup incomplete - missing secrets: {', '.join(missing)}. Nothing to do.")
        return

    state = load_json(STATE_PATH, {"photos": {}, "token_refreshed_at": int(time.time())})
    ig_hashes = load_json(IG_HASH_PATH, {})
    photos_state = state["photos"]

    maybe_refresh_token(state)

    base = album_base()
    album = fetch_album(base)
    album.sort(key=lambda p: p.get("date", ""),
               reverse=CONFIG.get("order") == "newest_first")
    print(f"Album photos: {len(album)}, already tracked: {len(photos_state)}")

    uid = ig_user_id()
    if not ig_hashes:
        sync_ig_hashes(uid, ig_hashes)
        save_json(IG_HASH_PATH, ig_hashes)
        commit_push([IG_HASH_PATH], "index existing IG posts")

    posted = 0
    checks = 0
    for p in album:
        if posted >= CONFIG["posts_per_day"] or checks >= CONFIG["max_vision_checks_per_run"]:
            break
        guid = p["guid"]
        if guid in photos_state:
            continue
        try:
            url = asset_url(base, guid, p["checksum"])
            raw = download(url)
            variants = phash_variants(raw)
            h = variants[0]
        except Exception as e:
            print(f"{guid[:8]}: fetch failed ({e}), retry next run")
            continue

        dup = is_duplicate(variants, ig_hashes, CONFIG["phash_threshold"])
        if dup:
            photos_state[guid] = {"status": "skipped_already_on_ig", "phash": h, "match": dup}
            print(f"{guid[:8]}: already on IG -> skip")
            continue

        checks += 1
        try:
            q = gemini(QUALIFY_PROMPT, raw)
        except Exception as e:
            print(f"{guid[:8]}: vision unavailable ({e}) - stopping early, will retry next run")
            break
        if not q.get("qualified"):
            photos_state[guid] = {"status": "disqualified", "phash": h,
                                  "reason": q.get("reason", "")}
            save_json(STATE_PATH, state)
            print(f"{guid[:8]}: not qualified ({q.get('reason','')[:60]})")
            continue

        info = gemini(IDENTIFY_PROMPT, raw)
        caption = build_caption(info)
        if not caption:
            photos_state[guid] = {"status": "unidentified", "phash": h}
            print(f"{guid[:8]}: could not identify bottle")
            continue

        processed = apply_preset(raw)
        os.makedirs(PHOTOS_DIR, exist_ok=True)
        img_path = f"{PHOTOS_DIR}/{guid}.jpg"
        with open(img_path, "wb") as f:
            f.write(processed)
        commit_push([img_path], f"photo {guid[:8]}")
        image_url = f"{RAW_BASE}/{img_path}"
        time.sleep(10)  # let raw.githubusercontent pick up the push

        try:
            media_id = ig_publish(uid, image_url, caption)
        except Exception as e:
            photos_state[guid] = {"status": "publish_failed", "phash": h, "error": str(e)[:300]}
            print(f"{guid[:8]}: publish FAILED: {e}")
            continue

        ig_hashes[media_id] = {"media_id": media_id, "phash": phash(processed), "ts": ""}
        photos_state[guid] = {"status": "posted", "phash": h, "ig_media_id": media_id,
                              "kind": info.get("kind")}
        posted += 1
        print(f"{guid[:8]}: POSTED as {media_id} ({info.get('kind')})")

    save_json(STATE_PATH, state)
    save_json(IG_HASH_PATH, ig_hashes)
    commit_push([STATE_PATH, IG_HASH_PATH], f"state: +{posted} posted, {checks} checked")
    print(f"Done. posted={posted} vision_checks={checks}")


if __name__ == "__main__":
    main()
