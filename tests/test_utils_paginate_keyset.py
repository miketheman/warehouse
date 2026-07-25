# SPDX-License-Identifier: Apache-2.0

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from sqlalchemy import Column, DateTime, Integer, create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from warehouse.utils.paginate import KeysetCursorPage


class Base(DeclarativeBase):
    pass


class Item(Base):
    __tablename__ = "items"
    id = Column(Integer, primary_key=True)
    submitted_date = Column(DateTime(timezone=True), nullable=False)


class _FakeGET:
    def __init__(self, data):
        self._data = data

    def dict_of_lists(self):
        return {k: (v if isinstance(v, list) else [v]) for k, v in self._data.items()}


class _FakeRequest:
    def __init__(self, params=None):
        params = params or {}
        self.params = params
        self.GET = _FakeGET(params)

    def current_route_path(self, _query=None):
        from urllib.parse import urlencode

        q = _query or []
        return "/fake?" + urlencode(q)


def _extract_query_param(url, key):
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    return qs.get(key, [None])[0]


def test_keyset_paginates_forward_and_backward():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()

    now = datetime.now(timezone.utc)
    # Create 60 items with descending submitted_date (newest has largest id)
    items = []
    for i in range(60):
        dt = now - timedelta(seconds=i)
        items.append(Item(id=i + 1, submitted_date=dt))
    session.add_all(items)
    session.commit()

    # Page 1 (no cursor)
    req1 = _FakeRequest()
    page1 = KeysetCursorPage(
        req1,
        session.query(Item),
        (Item.submitted_date, Item.id),
        page_size=25,
    )

    assert len(page1.items) == 25
    assert page1.prev_url is None
    assert page1.next_url is not None

    # Use next_url 'after' to get page 2
    after = _extract_query_param(page1.next_url, "after")
    assert after

    req2 = _FakeRequest({"after": after})
    page2 = KeysetCursorPage(
        req2,
        session.query(Item),
        (Item.submitted_date, Item.id),
        page_size=25,
    )

    assert len(page2.items) == 25
    assert page2.prev_url is not None

    # Go back using prev 'before' to return to page 1
    before = _extract_query_param(page2.prev_url, "before")
    assert before

    req_back = _FakeRequest({"before": before})
    page_back = KeysetCursorPage(
        req_back,
        session.query(Item),
        (Item.submitted_date, Item.id),
        page_size=25,
    )

    assert [i.id for i in page_back.items] == [i.id for i in page1.items]

    # Final page should have no next_url
    # Derive last page by walking until next_url is None (2 steps more)
    # Use page2.next_url to get page3
    after2 = _extract_query_param(page2.next_url, "after") if page2.next_url else None
    if after2:
        req3 = _FakeRequest({"after": after2})
        page3 = KeysetCursorPage(
            req3,
            session.query(Item),
            (Item.submitted_date, Item.id),
            page_size=25,
        )
        # Page3 should be the last (10 items remain)
        assert len(page3.items) == 10
        assert page3.next_url is None
