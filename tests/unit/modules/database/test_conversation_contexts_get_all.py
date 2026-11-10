"""An unreadable conversation_contexts table is not an empty one."""
import pytest

from modules.database.connection import DatabaseConnection
from modules.database.conversation_contexts import ConversationContexts


@pytest.mark.asyncio
async def test_get_all_raises_when_the_table_cannot_be_read(tmp_path):
    db = DatabaseConnection(tmp_path / "bot.db")
    await db.connect()
    try:
        with pytest.raises(Exception, match="no such table"):
            await ConversationContexts(db).get_all()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_get_all_of_an_empty_table_is_empty(tmp_path):
    db = DatabaseConnection(tmp_path / "bot.db")
    await db.connect()
    try:
        store = ConversationContexts(db)
        await store.create_table()
        assert await store.get_all() == []
    finally:
        await db.close()
