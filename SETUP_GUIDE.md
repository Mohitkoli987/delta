# Triple Keepalive Setup Guide

## Your Bot URL: https://delta-vjnt.onrender.com

## Layer 1: Internal Keepalive (Already Running) 
- keepalive.py pings every 10 minutes
- Built into your Flask app

## Layer 2: GitHub Actions (Ready to Deploy)
- File: `.github/workflows/keepalive.yml`
- Runs every 5 minutes
- Push to GitHub to activate

## Layer 3: Upptime (Manual Setup Required)

### Quick Setup:
1. Go to https://upptime.js.org/
2. Fork their template repository
3. Copy contents of `upptime.yml` to your fork
4. Replace their sites section with our config
5. Deploy to GitHub Pages

### Alternative: UptimeRobot
1. Sign up at https://uptimerobot.com/
2. Create HTTP Monitor
3. URL: https://delta-vjnt.onrender.com/ping
4. Interval: 5 minutes
5. Free plan allows 50 monitors

## Deployment Commands:

```bash
# Push GitHub Actions
git add .github/
git commit -m "Add GitHub Actions keepalive"
git push origin main

# GitHub Actions will auto-start
# Check: https://github.com/Mohitkoli987/delta/actions
```

## Testing:
```bash
# Test ping endpoint
curl https://delta-vjnt.onrender.com/ping
```

Expected response: `{"status": "alive", "timestamp": "..."}`
