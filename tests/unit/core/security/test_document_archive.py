from io import BytesIO
from zipfile import ZipFile, ZIP_DEFLATED
from unittest.mock import Mock

import pytest

from core.security import document_archive as guard


def archive(parts):
    stream = BytesIO()
    with ZipFile(stream, 'w', compression=ZIP_DEFLATED) as z:
        for name, content in parts:
            z.writestr(name, content)
    return stream.getvalue()


@pytest.mark.parametrize('xml', [
    '<!DOCTYPE a [<!ENTITY x "expanded">]><a>&x;</a>',
    '<!DOCTYPE a SYSTEM "https://example.invalid/private"><a/>',
    '<a xmlns:w="urn:word"><w:gridSpan w:val="999999999"/></a>',
    '<a xmlns:w="urn:word"><w:gridSpan w:val="-1"/></a>',
    '<a>' * 129 + '</a>' * 129,
    '<Types><Override PartName="/word/main.bin" ContentType="application/main+xml"/></Types>',
    '<Types><Default Extension="bin" ContentType="application/main+xml"/></Types>',
])
def test_hostile_xml_refused(xml):
    with pytest.raises(guard.DocumentLimitError):
        guard.validate_docx_archive(archive([('word/document.xml', xml)]))


@pytest.mark.parametrize('kind', ['expanded', 'part', 'count', 'nodes', 'cells'])
def test_resource_budgets(kind, monkeypatch):
    parts = [('word/document.xml', '<a>' + '<tc/>' * 20 + '</a>')]
    if kind == 'expanded':
        monkeypatch.setattr(guard, 'MAX_EXPANDED_BYTES', 100)
        parts = [('media/payload.bin', b'x' * 10000)]
    elif kind == 'part':
        monkeypatch.setattr(guard, 'MAX_PART_BYTES', 100)
        parts = [('media/payload.bin', b'x' * 101)]
    elif kind == 'count':
        monkeypatch.setattr(guard, 'MAX_PARTS', 1)
        parts += [('other.xml', '<a/>')]
    elif kind == 'nodes':
        monkeypatch.setattr(guard, 'MAX_XML_NODES', 10)
    else:
        monkeypatch.setattr(guard, 'MAX_EXPANDED_CELLS', 10)
    with pytest.raises(guard.DocumentLimitError):
        guard.validate_docx_archive(archive(parts))


def test_node_budget_shared_across_parts(monkeypatch):
    monkeypatch.setattr(guard, 'MAX_XML_NODES', 3)
    with pytest.raises(guard.DocumentLimitError):
        guard.validate_docx_archive(archive([('a.xml', '<a><b/></a>'), ('b.xml', '<a><b/></a>')]))


@pytest.mark.parametrize('name', ['/outside.xml', '../outside.xml', 'dir\\outside.xml'])
def test_ambiguous_paths_refused(name):
    with pytest.raises(guard.DocumentLimitError):
        guard.validate_docx_archive(archive([(name, '<a/>')]))


def test_duplicate_parts_refused():
    with pytest.warns(UserWarning):
        content = archive([('a.xml', '<a/>'), ('a.xml', '<b/>')])
    with pytest.raises(guard.DocumentLimitError):
        guard.validate_docx_archive(content)


def test_guard_failure_never_calls_document_parser(tmp_path, monkeypatch):
    import docx
    from tools.document_worker import parse_document
    parser = Mock(side_effect=AssertionError('parser must not run'))
    monkeypatch.setattr(docx, 'Document', parser)
    payload = archive([('word/document.xml', '<!DOCTYPE a [<!ENTITY x "x">]><a/>')])
    with pytest.raises(ValueError, match='DTDs and entities'):
        parse_document('docx', payload)
    parser.assert_not_called()


def test_accepted_small_archive():
    guard.validate_docx_archive(archive([
        ('[Content_Types].xml', '<Types><Default Extension="xml" ContentType="application/xml"/></Types>'),
        ('word/document.xml', '<a xmlns:w="urn:word"><w:tc><w:gridSpan w:val="2"/></w:tc></a>'),
        ('word/media/image.png', b'\x89PNG\r\n\x1a\n'),
    ]))


@pytest.mark.asyncio
async def test_rejected_replacement_preserves_previous_knowledge(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from modules.memory import registry
    import tools.knowledge_ingest as ki
    source = tmp_path / 'reference.docx'
    source.write_bytes(archive([('word/document.xml', '<!DOCTYPE a><a/>')]))
    remove = AsyncMock()
    ingest = AsyncMock()
    monkeypatch.setattr(ki, '_resolve_confinement_root', lambda *args: tmp_path)
    monkeypatch.setattr(registry, 'kb_source_hash', AsyncMock(return_value='previous-good-hash'))
    monkeypatch.setattr(registry, 'kb_remove', remove)
    monkeypatch.setattr(registry, 'kb_replace_source', ingest)
    result = await ki.kb_ingest(str(source), user_id='user', session_id='session')
    assert result['ingested'] == 0
    remove.assert_not_awaited()
    ingest.assert_not_awaited()
