# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import binascii
import os
import typing
import urllib.parse

from datetime import datetime
from json import JSONEncoder

import certifi
import meilisearch
import opensearchpy
import redis
import requests_aws4auth
import sentry_sdk

from opensearchpy.helpers import parallel_bulk
from redis.lock import Lock
from sqlalchemy import Integer, cast, extract, func, select, text
from urllib3.util import parse_url

from warehouse import tasks
from warehouse.packaging.models import (
    Classifier,
    Description,
    Project,
    Release,
    ReleaseClassifiers,
)
from warehouse.packaging.search import Project as ProjectDocument
from warehouse.search.utils import get_index

if typing.TYPE_CHECKING:
    from pyramid.request import Request


def _project_docs(db, project_name: str | None = None):
    classifiers_subquery = (
        select(func.array_agg(Classifier.classifier))
        .select_from(ReleaseClassifiers)
        .join(Classifier, Classifier.id == ReleaseClassifiers.trove_id)
        .filter(Release.id == ReleaseClassifiers.release_id)
        .correlate(Release)
        .scalar_subquery()
        .label("classifiers")
    )
    projects_to_index = (
        select(
            Description.raw.label("description"),
            Release.author,
            Release.author_email,
            Release.maintainer,
            Release.maintainer_email,
            Release.home_page,
            Release.summary,
            Release.keywords,
            Release.platform,
            Release.download_url,
            Release.created,
            classifiers_subquery,
            Project.normalized_name,
            Project.name,
        )
        .select_from(Release)
        .join(Description)
        .join(Project)
        .filter(
            Release.yanked.is_(False),
            Release.files.any(),
            # Filter by project_name if provided
            Project.name == project_name if project_name else text("TRUE"),
        )
        .order_by(
            Project.name,
            Release.is_prerelease.nullslast(),
            Release._pypi_ordering.desc(),
        )
        .distinct(Project.name)
        .execution_options(yield_per=25000)
    )

    results = db.execute(projects_to_index)

    for partition in results.partitions():
        for release in partition:
            p = ProjectDocument.from_db(release)
            p._index = None
            p.full_clean()
            doc = p.to_dict(include_meta=True)
            doc.pop("_index", None)
            yield doc


class SearchLock(Lock):
    def __init__(self, redis_client, timeout=None, blocking_timeout=None):
        super().__init__(
            redis_client,
            name="search-index",
            timeout=timeout,
            blocking_timeout=blocking_timeout,
        )


@tasks.task(bind=True, ignore_result=True, acks_late=True)
def reindex(self, request):
    """
    Recreate the Search Index.
    """
    r = redis.StrictRedis.from_url(request.registry.settings["celery.scheduler_url"])
    try:
        with SearchLock(r, timeout=30 * 60, blocking_timeout=30):
            p = parse_url(request.registry.settings["opensearch.url"])
            qs = urllib.parse.parse_qs(p.query)
            kwargs = {
                "hosts": [urllib.parse.urlunparse((p.scheme, p.netloc) + ("",) * 4)],
                "verify_certs": True,
                "ca_certs": certifi.where(),
                "timeout": 30,
                "retry_on_timeout": True,
                "serializer": opensearchpy.serializer.serializer,
            }
            aws_auth = bool(qs.get("aws_auth", False))
            if aws_auth:
                aws_region = qs.get("region", ["us-east-1"])[0]
                kwargs["connection_class"] = opensearchpy.RequestsHttpConnection
                kwargs["http_auth"] = requests_aws4auth.AWS4Auth(
                    request.registry.settings["aws.key_id"],
                    request.registry.settings["aws.secret_key"],
                    aws_region,
                    "es",
                )
            client = opensearchpy.OpenSearch(**kwargs)
            number_of_replicas = request.registry.get("opensearch.replicas", 0)
            refresh_interval = request.registry.get("opensearch.interval", "1s")

            # We use a randomly named index so that we can do a zero downtime reindex.
            # Essentially we'll use a randomly named index which we will use until all
            # of the data has been reindexed, at which point we'll point an alias at
            # our randomly named index, and then delete the old randomly named index.

            # Create the new index and associate all of our doc types with it.
            index_base = request.registry["opensearch.index"]
            random_token = binascii.hexlify(os.urandom(5)).decode("ascii")
            new_index_name = f"{index_base}-{random_token}"
            doc_types = request.registry.get("search.doc_types", set())
            shards = request.registry.get("opensearch.shards", 1)

            # Create the new index with zero replicas and index refreshes disabled
            # while we are bulk indexing.
            new_index = get_index(
                new_index_name,
                doc_types,
                using=client,
                shards=shards,
                replicas=0,
                interval="-1",
            )
            new_index.create(wait_for_active_shards=shards)

            # From this point on, if any error occurs, we want to be able to delete our
            # in progress index.
            try:
                request.db.execute(text("SET statement_timeout = '600s'"))

                for _ in parallel_bulk(
                    client, _project_docs(request.db), index=new_index_name
                ):
                    pass
            except:  # noqa
                new_index.delete()
                raise
            finally:
                request.db.rollback()
                request.db.close()

            # Now that we've finished indexing all of our data we can update the
            # replicas and refresh intervals.
            client.indices.put_settings(
                index=new_index_name,
                body={
                    "index": {
                        "number_of_replicas": number_of_replicas,
                        "refresh_interval": refresh_interval,
                    }
                },
            )

            # Point the alias at our new randomly named index and delete the old index.
            if client.indices.exists_alias(name=index_base):
                to_delete = set()
                actions = []
                for name in client.indices.get_alias(name=index_base):
                    to_delete.add(name)
                    actions.append({"remove": {"index": name, "alias": index_base}})
                actions.append({"add": {"index": new_index_name, "alias": index_base}})
                client.indices.update_aliases({"actions": actions})
                client.indices.delete(",".join(to_delete))
            else:
                client.indices.put_alias(name=index_base, index=new_index_name)
    except redis.exceptions.LockError as exc:
        sentry_sdk.capture_exception(exc)
        raise self.retry(countdown=60, exc=exc)


