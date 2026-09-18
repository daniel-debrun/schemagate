"""Regenerate the synthetic example files. Deterministic: same seed, same bytes (except XLSX metadata)."""

from __future__ import annotations

import csv
import random
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).parent
ITEMS = [
    ("BRK-1001", "Steel bracket, zinc plated", 2.35),
    ("FST-2040", "Hex bolt M8x40, box of 100", 11.90),
    ("PKG-0300", "Corrugated carton 40x30x30", 0.84),
    ("LBL-0012", "Thermal shipping labels, roll", 6.25),
    ("GLV-0550", "Nitrile gloves, size L, box", 9.10),
    ("TAP-0048", "Packing tape 48mm, 6 pack", 7.45),
    ("PAL-1200", "Wooden pallet 1200x800", 14.00),
    ("CLN-0090", "Industrial degreaser 5L", 23.60),
]


def lines(rng: random.Random, supplier_id: str, month: int, invoices: int, currency: str):
    out = []
    for n in range(invoices):
        inv_date = date(2026, month, 1) + timedelta(days=rng.randint(0, 26))
        inv_no = f"{supplier_id[:2]}-{month:02d}{n + 1:03d}"
        po = f"PO-{rng.randint(40000, 49999)}"
        for ln in range(1, rng.randint(2, 5) + 1):
            sku, desc, price = rng.choice(ITEMS)
            qty = rng.randint(1, 120)
            out.append({
                "invoice_number": inv_no, "line_number": ln, "invoice_date": inv_date,
                "supplier_id": supplier_id, "sku": sku, "item_description": desc, "quantity": qty,
                "unit_price": price, "line_total": round(qty * price, 2),
                "tax_rate": rng.choice([0, 5, 13]), "currency": currency, "po_number": po,
                "due_date": inv_date + timedelta(days=30),
            })
    return out


def write_csv(path: Path, header: list[str], rows: list[list[object]], *, delimiter: str = ",",
              encoding: str = "utf-8", preamble: list[str] | None = None) -> None:
    with path.open("w", newline="", encoding=encoding) as fh:
        for line in preamble or []:
            fh.write(line + "\r\n")
        w = csv.writer(fh, delimiter=delimiter)
        w.writerow(header)
        w.writerows(rows)


def northwind(month: int, rng: random.Random) -> None:
    header = ["Invoice Number", "Line", "Invoice Date", "Vendor ID", "Vendor Name", "SKU", "Description",
              "Qty", "Unit Price (USD)", "Line Total (USD)", "Tax %", "Currency", "PO Number", "Due Date"]
    rows = [[r["invoice_number"], r["line_number"], r["invoice_date"].isoformat(), r["supplier_id"],
             "Northwind Supplies Ltd", r["sku"], r["item_description"], r["quantity"],
             f"{r['unit_price']:.2f}", f"{r['line_total']:.2f}", f"{r['tax_rate']}%", r["currency"],
             r["po_number"], r["due_date"].isoformat()]
            for r in lines(rng, "NW-0042", month, 8, "USD")]
    (HERE / "northwind").mkdir(exist_ok=True)
    write_csv(HERE / "northwind" / f"invoices_2026-{month:02d}.csv", header, rows)


def bluepeak(month: int, rng: random.Random) -> None:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Summary"
    ws.append(["BluePeak Industrial - monthly billing summary"])
    ws.append([])
    ws.append(["Invoices", 6])
    ws.append(["Prepared by", "Accounts Receivable"])
    items = wb.create_sheet("Line Items")
    items.append(["BluePeak Industrial"])
    items.append([f"Invoice lines for 2026-{month:02d}"])
    items.append([])
    items.append(["Inv #", "Ln", "Billing Date", "Supp Code", "Supplier", "Item Code", "Item Description",
                  "Units", "Price Each", "Ext. Amount", "VAT Rate", "Ccy", "Purchase Order",
                  "Payment Due", "Warehouse"])
    for r in lines(rng, "BP-7781", month, 6, "CAD"):
        items.append([r["invoice_number"], r["line_number"], r["invoice_date"].strftime("%d.%m.%Y"),
                      r["supplier_id"], "BluePeak Industrial Inc.", r["sku"], r["item_description"],
                      r["quantity"], r["unit_price"], r["line_total"], f"{r['tax_rate']}%",
                      r["currency"], r["po_number"], r["due_date"].strftime("%d.%m.%Y"),
                      rng.choice(["TOR-1", "MTL-2"])])
    (HERE / "bluepeak").mkdir(exist_ok=True)
    wb.save(HERE / "bluepeak" / f"billing_2026-{month:02d}.xlsx")


def cedar(month: int, rng: random.Random) -> None:
    rows_src = lines(rng, "CD-3310", month, 7, "USD")
    if month == 7:
        header = ["InvoiceNo", "LineNo", "InvoiceDate", "SupplierRef", "SupplierName", "ItemSKU",
                  "ItemDesc", "QtyShipped", "UnitCost", "NetAmount", "TaxPct", "CurrencyCode",
                  "PORef", "DueDt"]
        rows = [[r["invoice_number"], r["line_number"], r["invoice_date"].strftime("%m/%d/%Y"),
                 r["supplier_id"], "Cédar Logistique SA", r["sku"], r["item_description"],
                 r["quantity"], f"${r['unit_price']:,.2f}", f"${r['line_total']:,.2f}",
                 r["tax_rate"], r["currency"], r["po_number"], r["due_date"].strftime("%m/%d/%Y")]
                for r in rows_src]
    else:
        header = ["InvoiceNo", "LineNo", "InvoiceDate", "SupplierRef", "SupplierName", "ItemSKU",
                  "ItemDesc", "QtyShipped", "Unit Price USD", "NetAmount", "TaxPct", "CurrencyCode",
                  "PORef", "DueDt", "FreightCharge"]
        rows = [[r["invoice_number"], r["line_number"], r["invoice_date"].isoformat(),
                 r["supplier_id"], "Cédar Logistique SA", r["sku"], r["item_description"],
                 r["quantity"], f"${r['unit_price']:,.2f}", f"${r['line_total']:,.2f}",
                 r["tax_rate"], r["currency"], r["po_number"], r["due_date"].strftime("%m/%d/%Y"),
                 f"${rng.choice([0, 12.5, 25]):,.2f}"]
                for r in rows_src]
    (HERE / "cedar").mkdir(exist_ok=True)
    write_csv(HERE / "cedar" / f"cedar_export_2026-{month:02d}.csv", header, rows, delimiter=";",
              encoding="cp1252", preamble=["Cedar Logistics export", "generated;2026-09-01"])


def main() -> None:
    rng = random.Random(20260901)
    for month in (7, 8):
        northwind(month, rng)
        bluepeak(month, rng)
        cedar(month, rng)


if __name__ == "__main__":
    main()
