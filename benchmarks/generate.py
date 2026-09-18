"""Synthetic sender-variant generator with ground truth.

Each variant is a table a sender might plausibly send for a target schema: a subset of fields, in a
shuffled order, under perturbed headers (known aliases, held-out paraphrases, abbreviations, casing,
unit suffixes, word reordering, typos, foreign-language tokens), with decoy columns that belong to no
field and value formats that differ from the canonical ones.

The alias lists in the schema YAML are the "dictionary" schemagate knows. Paraphrases and
foreign-language tokens below are deliberately NOT in those lists or in the package synonym table, so
the deterministic tiers cannot resolve them by construction.
"""

from __future__ import annotations

import random
import string
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from schemagate.schema.target import FieldSpec, TargetSchema, load_schema

SCHEMA_DIR = Path(__file__).parent / "schemas"

PARAPHRASES: dict[str, list[str]] = {
    "invoice_number": ["document number", "bill ref", "invoice reference"],
    "line_number": ["position", "row seq", "item position"],
    "invoice_date": ["date issued", "document date", "bill date"],
    "supplier_id": ["seller account", "vendor account number", "supplier ref"],
    "supplier_name": ["seller", "company name", "vendor legal name"],
    "sku": ["material", "article", "catalog number"],
    "item_description": ["goods description", "line text", "product details"],
    "quantity": ["qty billed", "number of units", "count"],
    "unit_price": ["price per unit", "unit rate", "each price"],
    "line_total": ["net line value", "extended value", "line value"],
    "tax_rate": ["tax percent", "vat pct", "tax pct"],
    "currency": ["currency code", "iso currency", "money code"],
    "po_number": ["order reference", "buyer po", "po ref"],
    "due_date": ["pay by", "payment date", "due on"],
    "study_id": ["study code", "protocol", "study number"],
    "site_id": ["site code", "site no", "location number"],
    "site_name": ["hospital", "facility name", "site title"],
    "country": ["nation", "country name", "site location country"],
    "principal_investigator": ["lead physician", "pi", "study doctor"],
    "report_month": ["month", "reporting month", "report date"],
    "patients_screened": ["subjects screened", "screens", "n screened"],
    "patients_enrolled": ["subjects enrolled", "randomised", "n enrolled"],
    "screen_failures": ["failed screening", "sf count", "screen fails"],
    "patients_discontinued": ["early terminations", "discontinuations", "subjects withdrawn"],
    "open_queries": ["queries open", "pending queries", "dm queries"],
    "protocol_deviations": ["pd count", "deviation count", "major and minor deviations"],
    "last_monitoring_visit": ["last monitor date", "recent visit", "last cra visit"],
    "site_status": ["status", "enrolment state", "site state"],
    "enrollment_target": ["recruitment goal", "target n", "contracted subjects"],
    "store_id": ["store", "store number", "outlet code"],
    "register_id": ["terminal", "checkout id", "register"],
    "transaction_id": ["receipt", "txn number", "transaction ref"],
    "transaction_timestamp": ["transaction date", "date sold", "trading date"],
    "cashier_id": ["employee", "staff id", "cashier"],
    "product_name": ["description", "product", "item desc"],
    "quantity_sold": ["units", "qty sold", "sold quantity"],
    "discount_amount": ["discount", "promo discount", "reduction"],
    "net_sales": ["net revenue", "sales net", "line revenue"],
    "tax_amount": ["sales tax", "tax", "tax collected"],
    "payment_method": ["payment type", "paid with", "tender method"],
    "loyalty_member": ["loyalty", "club member", "member"],
    "carrier_code": ["carrier", "trucking company code", "carrier scac"],
    "tracking_number": ["tracking id", "shipment number", "pro no"],
    "ship_date": ["shipped on", "pickup", "date shipped"],
    "delivery_date": ["delivered", "date delivered", "arrival date"],
    "origin_city": ["origin", "pickup city", "ship from city"],
    "origin_postal_code": ["origin postcode", "from zip", "pickup zip"],
    "destination_city": ["destination", "delivery city", "ship to city"],
    "destination_postal_code": ["destination zip", "to postcode", "delivery zip"],
    "service_level": ["service", "shipment mode", "level of service"],
    "pieces": ["pcs", "units shipped", "piece count"],
    "gross_weight_kg": ["weight", "gross wt", "kilos"],
    "freight_charge": ["freight", "line haul charge", "base charge"],
    "fuel_surcharge": ["fuel charge", "fuel adj", "surcharge fuel"],
    "accessorial_charges": ["additional charges", "accessorial fees", "misc charges"],
    "total_charge": ["grand total", "amount due", "total cost"],
}

