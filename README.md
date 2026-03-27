# Google Review Scraper API

Flask + Playwright API deployed on Render.com. Called by the Hat Trick iOS app to scrape Google Maps reviews.

## Deploy to Render (Step by Step)

### 1. Push this folder as its own GitHub repo

```bash
cd google-review-api
git init
git add .
git commit -m "Initial scraper API"
git remote add origin https://github.com/YOUR_USERNAME/google-review-scraper.git
git push -u origin main
```

### 2. Create a Render Web Service

1. Go to [https://render.com](https://render.com) → **New** → **Web Service**
2. Connect your GitHub account and select the `google-review-scraper` repo
3. Render will auto-detect `render.yaml` and fill in the settings
4. Click **Create Web Service**

Build takes ~5 minutes (installs Playwright + Chromium).

### 3. Get your API key

After deploy, go to **Environment** tab in Render dashboard.  
Copy the auto-generated `SCRAPER_API_KEY` value.  
Paste it into `my-google-reviews.page.ts` as `RENDER_API_KEY`.

### 4. Get your service URL

It will be something like: `https://google-review-scraper.onrender.com`  
Paste it into `my-google-reviews.page.ts` as `RENDER_API_URL`.

## API Endpoints

### GET /health
Returns `{ "status": "ok" }`

### POST /scrape
**Headers:** `X-API-Key: your-key-here`

**Body:**
```json
{
  "store": "cdjr",
  "first_name": "Brendan",
  "last_name": "Carrillo",
  "filter_5star": true
}
```

**Response:**
```json
{
  "success": true,
  "total": 450,
  "matched": 3,
  "reviews": [
    {
      "id": "abc123",
      "reviewer": "Jane Smith",
      "reviewerPhoto": "https://...",
      "rating": 5,
      "text": "Brendan was amazing...",
      "date": "3 months ago"
    }
  ]
}
```

## Store Keys
| Key | Dealership |
|-----|-----------|
| `cdjr` | Auffenberg CDJR |
| `mazda` | Auffenberg Mazda |
| `volkswagen` | Auffenberg Volkswagen |
| `nissan` | Auffenberg Nissan |
| `kia` | Auffenberg Kia |
| `ford_north` | Auffenberg Ford North |
| `ford_south` | Auffenberg Ford South |
| `econo` | Auffenberg Econo |

## Notes

- **Free tier sleeps after 15 min of inactivity.** First request after sleep takes ~30-60 seconds (cold start). The iOS app handles this with a longer timeout and "warming up" message.
- The scraper uses a real headless Chromium browser with session warm-up to bypass Google's bot detection.
- Set `SCRAPER_API_KEY` env var to protect the endpoint.
