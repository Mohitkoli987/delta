# Auto Trading Bot - Render Deployment Ready

## Features
- **Auto Trading Bot** with continuous trading loop
- **MySQL Database** with 9MB size management
- **Decision Bot** with random signal generation
- **Keep Alive** system for Render free tier
- **WebSocket** real-time position monitoring
- **TP/SL Management** with automatic attachment

## Deployment on Render.com

### 1. Push to GitHub
```bash
git add .
git commit -m "Ready for Render deployment"
git push origin main
```

### 2. Create Render Web Service
1. Go to [Render.com](https://render.com)
2. Connect your GitHub repository
3. Create new **Web Service**
4. Select **Docker** environment
5. Set environment variables (see below)

### 3. Environment Variables
```bash
# MySQL Database
MYSQL_HOST=bmh1rsh5f0sjmncv6ydc-mysql.services.clever-cloud.com
MYSQL_PORT=3306
MYSQL_USER=ujokhsx1defubkot
MYSQL_PASSWORD=hILZGFpJ60exq4oGj2hv
MYSQL_DB=bmh1rsh5f0sjmncv6ydc

# Delta Exchange API
DELTA_API_KEY=your_api_key
DELTA_API_SECRET=your_api_secret

# Render Configuration
RENDER_EXTERNAL_URL=https://your-app-name.onrender.com
```

### 4. Keep Alive System
- Automatically pings `/ping` endpoint every 10 minutes
- Prevents Render free tier sleep
- Runs in background thread
- Works 24/7 without interruption

## Files Structure
```
/
|- app.py              # Main Flask application
|- decision_bot.py     # Random signal generator
|- keepalive.py        # Render keep alive system
|- Dockerfile          # Docker configuration
|- render.yaml         # Render deployment config
|- requirements.txt    # Python dependencies
|- .env.example        # Environment variables template
|- start.sh           # Startup script
|- templates/
|  |- index.html      # Frontend UI
```

## API Endpoints
- `GET /` - Main trading interface
- `GET /ping` - Health check (for keepalive)
- `POST /api/start-bot` - Start trading bot
- `POST /api/stop-bot` - Stop trading bot
- `GET /api/bot-status` - Get bot status
- `GET /api/trade-history` - Get trade history

## Database Management
- **MySQL** on Clever Cloud
- **Auto cleanup** when > 8.5MB (9MB limit)
- **Oldest trades** deleted first
- **Minimum 10 rows** always preserved

## Trading Logic
- **Loss** = Double lot size
- **Profit** = Reset to base lot
- **TP** = 1% (configurable)
- **SL** = 0.5% (configurable)
- **Continuous loop** until stopped

## Monitoring
- Real-time position monitoring
- WebSocket updates
- Trade history with P&L
- Database size tracking
- Keep alive status logs

## Notes
- **No backend changes** - all logic preserved
- **24/7 operation** with keep alive
- **Free tier compatible** on Render
- **MySQL migration** complete
- **Size management** automatic
