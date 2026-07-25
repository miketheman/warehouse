# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import base64
import binascii
import json
import typing

from datetime import datetime, timezone
from json import JSONDecodeError
from typing import Any, Union

from paginate import Page
from sqlalchemy import text, select, table, literal_column
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Query
from sqlalchemy.sql import Select
from sqlalchemy.sql.elements import ClauseElement


if typing.TYPE_CHECKING:
    from pyramid.request import Request
    from sqlalchemy.orm import Session

class _OpenSearchWrapper:
    max_results = 10000

    def __init__(self, query):
        self.query = query
        self.results = None
        self.best_guess = None

    def __getitem__(self, range):
        # If we're asking for a range that extends past our maximum results,
        # then we need to clamp the start of our slice to our maximum results
        # size, and make sure that the end of our slice >= to that to ensure a
        # consistent slice.
        if range.start > self.max_results:
            range = slice(
                self.max_results, max(range.stop, self.max_results), range.step
            )

        # If we're being asked for a range that extends past our maximum result
        # then we'll clamp it to the maximum result size and stop there.
        if range.stop > self.max_results:
            range = slice(range.start, self.max_results, range.step)

        if self.results is not None:
            raise RuntimeError("Cannot reslice after having already sliced.")
        self.results = self.query[range].execute()

        if hasattr(self.results, "suggest"):
            if self.results.suggest.name_suggestion:
                suggestion = self.results.suggest.name_suggestion[0]
                if suggestion.options:
                    self.best_guess = suggestion.options[0]

        return list(self.results)

    def __len__(self):
        if self.results is None:
            raise RuntimeError("Cannot get length until a slice.")
        if isinstance(self.results.hits.total, int):
            return min(self.results.hits.total, self.max_results)
        return min(self.results.hits.total["value"], self.max_results)


def OpenSearchPage(*args, **kwargs):  # noqa
    kwargs.setdefault("wrapper_class", _OpenSearchWrapper)
    return Page(*args, **kwargs)


def paginate_url_factory(request, query_arg="page"):
    def make_url(page):
        query_seq = [
            (k, v)
            for k, vs in request.GET.dict_of_lists().items()
            for v in vs
            if k != query_arg
        ]
        query_seq += [(query_arg, page)]
        return request.current_route_path(_query=query_seq)

    return make_url


class KeysetCursorPage:
    """Cursor-based page for SQLAlchemy queries.

    Assumes a stable ordering by ``(timestamp, id)`` where the id
    is a unique tie-breaker. Produces pages displayed in descending order and
    constructs ``prev``/``next`` links via URL-safe cursors derived from the
    first/last items of the current page.

    - Filters forward when ``after`` is present and backward when ``before`` is present.
    - Preserves all existing query parameters except paging keys (``after``/``before``/``page``).
    - Optionally computes an approximate total using the database planner.
    """

    def __init__(
        self,
        request: Request,
        query: Query,
        order_cols: tuple[Any, Any],
        page_size: int = 25,
        *,
        after: str | None = None,
        before: str | None = None,
        estimate_total: bool = False,
        table_name: str | None = None,
    ):
        self.request: Request = request
        self.query: Query = query
        self.col1, self.col2 = order_cols
        self.page_size: int = page_size

        if after is None:
            after = request.params.get("after") if hasattr(request, "params") else None
        if before is None:
            before = (
                request.params.get("before") if hasattr(request, "params") else None
            )

        if after and before:
            raise ValueError("Only one of 'after' or 'before' may be provided.")

        self.items: list[Any] = []
        self.prev_url: str | None = None
        self.next_url: str | None = None

        has_prev = False
        has_next = False

        if before:
            cursor_dt, cursor_id = _cursor_decode(before)

            newer_q = self.query.filter(
                (self.col1 > cursor_dt)
                | ((self.col1 == cursor_dt) & (self.col2 > cursor_id))
            ).order_by(self.col1.asc(), self.col2.asc())

            rows_asc = newer_q.limit(self.page_size + 1).all()
            if len(rows_asc) > self.page_size:
                has_prev = True
                rows_asc = rows_asc[-self.page_size :]
            self.items = list(reversed(rows_asc))

            if self.items:
                last = self.items[-1]
                older_exists = self.query.filter(
                    (self.col1 < getattr(last, self.col1.key))
                    | (
                        (self.col1 == getattr(last, self.col1.key))
                        & (self.col2 < getattr(last, self.col2.key))
                    )
                ).first()
                has_next = older_exists is not None
        else:
            if after:
                cursor_dt, cursor_id = _cursor_decode(after)
                self.query = self.query.filter(
                    (self.col1 < cursor_dt)
                    | ((self.col1 == cursor_dt) & (self.col2 < cursor_id))
                )
                has_prev = True

            page_q = self.query.order_by(self.col1.desc(), self.col2.desc())
            rows = page_q.limit(self.page_size + 1).all()
            if len(rows) > self.page_size:
                has_next = True
                rows = rows[: self.page_size]
            self.items = rows

        def _preserve_query_except(*exclude):
            if not hasattr(self.request, "GET"):
                return []
            return [
                (k, v)
                for k, vs in self.request.GET.dict_of_lists().items()
                for v in vs
                if k not in exclude
            ]

        if self.items:
            first = self.items[0]
            last = self.items[-1]
            if has_prev:
                prev_cursor = _cursor_encode(
                    getattr(first, self.col1.key), getattr(first, self.col2.key)
                )
                qseq = _preserve_query_except("page", "after", "before") + [
                    ("before", prev_cursor)
                ]
                self.prev_url = self.request.current_route_path(_query=qseq)
            if has_next:
                next_cursor = _cursor_encode(
                    getattr(last, self.col1.key), getattr(last, self.col2.key)
                )
                qseq = _preserve_query_except("page", "after", "before") + [
                    ("after", next_cursor)
                ]
                self.next_url = self.request.current_route_path(_query=qseq)

        self.approx_total: int | None = None
        if estimate_total:
            self.approx_total = _estimate_count_query(
                request.db, self.query, table_name=table_name
            )

    def __iter__(self):
        return iter(self.items)

    def __len__(self):
        return len(self.items)


