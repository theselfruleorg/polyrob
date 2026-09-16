"""DOCX ZIP/XML admission limits before the higher-level document parser.

These structural budgets are not a process CPU/memory sandbox.
"""
from io import BytesIO
from pathlib import PurePosixPath
from xml.parsers import expat
from zipfile import ZipFile, ZIP_DEFLATED, ZIP_STORED

MAX_EXPANDED_BYTES = 32 * 1024 * 1024
MAX_PART_BYTES = 8 * 1024 * 1024
MAX_PARTS = 1024
MAX_XML_NODES = 100_000
MAX_XML_DEPTH = 128
MAX_TABLE_SPAN = 64
MAX_EXPANDED_CELLS = 100_000


class DocumentLimitError(ValueError):
    """Document exceeds the supported archive or XML safety contract."""


def validate_docx_archive(content: bytes) -> None:
    """Validate this exact byte snapshot; never extract archive paths to disk."""
    nodes = cells = expanded = 0
    with ZipFile(BytesIO(content)) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_PARTS:
            raise DocumentLimitError('too many document archive parts')
        if sum(entry.file_size for entry in entries) > MAX_EXPANDED_BYTES:
            raise DocumentLimitError('document expansion exceeds budget')
        seen = set()
        for entry in entries:
            name = entry.filename
            path = PurePosixPath(name)
            if (name in seen or path.is_absolute() or '..' in path.parts
                    or '\\' in name or entry.flag_bits & 1
                    or entry.compress_type not in (ZIP_STORED, ZIP_DEFLATED)):
                raise DocumentLimitError('unsupported or ambiguous archive part')
            seen.add(name)
            if entry.file_size > MAX_PART_BYTES:
                raise DocumentLimitError('document part exceeds budget')
            parser = None
            if name.lower().endswith(('.xml', '.rels')) or name == '[Content_Types].xml':
                parser = expat.ParserCreate(namespace_separator='}')
                depth = 0

                def refuse(*args):
                    raise DocumentLimitError('document DTDs and entities are not supported')

                def start(tag, attributes):
                    nonlocal nodes, cells, depth
                    nodes += 1
                    depth += 1
                    if nodes > MAX_XML_NODES or depth > MAX_XML_DEPTH:
                        raise DocumentLimitError('document XML structure exceeds budget')
                    if len(attributes) > 64 or sum(map(len, attributes.values())) > 65536:
                        raise DocumentLimitError('document XML attributes exceed budget')
                    local = tag.rsplit('}', 1)[-1]
                    # python-docx chooses XML parsers from content types, not
                    # file suffixes. Refuse noncanonical XML names so a .bin
                    # part cannot bypass this XML admission pass.
                    if local in ('Override', 'Default'):
                        content_type = attributes.get('ContentType', '').lower()
                        if content_type.endswith(('+xml', '/xml')):
                            part = attributes.get('PartName', '').lower()
                            extension = attributes.get('Extension', '').lower()
                            if ((local == 'Override' and not part.endswith(('.xml', '.rels')))
                                    or (local == 'Default' and extension not in ('xml', 'rels'))):
                                raise DocumentLimitError('XML content type requires canonical XML part name')
                    if local == 'tc':
                        cells += 1
                    if local == 'gridSpan':
                        raw = next((v for k, v in attributes.items()
                                    if k.rsplit('}', 1)[-1] == 'val'), '1')
                        try:
                            span = int(raw)
                        except ValueError as exc:
                            raise DocumentLimitError('invalid table span') from exc
                        if not 1 <= span <= MAX_TABLE_SPAN:
                            raise DocumentLimitError('table span exceeds budget')
                        cells += span
                    if cells > MAX_EXPANDED_CELLS:
                        raise DocumentLimitError('expanded table exceeds budget')

                def end(tag):
                    nonlocal depth
                    depth -= 1

                parser.StartElementHandler = start
                parser.EndElementHandler = end
                parser.StartDoctypeDeclHandler = refuse
                parser.EntityDeclHandler = refuse
                parser.ExternalEntityRefHandler = refuse
                parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
            actual = 0
            with archive.open(entry) as stream:
                while True:
                    chunk = stream.read(65536)
                    if not chunk:
                        break
                    actual += len(chunk)
                    expanded += len(chunk)
                    if actual > MAX_PART_BYTES or expanded > MAX_EXPANDED_BYTES:
                        raise DocumentLimitError('actual document expansion exceeds budget')
                    if parser is not None:
                        parser.Parse(chunk, False)
            if parser is not None:
                parser.Parse(b'', True)
