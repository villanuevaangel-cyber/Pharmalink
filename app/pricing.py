DEFAULT_MARKUP_PERCENT = 30.0


def markup_percent_for_drug(cur, drug_id: int) -> float:
    cur.execute(
        """
        SELECT cm.markup_percent
        FROM drugs_master d
        LEFT JOIN category_markup cm
            ON LOWER(TRIM(cm.category)) = LOWER(TRIM(d.category))
        WHERE d.drug_id = %s
        """,
        (drug_id,),
    )
    row = cur.fetchone() or {}
    if row.get("markup_percent") is None:
        return DEFAULT_MARKUP_PERCENT
    return float(row["markup_percent"])


def selling_price_from_cost(cost, markup_percent: float) -> float:
    cost = float(cost or 0)
    if cost <= 0:
        return 0.0
    return round(cost * (1 + float(markup_percent) / 100.0), 2)


def compute_selling_price(cur, drug_id: int, cost_price) -> float:
    """Selling price is always cost × (1 + category markup%)."""
    return selling_price_from_cost(cost_price, markup_percent_for_drug(cur, drug_id))


def resolve_selling_price(cur, drug_id: int, cost_price, given_price=None) -> float:
    """Selling price comes from markup. given_price is ignored."""
    return compute_selling_price(cur, drug_id, cost_price)


def reprice_lots_for_category(cur, category: str, markup_percent: float) -> dict:
    """Set selling price = cost × (1 + markup%) on lots in this category that have a unit cost."""
    markup = float(markup_percent)
    cur.execute(
        """
        UPDATE inventory_lots il
        SET price = ROUND((il.cost_price::numeric) * (1 + %s / 100.0), 2)
        FROM drugs_master dm
        WHERE il.drug_id = dm.drug_id
          AND LOWER(TRIM(dm.category)) = LOWER(TRIM(%s))
          AND il.cost_price IS NOT NULL
          AND il.cost_price > 0
        """,
        (markup, category),
    )
    updated = int(cur.rowcount or 0)
    cur.execute(
        """
        SELECT COUNT(*) AS n
        FROM inventory_lots il
        JOIN drugs_master dm ON dm.drug_id = il.drug_id
        WHERE LOWER(TRIM(dm.category)) = LOWER(TRIM(%s))
          AND (il.cost_price IS NULL OR il.cost_price <= 0)
        """,
        (category,),
    )
    row = cur.fetchone() or {}
    if isinstance(row, dict):
        skipped = int(row.get("n") or 0)
    else:
        skipped = int(row[0] or 0)
    return {"updated": updated, "skipped_no_cost": skipped}
