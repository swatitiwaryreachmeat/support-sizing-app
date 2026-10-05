"""Deterministic sizing model. Claude never does this arithmetic; it calls these functions as tools."""
import math

REF = dict(inc=12000, sr=2000, cr=50, crh=40, tpd=2.25, hpd=9, ovh=1.15, cap=240, l1=10, on=15)
LEVERS = [21, 21, 17, 10, 8]
BUFFER = 3

PARAM_DOC = {
    "inc": "incidents per year",
    "sr": "service requests per year",
    "cr": "change requests per year",
    "crh": "hours per change request",
    "tpd": "tickets handled per person-day",
    "hpd": "hours per person-day",
    "ovh": "overhead multiplier (1.15 = 15%)",
    "cap": "productive days per FTE per year",
    "l1": "percent of incidents/SRs handled at L1 (0-100)",
    "on": "percent of FTE placed onsite (0-100)",
}


def clean(p=None):
    """Merge overrides onto the reference case, coerce to floats, clamp to sane ranges."""
    out = dict(REF)
    for k, v in (p or {}).items():
        if k in out:
            try:
                out[k] = float(v)
            except (TypeError, ValueError):
                pass
    out["inc"] = max(0.0, out["inc"])
    out["sr"] = max(0.0, out["sr"])
    out["cr"] = max(0.0, out["cr"])
    out["crh"] = max(1.0, out["crh"])
    out["tpd"] = max(0.1, out["tpd"])
    out["hpd"] = max(1.0, out["hpd"])
    out["ovh"] = max(1.0, out["ovh"])
    out["cap"] = max(1.0, out["cap"])
    out["l1"] = min(100.0, max(0.0, out["l1"]))
    out["on"] = min(100.0, max(0.0, out["on"]))
    return out


def size(p=None):
    p = clean(p)
    l1, on = p["l1"] / 100, p["on"] / 100
    inc_d, sr_d, cr_d = p["inc"] / p["tpd"], p["sr"] / p["tpd"], p["cr"] * p["crh"] / p["hpd"]
    raw = inc_d + sr_d + cr_d
    inc_o, sr_o, cr_o = inc_d * p["ovh"], sr_d * p["ovh"], cr_d * p["ovh"]
    total_o = inc_o + sr_o + cr_o
    l1_pd = (inc_o + sr_o) * l1
    l2_pd = (inc_o + sr_o) * (1 - l1) + cr_o
    l1_fte, l2_fte = l1_pd / p["cap"], l2_pd / p["cap"]
    total = l1_fte + l2_fte
    on_l1, on_l2 = l1_fte * on, l2_fte * on
    off_l1, off_l2 = l1_fte - on_l1, l2_fte - on_l2
    design = math.ceil(total - 1e-9)
    d_on_l2 = math.ceil(on_l2 - 1e-9)
    d_off_l1 = math.ceil(off_l1 - 1e-9)
    d_off_l2 = max(0, design - d_on_l2 - d_off_l1)
    r = lambda x, n=2: round(x, n)
    return {
        "params": p,
        "effort_person_days": {
            "incidents_raw": r(inc_d, 1), "srs_raw": r(sr_d, 1), "crs_raw": r(cr_d, 1), "total_raw": r(raw, 1),
            "incidents_with_overhead": r(inc_o, 1), "srs_with_overhead": r(sr_o, 1),
            "crs_with_overhead": r(cr_o, 1), "total_with_overhead": r(total_o, 1),
        },
        "fte": {
            "l1": r(l1_fte), "l2": r(l2_fte), "total": r(total),
            "onsite": r(on_l1 + on_l2), "offshore": r(off_l1 + off_l2),
            "onsite_l1": r(on_l1), "onsite_l2": r(on_l2), "offshore_l1": r(off_l1), "offshore_l2": r(off_l2),
        },
        "rounded_design": {
            "total": d_on_l2 + d_off_l1 + d_off_l2, "onsite_l2": d_on_l2,
            "offshore_l1": d_off_l1, "offshore_l2": d_off_l2,
        },
        "recommended_headcount": design + BUFFER,
        "implied_hours_per_ticket": r(p["hpd"] / p["tpd"]),
    }


def project(p=None, levers=None, mode="comp"):
    """Five-year headcount. mode 'comp' compounds each lever on the prior year; 'cum' adds them."""
    p = clean(p)
    base = size(p)["fte"]["total"]
    lev = [float(x) for x in (levers if levers and len(levers) == 5 else LEVERS)]
    lev = [min(100.0, max(0.0, x)) for x in lev]
    prod, cum, rows = 1.0, 0.0, []
    for i, x in enumerate(lev):
        if mode == "cum":
            cum = min(100.0, cum + x)
            factor = max(0.0, 1 - cum / 100)
        else:
            prod *= 1 - x / 100
            factor = prod
        work = base * factor
        hc = math.ceil(work + BUFFER - 1e-9)
        onsite = max(3, round(hc * p["on"] / 100))
        rows.append({"year": i + 1, "lever_pct": x, "reduction_pct": round((1 - factor) * 100, 1),
                     "workload_fte": round(work, 1), "headcount": hc, "onsite": onsite, "offshore": hc - onsite})
    return {"mode": mode, "levers": lev, "buffer_fte": BUFFER, "years": rows}


def check_inputs(p=None):
    """Deterministic sanity checks that need no AI. Claude reviews these alongside free-text assumptions."""
    p = clean(p)
    out = []
    hpt = p["hpd"] / p["tpd"]
    if hpt < 5:
        out.append({"severity": "medium", "title": "Handling time per ticket looks light",
                    "detail": f"{p['tpd']} tickets per {p['hpd']:g}-hour day is {hpt:.1f} hours per ticket, applied to every ticket type."})
    if p["ovh"] > 1:
        pct = p["ovh"] - 1
        alt = 1 / (1 - pct) if pct < 1 else p["ovh"]
        if abs(alt - p["ovh"]) > 0.005:
            out.append({"severity": "low", "title": "Overhead method changes the answer",
                        "detail": f"Multiplying by {p['ovh']:.2f} differs from dividing by {1 - pct:.2f} (x{alt:.3f}), about {abs(alt / p['ovh'] - 1) * 100:.1f}% more effort."})
    if p["cap"] >= 235:
        out.append({"severity": "medium", "title": "Leave may be counted twice",
                    "detail": f"{p['cap']:g} productive days already removes leave and holidays from about 261 weekdays. Check the overhead does not remove it again."})
    if p["l1"] <= 10:
        out.append({"severity": "low", "title": "Thin L1 layer",
                    "detail": f"Only {p['l1']:g}% of tickets are L1, so L2 will absorb routine work."})
    if p["cr"] > 0 and p["crh"] >= 40:
        out.append({"severity": "low", "title": "Change request effort is high",
                    "detail": f"{p['crh']:g} hours per CR. Confirm it against the other input set, which used 16 hours."})
    return out
