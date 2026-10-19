"""Generate synthetic GST invoices with known correct answers.

Because we create every invoice, we know exactly what a perfect extraction
looks like. That gives us an eval set for free, and lets us inject the hard
cases on purpose:

  clean           Matches its purchase order exactly
  po_mismatch     Invoiced quantity or price differs from the PO
  no_po           No purchase order referenced
  arithmetic      Printed total doesn't equal subtotal + tax
  duplicate       Same invoice re-sent as a different file (different layout)
  scanned         Rendered to a slightly rotated, noisy image (like a phone scan)

Outputs (under --out, default data/generated):
  invoices/*.pdf|*.jpg    the documents
  manifest.json           sha256 -> ground truth + expected check codes
  purchase_orders.csv     POs to import, in Ledgerline's CSV format

    python -m scripts.generate_invoices --count 30 --seed 7
"""

import argparse
import csv
import hashlib
import io
import json
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from faker import Faker
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

from app.gstin import checksum_char

BUYER = {
    "name": "Acme Manufacturing Pvt Ltd",
    "gstin": "33AABCA1234D1ZK",
    "address": "Plot 14, SIPCOT Industrial Park, Sriperumbudur, Tamil Nadu 602105",
    "state": "33",
}

STATES = {"33": "Tamil Nadu", "29": "Karnataka", "27": "Maharashtra", "07": "Delhi", "24": "Gujarat"}
CITIES = {"33": ["Chennai", "Coimbatore", "Madurai"], "29": ["Bengaluru", "Mysuru", "Hubballi"],
          "27": ["Mumbai", "Pune", "Nagpur"], "07": ["New Delhi"], "24": ["Ahmedabad", "Surat", "Vadodara"]}
PIN_PREFIX = {"33": "6", "29": "5", "27": "4", "07": "11", "24": "3"}

CATALOG = {
    "office": [("A4 copier paper, 500 sheets", "4802", 12, 280, 340), ("Ergonomic office chair", "9401", 18, 6500, 9800),
               ("Whiteboard markers, box of 10", "9608", 18, 220, 320), ("Steel filing cabinet, 4 drawer", "9403", 18, 11500, 15000)],
    "it": [("Laptop, 14-inch, 16GB RAM", "8471", 18, 58000, 72000), ("27-inch monitor", "8528", 18, 14500, 19800),
           ("Wireless keyboard and mouse set", "8471", 18, 1900, 2800), ("Annual antivirus licence", "9973", 18, 900, 1400),
           ("Network switch, 24 port", "8517", 18, 16000, 23000)],
    "industrial": [("Hydraulic oil, 20 L drum", "2710", 18, 4200, 5600), ("Safety helmets", "6506", 18, 350, 520),
                   ("Ball bearings 6205-2RS", "8482", 18, 180, 260), ("Welding electrodes, 5 kg", "8311", 18, 1300, 1750),
                   ("Industrial gloves, pair", "4015", 12, 90, 160)],
    "services": [("Facility cleaning services (monthly)", "9985", 18, 42000, 60000), ("Security services (monthly)", "9985", 18, 65000, 90000),
                 ("Annual maintenance contract: HVAC", "9987", 18, 38000, 52000)],
    "food": [("Packaged drinking water, 20 L can", "2201", 18, 60, 90), ("Tea and coffee supplies", "0902", 5, 1800, 2600),
             ("Pantry snacks assortment", "1905", 18, 2400, 3500)],
}

TWO = Decimal("0.01")


def q2(d: Decimal) -> Decimal:
    return d.quantize(TWO, rounding=ROUND_HALF_UP)


def make_gstin(rng: random.Random, state: str) -> str:
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    pan = "".join(rng.choice(letters) for _ in range(3)) + rng.choice("CFP") + rng.choice(letters)
    pan += f"{rng.randint(1000, 9999)}" + rng.choice(letters)
    first14 = f"{state}{pan}1Z"
    return first14 + checksum_char(first14)




@dataclass
class Vendor:
    name: str
    gstin: str
    state: str
    address: str
    category: str
    number_style: int
    layout: int


@dataclass
class Line:
    description: str
    hsn: str
    rate: int
    quantity: Decimal
    unit_price: Decimal

    @property
    def amount(self) -> Decimal:
        return q2(self.quantity * self.unit_price)

    @property
    def tax(self) -> Decimal:
        return q2(self.amount * self.rate / 100)