# Cursor helpers for keyset pagination (datetime + id)
def _cursor_encode(dt: datetime, row_id: int) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    payload = {"t": dt.isoformat(), "i": int(row_id)}
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _cursor_decode(cursor: str):
    try:
        padding = "=" * (-len(cursor) % 4)
        data = base64.urlsafe_b64decode(cursor + padding)
        payload = json.loads(data.decode("utf-8"))
        t = payload["t"]
        i = int(payload["i"])
        # datetime.fromisoformat supports offsets like +00:00
        dt = datetime.fromisoformat(t)
        return dt, i
    except (ValueError, KeyError, JSONDecodeError, binascii.Error) as e:
        raise ValueError("Invalid cursor") from e


def _explain_rows_estimate(session, sql_text: str):
    try:
        res = session.execute(text(f"EXPLAIN (FORMAT JSON) {sql_text}"))
        row = res.scalar() if hasattr(res, "scalar") else None
        if row is None:
            first = res.first()
            row = first[0] if first else None
        if row is None:
            return None
        data = row if isinstance(row, list) else json.loads(row)
        plan = data[0].get("Plan") if isinstance(data, list) and data else None
        if not plan:
            return None
        rows = plan.get("Plan Rows") or plan.get("Rows")
        return int(rows) if rows is not None else None
    except (SQLAlchemyError, JSONDecodeError, ValueError):
        return None


def _table_rows_estimate(session: Session, table_name: str) -> int | None:
    try:
        pg_stat = table("pg_stat_all_tables")
        stmt = (
            select(literal_column("n_live_tup"))
            .select_from(pg_stat)
            .where(literal_column("relname") == table_name)
            .order_by(literal_column("schemaname") == "public")
            .limit(1)
        )
        val = session.execute(stmt).scalar()
        return int(val) if val is not None else None
    except SQLAlchemyError:
        return None


SASelectable = Union[Select, Query, ClauseElement]


def _estimate_count_query(
    session: Session, selectable: SASelectable, table_name: str | None = None
) -> int | None:
    """Return an approximate row count for a SQLAlchemy selectable/filter.

    Attempts EXPLAIN (FORMAT JSON) to extract the planner's estimated rows.
    Falls back to pg_stat_all_tables.n_live_tup for the given table.
    """
    sql_text = None
    try:
        sa_node: ClauseElement
        if isinstance(selectable, Query):
            sa_node = selectable.statement
        elif isinstance(selectable, (Select, ClauseElement)):
            sa_node = selectable

        bind = session.get_bind()
        sql_text = str(
            sa_node.compile(
                dialect=bind.dialect, compile_kwargs={"literal_binds": True}
            )
        )
    except SQLAlchemyError:
        sql_text = None

    if sql_text:
        est = _explain_rows_estimate(session, sql_text)
        if est is not None:
            return est

    if table_name:
        return _table_rows_estimate(session, table_name)

    return None
