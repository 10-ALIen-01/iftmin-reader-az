import streamlit as st
import pandas as pd
import re
from io import StringIO
from datetime import datetime

st.set_page_config(
    page_title="IFTMIN Decoder • Amazon/MNG Friendly",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="expanded",
)

# -----------------------------
# 🔧 Helpers
# -----------------------------
SEG_SEP = "'"
COMP_SEP = "+"
SUB_SEP = ":"

DECIMAL_COMMA = re.compile(r"(?<=\d),(?=\d)")


def _to_float(val: str | None) -> float | None:
    if not val:
        return None
    # Replace decimal comma with dot safely
    val = DECIMAL_COMMA.sub(".", val)
    try:
        return float(val)
    except Exception:
        return None


def _dtm(value: str, fmt_code: str) -> str:
    """Format DTM values into ISO-like display."""
    try:
        if fmt_code == "203" and len(value) == 12:  # yyyymmddHHMM
            return datetime.strptime(value, "%Y%m%d%H%M").strftime("%Y-%m-%d %H:%M")
        if fmt_code == "204" and len(value) == 14:  # yyyymmddHHMMSS
            return datetime.strptime(value, "%Y%m%d%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
        if fmt_code == "102" and len(value) == 8:   # yyyymmdd
            return datetime.strptime(value, "%Y%m%d").strftime("%Y-%m-%d")
    except Exception:
        pass
    return value


def split_segments(text: str) -> list[tuple[str, list[str]]]:
    # Normalize line breaks/spaces
    raw = text.strip().replace("\n", "").replace("\r", "")
    parts = [p for p in raw.split(SEG_SEP) if p]
    segs: list[tuple[str, list[str]]] = []
    for p in parts:
        if not p:
            continue
        chunks = p.split(COMP_SEP)
        tag = chunks[0].strip()
        elems = [c for c in chunks[1:]]
        segs.append((tag, elems))
    return segs


# -----------------------------
# 🧠 EDIFACT IFTMIN Parser (focused, resilient)
# -----------------------------
class IFTMINParser:
    def __init__(self, text: str):
        self.text = text
        self.segments = split_segments(text)

    def header(self) -> dict:
        data: dict[str, str] = {}
        for tag, elems in self.segments:
            if tag == "UNB":
                # UNB+UNOC:3+sender:qual+receiver:qual+yyyymmdd:hhmm+control++++1+EANCOM
                data["syntax"] = elems[0] if elems else ""
                snd = elems[1] if len(elems) > 1 else ""
                rcv = elems[2] if len(elems) > 2 else ""
                dt = elems[3] if len(elems) > 3 else ""
                ctrl = elems[4] if len(elems) > 4 else ""
                data["sender"] = snd.split(SUB_SEP)[0]
                data["receiver"] = rcv.split(SUB_SEP)[0]
                # yyyymmdd:hhmm
                if dt and ":" in dt:
                    d, t = dt.split(":", 1)
                    try:
                        data["interchange_datetime"] = datetime.strptime(
                            d + t, "%y%m%d%H%M"
                        ).strftime("%Y-%m-%d %H:%M")
                    except Exception:
                        data["interchange_datetime"] = dt
                data["interchange_control"] = ctrl
            elif tag == "UNH":
                data["message_ref"] = elems[0] if elems else ""
                data["message_type"] = elems[1] if len(elems) > 1 else ""
            elif tag == "BGM":
                # BGM+87+<docno>+9
                data["document_type"] = elems[0] if elems else ""
                data["manifest_number"] = elems[1] if len(elems) > 1 else ""
                data["msg_function"] = elems[2] if len(elems) > 2 else ""
            elif tag == "DTM":
                # capture key DTM codes 9,10
                if elems:
                    comps = elems[0].split(":")
                    if len(comps) >= 2:
                        code = comps[0]
                        value = comps[1]
                        fmt = comps[2] if len(comps) > 2 else ""
                        key = {"9": "message_datetime", "10": "shipment_date"}.get(code)
                        if key:
                            data[key] = _dtm(value, fmt)
            elif tag == "CUX":
                # CUX+2:EUR
                if elems:
                    parts = elems[0].split(":")
                    data["currency"] = parts[1] if len(parts) > 1 else elems[0]
            elif tag == "TOD":
                # TOD++PP
                if len(elems) >= 2:
                    data["terms"] = elems[1]
            elif tag == "LOC" and elems:
                if elems[0] == "198+WTAM":
                    data["warehouse"] = "WTAM"
                elif elems[0].startswith("198"):
                    # LOC+198+XYZ
                    tok = elems[0].split("+")
                    if len(tok) >= 2:
                        data["warehouse"] = tok[1]
        return data

    def counts_and_amounts(self) -> dict:
        out: dict[str, float | int | str] = {}
        for tag, elems in self.segments:
            if tag == "CNT" and elems:
                qual, val = (elems[0].split(":") + [None])[:2]
                if qual == "2":
                    out["line_count"] = int(_to_float(val) or 0)
                elif qual == "7":
                    out["total_gross_weight_kg"] = _to_float(val)
                elif qual == "8":
                    out["shipment_count"] = int(_to_float(val) or 0)
                elif qual == "12":
                    out["total_value_eur"] = _to_float(val)
        return out

    def parties(self) -> dict:
        parties: dict[str, dict] = {}
        current_cta = None
        for tag, elems in self.segments:
            if tag == "NAD" and elems:
                qual = elems[0]
                rec = {
                    "qualifier": qual,
                    "party_id": (elems[1] if len(elems) > 1 else "").split(":")[0],
                    "name": (elems[3] if len(elems) > 3 else "").replace(":", " "),
                    "addr": (elems[4] if len(elems) > 4 else "").replace(":", " "),
                    "city": (elems[5] if len(elems) > 5 else ""),
                    "state": (elems[6] if len(elems) > 6 else ""),
                    "zip": (elems[7] if len(elems) > 7 else ""),
                    "country": (elems[8] if len(elems) > 8 else ""),
                }
                parties[qual] = rec
            elif tag == "CTA" and elems:
                current_cta = elems[0]
                parties.setdefault("CTA", {})["role"] = current_cta
            elif tag == "COM" and elems:
                tel = elems[0].split(":")[0]
                parties.setdefault("CTA", {})["tel"] = tel
            elif tag == "RFF" and elems and elems[0].startswith("VA:"):
                parties.setdefault("IV", {}).setdefault("refs", {})["VAT"] = elems[0][3:]
        return parties

    def _shipment_groups(self) -> list[list[tuple[str, list[str]]]]:
        # group by GID sections
        groups: list[list[tuple[str, list[str]]]] = []
        idxs = [i for i, (t, _) in enumerate(self.segments) if t == "GID"]
        if not idxs:
            return groups
        for j, start in enumerate(idxs):
            end = idxs[j + 1] if j + 1 < len(idxs) else len(self.segments)
            groups.append(self.segments[start:end])
        return groups

    def _extract_items_from_pci(self, segs: list[tuple[str, list[str]]]) -> list[dict]:
        items = []
        pending_item = None
        for tag, elems in segs:
            if tag == "PCI" and elems:
                # Example: PCI+ZZZ+Unknown:0000.00.0000:TR:1:EA:528,00:528,00
                comp = ":".join(elems)  # merge in case of '+' split
                fields = comp.split(":")
                # Last two fields tend to be unit price and extended price (TRY)
                unit_price = _to_float(fields[-2]) if len(fields) >= 2 else None
                qty = None
                uom = None
                try:
                    # ...:1:EA:...
                    qty = _to_float(fields[-4]) if len(fields) >= 4 else None
                    uom = fields[-3] if len(fields) >= 3 else None
                except Exception:
                    pass
                pending_item = {
                    "uom": uom,
                    "qty": qty,
                    "unit_price_try": unit_price,
                }
            elif tag == "RFF" and elems and elems[0].startswith("VP:"):
                asin = elems[0][3:]
                if pending_item is None:
                    pending_item = {}
                pending_item["asin"] = asin
                items.append(pending_item)
                pending_item = None
        return items

    def shipments(self) -> list[dict]:
        result: list[dict] = []
        for group in self._shipment_groups():
            record: dict[str, any] = {
                "packages": None,
                "mode": None,
                "destination_city": None,
                "destination_country": None,
                "route": None,
                "monetary": {},
                "terms": {},
                "consignee": {},
                "weights": {},
                "dimensions_cm": {},
                "dates": {},
                "refs": {},
                "items": [],
            }
            for tag, elems in group:
                if tag == "GID" and elems:
                    # GID+<seq>+<qty>:PK
                    if len(elems) >= 2 and ":" in elems[1]:
                        qty = elems[1].split(":")[0]
                        record["packages"] = int(float(qty))
                elif tag == "TMD" and elems:
                    record["mode"] = elems[0].split(":")[-1]
                elif tag == "LOC" and elems:
                    # LOC+7+City  / LOC+25+Country / LOC+193+Route
                    first = elems[0]
                    if first.startswith("7+") or first.startswith("7"):
                        record["destination_city"] = first.split("+")[-1]
                    elif first.startswith("25+") or first.startswith("25"):
                        record["destination_country"] = first.split("+")[-1]
                    elif first.startswith("193+") or first.startswith("193"):
                        record["route"] = first.split("+")[-1]
                elif tag == "MOA" and elems:
                    # MOA+<qual>:<amount>
                    qual, amt = (elems[0].split(":") + [None])[:2]
                    record["monetary"][qual] = _to_float(amt)
                elif tag == "FTX" and elems:
                    if elems[0] == "AAR":
                        record["terms"]["delivery_terms"] = (elems[2] if len(elems) > 2 else "").strip()
                    elif elems[0] == "AAH":
                        record["terms"]["reason_for_export"] = (elems[2] if len(elems) > 2 else "").strip()
                elif tag == "NAD" and elems and elems[0] == "CN":
                    # Consignee address pieces come across multiple comps
                    name = (elems[2] if len(elems) > 2 else "")
                    name2 = (elems[3] if len(elems) > 3 else "")
                    street = (elems[4] if len(elems) > 4 else "").replace(":", " ")
                    city = (elems[5] if len(elems) > 5 else "")
                    state = (elems[6] if len(elems) > 6 else "")
                    zip_code = (elems[7] if len(elems) > 7 else "")
                    country = (elems[8] if len(elems) > 8 else "")
                    record["consignee"] = {
                        "name": (name + (" " + name2 if name2 else "")).strip("+ "),
                        "street": street,
                        "city": city,
                        "state": state,
                        "zip": zip_code,
                        "country": country,
                    }
                elif tag == "MEA" and elems:
                    # MEA+WT+G+KG:.00  MEA+WX+B+KG:3.00
                    if elems[0] == "WT":
                        # Gross
                        last = elems[-1]
                        if ":" in last:
                            unit, val = last.split(":")
                            record["weights"]["gross_kg"] = _to_float(val)
                    elif elems[0] == "WX":
                        last = elems[-1]
                        if ":" in last:
                            unit, val = last.split(":")
                            record["weights"]["declared_kg"] = _to_float(val)
                elif tag == "DIM" and elems:
                    # DIM+2+CMT:10.0:50.0:12.0
                    comp = elems[1] if len(elems) > 1 else ""
                    parts = comp.split(":") if comp else []
                    if len(parts) >= 4:
                        record["dimensions_cm"] = {
                            "length": _to_float(parts[1]),
                            "width": _to_float(parts[2]),
                            "height": _to_float(parts[3]),
                        }
                elif tag == "DTM" and elems:
                    c = elems[0].split(":")
                    if len(c) >= 2:
                        code, value = c[0], c[1]
                        fmt = c[2] if len(c) > 2 else ""
                        key = {
                            "17": "scheduled_delivery",
                            "200": "pickup_time",
                            "3": "invoice_date",
                        }.get(code)
                        if key:
                            record["dates"][key] = _dtm(value, fmt)
                elif tag == "RFF" and elems:
                    # RFF+CR:tracking / +TB:order / +TE:phone / +IV:pkg barcode
                    if elems[0].startswith("CR:"):
                        record["refs"]["tracking"] = elems[0][3:]
                    elif elems[0].startswith("TB:"):
                        record["refs"]["order_id"] = elems[0][3:]
                    elif elems[0].startswith("TE:"):
                        record["refs"]["phone"] = elems[0][3:]
                # Items are paired PCI + RFF+VP
            # After looping, extract item list from PCI/RFF pairs inside group
            record["items"] = self._extract_items_from_pci(group)
            result.append(record)
        return result


# -----------------------------
# 🎨 UI — Sidebar
# -----------------------------
st.sidebar.title("📦 IFTMIN Decoder")
st.sidebar.markdown(
    """
**Upload .edi / .txt** with IFTMIN content, or paste into the text box.

**Tip:** You can upload *multiple* IFTMIN files — results will be merged and comparable.
    """
)

uploaded = st.sidebar.file_uploader("Upload IFTMIN file(s)", type=["edi", "txt"], accept_multiple_files=True)
example_btn = st.sidebar.button("Use example from chat")

# -----------------------------
# 🧾 Input Area
# -----------------------------
st.title("IFTMIN Decoder ✨")
st.caption("Transforms EDIFACT IFTMIN manifests into clean, human-friendly views and exports.")

if example_btn:
    edi_text_input = (
        "UNA:+,? 'UNB+UNOC:3+5450534000000:14+MNGMFN:14+251013:0023+2243369++++1+EANCOM'"
        "UNH+1+IFTMIN:D:01A:UN:EAN008'BGM+87+1027214650005003+9'DTM+9:202510130023:203'DTM+10:20251013:102'"
        "TSR+1+5+4'CUX+2:EUR'FTX+DIN'CNT+2:6'CNT+7:6,0'CNT+8:2'CNT+12:63.37'TOD++PP'LOC+198+WTAM'"
        "RFF+ADJ:UNKW'RFF+CN:1027214650005003'RFF+IV:TJ4gj3FhN'RFF+DM:1'RFF+EQ:1'"
        "NAD+SF+::9++WTAM+Organize Deri Sanayi Bolgesi, Nokra:caddesi 1/A carsibasi Kozmetik Tuzl+Istanbul+Istanbul+34956+TR'"
        "NAD+IV+5450534005821::9++AMAZON EU SARL:SUCCURSALE FRANCAISE+67 BOULEVARD DU GENERAL LECLERC+CLICHY++92110+FR'"
        "CTA+TR'COM+0161081000:TE'RFF+VA:FR12487773327'GID+1+5:PK'TMD+9:MNG_EXPD_DOM'LOC+7+Afyonkarahisar'LOC+25+Turkey'LOC+193+MNG-TR-WTAM'"
        "MOA+ZZZ:58,28'M OA+141:0'M OA+40:5234'M OA+64:0'M OA+189:0'M OA+67:0'M OA+22:0'M OA+101:0'"
        "FTX+AAR++DDU'FTX+AAH++PERM'"
        "NAD+SE+0000000000000::9+n/a+notelephonenumber:noemailaddress+n/a+nocityname'"
        "NAD+CN++SELÇUK ÇOBANBAY++Kemal Aşkar Cad.:Öztabak apt. No?:2 K?:1 D?:2::Merkez+Afyonkarahisar+Derviş Paşa Mh.+03200+TR'"
        "MEA+WT+G+KG:.00'M EA+WX+B+KG:3.00'DIM+2+CMT:10.0:50.0:12.0'RFF+IV:TJ4gj3FhN_1'DTM+17:20251017:102'DTM+200:20251013110500'DTM+3:20251310:102'"
        "RFF+CR:ZR226361'RFF+TE:5445656666'RFF+TB:407-6554903-7357969'RFF+ANT:noemailaddress'"
        "PCI+ZZZ+Unknown:0000.00.0000:TR:1:EA:528,00:528,00'RFF+VP:B0B8TH8P45'"
        "PCI+ZZZ+Unknown:0000.00.0000:TR:1:EA:532,00:532,00'RFF+VP:B0BHDTQL18'"
        "PCI+ZZZ+Unknown:0000.00.0000:TR:1:EA:411,20:411,20'RFF+VP:B0B8XRZ2XY'"
        "PCI+ZZZ+Unknown:0000.00.0000:TR:1:EA:545,60:545,60'RFF+VP:B0BH995VC1'"
        "PCI+ZZZ+Unknown:0000.00.0000:TR:1:EA:527,20:527,20'RFF+VP:B0BNNL2S8K'"
        "GID+2+1:PK'TMD+9:MNG_EXPD_DOM'LOC+7+İstanbul'LOC+25+Turkey'LOC+193+MNG-TR-WTAM'MOA+ZZZ:58,28'MOA+40:1103'"
        "FTX+AAR++DDU'FTX+AAH++PERM'NAD+CN++Korkut Tüysüz++Yenişehir mahallesi çadır sokak:Kardelen sitesi Ablok daire 5::Pendik+İstanbul+Yenişehir Mh.+34912+TR'"
        "MEA+WT+G+KG:.50'M EA+WX+B+KG:3.00'DIM+2+CMT:33.0:26.0:2.5'RFF+IV:TGlWJxFQN_1'DTM+17:20251016:102'DTM+200:20251013110500'DTM+3:20251310:102'"
        "RFF+CR:ZR226178'RFF+TE:5333323138'RFF+TB:171-4425958-1031536'RFF+ANT:noemailaddress'PCI+ZZZ+Unknown:0000.00.0000:TR:1:EA:536,00:536,00'RFF+VP:B0BM6X8KLR'"
        "UNT+92+1'UNZ+1+2243369'"
    )
else:
    edi_text_input = ""

text_area = st.text_area(
    "Paste IFTMIN EDI here",
    value=edi_text_input,
    height=180,
    placeholder="Paste raw EDIFACT IFTMIN (segments end with ')')",
)

# Collect texts from uploaded files
uploaded_texts = []
if uploaded:
    for f in uploaded:
        try:
            uploaded_texts.append((f.name, f.read().decode("utf-8", errors="replace")))
        except Exception:
            uploaded_texts.append((f.name, f.read().decode("latin-1", errors="replace")))

if text_area.strip():
    uploaded_texts.insert(0, ("pasted.edi", text_area))

if not uploaded_texts:
    st.info("Upload or paste at least one IFTMIN file to begin.")
    st.stop()

# -----------------------------
# 🧰 Parse all files
# -----------------------------
all_shipments_rows = []
file_summaries = []

for fname, content in uploaded_texts:
    parser = IFTMINParser(content)
    hdr = parser.header()
    cnt = parser.counts_and_amounts()
    parties = parser.parties()
    shipments = parser.shipments()

    # Build per-shipment rows
    for i, sh in enumerate(shipments, start=1):
        for it in (sh["items"] or [{}]):
            row = {
                "file": fname,
                "manifest_number": hdr.get("manifest_number"),
                "shipment_index": i,
                "warehouse": hdr.get("warehouse"),
                "currency": hdr.get("currency"),
                "destination_city": sh.get("destination_city"),
                "destination_country": sh.get("destination_country"),
                "route": sh.get("route"),
                "packages": sh.get("packages"),
                "gross_kg": sh.get("weights", {}).get("gross_kg"),
                "declared_kg": sh.get("weights", {}).get("declared_kg"),
                "length_cm": sh.get("dimensions_cm", {}).get("length"),
                "width_cm": sh.get("dimensions_cm", {}).get("width"),
                "height_cm": sh.get("dimensions_cm", {}).get("height"),
                "scheduled_delivery": sh.get("dates", {}).get("scheduled_delivery"),
                "pickup_time": sh.get("dates", {}).get("pickup_time"),
                "invoice_date": sh.get("dates", {}).get("invoice_date"),
                "order_id": sh.get("refs", {}).get("order_id"),
                "tracking": sh.get("refs", {}).get("tracking"),
                "phone": sh.get("refs", {}).get("phone"),
                "consignee_name": sh.get("consignee", {}).get("name"),
                "consignee_street": sh.get("consignee", {}).get("street"),
                "consignee_zip": sh.get("consignee", {}).get("zip"),
                "consignee_city": sh.get("consignee", {}).get("city"),
                "consignee_state": sh.get("consignee", {}).get("state"),
                "consignee_country": sh.get("consignee", {}).get("country"),
                "moa_ZZZ": sh.get("monetary", {}).get("ZZZ"),
                "moa_40": sh.get("monetary", {}).get("40"),
                "asin": it.get("asin"),
                "qty": it.get("qty"),
                "uom": it.get("uom"),
                "unit_price_try": it.get("unit_price_try"),
                "delivery_terms": sh.get("terms", {}).get("delivery_terms"),
                "reason_for_export": sh.get("terms", {}).get("reason_for_export"),
            }
            all_shipments_rows.append(row)

    file_summaries.append({
        "file": fname,
        **{f"hdr_{k}": v for k, v in hdr.items()},
        **{f"cnt_{k}": v for k, v in cnt.items()},
        "shipments_found": len(shipments),
    })

# -----------------------------
# 📊 Display
# -----------------------------
left, right = st.columns([1, 1])
with left:
    st.subheader("📁 Files Summary")
    df_sum = pd.DataFrame(file_summaries)
    st.dataframe(df_sum, use_container_width=True)
    csv_sum = df_sum.to_csv(index=False).encode("utf-8")
    st.download_button("Download summary CSV", csv_sum, file_name="iftmin_summary.csv")

with right:
    st.subheader("📦 Shipments & Items (flattened)")
    df_ship = pd.DataFrame(all_shipments_rows)
    st.dataframe(df_ship, use_container_width=True)
    csv_ship = df_ship.to_csv(index=False).encode("utf-8")
    st.download_button("Download shipments CSV", csv_ship, file_name="iftmin_shipments.csv")

# -----------------------------
# 🧭 Deep Dive per Shipment (cards)
# -----------------------------
for fname, content in uploaded_texts:
    st.markdown("---")
    st.markdown(f"### 🗂️ File: `{fname}`")

    parser = IFTMINParser(content)
    hdr = parser.header()
    cnt = parser.counts_and_amounts()
    parties = parser.parties()
    shipments = parser.shipments()

    header_cols = st.columns(4)
    header_cols[0].metric("Manifest #", hdr.get("manifest_number", "—"))
    header_cols[1].metric("Currency", hdr.get("currency", "—"))
    header_cols[2].metric("Warehouse", hdr.get("warehouse", "—"))
    header_cols[3].metric("Shipments", len(shipments))

    subcols = st.columns(3)
    subcols[0].write(f"**Message datetime:** {hdr.get('message_datetime', '—')}")
    subcols[0].write(f"**Shipment date:** {hdr.get('shipment_date', '—')}")
    subcols[1].write(f"**Sender → Receiver:** {hdr.get('sender', '—')} → {hdr.get('receiver', '—')}")
    subcols[2].write(f"**Terms:** {hdr.get('terms', '—')}")

    st.caption("Counts and totals from CNT/MOA segments")
    totals = {
        "Line items": cnt.get("line_count"),
        "Shipments": cnt.get("shipment_count"),
        "Gross weight (kg)": cnt.get("total_gross_weight_kg"),
        "Total value (EUR)": cnt.get("total_value_eur"),
    }
    st.write(pd.DataFrame([totals]))

    for idx, sh in enumerate(shipments, start=1):
        st.markdown(f"#### 📦 Shipment {idx}")
        top = st.columns(4)
        top[0].metric("Packages", sh.get("packages"))
        top[1].metric("Route", sh.get("route", "—"))
        top[2].metric("City", sh.get("destination_city", "—"))
        top[3].metric("Country", sh.get("destination_country", "—"))

        mid = st.columns(3)
        w = sh.get("weights", {})
        mid[0].write(f"**Weights (kg):** gross={w.get('gross_kg', '—')}, declared={w.get('declared_kg', '—')}")
        d = sh.get("dimensions_cm", {})
        mid[1].write(f"**Dims (cm):** L={d.get('length', '—')}, W={d.get('width', '—')}, H={d.get('height', '—')}")
        m = sh.get("monetary", {})
        mid[2].write(f"**MOA:** ZZZ={m.get('ZZZ', '—')}, 40={m.get('40', '—')}")

        dates = sh.get("dates", {})
        cols = st.columns(3)
        cols[0].write(f"**Scheduled delivery:** {dates.get('scheduled_delivery', '—')}")
        cols[1].write(f"**Pickup time:** {dates.get('pickup_time', '—')}")
        cols[2].write(f"**Invoice date:** {dates.get('invoice_date', '—')}")

        refs = sh.get("refs", {})
        cols2 = st.columns(3)
        cols2[0].write(f"**Order ID:** {refs.get('order_id', '—')}")
        cols2[1].write(f"**Tracking:** {refs.get('tracking', '—')}")
        cols2[2].write(f"**Phone:** {refs.get('phone', '—')}")

        cons = sh.get("consignee", {})
        st.write(
            f"**Consignee:** {cons.get('name', '—')} — {cons.get('street', '')}, {cons.get('city', '')} {cons.get('zip', '')}, {cons.get('state', '')}, {cons.get('country', '')}"
        )

        items = sh.get("items", [])
        if items:
            df_items = pd.DataFrame(items)
            st.dataframe(df_items, use_container_width=True)
            # attach context columns for export
            df_items2 = df_items.copy()
            df_items2.insert(0, "shipment_index", idx)
            df_items2.insert(1, "manifest_number", hdr.get("manifest_number"))
            csv_items = df_items2.to_csv(index=False).encode("utf-8")
            st.download_button(
                f"Download items CSV – Shipment {idx}",
                csv_items,
                file_name=f"items_manifest_{hdr.get('manifest_number','')}_ship_{idx}.csv",
                use_container_width=True,
            )
        else:
            st.info("No item-level PCI/RFF pairs found in this shipment.")

# -----------------------------
# 🧾 Raw viewer
# -----------------------------
with st.expander("🔎 Raw segments (parsed)"):
    for fname, content in uploaded_texts:
        st.markdown(f"**{fname}**")
        for tag, elems in split_segments(content):
            st.code(f"{tag}+{' + '.join(elems)}'", language="edi")

st.success("Done! If you need XML cross-checking, we can add an XML tab later.")
