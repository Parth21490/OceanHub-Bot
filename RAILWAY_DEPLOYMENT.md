# 🚀 OceanHub Railway Deployment Guide

This folder (`OceanHub Server`) is fully configured and ready for 1-click or CLI deployment on **Railway** (https://railway.app).

---

## 🛠️ Step-by-Step Railway Deployment

### Option 1: GitHub + Railway Dashboard (Recommended)

1. **Create a GitHub Repository**:
   - Go to [GitHub](https://github.com/new) and create a new repository (e.g. `oceanhub-server`).
   - Push this `OceanHub Server` folder to your GitHub repo:
     ```bash
     cd "C:\Users\parth\OneDrive\Desktop\OceanHub Server"
     git init
     git add .
     git commit -m "Initial OceanHub Railway setup"
     git branch -M main
     git remote add origin https://github.com/YOUR_USERNAME/oceanhub-server.git
     git push -u origin main
     ```

2. **Deploy on Railway**:
   - Log in to [Railway.app](https://railway.app).
   - Click **"New Project"** -> **"Deploy from GitHub repo"**.
   - Select your `oceanhub-server` repository.
   - Railway will automatically detect the `Dockerfile` inside `backend/Dockerfile` (or `docker-compose.yml`) and begin building!

3. **Configure Environment Variables in Railway**:
   Inside your Railway service dashboard -> **Variables** tab, add:
   - `PORT`: `8080` (Railway will assign dynamically)
   - `DRY_RUN_MODE`: `True`
   - `GEMINI_API_KEY`: `your_gemini_api_key` *(Optional for daily AI reports)*
   - `DISCORD_WEBHOOK_URL`: `your_discord_webhook_url` *(Optional for live alerts)*

4. **Generate Public Domain**:
   - Go to the **Settings** tab in Railway -> **Networking** -> **Generate Domain**.
   - Your OceanHub backend will now be live on `https://oceanhub-server-production.up.railway.app`!

---

### Option 2: Railway CLI (Direct Command Line Deploy)

1. **Install Railway CLI** (if not installed):
   ```bash
   npm i -g @railway/cli
   ```

2. **Deploy from CLI**:
   ```bash
   cd "C:\Users\parth\OneDrive\Desktop\OceanHub Server"
   railway login
   railway init
   railway up
   ```

---

## 📁 Directory Structure
- `backend/`: Core Master Agent ML pipeline (`server.py`, `master_agent.py`, `ml_brain.py`, `models/`, `bot_state.json`)
- `railway.json`: Deployment configuration file for Railway.
- `docker-compose.yml`: Local & Docker container orchestrator.

---

## 🟢 Features Preserved for Production Deployment
- **State Persistence**: `bot_state.json`, `wallet_state.json`, and `trade_history.json` persist state across restarts.
- **Microstructure OBI & Maker Limits**: High-conviction entry timing active.
- **Dynamic 20x-25x Leverage**: Automatic confidence-based leverage scaling.
- **Trade Cooldown & Smart TP**: Prevents signal echo & front-runs target fills.
