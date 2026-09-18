from __future__ import annotations

from dataclasses import dataclass

from schemagate.ingest.values import DateFormat
from schemagate.materialize.plan import ColumnPlan, MaterializationPlan

NUMERIC_STRIP = ("$", "€", "£", "¥", "USD", "EUR", "GBP", "CAD", "AUD", "JPY", "%", ",", " ", ")")
TRUE_LITERALS = ("true", "t", "yes", "y", "1")
FALSE_LITERALS = ("false", "f", "no", "n", "0")
LINEAGE_TYPES = {"_source_file": "string", "_sheet": "string", "_row_number": "integer",
                 "_content_hash": "string", "_ingested_at": "string"}
META_COLUMNS = (("_feed", "string"), ("_spec_id", "integer"))


def _lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _in_list(values: tuple[str, ...]) -> str:
    return ", ".join(_lit(v) for v in values)


@dataclass(frozen=True)
class SqlBundle:
    dialect: str
    create_table: str
    evolve: list[str]
    merge: str
    notes: list[str]

    def script(self) -> str:
        parts = [f"-- dialect: {self.dialect}"]
        parts += [f"-- {n}" for n in self.notes]
        parts.append(self.create_table + ";")
        parts += [s + ";" for s in self.evolve]
        parts.append(self.merge + ";")
        return "\n\n".join(parts) + "\n"


# No comma at all, or commas only between groups of three digits before any decimal point.
GROUPED_NUMBER = "^([^,]*|[^0-9,]*[0-9]{1,3}(,[0-9]{3})+([.][0-9]*)?[^0-9,]*)$"
# The same rule for SQLite, which has GLOB but no regular expressions: values with a comma
# that match any of these patterns are refused.
_BAD_COMMA_GLOBS = (",*", "*,", "*.*,*", "*,[^0-9]*", "*,[0-9]", "*,[0-9][^0-9]*", "*,[0-9][0-9]",
                    "*,[0-9][0-9][^0-9]*", "*,[0-9][0-9][0-9][0-9]*")