@dataclass
class Invoice:
    vendor: Vendor
    number: str
    issued: date
    po_number: str | None
    lines: list[Line]
    scenario: str
    printed_total_override: Decimal | None = None
    layout: int = 0
    expected_flags: list[str] = field(default_factory=list)

    @property
    def intra_state(self) -> bool:
        return self.vendor.state == BUYER["state"]

    @property
    def subtotal(self) -> Decimal:
        return sum((line.amount for line in self.lines), Decimal(0))

    @property
    def tax_total(self) -> Decimal:
        return sum((line.tax for line in self.lines), Decimal(0))

    @property
    def printed_total(self) -> Decimal:
        return self.printed_total_override if self.printed_total_override is not None else self.subtotal + self.tax_total

    def truth(self) -> dict:
        return {
            "vendor_name": self.vendor.name,
            "vendor_tax_id": self.vendor.gstin,
            "invoice_number": self.number,
            "invoice_date": self.issued.isoformat(),
            "po_number": self.po_number,
            "currency": "INR",
            "line_items": [
                {
                    "description": line.description,
                    "quantity": f"{line.quantity.normalize():f}",
                    "unit_price": f"{line.unit_price:.2f}",
                    "tax_rate_percent": str(line.rate),
                    "amount": f"{line.amount:.2f}",
                }
                for line in self.lines
            ],
            "subtotal": f"{self.subtotal:.2f}",
            "tax_total": f"{self.tax_total:.2f}",
            "total": f"{self.printed_total:.2f}",
        }


def invoice_number(v: Vendor, seq: int, issued: date) -> str:
    fy = issued.year if issued.month >= 4 else issued.year - 1
    styles = [
        f"INV/{fy}-{str(fy + 1)[2:]}/{seq:04d}",
        f"{v.name.split()[0][:3].upper()}-{10000 + seq}",
        f"{fy}/{v.state}/{seq:03d}",
        f"GST{seq:05d}",
    ]
    return styles[v.number_style]


def money(d: Decimal) -> str:
    # Indian digit grouping: 12,34,567.89
    sign = "-" if d < 0 else ""
    whole, frac = f"{abs(d):.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return f"{sign}{whole}.{frac}"


# ------------------------------------------------------------------ rendering


def render_pdf(inv: Invoice) -> bytes:
    buf = io.BytesIO()
    # invariant=1: no timestamps or random IDs, so the same seed gives byte-identical files.
    c = canvas.Canvas(buf, pagesize=A4, invariant=1)
    c.setTitle(f"Tax Invoice {inv.number}")
    [_layout_classic, _layout_modern, _layout_compact][inv.layout](c, inv)
    c.showPage()
    c.save()
    return buf.getvalue()


def _party_block(c, x, y, title, name, address, gstin, size=9):
    c.setFont("Helvetica-Bold", size)
    c.drawString(x, y, title)
    c.setFont("Helvetica", size)
    c.drawString(x, y - 12, name)
    for i, part in enumerate(_wrap(address, 48)):
        c.drawString(x, y - 24 - 11 * i, part)
    c.drawString(x, y - 24 - 11 * len(_wrap(address, 48)), f"GSTIN: {gstin}")


