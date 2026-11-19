# viralvibe-renderer

Renders ViralVibe Story Engine reels on GitHub Actions, so the app runs without a browser open.

Every 4 hours (`.github/workflows/render.yml`) it asks the app for the next fully narrated story
(`GET /api/render/next`), renders a 1080x1920 MP4 with ffmpeg (title card, captions, follow card)
and uploads it (`POST /api/render/complete`). Up to 2 reels per run.

## Settings (repo Settings → Secrets and variables → Actions)
- Secret `RENDER_KEY` — must match the app's `RENDER_KEY` secret in Base44
- Variable `APP_URL` — `https://industrious-viral-story-snap.base44.app`

Run it by hand from **Actions → Render reels → Run workflow**.
