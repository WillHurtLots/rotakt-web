# RotaktWeb

Autonomous daily auditor for **rotakt.ro** & **micromobilitate.ro**.

Pulls the full product catalog from each site via the public WooCommerce
Store API, then publishes a Marketing dashboard on GitHub Pages.

## Ce face

- Numără produsele active per site (în stoc / fără stoc)
- Detectează produsele fără descriere (HTML strip < 20 caractere)
- Salvează snapshot zilnic + history pentru trenduri
- Generează raport markdown + JSON consumat de dashboard
- Rulează zilnic la 06:30 UTC via GitHub Actions
- Publică [dashboard-ul](./index.html) automat pe GitHub Pages

## Local run

```bash
pip install -r requirements.txt
python rotakt_web_agent.py
```

Apoi deschide `index.html` (servește local: `python -m http.server`).

## Output

| Path | Conținut |
|---|---|
| `data/latest.json` | snapshot curent (consumat de dashboard) |
| `data/history.json` | time series cu KPI per zi |
| `data/snapshot_<site>_YYYY-MM-DD.json` | snapshot brut per site |
| `reports/report_YYYY-MM-DD.md` | raport markdown citibil |
| `index.html` | dashboard Marketing (GitHub Pages) |

## Configurare repo (one-time)

1. Push pe GitHub
2. Settings → Pages → Source: **GitHub Actions**
3. Workflow-ul rulează zilnic și deploys automat