def _wrap(text: str, width: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    return lines + [cur] if cur else lines


def _lines_table(c, inv: Invoice, top: float, show_hsn: bool = True, font: str = "Helvetica", size: int = 9):
    w, _ = A4
    bold = "Helvetica-Bold" if font == "Helvetica" else "Courier-Bold"
    # (header, x, align) — "r" columns are right-aligned at x.
    cols = [("#", 20 * mm, "l"), ("Description", 27 * mm, "l"), ("HSN/SAC" if show_hsn else "", 104 * mm, "l"),
            ("Qty", 128 * mm, "r"), ("Rate (Rs)", 152 * mm, "r"), ("GST %", 166 * mm, "r"), ("Amount (Rs)", w - 20 * mm, "r")]
    c.setFont(bold, size)
    for header, x, align in cols:
        (c.drawRightString if align == "r" else c.drawString)(x, top, header)
    c.line(20 * mm, top - 4, w - 20 * mm, top - 4)
    c.setFont(font, size)
    y = top - 16
    for n, line in enumerate(inv.lines, 1):
        values = [str(n), line.description[:40], line.hsn if show_hsn else "", f"{line.quantity.normalize():f}",
                  money(line.unit_price), f"{line.rate}%", money(line.amount)]
        for (_, x, align), value in zip(cols, values):
            (c.drawRightString if align == "r" else c.drawString)(x, y, value)
        y -= 15
    c.line(20 * mm, y + 8, w - 20 * mm, y + 8)
    return y


def _totals(c, inv: Invoice, y: float, size: int = 9):
    w, _ = A4
    rows = [("Taxable value", inv.subtotal)]
    if inv.intra_state:
        cgst = sum((q2(line.tax / 2) for line in inv.lines), Decimal(0))
        rows += [("CGST", cgst), ("SGST", inv.tax_total - cgst)]
    else:
        rows += [("IGST", inv.tax_total)]
    rows += [("Total tax", inv.tax_total)]
    c.setFont("Helvetica", size)
    for label, value in rows:
        c.drawString(130 * mm, y, label)
        c.drawRightString(w - 20 * mm, y, money(value))
        y -= 13
    c.setFont("Helvetica-Bold", size + 2)
    c.drawString(130 * mm, y - 4, "Grand total (Rs)")
    c.drawRightString(w - 20 * mm, y - 4, money(inv.printed_total))
    return y - 20


def _footer(c, inv: Invoice, y: float):
    c.setFont("Helvetica", 8)
    c.drawString(20 * mm, y, "Payment terms: 30 days from invoice date. Bank: HDFC Bank, A/C 50200012345678, IFSC HDFC0001234")
    c.drawString(20 * mm, y - 11, "This is a computer generated invoice. Synthetic document for testing; not a real transaction.")
    c.drawRightString(A4[0] - 20 * mm, y - 40, f"For {inv.vendor.name}")
    c.drawRightString(A4[0] - 20 * mm, y - 70, "Authorised signatory")


def _layout_classic(c, inv: Invoice):
    w, h = A4
    c.setFont("Helvetica-Bold", 16)
    c.drawCentredString(w / 2, h - 22 * mm, "TAX INVOICE")
    c.setFont("Helvetica-Bold", 12)
    c.drawString(20 * mm, h - 34 * mm, inv.vendor.name)
    c.setFont("Helvetica", 9)
    for i, part in enumerate(_wrap(inv.vendor.address, 70)):
        c.drawString(20 * mm, h - 40 * mm - 11 * i, part)
    c.drawString(20 * mm, h - 52 * mm, f"GSTIN: {inv.vendor.gstin}    State: {STATES[inv.vendor.state]} ({inv.vendor.state})")
    c.drawRightString(w - 20 * mm, h - 34 * mm, f"Invoice No: {inv.number}")
    c.drawRightString(w - 20 * mm, h - 40 * mm, f"Date: {inv.issued.strftime('%d/%m/%Y')}")
    if inv.po_number:
        c.drawRightString(w - 20 * mm, h - 46 * mm, f"PO Ref: {inv.po_number}")
    c.line(20 * mm, h - 58 * mm, w - 20 * mm, h - 58 * mm)
    _party_block(c, 20 * mm, h - 66 * mm, "Bill to", BUYER["name"], BUYER["address"], BUYER["gstin"])
    y = _lines_table(c, inv, h - 100 * mm)
    y = _totals(c, inv, y - 10)
    _footer(c, inv, y - 10)


def _layout_modern(c, inv: Invoice):
    w, h = A4
    c.setFillColor(colors.HexColor("#1f3b57"))
    c.rect(0, h - 40 * mm, w, 40 * mm, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 18)
    c.drawString(20 * mm, h - 20 * mm, inv.vendor.name)
    c.setFont("Helvetica", 9)
    c.drawString(20 * mm, h - 27 * mm, inv.vendor.address[:95])
    c.drawString(20 * mm, h - 33 * mm, f"GSTIN {inv.vendor.gstin}")
    c.setFont("Helvetica-Bold", 22)
    c.drawRightString(w - 20 * mm, h - 22 * mm, "Invoice")
    c.setFillColor(colors.black)
    c.setFont("Helvetica", 9)
    meta = [("Invoice #", inv.number), ("Issued", inv.issued.strftime("%d %b %Y"))]
    meta.append(("Purchase order", inv.po_number or "—"))
    for i, (k, v) in enumerate(meta):
        c.setFont("Helvetica", 8)
        c.drawString(130 * mm + (i % 2) * 0, h - (50 + 11 * i) * mm, k.upper())
        c.setFont("Helvetica-Bold", 10)
        c.drawString(160 * mm, h - (50 + 11 * i) * mm, v)
    _party_block(c, 20 * mm, h - 50 * mm, "BILLED TO", BUYER["name"], BUYER["address"], BUYER["gstin"])
    y = _lines_table(c, inv, h - 95 * mm, show_hsn=True)
    y = _totals(c, inv, y - 10)
    _footer(c, inv, y - 10)


def _layout_compact(c, inv: Invoice):
    w, h = A4
    c.setFont("Courier-Bold", 13)
    c.drawString(20 * mm, h - 20 * mm, inv.vendor.name.upper())
    c.setFont("Courier", 9)
    c.drawString(20 * mm, h - 26 * mm, f"GST No. {inv.vendor.gstin}")
    c.drawString(20 * mm, h - 31 * mm, inv.vendor.address[:90])
    c.setFont("Courier-Bold", 11)
    c.drawString(20 * mm, h - 42 * mm, "GST INVOICE")
    c.setFont("Courier", 9)
    c.drawString(20 * mm, h - 48 * mm, f"Bill No.  : {inv.number}")
    c.drawString(20 * mm, h - 53 * mm, f"Bill Date : {inv.issued.strftime('%d-%m-%Y')}")
    c.drawString(20 * mm, h - 58 * mm, f"Your PO   : {inv.po_number or 'N/A'}")
    c.drawString(110 * mm, h - 48 * mm, f"Buyer: {BUYER['name']}")
    c.drawString(110 * mm, h - 53 * mm, f"Buyer GSTIN: {BUYER['gstin']}")
    y = _lines_table(c, inv, h - 72 * mm, show_hsn=False, font="Courier", size=9)
    y = _totals(c, inv, y - 10)
    _footer(c, inv, y - 10)


def to_scanned_image(pdf_bytes: bytes, rng: random.Random) -> bytes:
    import pypdfium2 as pdfium
    from PIL import Image, ImageFilter

    page = pdfium.PdfDocument(pdf_bytes)[0]
    img = page.render(scale=1.6).to_pil().convert("L")
    img = img.rotate(rng.uniform(-2.5, 2.5), expand=True, fillcolor=235)
    noise = Image.effect_noise(img.size, 22).convert("L")
    img = Image.blend(img, noise, 0.08).filter(ImageFilter.GaussianBlur(0.6))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=62)
    return out.getvalue()


