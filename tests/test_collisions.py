from schemagate.models import MappingProposal, Tier
from schemagate.resolve import resolve_collisions


def P(col, target, conf, tier):
    return MappingProposal(col, target, conf, tier, "r")


def test_highest_confidence_wins_and_loser_becomes_extension():
    out = resolve_collisions([
        P("Unit Cost", "unit_price", 0.70, Tier.MODEL),
        P("Unit Price", "unit_price", 0.95, Tier.EXACT),
        P("Notes", None, 0.0, Tier.UNRESOLVED),
    ])
    by = {p.source_column: p for p in out}
    assert by["Unit Price"].target_field == "unit_price"
    assert by["Unit Cost"].target_field is None
    assert by["Unit Cost"].extension_column == "ext_unit_cost"
    assert by["Unit Cost"].conflict == {
        "target_field": "unit_price", "winner": "Unit Price", "winner_confidence": 0.95,
        "winner_tier": "exact", "demoted_confidence": 0.70, "demoted_tier": "model"}
    assert by["Notes"].extension_column == "ext_notes"
    assert [p.source_column for p in out] == ["Unit Cost", "Unit Price", "Notes"]


def test_ties_break_on_tier_then_name():
    out = resolve_collisions([
        P("b", "qty", 0.75, Tier.MODEL),
        P("c", "qty", 0.75, Tier.DICTIONARY),
        P("a", "qty", 0.75, Tier.MODEL),
    ])
    assert [p.source_column for p in out if p.target_field] == ["c"]
    out = resolve_collisions([P("b", "qty", 0.8, Tier.SYNONYM), P("a", "qty", 0.8, Tier.SYNONYM)])
    assert [p.source_column for p in out if p.target_field] == ["a"]


def test_extension_names_are_unique():
    out = resolve_collisions([P("Note", None, 0, Tier.UNRESOLVED), P("note", None, 0, Tier.UNRESOLVED)])
    assert [p.extension_column for p in out] == ["ext_note", "ext_note_2"]
