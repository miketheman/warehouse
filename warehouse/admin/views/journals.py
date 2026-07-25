# SPDX-License-Identifier: Apache-2.0

import shlex

from pyramid.httpexceptions import HTTPBadRequest
from pyramid.view import view_config
from sqlalchemy import and_
from sqlalchemy.orm import joinedload

from warehouse.authnz import Permissions
from warehouse.packaging.models import JournalEntry
from warehouse.utils.paginate import KeysetCursorPage


@view_config(
    route_name="admin.journals.list",
    renderer="warehouse.admin:templates/admin/journals/list.html",
    permission=Permissions.AdminJournalRead,
    uses_session=True,
)
def journals_list(request):
    q = request.params.get("q")

    journals_query = request.db.query(JournalEntry).options(
        joinedload(JournalEntry.submitted_by)
    )

    if q:
        terms = shlex.split(q)

        filters = []
        for term in terms:
            if ":" in term:
                field, value = term.split(":", 1)
                if field.lower() == "project":
                    filters.append(JournalEntry.name.ilike(value))
                if field.lower() == "version":
                    filters.append(JournalEntry.version.ilike(value))
                if field.lower() == "user":
                    filters.append(JournalEntry._submitted_by.like(value))
            else:
                filters.append(JournalEntry.name.ilike(term))

        # if filters:
        #     base_query = base_query.filter(and_(*filters))
        journals_query = journals_query.filter(and_(*filters))

    try:
        journals = KeysetCursorPage(
            request,
            journals_query,
            (JournalEntry.submitted_date, JournalEntry.id),
            page_size=25,
            estimate_total=True,
            table_name=JournalEntry.__tablename__,
        )
    except ValueError as e:
        # Bad cursor or both after/before provided
        raise HTTPBadRequest(str(e)) from None

    return {"journals": journals, "query": q}