ABBREVIATIONS = {
    "quantity": "qty", "amount": "amt", "number": "no", "date": "dt", "description": "desc",
    "invoice": "inv", "customer": "cust", "supplier": "supp", "total": "ttl", "reference": "ref",
    "transaction": "txn", "identifier": "id", "percent": "pct", "currency": "ccy", "weight": "wt",
    "patients": "pts", "charge": "chg", "charges": "chgs", "postal": "post", "destination": "dest",
    "origin": "orig", "price": "prc", "discount": "disc", "line": "ln", "month": "mth",
}
FOREIGN = {
    "date": ["fecha", "datum"], "quantity": ["cantidad", "menge"], "number": ["numero", "nummer"],
    "price": ["precio", "prix"], "total": ["totale", "gesamt"], "supplier": ["proveedor", "lieferant"],
    "site": ["sitio", "centre"], "weight": ["peso", "gewicht"], "city": ["ciudad", "ville"],
    "store": ["tienda", "magasin"], "tax": ["impuesto", "steuer"], "due": ["vencimiento"],
    "name": ["nombre", "nom"], "country": ["pais", "pays"], "status": ["estado", "statut"],
}
DECOYS = [
    ("Notes", "text"), ("Comments", "text"), ("Internal Ref", "code"), ("Row ID", "int"),
    ("Export Timestamp", "date"), ("Region Manager", "name"), ("Batch", "code"), ("Page", "int"),
    ("Approved By", "name"), ("Cost Center", "code"), ("Last Modified", "date"), ("Flag", "bool"),
    ("Sheet Total", "money"), ("Record Status", "text"), ("Created By", "name"), ("Channel", "text"),
]
WORDS = ["alpha", "north", "blue", "steel", "cedar", "prime", "delta", "harbor", "summit", "river"]
NAMES = ["A. Rivera", "J. Chen", "M. Okafor", "S. Patel", "L. Novak", "K. Tanaka"]
DATE_STYLES = ["%Y-%m-%d", "%m/%d/%Y", "%d.%m.%Y", "%d/%m/%Y", "%d-%b-%Y"]


@dataclass
class Variant:
    schema: str
    variant_id: int
    columns: list[str]
    rows: list[list[str]]
    truth: dict[str, str | None]
    operations: dict[str, list[str]]


def _tokens(label: str) -> list[str]:
    return label.replace("_", " ").split()


def _case(tokens: list[str], rng: random.Random) -> str:
    style = rng.choice(["title", "lower", "upper", "camel", "snake", "pascal", "kebab"])
    if style == "title":
        return " ".join(t.capitalize() for t in tokens)
    if style == "lower":
        return " ".join(tokens)
    if style == "upper":
        return " ".join(t.upper() for t in tokens)
    if style == "camel":
        return tokens[0] + "".join(t.capitalize() for t in tokens[1:])
    if style == "pascal":
        return "".join(t.capitalize() for t in tokens)
    if style == "kebab":
        return "-".join(tokens)
    return "_".join(tokens)


def _typo(token: str, rng: random.Random) -> str:
    if len(token) < 4:
        return token
    i = rng.randrange(1, len(token) - 1)
    op = rng.choice(["drop", "swap", "double", "replace"])
    if op == "drop":
        return token[:i] + token[i + 1 :]
    if op == "swap":
        return token[: i - 1] + token[i] + token[i - 1] + token[i + 1 :]
    if op == "double":
        return token[:i] + token[i] + token[i:]
    return token[:i] + rng.choice(string.ascii_lowercase) + token[i + 1 :]


def perturb_header(field: FieldSpec, rng: random.Random) -> tuple[str, list[str]]:
    ops: list[str] = []
    r = rng.random()
    if r < 0.25:
        tokens = _tokens(field.name)
        ops.append("canonical")
    elif r < 0.5 and field.aliases:
        tokens = _tokens(rng.choice(field.aliases))
        ops.append("alias")
    else:
        tokens = _tokens(rng.choice(PARAPHRASES.get(field.name, [field.name])))
        ops.append("paraphrase")
    if rng.random() < 0.35:
        new = [ABBREVIATIONS.get(t, t) for t in tokens]
        if new != tokens:
            tokens = new
            ops.append("abbreviation")
    if rng.random() < 0.12:
        options = [i for i, t in enumerate(tokens) if t in FOREIGN]
        if options:
            i = rng.choice(options)
            tokens[i] = rng.choice(FOREIGN[tokens[i]])
            ops.append("foreign")
    if len(tokens) > 1 and rng.random() < 0.15:
        tokens = tokens[::-1]
        ops.append("reorder")
    if rng.random() < 0.12:
        i = rng.randrange(len(tokens))
        typo = _typo(tokens[i], rng)
        if typo != tokens[i]:
            tokens[i] = typo
            ops.append("typo")
    header = _case(tokens, rng)
    if field.unit and rng.random() < 0.4:
        suffix = {"usd": " (USD)", "percent": " %", "kg": " [kg]"}.get(field.unit, f" ({field.unit})")
        header += suffix
        ops.append("unit_suffix")
    return header, ops