class Dialect:
    name = "ansi"
    types = {"string": "TEXT", "integer": "BIGINT", "number": "DECIMAL(38,6)", "date": "DATE",
             "boolean": "BOOLEAN"}
    true_value, false_value = "TRUE", "FALSE"
    add_column_if_not_exists = True

    def quote(self, ident: str) -> str:
        return '"' + ident.replace('"', '""') + '"'

    def trimmed(self, col: str) -> str:
        return f"NULLIF(TRIM({self.quote(col)}), '')"

    def numeric_text(self, col: str) -> str:
        expr = f"UPPER(TRIM({self.quote(col)}))"
        for token in NUMERIC_STRIP:
            expr = f"REPLACE({expr}, {_lit(token)}, '')"
        return f"REPLACE({expr}, '(', '-')"

    def try_number(self, text: str, integer: bool) -> str:
        raise NotImplementedError

    def regex_match(self, text: str, pattern: str) -> str:
        raise NotImplementedError

    def comma_grouping_ok(self, col: str) -> str:
        """True unless the value uses commas other than as thousands separators.

        Commas are stripped before casting, so ``2,5`` (a decimal comma) would otherwise
        load as 25 and ``1.234,50`` as 1.2345. Such values become NULL instead.
        """
        return self.regex_match(f"UPPER(TRIM({self.quote(col)}))", GROUPED_NUMBER)

    def try_date(self, text: str, fmt: DateFormat) -> str:
        raise NotImplementedError

    def cast(self, c: ColumnPlan) -> str:
        if c.source_column is None:
            return f"CAST(NULL AS {self.types[c.target_type]})"
        if c.target_type == "string":
            return self.trimmed(c.source_column)
        if c.target_type in ("integer", "number"):
            number = self.try_number(self.numeric_text(c.source_column), c.target_type == "integer")
            return f"CASE WHEN {self.comma_grouping_ok(c.source_column)} THEN {number} END"
        if c.target_type == "date":
            fmt = DateFormat.from_token(c.date_format) if c.date_format else DateFormat("ymd", "-")
            return self.try_date(f"TRIM({self.quote(c.source_column)})", fmt)
        low = f"LOWER(TRIM({self.quote(c.source_column)}))"
        return (f"CASE WHEN {low} IN ({_in_list(TRUE_LITERALS)}) THEN {self.true_value}"
                f" WHEN {low} IN ({_in_list(FALSE_LITERALS)}) THEN {self.false_value} END")

    def column_defs(self, plan: MaterializationPlan) -> list[tuple[str, str]]:
        cols = [(c.output_column, c.target_type) for c in plan.columns]
        cols += [(name, LINEAGE_TYPES[name]) for name in plan.lineage_columns]
        cols += list(META_COLUMNS)
        return cols

    def create_table(self, plan: MaterializationPlan) -> str:
        defs = ",\n  ".join(f"{self.quote(n)} {self.types[t]}"
                            for n, t in self.column_defs(plan) if not self._is_ext(plan, n))
        return f"CREATE TABLE IF NOT EXISTS {self.quote(plan.target_table)} (\n  {defs}\n)"

    @staticmethod
    def _is_ext(plan: MaterializationPlan, name: str) -> bool:
        return any(c.output_column == name and c.is_extension for c in plan.columns)

    def add_column(self, table: str, column: str, type_: str) -> str:
        guard = " IF NOT EXISTS" if self.add_column_if_not_exists else ""
        return f"ALTER TABLE {self.quote(table)} ADD COLUMN{guard} {self.quote(column)} {self.types[type_]}"

    def evolve(self, plan: MaterializationPlan) -> list[str]:
        return [self.add_column(plan.target_table, c.output_column, "string")
                for c in plan.extension_columns]

    def staged_select(self, plan: MaterializationPlan) -> str:
        exprs = [f"{self.cast(c)} AS {self.quote(c.output_column)}" for c in plan.columns]
        exprs += [self.quote(n) for n in plan.lineage_columns if n != "_row_number"]
        exprs.append(f"CAST({self.quote('_row_number')} AS {self.types['integer']}) AS {self.quote('_row_number')}")
        exprs.append(f"{_lit(plan.feed_name)} AS {self.quote('_feed')}")
        exprs.append(f"{plan.spec_id} AS {self.quote('_spec_id')}")
        key = ", ".join(self.quote(k) for k in plan.key)
        not_null = " AND ".join(f"{self.quote(k)} IS NOT NULL" for k in plan.key) or "1=1"
        inner = ",\n      ".join(exprs)
        return (
            f"SELECT * FROM (\n  SELECT t.*, ROW_NUMBER() OVER (PARTITION BY {key or '1'}"
            f" ORDER BY {self.quote('_row_number')} DESC) AS {self.quote('_sg_rn')}\n"
            f"  FROM (\n    SELECT {inner}\n    FROM {self.quote(plan.raw_table)}\n  ) t\n"
            f"  WHERE {not_null}\n) s WHERE {self.quote('_sg_rn')} = 1"
        )

    def output_names(self, plan: MaterializationPlan) -> list[str]:
        return [n for n, _ in self.column_defs(plan)]

    def merge(self, plan: MaterializationPlan) -> str:
        cols = self.output_names(plan)
        col_list = ", ".join(self.quote(c) for c in cols)
        non_key = [c for c in cols if c not in plan.key]
        sets = ", ".join(f"{self.quote(c)} = excluded.{self.quote(c)}" for c in non_key)
        conflict = ", ".join(self.quote(k) for k in plan.key)
        return (f"INSERT INTO {self.quote(plan.target_table)} ({col_list})\n"
                f"SELECT {col_list} FROM (\n{self.staged_select(plan)}\n) staged WHERE true\n"
                f"ON CONFLICT ({conflict}) DO UPDATE SET {sets}")

    def key_index(self, plan: MaterializationPlan) -> str | None:
        if not plan.key:
            return None
        cols = ", ".join(self.quote(k) for k in plan.key)
        return (f"CREATE UNIQUE INDEX IF NOT EXISTS {self.quote('ux_' + plan.target_table + '_key')}"
                f" ON {self.quote(plan.target_table)} ({cols})")

    def notes(self) -> list[str]:
        return []

    def render(self, plan: MaterializationPlan) -> SqlBundle:
        evolve = self.evolve(plan)
        idx = self.key_index(plan)
        if idx:
            evolve = [idx, *evolve]
        return SqlBundle(self.name, self.create_table(plan), evolve, self.merge(plan),
                         [f"spec {plan.spec_id} (v{plan.spec_version}), source object "
                          f"{plan.source_object_id}, staging table {plan.raw_table}",
                          *plan.warnings, *self.notes()])


