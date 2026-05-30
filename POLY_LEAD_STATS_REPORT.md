# POLY_LEAD_STATS_REPORT

> **STATS_ONLY / DRY_RUN — No real orders placed. No capital at risk.**

Generated: 2026-05-18T00:31:14Z  
Elapsed: 14.0h  
Thresholds: JUMP_10S=0.02 | JUMP_30S=0.035 | JUMP_60S=0.06  
Filters: MIN_LIQ=500 | YES=[0.3,0.8]

---

## Summary

| Metric | Value |
|---|---|
| Total signals | **250** |
| Jump signals | 131 |
| Drop signals | 119 |
| Has positive expectation | ✅ YES |

## By Asset

| Asset | Signals |
|---|---|
| BTC | 79 |
| ETH | 91 |
| SOL | 80 |

## OKX Forward Return Analysis

### OKX +60s after Poly YES event

- **jump**: n=116 mean=-0.0564% win=28.4483% med=-0.0160% p25=-0.0821% p75=0.0155%
- **drop**: n=111 mean=-0.2225% win=41.4414% med=-0.0233% p25=-0.1736% p75=0.0487%
- **aligned**: n=227 mean=0.0800% win=43.1718% med=-0.0085% p25=-0.0670% p75=0.0470%

### OKX +180s after Poly YES event

- **jump**: n=115 mean=0.0237% win=33.9130% med=-0.0244% p25=-0.0938% p75=0.0470%
- **drop**: n=118 mean=-0.0749% win=37.2881% med=-0.0563% p25=-0.1976% p75=0.2127%
- **aligned**: n=233 mean=0.0496% win=48.4979% med=-0.0018% p25=-0.1042% p75=0.1971%

### OKX +300s after Poly YES event

- **jump**: n=83 mean=-0.0632% win=42.1687% med=-0.0655% p25=-0.2419% p75=0.1528%
- **drop**: n=88 mean=-0.1239% win=37.5000% med=-0.0545% p25=-0.2608% p75=0.1640%
- **aligned**: n=171 mean=0.0331% win=52.6316% med=0.0259% p25=-0.2084% p75=0.2177%

## BINANCE Forward Return Analysis

### BINANCE +60s after Poly YES event

- **jump**: n=95 mean=-0.0077% win=38.9474% med=-0.0108% p25=-0.0438% p75=0.0237%
- **drop**: n=102 mean=0.0385% win=50.9804% med=0.0075% p25=-0.0349% p75=0.1191%
- **aligned**: n=197 mean=-0.0237% win=44.1624% med=-0.0106% p25=-0.0821% p75=0.0283%

### BINANCE +180s after Poly YES event

- **jump**: n=75 mean=-0.0291% win=34.6667% med=-0.0394% p25=-0.0617% p75=0.0137%
- **drop**: n=94 mean=-0.0003% win=40.4255% med=-0.0151% p25=-0.0607% p75=0.0357%
- **aligned**: n=169 mean=-0.0127% win=47.9290% med=-0.0043% p25=-0.0540% p75=0.0462%

### BINANCE +300s after Poly YES event

- **jump**: n=88 mean=-0.0243% win=36.3636% med=-0.0205% p25=-0.0666% p75=0.0393%
- **drop**: n=81 mean=-0.0043% win=45.6790% med=-0.0131% p25=-0.0474% p75=0.0429%
- **aligned**: n=169 mean=-0.0106% win=44.9704% med=-0.0091% p25=-0.0586% p75=0.0473%

## BYBIT Forward Return Analysis

### BYBIT +60s after Poly YES event

- **jump**: n=95 mean=-0.0022% win=42.1053% med=-0.0058% p25=-0.0485% p75=0.0384%
- **drop**: n=104 mean=0.0483% win=59.6154% med=0.0200% p25=-0.0288% p75=0.1419%
- **aligned**: n=199 mean=-0.0263% win=40.7035% med=-0.0072% p25=-0.0974% p75=0.0353%

### BYBIT +180s after Poly YES event

- **jump**: n=94 mean=-0.0273% win=40.4255% med=-0.0061% p25=-0.0692% p75=0.0180%
- **drop**: n=106 mean=-0.0206% win=39.6226% med=-0.0337% p25=-0.1098% p75=0.0421%
- **aligned**: n=200 mean=-0.0019% win=51.0000% med=0.0034% p25=-0.0554% p75=0.0620%

### BYBIT +300s after Poly YES event

- **jump**: n=91 mean=-0.0175% win=43.9560% med=-0.0133% p25=-0.0774% p75=0.0353%
- **drop**: n=96 mean=0.0270% win=57.2917% med=0.0254% p25=-0.0275% p75=0.0744%
- **aligned**: n=187 mean=-0.0223% win=43.3155% med=-0.0236% p25=-0.0774% p75=0.0309%

---

## Exchange Comparison

