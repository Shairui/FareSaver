# FareSaver

**Beat Uber surge pricing — know the cheapest window before you book.**

FareSaver is an individual marketing proposal project for RSM 8542 (Marketing Strategy, Spring 2026) at the University of Toronto. It consists of two deliverables:

1. **Customer-facing demo** — a Streamlit web app (`app.py`) that shows riders when to book to avoid surge
2. **Analytics module** — a Jupyter notebook (`FareSaver_Analytics_1.ipynb`) covering fare forecasting, user segmentation, CLV modelling, and pricing optimisation

---

## Project Structure

```
FareSaver/
├── app.py                        # Streamlit demo app
├── FareSaver_Analytics_1.ipynb   # Analytics notebook (Module F)
├── requirements.txt              # Python dependencies
└── README.md
```

---

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Run the demo app

```bash
streamlit run app.py
```

Opens at `http://localhost:8501`. Four tabs:

| Tab | What it shows |
|---|---|
| **Home** | Hero section — value proposition and key stats |
| **Plan My Trip** | Fare forecast bar chart + savings breakdown + price alert |
| **Premium** | Free vs Premium plan comparison ($2.20/month) |
| **About Us** | Mission, values, and team |

### 3. Run the analytics notebook

Open `FareSaver_Analytics_1.ipynb` in Jupyter Lab / Notebook / VS Code.

The notebook runs fully offline using a simulated dataset (50 k NYC Uber trips, 2015 calibration). To use the real Kaggle dataset, set `USE_REAL = True` in Section 0 and place `uber.csv` in the same folder.

---

## Course Context

RSM 8542 · Analytics for Marketing Strategy  
Founder: Xuezhu Liu · xzhu.liu@rotman.utoronto.ca
