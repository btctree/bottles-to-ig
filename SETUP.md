# One-time setup guide

Two keys are needed. Do these on a laptop browser. Total ~40 min.

---

## A. Gemini API key (free, ~3 min)

1. Go to **https://aistudio.google.com** and sign in with any Google account
2. Click **Get API key** (left sidebar or top right)
3. Click **Create API key** → **Create API key in new project**
4. Copy the key (starts with `AIza...`)
5. Add it to GitHub:
   - Open https://github.com/btctree/bottles-to-ig/settings/secrets/actions
   - Click **New repository secret**
   - Name: `GEMINI_API_KEY` — Secret: paste the key → **Add secret**

---

## B. Instagram access token (~30–40 min, the fiddly one)

Uses Meta's official "Instagram API with Instagram Login" — no Facebook Page
needed, free, no app review required for posting to YOUR OWN account.

### B1. Create a Meta developer account & app

1. Go to **https://developers.facebook.com** → **Log in** (use the
   Facebook/Meta login tied to you; any personal account works)
2. Accept developer terms if asked (verify with phone number if prompted)
3. Top right **My Apps** → **Create App**
4. Choose use case: **"Other"** → app type **"Business"** → Next
   (naming varies; if asked "What do you want your app to do?" pick anything
   with **Instagram** in it)
5. App name: `bottles-poster` (anything) → your email → **Create App**

### B2. Add the Instagram product

1. In the app dashboard, find **Instagram** → **Set up**
   (product may be called **"Instagram API setup with Instagram login"**)
2. Under **API setup with Instagram business login**, find
   **"Generate access tokens"** / **Add account**
3. Click **Add account** → log in as **drink_drink_goodmorning** → authorize
4. Click **Generate token** next to the account → approve → **copy the token**
   (long string starting with `IG...` or `EAA...`). This is a
   **long-lived token (60 days)** — the bot auto-renews it monthly.

### B3. Store the token

1. Open https://github.com/btctree/bottles-to-ig/settings/secrets/actions
2. **New repository secret** → Name: `IG_ACCESS_TOKEN` → paste → **Add secret**
3. Also add secret `ADMIN_PAT` = a GitHub fine-grained PAT with
   **Secrets: Read and write** + **Contents: Read and write** on this repo
   (github.com → Settings → Developer settings → Fine-grained tokens).
   This lets the bot renew the IG token by itself. Set expiry to 1 year.

### B4. Test

1. Repo → **Actions** tab → **daily-post** → **Run workflow**
2. Watch the log: it should print `IG account: drink_drink_goodmorning`,
   index existing posts, then post (or clearly say why it skipped).

---

## Troubleshooting

- **"Setup incomplete - missing secrets"** → a secret name is misspelled.
- **Token invalid / expired** → regenerate in the Meta app dashboard (B2.4),
  update the `IG_ACCESS_TOKEN` secret.
- **Nothing posted** → check the run log; every photo gets a one-line reason
  (already on IG / not qualified / could not identify).
