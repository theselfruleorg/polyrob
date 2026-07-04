"""PDF extraction mixin for FileSystem tool.

This module contains all PDF-related extraction logic extracted from filesystem.py.
The PdfExtractionMixin is composed into FileSystem via multiple-inheritance.

IMPORTANT: Do NOT add `from __future__ import annotations` to this file.
The registry inspects first-param annotations on @action closures via issubclass()
and stringized annotations break that routing.
"""

import logging
import re
import os
import tempfile
from io import BytesIO
from typing import Dict, Any

import pypdf  # type: ignore
from pypdf.errors import PdfReadError  # type: ignore

from core.exceptions import ServiceError
from utils.time_utils import get_current_timestamp


class PdfExtractionMixin:
    """Mixin providing PDF extraction capabilities for FileSystem."""

    async def _process_pdf(self, content) -> Dict[str, Any]:
        """Process PDF content with enhanced extraction capabilities and validation."""
        from io import BytesIO
        import pypdf  #type: ignore
        from pypdf.errors import PdfReadError  #type: ignore
        import re

        # Ensure content is in bytes
        if isinstance(content, str):
            try:
                content = content.encode('utf-8')
            except UnicodeEncodeError as e:
                raise ServiceError(f"Failed to encode PDF content: {str(e)}")

        try:
            # Extract metadata early before any other processing
            metadata = {}
            title = "Untitled PDF Document"

            try:
                # Create a new dedicated BytesIO for metadata extraction to avoid issues
                with BytesIO(content) as metadata_file:
                    try:
                        # Create a PDF reader specifically for metadata extraction
                        metadata_reader = pypdf.PdfReader(metadata_file, strict=False)

                        # Immediately extract and copy metadata to avoid closed file issues
                        if hasattr(metadata_reader, 'metadata') and metadata_reader.metadata:
                            # Get a shallow copy of the metadata dictionary
                            metadata_dict = dict(metadata_reader.metadata or {})

                            # Copy values to a new dictionary to avoid references to PDF objects
                            for key, value in metadata_dict.items():
                                if isinstance(key, str):
                                    clean_key = key.lstrip('/')
                                    metadata[clean_key] = str(value) if value is not None else ""

                                    # Look for title specifically
                                    if clean_key.lower() == 'title' and value:
                                        title = str(value)
                    except Exception as meta_err:
                        self.logger.warning(f"Failed initial metadata extraction: {str(meta_err)}")
            except Exception as meta_outer_err:
                self.logger.warning(f"Error during dedicated metadata extraction: {str(meta_outer_err)}")

            # More lenient PDF header check
            if not (content.startswith(b'%PDF-') or b'%PDF-' in content[:1024]):
                self.logger.warning("PDF header not found in expected location, attempting recovery...")

            # Try different PDF recovery methods
            recovery_methods = [
                # Method 1: Standard reading
                lambda f: pypdf.PdfReader(f, strict=False),

                # Method 2: Try with stream parsing and error recovery
                lambda f: self._read_pdf_with_recovery(f),

                # Method 3: Most permissive reading
                lambda f: self._read_pdf_permissive(f),

                # Method 4: New advanced PDF recovery
                lambda f: self._read_pdf_advanced_recovery(f)
            ]

            last_error = None
            pdf_reader = None

            # Try each recovery method
            for method in recovery_methods:
                try:
                    with BytesIO(content) as pdf_file:
                        pdf_file.seek(0)
                        pdf_reader = method(pdf_file)

                        # Test page access more thoroughly
                        valid_pages = 0
                        for i in range(min(3, len(pdf_reader.pages))):
                            try:
                                page = pdf_reader.pages[i]
                                # If we can get any text, consider it valid
                                text = self._extract_page_text_with_fallbacks(page)
                                if text and text.strip():
                                    valid_pages += 1
                            except Exception as e:
                                self.logger.debug(f"Error accessing page {i}: {str(e)}")
                                continue

                        if valid_pages > 0:
                            break  # Found a working method

                except Exception as e:
                    last_error = e
                    self.logger.debug(f"PDF recovery method failed: {str(e)}")
                    continue

            if pdf_reader is None:
                # Try the raw extraction method as last resort
                raw_text = self._extract_raw_pdf_text(content)
                if raw_text and len(raw_text) > 100 and self._is_readable_text(raw_text):
                    return {
                        'type': 'pdf',
                        'content': raw_text,
                        'metadata': {
                            'title': 'Extracted PDF Content',
                            'pages': 1,
                            'extraction_method': 'raw_bytes',
                            'extraction_quality': 'low'
                        }
                    }

                # If we still don't have content, try OCR if available
                try:
                    if self.container and self.container.has_service('ocr_service'):
                        self.logger.info("Trying OCR service for image-based PDF")
                        ocr_service = self.container.get_service('ocr_service')

                        # Save content to temporary file for OCR processing
                        temp_path = None
                        try:
                            with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as temp_file:
                                temp_path = temp_file.name
                                temp_file.write(content)

                            # Process with OCR
                            ocr_result = await ocr_service.extract_text_from_pdf(temp_path)

                            # If OCR was successful, return its result
                            if ocr_result and ocr_result.get('content'):
                                self.logger.info("Successfully extracted text using OCR service")
                                return ocr_result
                        except Exception as ocr_error:
                            self.logger.warning(f"OCR processing failed: {str(ocr_error)}")
                        finally:
                            # Ensure temp file is removed
                            if temp_path and os.path.exists(temp_path):
                                try:
                                    os.unlink(temp_path)
                                except Exception as cleanup_error:
                                    self.logger.warning(f"Failed to clean up temp file: {str(cleanup_error)}")
                except Exception as ocr_setup_error:
                    self.logger.warning(f"Failed to setup OCR processing: {str(ocr_setup_error)}")

                raise ServiceError(f"Failed to read PDF after all recovery attempts: {str(last_error)}")

            # Extract text with multiple fallback methods
            pages = []
            full_text = []
            total_text_length = 0
            successful_pages = 0
            # We already have metadata from the beginning of the method

            # Get page count with fallback methods
            try:
                page_count = len(pdf_reader.pages)
            except Exception as page_count_error:
                try:
                    # Try direct access to pages array
                    page_count = len(pdf_reader._get_num_pages())
                except Exception:
                    try:
                        # Try to access pages dict
                        page_count = len(pdf_reader._pages_cache)
                    except Exception:
                        # Last resort: binary search for valid pages
                        page_count = self._find_max_valid_page(pdf_reader)

                if page_count == 0:
                    self.logger.warning(f"Could not determine PDF page count: {str(page_count_error)}")
                    page_count = 1  # Assume at least one page

            # Process each page with enhanced extraction
            for i in range(page_count):
                try:
                    page_text = ""
                    try:
                        page = pdf_reader.pages[i]
                        page_text = self._extract_page_text_with_fallbacks(page)
                    except Exception as page_error:
                        self.logger.debug(f"Error extracting text from page {i+1}: {str(page_error)}")
                        page_text = f"[Error extracting page {i+1}]"

                    # Apply post-processing to improve text quality
                    page_text = self._post_process_pdf_text(page_text)

                    # Skip pages with no meaningful content
                    if not self._is_meaningful_content(page_text):
                        continue

                    # Skip pages that appear to contain binary data
                    if self._contains_binary_data(page_text):
                        self.logger.warning(f"Page {i+1} appears to contain binary data, skipping")
                        continue

                    # Add to page collection
                    pages.append({
                        'number': i + 1,
                        'content': page_text
                    })

                    full_text.append(page_text)
                    total_text_length += len(page_text)
                    successful_pages += 1

                except Exception as e:
                    self.logger.warning(f"Error processing page {i+1}: {str(e)}")

            # Check if we have any usable content
            combined_text = "\n\n".join(full_text)

            if not combined_text or len(combined_text.strip()) < 50:
                # Try fallback methods if regular extraction failed
                raw_text = self._extract_raw_pdf_text(content)
                if raw_text and len(raw_text) > 100:
                    combined_text = raw_text
                    successful_pages = 1
                else:
                    # If we still don't have good content, try OCR if available
                    try:
                        if self.container and self.container.has_service('ocr_service'):
                            self.logger.info("Trying OCR service for image-based PDF")
                            ocr_service = self.container.get_service('ocr_service')

                            # Save content to temporary file for OCR processing
                            temp_path = None
                            try:
                                with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as temp_file:
                                    temp_path = temp_file.name
                                    temp_file.write(content)

                                # Process with OCR
                                ocr_result = await ocr_service.extract_text_from_pdf(temp_path)

                                # If OCR was successful, return its result
                                if ocr_result and ocr_result.get('content'):
                                    self.logger.info("Successfully extracted text using OCR service")
                                    return ocr_result
                            except Exception as ocr_error:
                                self.logger.warning(f"OCR processing failed: {str(ocr_error)}")
                            finally:
                                # Ensure temp file is removed
                                if temp_path and os.path.exists(temp_path):
                                    try:
                                        os.unlink(temp_path)
                                    except Exception as cleanup_error:
                                        self.logger.warning(f"Failed to clean up temp file: {str(cleanup_error)}")
                    except Exception as ocr_setup_error:
                        self.logger.warning(f"Failed to setup OCR processing: {str(ocr_setup_error)}")

                    # If OCR failed or is not available, use a placeholder
                    combined_text = "[PDF CONTENT REQUIRES OCR PROCESSING - TEXT EXTRACTION FAILED]"

            # Validate extracted content
            if self._contains_binary_data(combined_text):
                # If it looks like binary, try more aggressive cleaning
                combined_text = self._aggressive_binary_cleanup(combined_text)

            # Final validation
            if not self._is_readable_text(combined_text):
                self.logger.warning("Extracted PDF content does not appear to be readable text")
                # Add indicator for clients to know content may need further processing
                combined_text = "[WARNING: PDF CONTENT MAY BE IMAGE-BASED OR CORRUPTED]\n\n" + combined_text

            return {
                'type': 'pdf',
                'content': combined_text,
                'title': title,
                'structure': {
                    'pages': pages,
                    'total_pages': page_count
                },
                'metadata': {
                    **metadata,
                    'processor_version': '2.0',
                    'processing_time': get_current_timestamp(),
                    'original_size': len(content),
                    'processed_size': total_text_length,
                    'successful_pages': successful_pages,
                    'total_pages': page_count,
                    'extraction_quality': 'high' if successful_pages == page_count else 'medium' if successful_pages > 0 else 'low'
                }
            }

        except ServiceError:
            raise
        except Exception as e:
            self.logger.error(f"Error processing PDF: {str(e)}", exc_info=True)
            raise ServiceError(f"Failed to process PDF: {str(e)}")

    def _read_pdf_with_recovery(self, file_obj) -> pypdf.PdfReader:
        """Read PDF with additional recovery options."""
        try:
            # Create a copy of the file content to avoid "I/O operation on closed file" errors
            file_obj.seek(0)
            content = file_obj.read()
            file_copy = BytesIO(content)

            try:
                reader = pypdf.PdfReader(file_copy, strict=False)
                # Force load document info to catch early failures
                _ = reader.metadata
                return reader
            except Exception as e:
                # Try to repair common issues
                repair_copy = BytesIO(content)

                # Try to fix common PDF corruptions
                if b'startxref' not in content:
                    # Add missing startxref
                    repaired_content = content + b'\nstartxref\n0\n%%EOF'
                    return pypdf.PdfReader(BytesIO(repaired_content), strict=False)

                return pypdf.PdfReader(repair_copy, strict=False)
        except Exception as e:
            self.logger.warning(f"Recovery failed: {str(e)}")
            raise e

    def _read_pdf_permissive(self, file_obj) -> pypdf.PdfReader:
        """Most permissive PDF reading attempt."""
        try:
            # Create a copy of the file content
            file_obj.seek(0)
            content = file_obj.read()
            file_copy = BytesIO(content)

            try:
                # Try to read with minimal validation
                reader = pypdf.PdfReader(file_copy)
                reader.strict = False
                return reader
            except Exception as e:
                # Last resort: try to extract any readable content
                if b'obj' in content and b'endobj' in content:
                    # Create minimal PDF structure
                    pdf_content = b'%PDF-1.4\n' + content + b'\nstartxref\n0\n%%EOF'
                    return pypdf.PdfReader(BytesIO(pdf_content), strict=False)
                raise ServiceError("Could not recover PDF structure")
        except ServiceError:
            raise
        except Exception as e:
            self.logger.warning(f"Permissive reading failed: {str(e)}")
            raise e

    def _read_pdf_advanced_recovery(self, file_obj) -> pypdf.PdfReader:
        """Advanced PDF recovery for damaged files."""
        try:
            # Create a copy of the file content
            file_obj.seek(0)
            content = file_obj.read()

            # First try normal reading with error handling
            try:
                file_copy = BytesIO(content)
                return pypdf.PdfReader(file_copy, strict=False)
            except Exception as normal_error:
                self.logger.debug(f"Normal reading failed: {str(normal_error)}")
                pass

            # Try to repair the file
            # Add PDF header if missing
            if not content.startswith(b'%PDF-'):
                content = b'%PDF-1.4\n' + content

            # Add EOF marker if missing
            if not content.endswith(b'%%EOF'):
                content = content + b'\n%%EOF'

            # Check for and fix xref table
            if b'xref' not in content:
                content = content + b'\nxref\n0 1\n0000000000 65535 f\ntrailer\n<<>>\nstartxref\n0\n%%EOF'

            # Try to create reader from repaired content
            repair_copy = BytesIO(content)
            return pypdf.PdfReader(repair_copy, strict=False)

        except Exception as e:
            self.logger.warning(f"Advanced PDF recovery failed: {str(e)}")
            raise ServiceError(f"Advanced PDF recovery failed: {str(e)}")

    def _extract_page_text_with_fallbacks(self, page) -> str:
        """Extract text using multiple fallback methods with enhanced capabilities."""
        all_extracted_text = []
        methods = [
            # Standard extraction
            lambda p: p.extract_text(),

            # Try getting text using textual objects
            lambda p: self._extract_textual_objects(p),

            # Raw extraction
            lambda p: p.get_contents() if hasattr(p, 'get_contents') else b'',

            # Extract from raw page object
            lambda p: self._extract_from_raw_page(p),

            # OCR-like extraction (if available)
            lambda p: self._extract_text_from_images(p)
        ]

        for method in methods:
            try:
                result = method(page)
                if isinstance(result, bytes):
                    result = result.decode('utf-8', errors='ignore')

                if result and result.strip():
                    all_extracted_text.append(result)
            except Exception as e:
                continue

        # If we have multiple extraction results, choose the best one
        if len(all_extracted_text) > 1:
            # Find the extraction with the most alphanumeric characters
            best_text = ""
            max_alphanumeric = 0

            for text in all_extracted_text:
                alphanumeric_count = sum(c.isalnum() for c in text)
                if alphanumeric_count > max_alphanumeric:
                    max_alphanumeric = alphanumeric_count
                    best_text = text

            if best_text:
                return best_text

        # Fall back to first non-empty result or empty string
        return next((text for text in all_extracted_text if text), '')

    def _extract_textual_objects(self, page) -> str:
        """Extract text from PDF page text objects."""
        try:
            if not hasattr(page, '/Contents'):
                return ""

            raw_content = ""
            if hasattr(page, 'get_contents'):
                raw_content = page.get_contents()
            elif hasattr(page, '_contents'):
                raw_content = page._contents

            if not raw_content:
                return ""

            # Convert to string if needed
            if isinstance(raw_content, bytes):
                raw_content = raw_content.decode('utf-8', errors='ignore')

            # Extract text between BT and ET markers (text objects)
            text_objects = []
            start = 0
            while True:
                bt_pos = raw_content.find('BT', start)
                if bt_pos == -1:
                    break

                et_pos = raw_content.find('ET', bt_pos + 2)
                if et_pos == -1:
                    break

                text_objects.append(raw_content[bt_pos:et_pos+2])
                start = et_pos + 2

            # Extract text from text objects
            extracted_text = []
            for obj in text_objects:
                # Look for text in parentheses
                in_text = False
                text_buffer = []
                current_text = ""

                for i, char in enumerate(obj):
                    if char == '(' and (i == 0 or obj[i-1] != '\\'):
                        in_text = True
                        current_text = ""
                    elif char == ')' and (i == 0 or obj[i-1] != '\\') and in_text:
                        in_text = False
                        text_buffer.append(current_text)
                    elif in_text:
                        current_text += char

                if text_buffer:
                    extracted_text.append(" ".join(text_buffer))

            return "\n".join(extracted_text)
        except Exception as e:
            return ""

    def _clean_pdf_text(self, text: str) -> str:
        """Enhanced cleaning of extracted PDF text."""
        if not text:
            return ''

        # Remove control characters
        text = ''.join(char for char in text if ord(char) >= 32 or char in '\n\t')

        # Fix common PDF extraction issues
        text = re.sub(r'([a-z])([A-Z])', r'\1 \2', text)  # Add space between words
        text = re.sub(r'(\w)([\(\)\[\]\{\}])', r'\1 \2', text)  # Add space before brackets
        text = re.sub(r'([\(\)\[\]\{\}])(\w)', r'\1 \2', text)  # Add space after brackets

        # Fix spacing issues
        text = re.sub(r'\s+', ' ', text)  # Normalize whitespace
        text = re.sub(r'[^\S\n]+', ' ', text)  # Normalize spaces but keep newlines
        text = re.sub(r'\n{3,}', '\n\n', text)  # Limit consecutive newlines

        # Fix hyphenated words at line breaks
        text = re.sub(r'(\w)-\s*\n\s*(\w)', r'\1\2', text)

        # Remove isolated punctuation
        text = re.sub(r'\s+([,.;:])\s+', r'\1 ', text)

        return text.strip()

    def _post_process_pdf_text(self, text: str) -> str:
        """Apply post-processing to improve PDF text quality."""
        if not text:
            return ''

        # First apply standard cleaning
        text = self._clean_pdf_text(text)

        # Enhanced processing
        # Fix unicode normalization issues
        import unicodedata
        text = unicodedata.normalize('NFKC', text)

        # Fix common PDF-specific issues
        # 1. Remove form field placeholders
        text = re.sub(r'\[+\s*\]+', '', text)

        # 2. Fix mis-encoded characters
        text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)

        # 3. Fix duplicated punctuation
        text = re.sub(r'([,.!?;:])\1+', r'\1', text)

        # 4. Remove redundant spaces around newlines
        text = re.sub(r'\s*\n\s*', '\n', text)

        # 5. Try to detect and replace common character encoding issues
        replacements = {
            'â': "'", 'â€™': "'", 'â€œ': '"', 'â€': '"',
            'â€"': '-', 'â€"': '--', 'â€¦': '...',
            'Ã©': 'é', 'Ã¨': 'è', 'Ã': 'à',
            'Â': '', 'Â°': '°'
        }

        for wrong, right in replacements.items():
            text = text.replace(wrong, right)

        return text.strip()

    def _is_meaningful_content(self, text: str) -> bool:
        """Check if text contains meaningful content."""
        if not text or len(text.strip()) < 10:
            return False

        # Check if the text contains enough alphanumeric characters
        alphanumeric_ratio = sum(c.isalnum() for c in text) / max(1, len(text))
        if alphanumeric_ratio < 0.1:  # Less than 10% alphanumeric characters
            return False

        # Check for common placeholder patterns
        placeholders = ['[Empty]', '[blank]', '[N/A]', '[?]', '[...]']
        if any(p in text for p in placeholders):
            return False

        return True

    def _contains_binary_data(self, text: str) -> bool:
        """Check if text appears to contain binary data."""
        if not text:
            return False

        # Check for high concentration of unusual characters
        unusual_char_count = sum(1 for c in text if ord(c) > 127 or ord(c) < 32)
        unusual_ratio = unusual_char_count / max(1, len(text))

        # Binary data typically has many unusual characters - use more lenient threshold for PDFs
        if unusual_ratio > 0.4:  # Increased from 0.3 to 0.4 to be more lenient
            return True

        # Check for PDF binary stream markers
        binary_markers = ['stream', 'endstream', 'obj', 'endobj', '/Filter', '/Length']
        marker_count = sum(text.count(marker) for marker in binary_markers)

        # PDF binary content often contains these markers - increased threshold
        if marker_count > 10 and unusual_ratio > 0.2:  # Increased from 5 to 10 and 0.1 to 0.2
            return True

        # Look for long sequences of random-looking characters
        random_sequences = re.findall(r'[A-Za-z0-9+/=]{20,}', text)
        if random_sequences and sum(len(seq) for seq in random_sequences) / len(text) > 0.3:  # Increased from 0.2 to 0.3
            return True

        return False

    def _is_readable_text(self, text: str) -> bool:
        """Check if text appears to be human-readable."""
        if not text or len(text) < 30:  # Reduced from 50 to 30 characters minimum
            return False

        # Check for reasonable distribution of characters
        char_counts = {}
        for char in text:
            char_counts[char] = char_counts.get(char, 0) + 1

        # Human text typically uses a variety of characters - reduced minimum
        if len(char_counts) < 15:  # Reduced from 20 to 15
            return False

        # Check for reasonable word formation
        words = re.findall(r'\b[a-zA-Z]{1,20}\b', text)

        # Human text has a reasonable number of words - reduced minimum
        if len(words) < 8:  # Reduced from 10 to 8
            return False

        # Check distribution of word lengths
        word_lengths = [len(word) for word in words]
        if word_lengths:
            avg_word_length = sum(word_lengths) / len(word_lengths)
            # Extremely long average word length suggests non-text content
            if avg_word_length > 15:
                return False

        # Check for reasonable whitespace - modified ratio
        whitespace_ratio = text.count(' ') / max(1, len(text))
        if whitespace_ratio < 0.03 or whitespace_ratio > 0.6:  # Reduced from 0.05 to 0.03, increased from 0.5 to 0.6
            return False

        # If we have a reasonable number of words or paragraphs, consider it readable
        # even if it doesn't strictly meet our other criteria (helps with image-based PDFs)
        if len(words) > 30 or len(text.split('\n\n')) > 5:
            return True

        return True  # More permissive - assume text is readable unless proven otherwise

    def _aggressive_binary_cleanup(self, text: str) -> str:
        """Aggressively clean binary-looking content to extract readable text."""
        if not text:
            return ""

        # First pass: remove all non-printable characters
        text = re.sub(r'[^\x20-\x7E\n\t]', '', text)

        # Second pass: remove PDF binary markers and their content
        binary_sections = [
            (r'stream.*?endstream', ''),
            (r'obj.*?endobj', ''),
            (r'/Filter.*?/Length \d+', ''),
            (r'<<.*?>>', ''),
            (r'[0-9]+ [0-9]+ obj.*?endobj', ''),
            (r'xref.*?trailer', ''),
            (r'startxref.*?%%EOF', '')
        ]

        for pattern, replacement in binary_sections:
            text = re.sub(pattern, replacement, text, flags=re.DOTALL)

        # Third pass: clean up remaining issues
        text = re.sub(r'\s+', ' ', text)  # Normalize whitespace
        text = re.sub(r'(\w)\1{5,}', r'\1\1\1', text)  # Remove character repetitions
        text = re.sub(r'[^a-zA-Z0-9\s,.;:!?"\'()-]', '', text)  # Remove remaining non-text chars

        # Final pass: segment into paragraphs
        lines = text.split('\n')
        paragraphs = []
        current_paragraph = []

        for line in lines:
            if not line.strip():
                if current_paragraph:
                    paragraphs.append(' '.join(current_paragraph))
                    current_paragraph = []
            else:
                current_paragraph.append(line.strip())

        if current_paragraph:
            paragraphs.append(' '.join(current_paragraph))

        return '\n\n'.join(paragraphs)

    def _extract_raw_pdf_text(self, content: bytes) -> str:
        """Extract any text-like content from raw PDF bytes with enhanced capabilities."""
        try:
            # Convert to string, ignoring errors
            text = content.decode('utf-8', errors='ignore')

            # Find text between PDF markers
            text_markers = [
                (b'(', b')'),  # Text in parentheses
                (b'<', b'>'),  # Hex encoded text
                (b'BT', b'ET')  # Text blocks
            ]

            extracted = []
            for start_marker, end_marker in text_markers:
                pos = 0
                while True:
                    start = content.find(start_marker, pos)
                    if start == -1:
                        break
                    end = content.find(end_marker, start + len(start_marker))
                    if end == -1:
                        break

                    text_chunk = content[start+len(start_marker):end]
                    try:
                        # Try to decode as text
                        decoded = text_chunk.decode('utf-8', errors='ignore')
                        if decoded.strip():
                            extracted.append(decoded)
                    except Exception:
                        # Silently skip non-decodable chunks
                        pass

                    pos = end + len(end_marker)

            # Clean up extracted text
            if extracted:
                text = '\n'.join(extracted)
                return self._clean_pdf_text(text)

            return ''

        except Exception as e:
            self.logger.warning(f"Failed to extract raw text: {str(e)}")
            return ''
