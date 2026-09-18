from schemagate.schema.normalize import extension_column_name, normalize_name, snake_case
from schemagate.schema.vocabulary import Vocabulary


def test_case_punctuation_and_camel_split():
    assert normalize_name("InvoiceDate").tokens == ("invoice", "date")
    assert normalize_name("invoice_date").tokens == ("invoice", "date")
    assert normalize_name("  Invoice-Date. ").tokens == ("invoice", "date")
    assert normalize_name("unitPriceUSD").snake == "unit_price"
    assert normalize_name("HTTPStatusCode").tokens == ("http", "status", "code")


def test_unit_suffixes_are_stripped_into_hints():
    n = normalize_name("Unit Price (USD)")
    assert n.snake == "unit_price"
    assert n.unit_hints == ("usd",)
    pct = normalize_name("Tax Rate %")
    assert pct.snake == "tax_rate"
    assert "percent" in pct.unit_hints
    assert normalize_name("Weight [kg]").unit_hints == ("kg",)
    assert normalize_name("Amount $").unit_hints == ("usd",)


def test_bracketed_non_units_are_kept():
    assert normalize_name("Qty (shipped)").tokens == ("qty", "shipped")


def test_hash_becomes_number_and_accents_removed():
    assert normalize_name("Invoice #").snake == "invoice_number"
    assert normalize_name("Échéance").snake == "echeance"


def test_trailing_currency_code_is_a_hint_but_lone_code_is_kept():
    assert normalize_name("Freight USD").snake == "freight"
    assert normalize_name("USD").snake == "usd"


def test_snake_and_extension_names():
    assert snake_case("%%%") == "column"
    assert extension_column_name("Warehouse Code") == "ext_warehouse_code"


def test_vocabulary_expansion_and_stopwords():
    vocab = Vocabulary.default({"fx": "exchange rate"})
    assert vocab.expand_name("Qty") == ("quantity",)
    assert vocab.expand_name("Inv Dt") == ("invoice", "date")
    assert vocab.expand_name("Price per Unit") == ("price", "unit")
    assert vocab.expand_name("FX") == ("exchange", "rate")
