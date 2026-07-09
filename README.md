# algo

Исследование стратегий алго-трейдинга: боты и бэктесты. Основной рынок — фьючерсы / forex.

## Структура

- `strategies/` — реализации стратегий (сигналы, правила входа/выхода).
- `backtest/` — движок и раннеры бэктестов, конфиги прогонов.
- `data/raw/` — raw historical data grouped by instrument folders, for example `data/raw/EURUSD/EURUSDd1.csv` (not in git).
- `data/processed/` — подготовленные датасеты (не в git).
- `reports/` — сгенерированные отчёты и графики (не в git).
- `notebooks/` — исследовательские ноутбуки.
- `utils/` — общие утилиты (загрузка данных, метрики).
- `tests/` — тесты.

## Окружение

    python -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt

## Рабочий процесс

1. Идея стратегии -> быстрый скрининг на vectorbt.
2. Валидация на event-driven движке (nautilus_trader) с реалистичными комиссиями/плечом/роллами.
3. Метрики (Sharpe, Sortino, max DD, CAGR, win rate) -> отчёт в reports/.
4. Решения и результаты фиксируются в памяти проекта Claude.
