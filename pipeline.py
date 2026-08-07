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

# 16x16 DCT -> 256-bit hashes. At 64 bits this feed is inseparable: every photo
# is the same hand, wall and crop, so a confirmed duplicate pair and two totally
# different bottles both landed at the same distance. Measured over the 27
# published photos at 256 bits: confirmed duplicate 48/256 (18.8%), nearest
# genuinely-distinct pair 72/256 (28.1%).
HASH_SIZE = 16
HASH_BITS = HASH_SIZE * HASH_SIZE

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
    """True when the change reached origin. Never raises: a push that dies here
    after a photo has already gone live would otherwise kill the run before the
    'posted' record is written, and the same photo would be posted again
    tomorrow. The caller decides what a False means."""
    if not GIT_PUSH:
        print(f"[dry] would commit: {msg}")
        return True
    git("add", *paths)
    r = git("-c", "user.name=bottles-bot",
            "-c", "user.email=actions@users.noreply.github.com",
            "commit", "-m", msg, check=False)
    if r.returncode != 0:
        if "nothing to commit" in (r.stdout + r.stderr):
            return True
        print(f"git commit failed: {(r.stderr or r.stdout)[:200]}")
        return False
    p = git("push", check=False)
    if p.returncode != 0:
        print("  push rejected, rebasing on origin and retrying")
        git("pull", "--rebase", check=False)
        p = git("push", check=False)
    if p.returncode != 0:
        print(f"git push FAILED: {(p.stderr or p.stdout)[:200]}")
        return False
    return True


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
    return str(imagehash.phash(Image.open(io.BytesIO(img_bytes)).convert("RGB"),
                               hash_size=HASH_SIZE))


def hash_distance(h1, h2):
    """None when the two hashes were built at different sizes, i.e. one of them
    predates the 256-bit rebuild and the comparison would be meaningless."""
    import imagehash
    if not h1 or not h2 or len(h1) != len(h2):
        return None
    # int(): imagehash returns numpy.int64, which json.dump cannot serialise -
    # it would truncate state/posted.json mid-write when a distance is recorded
    return int(imagehash.hex_to_hash(h1) - imagehash.hex_to_hash(h2))


def nearest(variants, pool):
    """(key, distance) of the closest entry in pool. pool maps key -> hex hash."""
    best_key, best_dist = None, None
    for key, h in pool.items():
        dists = [d for d in (hash_distance(v, h) for v in variants) if d is not None]
        if not dists:
            continue
        d = min(dists)
        if best_dist is None or d < best_dist:
            best_key, best_dist = key, d
    return best_key, best_dist


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
    return [str(imagehash.phash(v, hash_size=HASH_SIZE)) for v in (img, square, canvas)]


def dup_threshold():
    """Bits of difference below which two photos count as the same shot."""
    return int(HASH_BITS * CONFIG.get("phash_threshold_pct", 24) / 100)


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
        try:
            r = requests.post(url, json=body, timeout=120)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last = f"{model} {type(e).__name__}"
            print(f"  {last}, retry {i + 1}")
            time.sleep(20 * (i + 1))
            continue
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


QUALIFY_PROMPT = """You check photos for an Instagram feed about alcohol bottles
(sake, wine, whisky, beer, champagne, gin, shochu, umeshu — any alcoholic drink).
Answer in JSON: {"qualified": true/false, "reason": "..."}
qualified=true ONLY if ALL hold:
- exactly one alcohol bottle is the clear main subject
- a hand is holding the bottle (not standing on a table/shelf/ice bucket)
- the visible label is the FRONT brand label, not the back label (reject if it
  mainly shows ingredients 原材料名, alcohol %, legal text, barcode, contact info)
- the label is readable enough to identify the drink
(Do NOT judge the bottle's tilt angle — that is measured separately.)"""

ANGLE_PROMPT = """The photo shows one bottle. Point to two locations:
1) the center of the bottle's mouth/cap (very top of the bottle)
2) the center of the bottle's base (very bottom of the bottle; if hidden behind
   a hand, estimate where it is)
Answer in JSON only: {"top": [y, x], "bottom": [y, x]}
with y and x as integers 0-1000 normalized to image height and width (y grows
downward, x grows rightward)."""