| exchange | signals | win_rate_60s | mean_60s | median_60s | mean_180s | median_180s | mean_300s | median_300s |
|---|---|---|---|---|---|---|---|---|
| okx | 250 | 43.1718% | 0.0800% | -0.0085% | 0.0496% | -0.0018% | 0.0331% | 0.0259% |
| binance | 250 | 44.1624% | -0.0237% | -0.0106% | -0.0127% | -0.0043% | -0.0106% | -0.0091% |
| bybit | 250 | 40.7035% | -0.0263% | -0.0072% | -0.0019% | 0.0034% | -0.0223% | -0.0236% |

## Venue Ranking

🥇 **OKX** — pos_horizons=1 | win60=43.1718% | mean60=0.0800% | n60=227
🥈 **BINANCE** — pos_horizons=0 | win60=44.1624% | mean60=-0.0237% | n60=197
🥉 **BYBIT** — pos_horizons=0 | win60=40.7035% | mean60=-0.0263% | n60=199

- **best_venue:** `OKX`
- **second_venue:** `BINANCE`
- **weakest_venue:** `BYBIT`
- **recommend_paper_trade:** ✅ Yes — at least one venue shows positive expectation

---

## Recent Signals (last 20)

| Time | Asset | Dir | Win(s) | Mag | OKX+60s | OKX+300s | BNB+60s | BNB+300s | BYB+60s | BYB+300s |
|---|---|---|---|---|---|---|---|---|---|---|
| 00:27:14 | SOL | jump | 10s | 0.350 | -0.0117% | ⏳ | ⏳ | ⏳ | -0.0117% | ⏳ |
| 00:27:14 | ETH | drop | 10s | 0.500 | -0.0090% | ⏳ | 0.0340% | ⏳ | -0.0080% | ⏳ |
| 00:27:14 | SOL | jump | 10s | 0.130 | -0.0117% | ⏳ | ⏳ | ⏳ | -0.0117% | ⏳ |
| 00:27:14 | BTC | jump | 10s | 0.030 | 0.0000% | ⏳ | -0.0105% | ⏳ | 0.0017% | ⏳ |
| 00:27:14 | ETH | drop | 10s | 0.060 | -0.0090% | ⏳ | 0.0340% | ⏳ | -0.0080% | ⏳ |
| 00:27:15 | ETH | drop | 30s | 0.105 | -0.0421% | ⏳ | -0.0317% | ⏳ | -0.0170% | ⏳ |
| 00:27:15 | SOL | jump | 30s | 0.350 | -0.0117% | ⏳ | -0.0470% | ⏳ | -0.0470% | ⏳ |
| 00:27:15 | ETH | drop | 30s | 0.500 | -0.0421% | ⏳ | -0.0317% | ⏳ | -0.0170% | ⏳ |
| 00:27:15 | SOL | jump | 30s | 0.130 | -0.0117% | ⏳ | -0.0470% | ⏳ | -0.0470% | ⏳ |
| 00:27:15 | ETH | drop | 30s | 0.060 | -0.0421% | ⏳ | -0.0317% | ⏳ | -0.0170% | ⏳ |
| 00:27:16 | ETH | drop | 60s | 0.105 | -0.3436% | ⏳ | -0.2964% | ⏳ | -0.2933% | ⏳ |
| 00:27:16 | SOL | jump | 60s | 0.350 | -0.1996% | ⏳ | -0.2113% | ⏳ | -0.2114% | ⏳ |
| 00:27:16 | ETH | drop | 60s | 0.500 | -0.3436% | ⏳ | -0.2964% | ⏳ | -0.2933% | ⏳ |
| 00:27:16 | SOL | jump | 60s | 0.130 | -0.1996% | ⏳ | -0.2113% | ⏳ | -0.2114% | ⏳ |
| 00:27:16 | ETH | drop | 60s | 0.060 | -0.3436% | ⏳ | -0.2964% | ⏳ | -0.2933% | ⏳ |
| 00:27:46 | ETH | drop | 60s | 0.105 | -0.0449% | ⏳ | 0.1497% | ⏳ | 0.2800% | ⏳ |
| 00:27:46 | SOL | jump | 60s | 0.350 | 0.0000% | ⏳ | -0.0117% | ⏳ | 0.0000% | ⏳ |
| 00:27:46 | ETH | drop | 60s | 0.500 | -0.0449% | ⏳ | 0.1497% | ⏳ | 0.2800% | ⏳ |
| 00:27:46 | SOL | jump | 60s | 0.130 | 0.0000% | ⏳ | -0.0117% | ⏳ | 0.0000% | ⏳ |
| 00:27:46 | ETH | drop | 60s | 0.060 | -0.0449% | ⏳ | 0.1497% | ⏳ | 0.2800% | ⏳ |

---

## Recommendation

- ✅ Positive expectation detected across at least one horizon.
- Consider paper-trading with tight risk controls after 2+ independent confirming sessions.
- Recommended venue for paper trade: **OKX** (stronger lead response).

*STATS_ONLY / REAL_ORDER_DISABLED — no real orders placed.*

---

## Long Run Summary

- **Runtime:** 14.00h
- **Total signals:** 250
- **Per-hour signals (completed hrs):** [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 4]
- **Reconnects:** OKX=0 BNB=0 BYB=0
- **Discovery refreshes:** 82