def _sqlite_date(text: str, fmt: DateFormat) -> str:
    s = text
    if fmt.order == "ymd" and fmt.sep == "-" and not fmt.month_name:
        return f"date(substr({s}, 1, 10))"
    sep = _lit(fmt.sep)
    p1 = f"instr({s}, {sep})"
    rest = f"substr({s}, {p1} + 1)"
    p2 = f"instr({rest}, {sep})"
    parts = [f"substr({s}, 1, {p1} - 1)", f"substr({rest}, 1, {p2} - 1)", f"substr({rest}, {p2} + 1)"]
    comp = dict(zip(fmt.order, parts))
    month = comp["m"]
    if fmt.month_name:
        cases = " ".join(f"WHEN {_lit(m)} THEN {i}" for i, m in enumerate(
            ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1))
        month = f"(CASE UPPER(substr({comp['m']}, 1, 3)) {cases} END)"
    y, d = comp["y"], comp["d"]
    return (f"CASE WHEN {p1} > 0 AND {p2} > 0 THEN date(printf('%04d-%02d-%02d', {y}, {month}, {d})) END")


class SQLiteDialect(Dialect):
    name = "sqlite"
    types = {"string": "TEXT", "integer": "INTEGER", "number": "REAL", "date": "TEXT",
             "boolean": "INTEGER"}
    true_value, false_value = "1", "0"
    add_column_if_not_exists = False

    def comma_grouping_ok(self, col: str) -> str:
        text = f"TRIM({self.quote(col)})"
        bad = " OR ".join(f"{text} GLOB {_lit(g)}" for g in _BAD_COMMA_GLOBS)
        return f"NOT ({bad})"

    def try_number(self, text: str, integer: bool) -> str:
        cast = "INTEGER" if integer else "REAL"
        return (f"CASE WHEN {text} <> '' AND {text} NOT GLOB '*[^0-9.+-]*' AND {text} GLOB '*[0-9]*'"
                f" THEN CAST({text} AS {cast}) END")

    def try_date(self, text: str, fmt: DateFormat) -> str:
        return _sqlite_date(text, fmt)

    def notes(self) -> list[str]:
        return ["ALTER TABLE statements for extension columns must only run for columns not yet"
                " present (the SQLite executor checks PRAGMA table_info)"]


def _pattern(fmt: DateFormat, java: bool = False, strftime: bool = False, mon: str = "Mon") -> str:
    if strftime:
        parts = {"y": "%Y", "m": "%b" if fmt.month_name else "%m", "d": "%d"}
    elif java:
        parts = {"y": "yyyy", "m": "MMM" if fmt.month_name else "M", "d": "d"}
    else:
        parts = {"y": "YYYY", "m": mon if fmt.month_name else "MM", "d": "DD"}
    return fmt.sep.join(parts[c] for c in fmt.order)


class PostgresDialect(Dialect):
    name = "postgres"
    types = {"string": "TEXT", "integer": "BIGINT", "number": "NUMERIC(38,6)", "date": "DATE",
             "boolean": "BOOLEAN"}

    def regex_match(self, text: str, pattern: str) -> str:
        return f"{text} ~ {_lit(pattern)}"

    def try_number(self, text: str, integer: bool) -> str:
        num = f"CAST({text} AS NUMERIC(38,6))"
        value = f"CAST(ROUND({num}) AS BIGINT)" if integer else num
        return f"CASE WHEN {text} ~ '^[+-]?([0-9]+\\.?[0-9]*|\\.[0-9]+)$' THEN {value} END"

    def try_date(self, text: str, fmt: DateFormat) -> str:
        month = "[A-Za-z]{3,9}" if fmt.month_name else "[0-9]{1,2}"
        rx_parts = {"y": "[0-9]{4}", "m": month, "d": "[0-9]{1,2}"}
        sep = "\\." if fmt.sep == "." else fmt.sep
        rx = "^" + sep.join(rx_parts[c] for c in fmt.order) + ("$" if fmt.order != "ymd" else "")
        return f"CASE WHEN {text} ~ {_lit(rx)} THEN TO_DATE({text}, {_lit(_pattern(fmt))}) END"


