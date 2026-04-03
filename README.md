# CricPulse – IPL Live Score App

Real-time cricket scores scraped from **Cricbuzz** via a Python Vercel backend.

## 📁 Project Structure

```
cricpulse/
├── index.html          ← Frontend (open directly OR serve from Vercel)
├── vercel.json         ← Vercel deployment config
├── requirements.txt    ← Python deps (httpx)
└── api/
    ├── scores.py       ← /api/scores  — live matches list
    └── scorecard.py    ← /api/scorecard?id=X — batting/bowling/ball-by-ball
```

## 🚀 Deploy to Vercel (Free)

### Option A: Vercel CLI
```bash
npm i -g vercel
cd cricpulse
vercel --prod
```

### Option B: GitHub → Vercel
1. Push this folder to a GitHub repo
2. Go to [vercel.com/new](https://vercel.com/new)
3. Import the repo → Deploy
4. Done! Your URL: `https://cricpulse-xxx.vercel.app`

## 🌐 API Endpoints

| Endpoint | Description |
|---|---|
| `GET /api/scores` | All current + upcoming matches |
| `GET /api/scores?filter=live` | Live matches only |
| `GET /api/scores?filter=ipl` | IPL matches only |
| `GET /api/scorecard?id={cricbuzz_id}` | Full scorecard + ball-by-ball |
| `GET /api/scorecard?id={id}&include=scorecard,bbb,xi` | Select data |

## 📱 Using the HTML from Anywhere

If you want to use `index.html` **outside Vercel** (e.g. locally), update the `API_BASE` variable:

```js
// In index.html, line ~2:
const API_BASE = 'https://your-app.vercel.app';
```

The HTML also has a **CricketData API fallback** with your key built in, so it works even without Vercel.

## 🔑 Data Sources

| Priority | Source | How |
|---|---|---|
| 1st | Cricbuzz (via Vercel) | Python scrapes Next.js chunks |
| 2nd (fallback) | CricketData API | Key: `0aaa3768-a8c4-4a0a-b869-6bbab7afca30` |

## ⚡ Features

- 🔴 Live match cards with real Cricbuzz data
- 📊 Full batting + bowling scorecard  
- 🏏 Ball-by-ball commentary grouped by over
- 👥 Playing XI (after toss)
- 🔄 Auto-refresh every 30 seconds
- 📱 Mobile-first glass UI
- 🏆 Team logos from Wikipedia
- 🖼️ Stock photos from Unsplash (no watermarks)