@tasks.task(bind=True, ignore_result=True, acks_late=True)
def reindex_project(self, request, project_name):
    r = redis.StrictRedis.from_url(request.registry.settings["celery.scheduler_url"])
    try:
        with SearchLock(r, timeout=15, blocking_timeout=1):
            client = request.registry["opensearch.client"]
            doc_types = request.registry.get("search.doc_types", set())
            index_name = request.registry["opensearch.index"]
            get_index(
                index_name,
                doc_types,
                using=client,
                shards=request.registry.get("opensearch.shards", 1),
                replicas=request.registry.get("opensearch.replicas", 0),
            )

            for _ in parallel_bulk(
                client, _project_docs(request.db, project_name), index=index_name
            ):
                pass
    except redis.exceptions.LockError as exc:
        sentry_sdk.capture_exception(exc)
        raise self.retry(countdown=60, exc=exc)


@tasks.task(bind=True, ignore_result=True, acks_late=True)
def unindex_project(self, request, project_name):
    r = redis.StrictRedis.from_url(request.registry.settings["celery.scheduler_url"])
    try:
        with SearchLock(r, timeout=15, blocking_timeout=1):
            client = request.registry["opensearch.client"]
            index_name = request.registry["opensearch.index"]
            try:
                client.delete(index=index_name, id=project_name)
            except opensearchpy.exceptions.NotFoundError:
                pass
    except redis.exceptions.LockError as exc:
        sentry_sdk.capture_exception(exc)
        raise self.retry(countdown=60, exc=exc)


class CustomEncoder(JSONEncoder):
    """Used when passing objects to Meilisearch"""

    def default(self, obj):
        if isinstance(obj, datetime):
            # TODO: Need to figure out the right input to humanize
            #  Meilisearch does not support a "date" type, convert to a string
            #  We also currently store the `created_timestamp` as an integer,
            #  might be better to only use that, and skip custom encoder completely?
            #  Also need to decide whether we want `str(obj)` or `obj.isoformat()` here
            return obj.isoformat()

        # Let the base class default method raise the TypeError
        return super().default(obj)