# ------------------------------------------------------------------ generation


def build(count: int, seed: int) -> tuple[list[Vendor], list[Invoice], dict[str, list[Line]]]:
    rng = random.Random(seed)
    fake = Faker("en_IN")
    Faker.seed(seed)

    vendors = []
    for i, category in enumerate(["office", "it", "industrial", "services", "food", "it", "industrial", "office"]):
        state = rng.choice(list(STATES))
        suffix = rng.choice(["Pvt Ltd", "Enterprises", "Traders", "Solutions LLP", "& Co."])
        name = f"{fake.last_name()} {category.title() if category != 'it' else 'Infotech'} {suffix}"
        pin = PIN_PREFIX[state] + "".join(str(rng.randint(0, 9)) for _ in range(6 - len(PIN_PREFIX[state])))
        address = f"{fake.building_number()}, {fake.street_name()}, {rng.choice(CITIES[state])}, {STATES[state]} {pin}"
        vendors.append(Vendor(name, make_gstin(rng, state), state, address, category, i % 4, i % 3))

    scenarios = (["clean"] * 12 + ["po_mismatch"] * 3 + ["no_po"] * 2 + ["arithmetic"] * 2 + ["scanned"] * 3)
    invoices: list[Invoice] = []
    pos: dict[str, list[Line]] = {}
    today = date(2026, 10, 15)
    seq = 100

    for n in range(count):
        scenario = scenarios[n % len(scenarios)] if n < count - 2 else "duplicate"
        if scenario == "duplicate":
            original = rng.choice([i for i in invoices if i.scenario == "clean"])
            dup = Invoice(
                original.vendor, original.number, original.issued, original.po_number, original.lines, "duplicate",
                layout=(original.layout + 1) % 3, expected_flags=["duplicate_invoice_number"],
            )
            invoices.append(dup)
            continue

        vendor = rng.choice(vendors)
        seq += rng.randint(1, 9)
        issued = today - timedelta(days=rng.randint(0, 90))
        items = rng.sample(CATALOG[vendor.category], k=min(len(CATALOG[vendor.category]), rng.randint(1, 4)))
        lines = []
        for desc, hsn, rate, lo, hi in items:
            qty = Decimal(1) if vendor.category == "services" else Decimal(rng.choice([1, 2, 3, 5, 10, 12, 20, 25, 50]))
            price = Decimal(rng.randrange(lo, hi)) + rng.choice([Decimal("0"), Decimal("0.50"), Decimal("0.75")])
            lines.append(Line(desc, hsn, rate, qty, price))

        po_number = f"PO-{issued.year}-{1000 + n:04d}"
        po_lines = [Line(line.description, line.hsn, line.rate, line.quantity, line.unit_price) for line in lines]
        inv = Invoice(vendor, invoice_number(vendor, seq, issued), issued, po_number, lines, scenario, layout=vendor.layout)

        if scenario == "po_mismatch":
            target = rng.randrange(len(lines))
            if rng.random() < 0.5:
                lines[target].quantity += rng.choice([1, 2, 5])
            else:
                lines[target].unit_price = q2(lines[target].unit_price * Decimal("1.08"))
            inv.expected_flags = ["po_amount_mismatch", "po_line_mismatch"]
        elif scenario == "no_po":
            inv.po_number = None
            po_lines = []
            inv.expected_flags = ["no_po"]
        elif scenario == "arithmetic":
            inv.printed_total_override = inv.subtotal + inv.tax_total + Decimal(rng.choice([100, 1000, 450]))
            inv.expected_flags = ["total_mismatch"]

        if po_lines:
            pos[po_number] = (vendor, po_lines)
        invoices.append(inv)
    return vendors, invoices, pos


