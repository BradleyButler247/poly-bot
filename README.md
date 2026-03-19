# Polymarket AI Trading Bot

An autonomous trading bot for [Polymarket](https://polymarket.com) powered by Claude AI.

Each cycle it:
1. Fetches active markets from Polymarket's API
2. Searches the web for relevant news context
3. Uses Claude to reason about true probabilities vs. market odds
4. Applies your custom rules and risk limits
5. Places limit orders when edge is found

---

## Setup

### 1. Get Polymarket API credentials

1. Go to [polymarket.com](https://polymarket.com) and connect your wallet
2. Ensure your wallet is funded with USDC on Polygon
3. Navigate to **Profile → API** and generate your API key/secret/passphrase
4. Copy your wallet's private key (from MetaMask or your wallet app)

### 2. Get an Anthropic API key

Sign up at [console.anthropic.com](https://console.anthropic.com) and create an API key.

### 3. Deploy to Railway

```bash
# Install Railway CLI
npm install -g @railway/cli

# Login
railway login

# Create a new project
railway new

# Deploy
railway up
```

Then go to **Railway dashboard → Your project → Variables** and add all the variables from `.env.example`.

### 4. Configure your risk limits

Edit the variables in Railway to match your risk tolerance. **Start conservative:**

| Variable | Recommended start |
|---|---|
| `MAX_TRADE_USDC` | $5–10 |
| `MAX_DAILY_LOSS_USDC` | $25–50 |
| `MIN_EDGE` | 0.07 (7%) |
| `KELLY_FRACTION` | 0.25 |

---

## Customising trading rules

Edit the `SYSTEM_PROMPT` in `src/ai_analyst.py` to change how Claude reasons about markets. The section labelled `USER'S CUSTOM TRADING RULES` is where your preferences live.

Examples of rules you might add:
- "Only trade markets that resolve within 7 days"
- "Avoid any market about a specific person"
- "Prefer markets where news consensus is clear"

---

## Monitoring

Logs are written to two places:

- **stdout** → visible in Railway's live log view
- **logs/audit.jsonl** → structured JSON log of every decision

Each audit entry looks like:
```json
{"event": "analysis", "market_id": "...", "question": "Will X happen?", "should_trade": true, "your_probability": 0.72, "market_price": 0.55, "edge": 0.17, "reasoning": "..."}
{"event": "trade", "outcome": "YES", "price": 0.55, "usdc_size": 8.50, "success": true, "order_id": "..."}
```

---

## Emergency stop

If the bot behaves unexpectedly:

1. Go to Railway → your project → **Pause** the service immediately
2. The daily loss limit (`MAX_DAILY_LOSS_USDC`) is your automated circuit breaker
3. Open orders can be cancelled manually at polymarket.com

---

## Architecture

```
main.py                 ← entry point
src/
  bot.py               ← main loop + orchestration
  config.py            ← all config from env vars
  market_fetcher.py    ← fetches + filters markets from Gamma API
  ai_analyst.py        ← Claude analysis + web search
  risk_manager.py      ← hard limits + Kelly sizing
  trader.py            ← CLOB order execution
  audit_log.py         ← append-only decision log
logs/
  bot.log              ← human-readable log
  audit.jsonl          ← structured decision log
```

---

## Disclaimer

This bot trades real money autonomously. Prediction markets are risky. You can lose your entire deposit. Start with small amounts you can afford to lose entirely while evaluating performance.
