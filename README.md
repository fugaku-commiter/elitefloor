# Solana Self-Transfer Verification (Flask)

A minimal Flask web app where users enter a username and Solana address, are given a random amount under 0.01 SOL to self-transfer, and the app verifies them by polling public RPCs for a matching self-transfer transaction.

## Features
- Username + Solana address form
- Generates a random verification amount (0.0005 - 0.0099 SOL)
- Instructions to send the exact amount from the address to itself
- Polls public RPCs (Solana mainnet) to detect the transfer
- Displays "User verified with solana address" upon success

## Requirements
- Python 3.9+

## Setup
```bash
python -m venv .venv
.\.venv\Scripts\Activate.ps1  # Windows PowerShell
pip install -r requirements.txt
$env:FLASK_SECRET_KEY = "change-me"  # optional
python app.py
```

Then open http://localhost:5000

## Notes
- Uses public endpoints: `https://api.mainnet-beta.solana.com`, `https://rpc.ankr.com/solana`.
- Verification logic searches recent signatures and inspects `system` `transfer` instructions for a self-transfer equal to the requested amount.
- Keep the exact precision when sending the amount.

## Caveats
- Public RPCs may rate-limit; the app falls back between endpoints.
- If the address has very high activity, the recent history limit might miss older verification transfers; reload and retry if needed.
- No database is used; verification data lives in session cookies.


"# elitefloor" 