def bottle_tilt_degrees(img_bytes):
    """True tilt from vertical, computed from model-pointed cap/base coordinates."""
    import math
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(img_bytes)))
    w, h = img.size
    p = gemini(ANGLE_PROMPT, img_bytes)
    (y1, x1), (y2, x2) = p["top"], p["bottom"]
    dx = abs(x1 - x2) / 1000 * w
    dy = abs(y1 - y2) / 1000 * h
    if dx == dy == 0:
        return 0.0
    return math.degrees(math.atan2(dx, dy))

IDENTIFY_PROMPT = """Identify this bottle precisely from its label. Answer in JSON only.
If it is SAKE (nihonshu):
{"kind":"sake","name_en":"","name_ja":"",
"name_parts_en":["brand e.g. Nihon Sakari","style e.g. Nama Genshu","grade e.g. Daiginjo","edition/collab e.g. montbell"],
"name_parts_ja":["e.g. 日本盛","e.g. 生原酒","e.g. 大吟醸","e.g. モンベルボトル"],
"type_en":"e.g. Junmai Daiginjo","type_ja":"e.g. 純米大吟醸",
"polish_en":"e.g. polished to 50 percent -> write: ricepolishingratio50","polish_ja":"e.g. 精米歩合50",
"rice_en":"e.g. yamadanishiki","rice_ja":"e.g. 山田錦","brewery_en":"","brewery_ja":"",
"city_en":"","city_ja":"","flavor_en":"one word e.g. fruity","flavor_ja":"e.g. フルーティー"}
If it is WINE:
{"kind":"wine","name":"","country":"","region":"","village":"","grapes":["",""],"vintage":"e.g. 2019","flavor":"one word"}
If it is any OTHER alcohol (whisky, beer, champagne, gin, shochu, umeshu, liqueur...):
{"kind":"other","name_en":"","name_ja":"","category_en":"e.g. Scotch whisky","category_ja":"e.g. スコッチウイスキー",
"maker_en":"","maker_ja":"","country_en":"","country_ja":"","region":"","age_or_vintage":"","flavor_en":"one word","flavor_ja":""}
If you cannot identify it at all: {"kind":"unknown"}
Rules: name_parts must split the product name into its meaningful components —
brand, style words (each its own part: 生原酒, 大吟醸...), edition/collaboration —
NOT the whole name as one string; 2-6 parts, same split in both languages.
Read fields from the label when visible (especially rice polishing ratio
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
        parts = (info.get("name_parts_en") or []) + (info.get("name_parts_ja") or [])
        if parts:
            for p in parts:
                tags.append(tagify(str(p)))
        else:
            tags += [tagify(str(info.get("name_en", ""))), tagify(str(info.get("name_ja", "")))]
        pool_ja = "".join(str(p) for p in (info.get("name_parts_ja") or []))
        pool_en = norm_name("".join(str(p) for p in (info.get("name_parts_en") or [])))
        type_ja = str(info.get("type_ja", ""))
        if type_ja and not all(ch in pool_ja for ch in type_ja):
            tags.append(tagify(type_ja))
        type_en = str(info.get("type_en", ""))
        if type_en and not all(norm_name(w) in pool_en for w in type_en.split()):
            tags.append(tagify(type_en))
        order = ["polish_ja", "polish_en",
                 "rice_ja", "rice_en", "brewery_ja", "brewery_en", "city_ja", "city_en",
                 "flavor_ja", "flavor_en"]
        for k in order:
            tags.append(tagify(str(info.get(k, ""))))
        tags += ["#nihonshu", "#日本酒", "#sake", "#清酒", "#japan", "#日本",
                 "#日本酒好き", "#日本酒好きな人と繋がりたい", "#sakelover"]
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
    elif info.get("kind") == "other":
        order = ["name_en", "name_ja", "category_ja", "category_en", "maker_ja",
                 "maker_en", "country_ja", "country_en", "region",
                 "flavor_ja", "flavor_en"]
        for k in order:
            tags.append(tagify(str(info.get(k, ""))))
        a = str(info.get("age_or_vintage", "")).strip()
        if a:
            tags.append(tagify(f"aged{a}" if a.isdigit() else a))
        words = str(info.get("category_en", "")).lower().split()
        cat = re.sub(r"[^a-z]", "", words[-1]) if words else ""
        if cat:
            tags += [f"#{cat}", f"#{cat}lover"]
        title = info.get("name_ja") or info.get("name_en") or ""
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


def norm_name(s):
    return re.sub(r"[^0-9a-z぀-ヿ㐀-鿿]", "", str(s).lower())


def closest_name(cand_names, known_names):
    """(candidate, matched, ratio) for the best fuzzy match against known_names.

    Exact matching missed the duplicate that started all this: Gemini read the
    same label 一双 as "ISSOU" one day and "SOU" the next, which normalise to
    chiyomusubiassemblageissou vs chiyomusubiassemblagesou - 0.96 similar, but
    not equal. Genuinely different bottles score 0.27-0.44, so the gap is wide.
    """
    import difflib
    best = (None, None, 0.0)
    for c in cand_names:
        n = norm_name(c)
        if not n:
            continue
        for k in known_names:
            ratio = difflib.SequenceMatcher(None, n, k).ratio()
            if ratio > best[2]:
                best = (c, k, ratio)
    return best


def posted_bottle_names(uid):
    """First line of every existing IG caption, normalized — so the same bottle
    photographed again is never posted twice."""
    names = set()
    for m in ig_media_pages(uid):
        lines = (m.get("caption") or "").strip().splitlines()
        if lines:
            n = norm_name(lines[0])
            if n:
                names.add(n)
    return names


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
    if not r.ok:
        raise RuntimeError(f"media container HTTP {r.status_code}: {r.text[:300]}")
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
    if state.get("hash_bits") != HASH_BITS:
        print(f"rebuilding the IG index at {HASH_BITS} bits (one time, this run will be slow)")
        ig_hashes.clear()
        state["hash_bits"] = HASH_BITS
    # every run, not just the first: anything posted from the phone by hand was
    # never indexed before, so the pipeline could not know it existed
    added = sync_ig_hashes(uid, ig_hashes)
    if added:
        save_json(IG_HASH_PATH, ig_hashes)
        commit_push([IG_HASH_PATH], f"index {added} IG posts at {HASH_BITS}-bit")

    known_names = posted_bottle_names(uid)
    for s in photos_state.values():
        n = norm_name(s.get("name", ""))
        if n:
            known_names.add(n)
    print(f"Known bottle names: {len(known_names)}")

    # two pools, each compared like with like: album photos are hashed raw, so
    # they are matched against other raw album hashes; the IG index holds the
    # rendition Instagram serves, so the candidate is matched against it only
    # after the same preset has been applied.
    ig_pool = {k: v["phash"] for k, v in ig_hashes.items() if v.get("phash")}
    album_pool = {g: s["phash"] for g, s in photos_state.items() if s.get("phash")}
    thr = dup_threshold()
    print(f"Dedup: {len(ig_pool)} IG hashes, {len(album_pool)} album hashes, "
          f"threshold {thr}/{HASH_BITS} bits")

    posted = 0
    checks = 0
    try:
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
                processed = apply_preset(raw)
                proc_variants = phash_variants(processed)
            except Exception as e:
                print(f"{guid[:8]}: fetch failed ({e}), retry next run")
                continue

            ig_key, ig_dist = nearest(proc_variants, ig_pool)
            album_key, album_dist = nearest(variants, album_pool)
            print(f"{guid[:8]}: nearest IG {ig_dist}, nearest album {album_dist} "
                  f"(dup at <= {thr})")

            if ig_dist is not None and ig_dist <= thr:
                photos_state[guid] = {"status": "skipped_already_on_ig", "phash": h,
                                      "match": ig_key, "distance": ig_dist}
                album_pool[guid] = h
                print(f"{guid[:8]}: already on IG ({ig_dist} bits from {ig_key}) -> skip")
                continue
            if album_dist is not None and album_dist <= thr:
                photos_state[guid] = {"status": "skipped_same_shot", "phash": h,
                                      "match": album_key, "distance": album_dist}
                album_pool[guid] = h
                print(f"{guid[:8]}: same shot as {album_key[:8]} ({album_dist} bits) -> skip")
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
                album_pool[guid] = h
                save_json(STATE_PATH, state)
                print(f"{guid[:8]}: not qualified ({q.get('reason','')[:60]})")
                continue

            try:
                tilt = bottle_tilt_degrees(raw)
            except Exception as e:
                print(f"{guid[:8]}: tilt check unavailable ({e}) - stopping early, will retry next run")
                break
            if not (CONFIG.get("tilt_min", 25) <= tilt <= CONFIG.get("tilt_max", 65)):
                photos_state[guid] = {"status": "disqualified", "phash": h,
                                      "reason": f"tilt {tilt:.0f} deg, need ~45"}
                album_pool[guid] = h
                save_json(STATE_PATH, state)
                print(f"{guid[:8]}: not qualified (tilt {tilt:.0f} deg, need 25-65)")
                continue

            try:
                info = gemini(IDENTIFY_PROMPT, raw)
            except Exception as e:
                print(f"{guid[:8]}: identify unavailable ({e}) - stopping early, will retry next run")
                break
            caption = build_caption(info)
            if not caption:
                photos_state[guid] = {"status": "unidentified", "phash": h}
                album_pool[guid] = h
                print(f"{guid[:8]}: could not identify bottle")
                continue

            cand_names = [info.get(k, "") for k in ("name", "name_en", "name_ja")]
            cand, matched, ratio = closest_name(cand_names, known_names)
            cutoff = CONFIG.get("name_similarity", 0.90)
            print(f"{guid[:8]}: closest known name {ratio:.2f} (dup at >= {cutoff})")
            if cand and ratio >= cutoff:
                photos_state[guid] = {"status": "skipped_same_bottle", "phash": h,
                                      "name": str(cand), "match": matched,
                                      "similarity": round(ratio, 3)}
                album_pool[guid] = h
                save_json(STATE_PATH, state)
                print(f"{guid[:8]}: same bottle already posted ({cand} ~ {matched}) -> skip")
                continue

            os.makedirs(PHOTOS_DIR, exist_ok=True)
            img_path = f"{PHOTOS_DIR}/{guid}.jpg"
            with open(img_path, "wb") as f:
                f.write(processed)
            if not commit_push([img_path], f"photo {guid[:8]}"):
                # Instagram pulls the image from raw.githubusercontent, so there
                # is nothing to publish. No status written - retry next run.
                print(f"{guid[:8]}: photo did not reach GitHub, not publishing")
                continue
            image_url = f"{RAW_BASE}/{img_path}"
            time.sleep(10)  # let raw.githubusercontent pick up the push

            try:
                media_id = ig_publish(uid, image_url, caption)
            except Exception as e:
                photos_state[guid] = {"status": "publish_failed", "phash": h, "error": str(e)[:300]}
                album_pool[guid] = h
                print(f"{guid[:8]}: publish FAILED: {e}")
                continue

            # Deliberately not indexed here from the local bytes - that stored a
            # hash of something Instagram never serves, so later candidates were
            # compared against the wrong image. Next run's sync picks up the real
            # rendition from media_url.
            photos_state[guid] = {"status": "posted", "phash": h, "ig_media_id": media_id,
                                  "kind": info.get("kind"),
                                  "name": next((c for c in cand_names if c), "")}
            album_pool[guid] = h
            for c in cand_names:
                if c:
                    known_names.add(norm_name(c))
            posted += 1
            print(f"{guid[:8]}: POSTED as {media_id} ({info.get('kind')})")
    finally:
        try:
            save_json(STATE_PATH, state)
            save_json(IG_HASH_PATH, ig_hashes)
            if not commit_push([STATE_PATH, IG_HASH_PATH],
                               f"state: +{posted} posted, {checks} checked"):
                raise RuntimeError("state did not reach GitHub - a posted photo "
                                   "may be unrecorded and could be posted again")
            print(f"Done. posted={posted} vision_checks={checks}")
        except Exception as e:
            print(f"could not save state: {e}")
            raise


if __name__ == "__main__":
    main()
