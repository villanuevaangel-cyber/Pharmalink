from psycopg2.extras import RealDictCursor


def sync_stock_status_for_drug(cur, drug_id: int) -> None:
    cur.execute(
        """
        UPDATE drugs_master dm
        SET stock_status = CASE
            WHEN COALESCE(dm.minimum_stock, 0) = 0
             AND NOT EXISTS (SELECT 1 FROM inventory_lots WHERE drug_id = dm.drug_id)
            THEN 'ok'
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
                "stock": int(row["total_stock"] or 0),
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
            match["status"] = "In Stock" if match["stock"] > 0 else "Out of Stock"
    return matches


def sellable_lots(cur, drug_id: int, lock: bool = False) -> list[dict]:
    cur.execute(
        f"""
        SELECT lot_inventory_id, current_stock, price, expiration_date
        FROM inventory_lots
        WHERE drug_id = %s
          AND is_active = 1
          AND expiration_date >= CURRENT_DATE
          AND current_stock > 0
        ORDER BY expiration_date ASC, lot_inventory_id ASC
        {"FOR UPDATE" if lock else ""}
        """,
        (drug_id,),
    )
    return [dict(row) if not isinstance(row, dict) else row for row in cur.fetchall()]


def sellable_quantity(cur, drug_id: int) -> int:
    return sum(int(lot["current_stock"] or 0) for lot in sellable_lots(cur, drug_id))


def drug_id_for_lot(cur, lot_id: int) -> int:
    cur.execute(
        "SELECT drug_id FROM inventory_lots WHERE lot_inventory_id = %s",
        (lot_id,),
    )
    row = cur.fetchone()
    if not row:
        raise ValueError("One of the items in the cart is no longer available.")
    row = dict(row) if not isinstance(row, dict) else row
    return int(row["drug_id"])


def split_money(total: float, qty: int, part_qtys: list[int]) -> list[float]:
    amount = round(float(total or 0), 2)
    if not part_qtys:
        return []
    if qty <= 0:
        return [0.0 for _ in part_qtys]
    shares = []
    used = 0.0
    for index, part_qty in enumerate(part_qtys):
        if index == len(part_qtys) - 1:
            share = round(amount - used, 2)
        else:
            share = round(amount * part_qty / qty, 2)
            used += share
        shares.append(share)
    return shares


def allocate_expiring_first(cur, drug_id: int, qty: int) -> list[dict]:
    """Take stock from the soonest expiration first, then the next lot."""
    if qty <= 0:
        raise ValueError("Quantity must be at least 1.")
    remaining = qty
    parts = []
    for lot in sellable_lots(cur, drug_id, lock=True):
        available = int(lot["current_stock"] or 0)
        take = min(remaining, available)
        if take <= 0:
            continue
        parts.append({
            "lot_id": int(lot["lot_inventory_id"]),
            "qty": take,
            "price": float(lot["price"] or 0),
        })
        remaining -= take
        if remaining == 0:
            break
    if remaining > 0:
        on_hand = qty - remaining
        raise ValueError(f"Not enough stock. Only {on_hand} left, but {qty} requested.")
    for part in parts:
        cur.execute(
            "UPDATE inventory_lots SET current_stock = current_stock - %s WHERE lot_inventory_id = %s",
            (part["qty"], part["lot_id"]),
        )
    return parts


def restore_order_stock(cur, order_id: int) -> None:
    """Put reserved units back on the lots this order took."""
    cur.execute(
        """
        SELECT drug_id, lot_inventory_id, quantity
        FROM order_details
        WHERE order_id = %s
        FOR UPDATE
        """,
        (order_id,),
    )
    drugs = set()
    for raw in cur.fetchall():
        row = dict(raw) if not isinstance(raw, dict) else raw
        qty = int(row.get("quantity") or 0)
        lot_id = int(row.get("lot_inventory_id") or 0)
        drug_id = int(row.get("drug_id") or 0)
        if qty <= 0 or lot_id <= 0:
            continue
        cur.execute(
            """
            UPDATE inventory_lots
            SET current_stock = current_stock + %s
            WHERE lot_inventory_id = %s
            """,
            (qty, lot_id),
        )
        if cur.rowcount != 1:
            raise ValueError("Could not return stock for one of the items.")
        if drug_id:
            drugs.add(drug_id)
    for drug_id in drugs:
        sync_stock_status_for_drug(cur, drug_id)