@tasks.task(bind=True, ignore_result=True, acks_late=True)
def reindex_ms(self, request: Request) -> None:
    """
    Recreate the Search Index using Meilisearch.

    TODO: Needs a lot of work before it's production-ready.
     See inline for lots of TODOs.
    """
    # TODO: Convert to an interface and client
    client = meilisearch.Client(url="http://meilisearch:7700", timeout=30)

    # TODO: Move index creation/swapping to a separate function
    #  Implement a `blue/green` index naming strategy?
    #  https://www.meilisearch.com/docs/learn/getting_started/indexes#swapping-indexes
    #  For now, drop the index and recreate it
    client.index("projects").delete()

    client.create_index("projects", options={"primaryKey": "normalized_name"})
    client.index("projects").update_searchable_attributes(
        # Controls the searchable fields, as well as the search order for relevancy.
        # The order is important, as the earlier fields are considered more important.
        # https://www.meilisearch.com/docs/learn/relevancy/displayed_searchable_attributes#the-searchableattributes-list
        [
            "name",
            "normalized_name",  # TODO: Should normalized_name come first?
            "keywords",
            # "keywords_array"  # TODO: need a data migration to fully populate
            "description",
            "summary",
            "classifiers",
            "home_page",  # TODO: Do we even need this?
        ]
    )
    client.index("projects").update_filterable_attributes(
        [
            "classifiers",
            # TODO: What does facets give us?
            #  https://www.meilisearch.com/docs/learn/filtering_and_sorting/search_with_facet_filters
            # "keywords_array",
        ]
    )
    client.index("projects").update_sortable_attributes(
        [
            "created_timestamp",
        ]
    )
    # TODO: Do we need to customize separators to make `.` and `-` tokens
    #  that shouldn't be split, as they are used in package names,
    #  or will the other tokenization settings be enough?
    #  See https://www.meilisearch.com/docs/reference/api/settings#non-separator-tokens
    # client.index("projects").update_non_separator_tokens([".", "-"])

    # TODO: Likely need to tweak the ranking rules to get the best search results
    #  https://www.meilisearch.com/docs/learn/relevancy/ranking_rules
    client.index("projects").update_ranking_rules(
        [
            "attribute",  # use attribute order first (name, normalized_name, etc.)
            "words",
            "typo",
            "proximity",
            "sort",
            "exactness",
        ]
    )
    # TODO: Determine what attributes we don't care about returning to the user
    #  and exclude them from the response, saving bytes over the wire
    client.index("projects").update_displayed_attributes(
        [
            "name",
            "summary",
            "created_timestamp",
        ]
    )

    index = client.index("projects")

    # TODO: Wrap up everything with a singleton lock
    # r = redis.StrictRedis.from_url(request.registry.settings["celery.scheduler_url"])
    # try:
    #     with SearchLock(r, timeout=30 * 60, blocking_timeout=30):
    #         ...
    # except redis.exceptions.LockError as exc:
    #     sentry_sdk.capture_exception(exc)
    #     raise self.retry(countdown=60, exc=exc)

    # TODO: extract to a utility function, and add filter for `project_name` is passed
    project_name: str | None = None

    classifiers_subquery = (
        select(func.array_agg(Classifier.classifier))
        .select_from(ReleaseClassifiers)
        .join(Classifier, Classifier.id == ReleaseClassifiers.trove_id)
        .filter(Release.id == ReleaseClassifiers.release_id)
        .correlate(Release)
        .scalar_subquery()
        .label("classifiers")
    )
    projects_to_index = (
        select(
            Project.name,
            Project.normalized_name,
            Release.keywords,
            # Release.keywords_array, # TODO: need a data migration to fully populate
            # TODO: Trim the description to a reasonable length.
            #  In dev, cuts ~33% of the index size,
            #  leave whole for now to compare search results with OpenSearch
            # func.left(Description.raw, 1000).label("description"),
            Description.raw.label("description"),
            Release.summary,
            classifiers_subquery,
            Release.home_page,  # TODO: Do we even need this?
            # Release.created,  # TODO: Is created_timestamp is enough for our needs?
            cast(extract("epoch", Release.created), Integer).label("created_timestamp"),
        )
        .select_from(Release)
        .join(Description)
        .join(Project)
        .filter(
            Release.yanked.is_(False),
            Release.files.any(),
            # Filter by project_name if provided
            Project.name == project_name if project_name else text("TRUE"),
        )
        .order_by(
            Project.name,
            Release.is_prerelease.nullslast(),
            Release._pypi_ordering.desc(),
        )
        .distinct(Project.name)
        .execution_options(yield_per=1000)
    )

    results = request.db.execute(projects_to_index)

    for partition in results.partitions():
        # Transform each result to a dict representation of the row
        results_batch = [row._asdict() for row in partition]
        # Add (or replace existing) documents, with a custom encoder to handle datetime
        index.add_documents(results_batch, serializer=CustomEncoder)
        # TODO: Log/metrics the number of documents added
        print(f"Added {len(results_batch)} documents to the index")
