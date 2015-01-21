"""drop event constraint

Revision ID: 557378226d9f
Revises: 1c73ca99c03b
Create Date: 2015-03-11 02:42:57.477016

"""

# revision identifiers, used by Alembic.
revision = '557378226d9f'
down_revision = '1c73ca99c03b'

from alembic import op


def upgrade():
    conn = op.get_bind()
    conn.execute('''ALTER TABLE event DROP INDEX uuid;''')


def downgrade():
    raise Exception
