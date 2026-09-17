#!/usr/bin/env python3
"""
forecast_prophet.py
--------------------
Real machine-learning sales forecasting for PharmaLink, powered by
Facebook Prophet (https://facebook.github.io/prophet/).

Contract (stdin -> stdout, both JSON):
  IN:
    {
      "period": 30,
      "today": "2026-07-11",
      "range_start": "2026-01-12",
      "daily_sales": [{"date": "2026-07-01", "total": 1709.99, "qty": 76}, ...],
      "items": {
        "19": {"name": "Paracetamol (RiteMed)", "category": "analgesic",
               "series": [{"date": "2026-07-01", "qty": 5}, ...]},
        ...
      }
    }
    `daily_sales`/`items[*].series` only need to contain rows for days that
    HAD sales — this script zero-fills every day between `range_start` and
    `today` before fitting, so Prophet correctly learns "no sales that day"
    as a real zero rather than a gap in the timeline.

  OUT (success):
    {"success": true, "insufficient_data": false, "period": 30,
     "forecast": {"labels": [...], "values": [...], "lower": [...], "upper": [...]},
     "predicted_total_sales": ..., "predicted_items_sold": ...,
     "top_category": ..., "top_items": {"labels": [...], "data": [...]},
     "history_days": ..., "engine": "prophet"}

  OUT (failure): {"success": false, "error": "..."}  (exit code 1)
    The PHP caller falls back to a simpler trend model when this happens.

Model: linear-growth Prophet, weekly seasonality enabled once there's at
least ~2 weeks of history (yearly/daily seasonality stay off — a pharmacy's
history is rarely long/granular enough to estimate those reliably).
"""
import sys
import json
import logging
import warnings

# Prophet/cmdstanpy print progress + INFO logs to stderr; silence them so
# nothing can end up mixed into the stdout JSON the PHP caller parses.
logging.getLogger('prophet').setLevel(logging.ERROR)
logging.getLogger('cmdstanpy').setLevel(logging.ERROR)
warnings.filterwarnings('ignore')

import pandas as pd  # noqa: E402
from prophet import Prophet  # noqa: E402

MIN_DAYS_WITH_DATA = 2
MAX_ITEM_MODELS = 12  # cap per-drug models so a large catalog stays fast


def zero_fill(rows, value_key, range_start, range_end):
    """
    rows: list of {"date": "YYYY-MM-DD", value_key: number}
    Returns a continuous daily pandas Series indexed by date from
    range_start to range_end inclusive, with 0 on days absent from `rows`.
    """
    idx = pd.date_range(start=range_start, end=range_end, freq='D')
    s = pd.Series(0.0, index=idx)
    for row in rows:
        d = pd.Timestamp(row['date'])
        if idx[0] <= d <= idx[-1]:
            s.loc[d] += float(row[value_key])
    return s


def fit_and_forecast(series, period):
    """
    series: continuous daily pandas Series (zero-filled), most recent date
            is the forecast anchor ("today").
    Returns a DataFrame of the `period` future rows: ds, yhat, yhat_lower, yhat_upper.
    """
    df = pd.DataFrame({'ds': series.index, 'y': series.values})

    span_days = (df['ds'].max() - df['ds'].min()).days
    weekly = span_days >= 14

    model = Prophet(
        growth='linear',
        yearly_seasonality=False,
        weekly_seasonality=weekly,
        daily_seasonality=False,
        seasonality_mode='additive',
        interval_width=0.8,
    )
    model.fit(df)

    future = model.make_future_dataframe(periods=period, freq='D', include_history=False)
    forecast = model.predict(future)

    for col in ('yhat', 'yhat_lower', 'yhat_upper'):
        forecast[col] = forecast[col].clip(lower=0)

    return forecast[['ds', 'yhat', 'yhat_lower', 'yhat_upper']]


def main():
    payload = json.loads(sys.stdin.read())

    period = int(payload.get('period', 30))
    today = payload['today']
    range_start = payload['range_start']
    daily_sales = payload.get('daily_sales', [])
    items = payload.get('items', {})

    days_with_sales = len({row['date'] for row in daily_sales})
    if days_with_sales < MIN_DAYS_WITH_DATA:
        print(json.dumps({
            'success': True,
            'insufficient_data': True,
            'message': 'Not enough sales history yet to generate a reliable forecast. Record at least 2 days of completed sales first.',
            'history_days': days_with_sales,
            'engine': 'prophet',
        }))
        return

    sales_series = zero_fill(daily_sales, 'total', range_start, today)
    qty_series = zero_fill(daily_sales, 'qty', range_start, today)

    sales_forecast = fit_and_forecast(sales_series, period)
    qty_forecast = fit_and_forecast(qty_series, period)

    forecast_labels = [d.strftime('%b %-d') for d in sales_forecast['ds']]
    forecast_values = [round(float(v), 2) for v in sales_forecast['yhat']]
    forecast_lower = [round(float(v), 2) for v in sales_forecast['yhat_lower']]
    forecast_upper = [round(float(v), 2) for v in sales_forecast['yhat_upper']]

    predicted_total_sales = round(float(sales_forecast['yhat'].sum()), 2)
    predicted_items_sold = int(round(float(qty_forecast['yhat'].sum())))

    # ---- Per-drug forecasts (Top Forecasted Items + Top Category) ----
    ranked_drug_ids = sorted(
        items.keys(),
        key=lambda k: sum(pt['qty'] for pt in items[k]['series']),
        reverse=True,
    )[:MAX_ITEM_MODELS]

    item_forecasts = []
    category_totals = {}

    for drug_id in ranked_drug_ids:
        info = items[drug_id]
        series_rows = info['series']
        days_with_item_sales = len({pt['date'] for pt in series_rows})

        if days_with_item_sales < MIN_DAYS_WITH_DATA:
            predicted_qty = sum(pt['qty'] for pt in series_rows)
        else:
            try:
                item_series = zero_fill(series_rows, 'qty', range_start, today)
                item_forecast = fit_and_forecast(item_series, period)
                predicted_qty = round(float(item_forecast['yhat'].sum()))
            except Exception:
                predicted_qty = sum(pt['qty'] for pt in series_rows)

        item_forecasts.append({'name': info['name'], 'predicted_qty': int(predicted_qty)})
        cat = info.get('category') or 'Uncategorized'
        category_totals[cat] = category_totals.get(cat, 0) + predicted_qty

    item_forecasts.sort(key=lambda x: x['predicted_qty'], reverse=True)
    top_items = item_forecasts[:5]

    top_category = 'N/A'
    if category_totals:
        top_category = max(category_totals, key=category_totals.get)

    print(json.dumps({
        'success': True,
        'insufficient_data': False,
        'period': period,
        'forecast': {
            'labels': forecast_labels,
            'values': forecast_values,
            'lower': forecast_lower,
            'upper': forecast_upper,
        },
        'predicted_total_sales': predicted_total_sales,
        'predicted_items_sold': predicted_items_sold,
        'top_category': top_category,
        'top_items': {
            'labels': [i['name'] for i in top_items],
            'data': [i['predicted_qty'] for i in top_items],
        },
        'history_days': days_with_sales,
        'engine': 'prophet',
    }))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({'success': False, 'error': str(exc)}))
        sys.exit(1)