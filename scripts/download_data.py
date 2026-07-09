"""Загрузка дневных OHLC по индексам и другим инструментам через yfinance.

Запуск с машины, где есть интернет (например, терминал PyCharm):

    pip install yfinance
    python scripts/download_data.py

Файлы сохраняются в data/raw/<ИМЯ>.csv в формате, который понимает
utils.data.load_csv: колонки date,open,high,low,close,volume.

Правь словарь TICKERS под нужные инструменты. Тикеры Yahoo:
  индексы США:   ^GSPC (S&P500), ^IXIC (Nasdaq Comp), ^NDX (Nasdaq100), ^DJI (Dow), ^RUT (Russell2000)
  индексы Европы: ^GDAXI (DAX), ^STOXX50E (EuroStoxx50), ^FTSE (FTSE100), ^FCHI (CAC40), ^STOXX (STOXX600), ^IBEX (IBEX35)
  прочее:        BTC-USD, GC=F (золото фьюч), CL=F (нефть WTI), ES=F (S&P фьюч)
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import yfinance as yf

OUT = Path(__file__).resolve().parent.parent / "data" / "raw"

# имя_файла -> тикер Yahoo
TICKERS = {
    # --- США ---
    "SP500":    "^GSPC",
    "NASDAQ":   "^IXIC",
    "DOW":      "^DJI",
    "RUSSELL":  "^RUT",
    # --- Европа ---
    "DAX":      "^GDAXI",
    "ESTOXX50": "^STOXX50E",
    "FTSE100":  "^FTSE",
    "CAC40":    "^FCHI",
}

START = "2000-01-01"


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for name, ticker in TICKERS.items():
        try:
            df = yf.download(ticker, start=START, interval="1d",
                             auto_adjust=False, progress=False)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
            df.index.name = "date"
            df = df.dropna(subset=["close"])
            out = OUT / f"{name}.csv"
            df.to_csv(out)
            print(f"OK  {name:10} {ticker:10} {len(df):5} баров  "
                  f"{df.index.min().date()}..{df.index.max().date()}  -> {out.name}")
        except Exception as e:
            print(f"FAIL {name:10} {ticker:10} {e}")


if __name__ == "__main__":
    main()
