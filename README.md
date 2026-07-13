# bottles-to-ig

Daily automation: wine & sake bottle photos from an iCloud Shared Album are
posted to Instagram (`drink_drink_goodmorning`) with bilingual hashtags.

## How it works (once a day, 09:00 HKT, GitHub Actions)

1. Read the public iCloud Shared Album **Bottles** (no login needed).
2. For each photo not yet handled (newest first):
   - **Dedup** — perceptual-hash compare against every existing Instagram post
     and everything posted before. Similar photo already on IG → skip.
   - **Qualify** — Gemini vision checks: one bottle, displayed at ~45°, label readable.
   - **Identify** — Gemini reads the label → sake or wine details.
   - **Caption** — hashtag template:
     - sake: name / type / rice polishing ratio / rice variety / brewery /
       city / flavor, each in Japanese + English, plus #japan #日本 #日本酒 tags
     - wine: name / country / region / village / grapes / vintage / flavor
   - **Filter** — one fixed preset (warmth + contrast + saturation) so the feed
     looks uniform; padded to Instagram's 4:5 ratio when needed.
   - **Publish** — image committed to `photos/`, Instagram fetches it from the
     raw URL via the official Instagram API.
3. State saved in `state/posted.json`; max 1 post/day (`config.json`).

## Secrets required (repo Settings → Secrets → Actions)

| Secret | What |
|---|---|
| `GEMINI_API_KEY` | Google AI Studio key (free tier) |
| `IG_ACCESS_TOKEN` | Instagram long-lived access token (see SETUP.md) |
| `ADMIN_PAT` | Fine-grained GitHub PAT with Secrets write — lets the bot auto-refresh the IG token every 30 days |

Until secrets are set, the daily run exits harmlessly with "Setup incomplete".

## Knobs (`config.json`)

- `posts_per_day` — default 1
- `max_vision_checks_per_run` — Gemini call budget per day (free-tier friendly)
- `phash_threshold` — duplicate sensitivity (lower = stricter)
- `order` — `newest_first` backlog order