def write(out: Path, invoices: list[Invoice], pos: dict, seed: int) -> None:
    rng = random.Random(seed + 1)
    docs = out / "invoices"
    docs.mkdir(parents=True, exist_ok=True)
    for old in docs.iterdir():
        old.unlink()

    manifest = {"seed": seed, "buyer": BUYER, "invoices": []}
    for n, inv in enumerate(invoices, 1):
        pdf = render_pdf(inv)
        if inv.scenario == "scanned":
            data, ext = to_scanned_image(pdf, rng), ".jpg"
        else:
            data, ext = pdf, ".pdf"
        safe_number = "".join(ch if ch.isalnum() else "-" for ch in inv.number)
        filename = f"{n:03d}-{inv.scenario}-{safe_number}{ext}"
        (docs / filename).write_bytes(data)
        manifest["invoices"].append({
            "file": f"invoices/{filename}",
            "sha256": hashlib.sha256(data).hexdigest(),
            "scenario": inv.scenario,
            "truth": inv.truth(),
            "expected_flags": inv.expected_flags,
        })

    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))

    with (out / "purchase_orders.csv").open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["po_number", "vendor_gstin", "vendor_name", "currency", "description", "quantity", "unit_price"])
        for po_number, (vendor, lines) in pos.items():
            for line in lines:
                writer.writerow([po_number, vendor.gstin, vendor.name, "INR", line.description,
                                 f"{line.quantity.normalize():f}", f"{line.unit_price:.2f}"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--count", type=int, default=30)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parent.parent / "data" / "generated")
    args = parser.parse_args()

    _, invoices, pos = build(args.count, args.seed)
    write(args.out, invoices, pos, args.seed)
    by_scenario: dict[str, int] = {}
    for inv in invoices:
        by_scenario[inv.scenario] = by_scenario.get(inv.scenario, 0) + 1
    print(f"Wrote {len(invoices)} invoices and {len(pos)} purchase orders to {args.out}")
    print("Scenarios: " + ", ".join(f"{k}={v}" for k, v in sorted(by_scenario.items())))


if __name__ == "__main__":
    main()
