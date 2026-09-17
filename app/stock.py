from psycopg2.extras import RealDictCursor


def sync_stock_status_for_drug(cur, drug_id: int) -> None:
    cur.execute(
        """
        UPDATE drugs_master dm
        SET stock_status = CASE
            WHEN COALESCE((
                SELECT SUM(current_stock) FROM inventory_lots
                WHERE is_active = 1 AND expiration_date >= CURRENT_DATE AND drug_id = %s
            ), 0) <= 0 THEN 'out'
            WHEN COALESCE((
                SELECT SUM(current_stock) FROM inventory_lots
                WHERE is_active = 1 AND expiration_date >= CURRENT_DATE AND drug_id = %s
            ), 0) <= dm.minimum_stock THEN 'low'
            ELSE 'ok'
        END
        WHERE dm.drug_id = %s
        """,
        (drug_id, drug_id, drug_id),
    )


def match_prescription_to_stock(cur, text: str) -> list[dict]:
    import re

    normalized = " " + re.sub(r"[^A-Za-z0-9]+", " ", text).upper() + " "
    cur.execute(
        """
        SELECT dm.drug_id, dm.generic_name, dm.brand_name,
               COALESCE(SUM(CASE WHEN il.is_active = 1 AND il.expiration_date >= CURRENT_DATE
                                  THEN il.current_stock ELSE 0 END), 0) AS total_stock
        FROM drugs_master dm
        LEFT JOIN inventory_lots il ON dm.drug_id = il.drug_id
        WHERE dm.is_active = 1
        GROUP BY dm.drug_id, dm.generic_name, dm.brand_name
        """
    )
    matches = []
    for row in cur.fetchall():
        row = dict(row) if not isinstance(row, dict) else row
        candidates = []
        if row.get("generic_name"):
            candidates.append(str(row["generic_name"]).strip())
        if row.get("brand_name"):
            candidates.append(str(row["brand_name"]).strip())
        matched_name = None
        for name in dict.fromkeys(candidates):
            if not name:
                continue
            needle = " " + name.upper() + " "
            if needle in normalized:
                matched_name = name
                break
        if matched_name is not None:
            matches.append({
                "drug_id": int(row["drug_id"]),
                "name": matched_name,
                "generic_name": row.get("generic_name") or "",
                "brand_name": row.get("brand_name") or "",
                "status": "In Stock" if int(row["total_stock"] or 0) > 0 else "Out of Stock",
                "lot_id": None,
                "price": 0.0,
                "stock": 0,
            })
    if matches:
        ids = [m["drug_id"] for m in matches]
        cur.execute(
            """
            SELECT DISTINCT ON (drug_id)
                drug_id, lot_inventory_id, price, current_stock
            FROM inventory_lots
            WHERE is_active = 1
              AND expiration_date >= CURRENT_DATE
              AND current_stock > 0
              AND drug_id = ANY(%s)
            ORDER BY drug_id, expiration_date ASC
            """,
            (ids,),
        )
        lots = {}
        for raw in cur.fetchall():
            lot = dict(raw) if not isinstance(raw, dict) else raw
            lots[int(lot["drug_id"])] = lot
        for match in matches:
            lot = lots.get(match["drug_id"])
            if not lot:
                match["status"] = "Out of Stock"
                continue
            match["lot_id"] = int(lot["lot_inventory_id"])
            match["price"] = float(lot["price"] or 0)
            match["stock"] = int(lot["current_stock"] or 0)
            match["status"] = "In Stock" if match["stock"] > 0 else "Out of Stock"
    return matches