class MergeDialect(Dialect):
    def merge(self, plan: MaterializationPlan) -> str:
        cols = self.output_names(plan)
        on = " AND ".join(f"tgt.{self.quote(k)} = src.{self.quote(k)}" for k in plan.key)
        non_key = [c for c in cols if c not in plan.key]
        sets = ", ".join(f"{self.quote(c)} = src.{self.quote(c)}" for c in non_key)
        col_list = ", ".join(self.quote(c) for c in cols)
        values = ", ".join(f"src.{self.quote(c)}" for c in cols)
        return (f"MERGE INTO {self.quote(plan.target_table)} AS tgt\nUSING (\n{self.staged_select(plan)}\n) AS src\n"
                f"ON {on}\nWHEN MATCHED THEN UPDATE SET {sets}\n"
                f"WHEN NOT MATCHED THEN INSERT ({col_list}) VALUES ({values})")

    def key_index(self, plan: MaterializationPlan) -> str | None:
        return None


class DuckDBDialect(MergeDialect):
    name = "duckdb"
    types = {"string": "VARCHAR", "integer": "BIGINT", "number": "DECIMAL(38,6)", "date": "DATE",
             "boolean": "BOOLEAN"}

    def regex_match(self, text: str, pattern: str) -> str:
        return f"regexp_full_match({text}, {_lit(pattern)})"

    def try_number(self, text: str, integer: bool) -> str:
        if integer:
            return f"TRY_CAST(TRY_CAST({text} AS DECIMAL(38,6)) AS BIGINT)"
        return f"TRY_CAST({text} AS DECIMAL(38,6))"

    def try_date(self, text: str, fmt: DateFormat) -> str:
        return f"CAST(TRY_STRPTIME({text}, {_lit(_pattern(fmt, strftime=True))}) AS DATE)"


class SnowflakeDialect(MergeDialect):
    name = "snowflake"
    types = {"string": "VARCHAR", "integer": "NUMBER(38,0)", "number": "NUMBER(38,6)", "date": "DATE",
             "boolean": "BOOLEAN"}

    def regex_match(self, text: str, pattern: str) -> str:
        return f"REGEXP_LIKE({text}, {_lit(pattern)})"

    def try_number(self, text: str, integer: bool) -> str:
        return f"TRY_TO_NUMBER({text}, 38, {0 if integer else 6})"

    def try_date(self, text: str, fmt: DateFormat) -> str:
        return f"TRY_TO_DATE({text}, {_lit(_pattern(fmt, mon='MON'))})"

    def notes(self) -> list[str]:
        return ["generated text only; not executed against a live Snowflake account"]


class DatabricksDialect(MergeDialect):
    name = "databricks"
    types = {"string": "STRING", "integer": "BIGINT", "number": "DECIMAL(38,6)", "date": "DATE",
             "boolean": "BOOLEAN"}
    add_column_if_not_exists = False

    def quote(self, ident: str) -> str:
        return "`" + ident.replace("`", "``") + "`"

    def regex_match(self, text: str, pattern: str) -> str:
        return f"{text} RLIKE {_lit(pattern)}"

    def try_number(self, text: str, integer: bool) -> str:
        if integer:
            return f"try_cast(try_cast({text} AS DECIMAL(38,6)) AS BIGINT)"
        return f"try_cast({text} AS DECIMAL(38,6))"

    def try_date(self, text: str, fmt: DateFormat) -> str:
        return f"to_date(try_to_timestamp({text}, {_lit(_pattern(fmt, java=True))}))"

    def create_table(self, plan: MaterializationPlan) -> str:
        return super().create_table(plan) + " USING DELTA"

    def add_column(self, table: str, column: str, type_: str) -> str:
        return f"ALTER TABLE {self.quote(table)} ADD COLUMNS ({self.quote(column)} {self.types[type_]})"

    def notes(self) -> list[str]:
        return ["generated text only; not executed against a live Databricks workspace",
                "ADD COLUMNS has no IF NOT EXISTS guard; skip statements for columns that already exist"]


DIALECTS: dict[str, type[Dialect]] = {
    "sqlite": SQLiteDialect,
    "duckdb": DuckDBDialect,
    "postgres": PostgresDialect,
    "snowflake": SnowflakeDialect,
    "databricks": DatabricksDialect,
}


def get_dialect(name: str) -> Dialect:
    try:
        return DIALECTS[name]()
    except KeyError:
        raise ValueError(f"unknown dialect {name!r}; choose from {sorted(DIALECTS)}") from None
