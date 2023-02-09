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
"""
Add Search via Postgres flag

Revision ID: 533b02ea6ede
Revises: f93cf2d43974
Create Date: 2023-02-09 16:48:02.585408
"""

import sqlalchemy as sa

from alembic import op

revision = "533b02ea6ede"
down_revision = "f93cf2d43974"

def upgrade():
    op.execute(
        """
        INSERT INTO admin_flags(id, description, enabled, notify)
        VALUES (
            'search-via-postgres',
            'Search via Postgres instead of Elasticsearch',
            FALSE,
            FALSE
        )
    """
    )


def downgrade():
    op.execute("DELETE FROM admin_flags WHERE id = 'search-via-postgres'")
