"""Reconcile: what LP keyed vs the MHE Daily Movement file.

Matching key: Trip (MHE TripNo = our Document/Load/Trip No., leading zeros
ignored) + Store Code + Type (Pallet, Totebox).

The MHE file is written from the *store's* point of view, LP keys from the
*warehouse's* point of view:
    LP ขาออกจากคลัง (out)  <->  MHE "In"  (into the store)
    LP ขากลับเข้าคลัง (ret) <->  MHE "Out" (out of the store, back to DC)
Rollcage / Box are not in the MHE file.
"""
from collections import defaultdict

LEG_COL = {"ret": "qty_out", "out": "qty_in"}
MHE_COL_LABEL = {"ret": "Out", "out": "In"}
TYPE_LABEL = {"pallet": "Pallet", "totebox": "Totebox"}


def run(c, *, date_from, date_to, leg="ret", bu="", mtype="all"):
    col = LEG_COL[leg]
    types = ["pallet", "totebox"] if mtype == "all" else [mtype]

    q = """SELECT trip_no, trip_key, store_code, store_name, bu, mtype, tn_date, qty_in, qty_out
           FROM mhe_lines WHERE tn_date BETWEEN %s AND %s AND mtype = ANY(%s)"""
    args = [date_from, date_to, types]
    if bu:
        q += " AND bu = %s"
        args.append(bu)
    mhe = c.execute(q, args).fetchall()

    mhe_by_trip = defaultdict(list)
    for m in mhe:
        mhe_by_trip[m["trip_key"]].append(m)
    keys = list(mhe_by_trip)

    # LP records of this leg that match any MHE trip in range
    recs = c.execute(
        """SELECT r.id, r.doc_no, r.doc_key, r.load_key, r.trip_key, r.complete,
                  r.updated_at, u.display_name AS keyed_by
           FROM trip_records r LEFT JOIN users u ON u.id = r.updated_by
           WHERE r.leg = %s AND (r.doc_key = ANY(%s) OR r.load_key = ANY(%s) OR r.trip_key = ANY(%s))""",
        (leg, keys, keys, keys)).fetchall()
    rec_for_trip = {}
    for r in recs:
        for k in (r["doc_key"], r["load_key"], r["trip_key"]):
            if k and k in mhe_by_trip and k not in rec_for_trip:
                rec_for_trip[k] = r

    rec_ids = list({r["id"] for r in rec_for_trip.values()})
    lines_by_rec = defaultdict(dict)
    if rec_ids:
        for l in c.execute(
                "SELECT record_id, store_code, store_name, deleted, pallet, totebox FROM record_lines "
                "WHERE record_id = ANY(%s)", (rec_ids,)).fetchall():
            lines_by_rec[l["record_id"]][l["store_code"]] = l

    store_bu = {r["code"]: r["bu"] for r in c.execute("SELECT code, bu FROM stores").fetchall()}

    rows = []           # every compared pair
    only_mhe_trips = []
    for tk, mlines in mhe_by_trip.items():
        rec = rec_for_trip.get(tk)
        if not rec:
            tot = sum(m[col] for m in mlines)
            only_mhe_trips.append({
                "trip_no": mlines[0]["trip_no"], "tn_date": mlines[0]["tn_date"],
                "stores": len({m["store_code"] for m in mlines}), "qty": tot,
                "bu": ", ".join(sorted({m["bu"] for m in mlines if m["bu"]})),
            })
            continue
        lp = lines_by_rec[rec["id"]]
        mh = {(m["store_code"], m["mtype"]): m for m in mlines}
        stores = {m["store_code"] for m in mlines} | set(lp)
        for sc in stores:
            if bu and sc not in {m["store_code"] for m in mlines} and store_bu.get(sc, "") != bu:
                continue
            for t in types:
                m = mh.get((sc, t))
                l = lp.get(sc)
                lp_v = 0 if (l is None or l["deleted"]) else l[t]
                mhe_v = m[col] if m else 0
                if lp_v == 0 and mhe_v == 0:
                    continue
                rows.append({
                    "trip_no": mlines[0]["trip_no"], "doc_no": rec["doc_no"], "record_id": rec["id"],
                    "store_code": sc,
                    "store_name": (m and m["store_name"]) or (l and l["store_name"]) or "",
                    "bu": (m and m["bu"]) or store_bu.get(sc, ""),
                    "mtype": t, "lp": lp_v if l is not None else None, "mhe": mhe_v if m else None,
                    "diff": lp_v - mhe_v, "keyed_by": rec["keyed_by"] or "",
                    "status": "match" if lp_v == mhe_v else "mismatch",
                    "tn_date": mlines[0]["tn_date"],
                })

    # LP trips (created in range) that the MHE file doesn't have at all
    lp_only = c.execute(
        """SELECT r.id, r.doc_no, r.doc_key, r.load_key, r.trip_key, u.display_name AS keyed_by,
                  (r.created_at AT TIME ZONE 'Asia/Bangkok')::date AS d
           FROM trip_records r LEFT JOIN users u ON u.id = r.updated_by
           WHERE r.leg = %s AND (r.created_at AT TIME ZONE 'Asia/Bangkok')::date BETWEEN %s AND %s""",
        (leg, date_from, date_to)).fetchall()
    all_mhe_keys = set()
    cand = set()
    for r in lp_only:
        cand |= {r["doc_key"], r["load_key"], r["trip_key"]} - {""}
    if cand:
        all_mhe_keys = {x["trip_key"] for x in c.execute(
            "SELECT DISTINCT trip_key FROM mhe_lines WHERE trip_key = ANY(%s)", (list(cand),)).fetchall()}
    only_lp = []
    lp_only = [r for r in lp_only if not ({r["doc_key"], r["load_key"], r["trip_key"]} & all_mhe_keys)]
    if lp_only:
        ls = c.execute(
            "SELECT record_id, store_code, store_name, deleted, pallet, totebox FROM record_lines "
            "WHERE record_id = ANY(%s)", ([r["id"] for r in lp_only],)).fetchall()
        by = defaultdict(list)
        for l in ls:
            by[l["record_id"]].append(l)
        for r in lp_only:
            for l in by[r["id"]]:
                if bu and store_bu.get(l["store_code"], "") != bu:
                    continue
                for t in types:
                    v = 0 if l["deleted"] else l[t]
                    if v == 0:
                        continue
                    only_lp.append({
                        "trip_no": r["doc_no"], "doc_no": r["doc_no"], "record_id": r["id"],
                        "store_code": l["store_code"], "store_name": l["store_name"],
                        "bu": store_bu.get(l["store_code"], ""), "mtype": t, "lp": v, "mhe": None,
                        "diff": v, "keyed_by": r["keyed_by"] or "", "status": "only_lp",
                        "tn_date": r["d"],
                    })

    match = sum(1 for r in rows if r["status"] == "match")
    mismatch = sum(1 for r in rows if r["status"] == "mismatch")
    people = defaultdict(lambda: {"match": 0, "total": 0})
    for r in rows:
        p = people[r["keyed_by"] or "-"]
        p["total"] += 1
        p["match"] += r["status"] == "match"
    people_list = sorted(
        ({"name": k, **v, "pct": round(100 * v["match"] / v["total"], 1) if v["total"] else None}
         for k, v in people.items()), key=lambda x: (x["pct"] is None, -(x["pct"] or 0)))

    diffs = [r for r in rows if r["status"] == "mismatch"] + only_lp
    diffs.sort(key=lambda r: -abs(r["diff"]))
    only_mhe_trips.sort(key=lambda t: (t["tn_date"] or date_from, t["trip_no"]), reverse=True)

    bus = [r["bu"] for r in c.execute(
        "SELECT DISTINCT bu FROM mhe_lines WHERE bu <> '' ORDER BY bu").fetchall()]
    return {
        "summary": {
            "match": match, "mismatch": mismatch, "only_lp": len(only_lp),
            "only_mhe_trips": len(only_mhe_trips),
            "accuracy": round(100 * match / (match + mismatch), 1) if (match + mismatch) else None,
            "matched_trips": len(rec_for_trip), "mhe_trips": len(mhe_by_trip),
        },
        "people": people_list,
        "diffs": diffs,
        "only_mhe_trips": only_mhe_trips,
        "all_rows": rows + only_lp,
        "bus": bus,
    }