def _value(field: FieldSpec, rng: random.Random, style: dict[str, str]) -> str:
    if field.allowed_values:
        v = rng.choice(field.allowed_values)
        return v.upper() if style.get("upper_enum") else v
    if field.pattern:
        return f"PO-{rng.randint(10000, 99999)}"
    if field.type == "integer":
        return str(rng.randint(0, 400))
    if field.type == "number":
        v = round(rng.uniform(0, 2500), 2)
        if field.unit == "usd" and style["money"] == "symbol":
            return f"${v:,.2f}"
        if field.unit == "percent" and style["percent"] == "sign":
            return f"{rng.choice([0, 5, 7.5, 13, 20])}%"
        if field.unit == "percent":
            return str(rng.choice([0, 5, 7.5, 13, 20]))
        return f"{v:.2f}"
    if field.type == "date":
        d = date(2026, 1, 1) + timedelta(days=rng.randint(0, 300))
        return d.strftime(style["date"])
    if field.type == "boolean":
        return rng.choice(["Y", "N"]) if style["bool"] == "yn" else rng.choice(["true", "false"])
    name = field.name
    if "name" in name or "investigator" in name or "cashier" in name:
        return rng.choice(NAMES) if "investigator" in name or "cashier" in name else (
            f"{rng.choice(WORDS).title()} {rng.choice(['Ltd', 'Inc', 'GmbH', 'Hospital'])}")
    if "city" in name:
        return rng.choice(["Toronto", "Lyon", "Austin", "Leeds", "Osaka"])
    if "postal" in name:
        return f"{rng.randint(10000, 99999)}"
    if name == "country":
        return rng.choice(["Canada", "France", "Japan", "Brazil"])
    if name == "currency":
        return rng.choice(["USD", "EUR", "GBP", "CAD"])
    if "description" in name or name == "product_name":
        return f"{rng.choice(WORDS)} {rng.choice(['bracket', 'gloves', 'cable', 'tape', 'kit'])}"
    return f"{name[:2].upper()}-{rng.randint(100, 99999)}"


def _decoy_value(kind: str, rng: random.Random) -> str:
    if kind == "int":
        return str(rng.randint(1, 5000))
    if kind == "date":
        return (date(2026, 1, 1) + timedelta(days=rng.randint(0, 300))).isoformat()
    if kind == "name":
        return rng.choice(NAMES)
    if kind == "bool":
        return rng.choice(["Y", "N"])
    if kind == "money":
        return f"{rng.uniform(0, 9999):.2f}"
    if kind == "code":
        return f"{rng.choice(WORDS)[:3].upper()}{rng.randint(10, 999)}"
    return rng.choice(["ok", "check later", "", "see email", "n/a", "priority"])


def generate_variant(schema: TargetSchema, variant_id: int, seed: int, rows: int = 30) -> Variant:
    rng = random.Random(f"{schema.name}:{variant_id}:{seed}")
    required = [f for f in schema.fields if f.required]
    optional = [f for f in schema.fields if not f.required]
    chosen = required + rng.sample(optional, k=rng.randint(len(optional) // 2, len(optional)))
    style = {"money": rng.choice(["plain", "symbol"]), "percent": rng.choice(["sign", "plain"]),
             "date": rng.choice(DATE_STYLES), "bool": rng.choice(["yn", "tf"]),
             "upper_enum": rng.random() < 0.3}
    columns: list[str] = []
    truth: dict[str, str | None] = {}
    operations: dict[str, list[str]] = {}
    makers = []
    for f in chosen:
        header, ops = perturb_header(f, rng)
        while header in truth:
            header += " 2"
        columns.append(header)
        truth[header] = f.name
        operations[header] = ops
        makers.append(lambda f=f: _value(f, rng, style))
    for name, kind in rng.sample(DECOYS, k=rng.randint(1, 4)):
        if name in truth:
            continue
        columns.append(name)
        truth[name] = None
        operations[name] = ["decoy"]
        makers.append(lambda kind=kind: _decoy_value(kind, rng))
    order = list(range(len(columns)))
    rng.shuffle(order)
    columns = [columns[i] for i in order]
    makers = [makers[i] for i in order]
    data = [[make() for make in makers] for _ in range(rows)]
    return Variant(schema.name, variant_id, columns, data, truth, operations)


def load_benchmark_schemas() -> list[TargetSchema]:
    return [load_schema(p) for p in sorted(SCHEMA_DIR.glob("*.yaml"))]


def generate_suite(variants_per_schema: int, seed: int) -> list[tuple[TargetSchema, Variant]]:
    return [(schema, generate_variant(schema, i, seed))
            for schema in load_benchmark_schemas() for i in range(variants_per_schema)]
