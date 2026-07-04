/* Workspace view: file tree and preview */
import { fetchJSON, escapeHtml, on, emit, logger, sessionState } from '/static/js/ui-utils.js?v=2';
import { loadFileTree as loadTree, loadFile } from '/static/js/file-loader.js?v=2';

/**
 * Format content with URL detection and turn URLs into clickable links
 */
function formatContentWithUrls(content) {
    if (typeof content !== 'string') {
        return escapeHtml(String(content));
    }
    
    logger.debug('🔍 formatContentWithUrls called, content length:', content.length);
    
    // First escape the HTML to prevent XSS
    const escapedContent = escapeHtml(content);
    
    // Enhanced regex to find URLs in text - more comprehensive pattern
    const urlRegex = /https?:\/\/[^\s"<>\[\](){}]+/g;
    
    // Find URLs for debugging
    const urlMatches = escapedContent.match(urlRegex);
    logger.debug('🔗 Found URLs:', urlMatches);
    
    // Replace URLs with clickable links
    const result = escapedContent.replace(urlRegex, (url) => {
        // Clean up the URL by removing trailing punctuation that shouldn't be part of the link
        const cleanUrl = url.replace(/[.,;:!?]+$/, '');
        logger.debug('✨ Converting URL to link:', cleanUrl);
        return `<a href="${cleanUrl}" target="_blank" rel="noopener noreferrer" class="content-url" title="${cleanUrl}" onclick="logger.debug('🖱️ Link clicked: ${cleanUrl}'); return true;">${cleanUrl}</a>`;
    });
    
    logger.debug('📝 formatContentWithUrls result contains links:', result.includes('<a href'));
    return result;
}

/**
 * Check if a file path is a presentation file (reveal.js HTML in presentations folder)
 */
function isPresentationFile(path) {
    return path && path.match(/presentations\/[^\/]+\/index\.html$/i);
}

document.addEventListener('DOMContentLoaded', function() {
    // Initialize workspace tab functionality
    initWorkspace();

    // Initialize preview expand functionality
    initPreviewExpand();

    // Listen for file tree events from the centralized loader
    on('filetree:loading', () => {
        const treeLoading = document.getElementById('file-tree-loading');
        if (treeLoading) treeLoading.style.display = 'flex';
    });

    on('filetree:loaded', (event) => {
        const treeLoading = document.getElementById('file-tree-loading');
        if (treeLoading) treeLoading.style.display = 'none';

        // Render the file tree with the received data
        renderFileTree(event.detail.data);
    });

    on('filetree:error', (event) => {
        const treeLoading = document.getElementById('file-tree-loading');
        const treeError = document.getElementById('file-tree-error');

        if (treeLoading) treeLoading.style.display = 'none';
        if (treeError) {
            treeError.textContent = event.detail.error.message;
            treeError.style.display = 'block';
        }
    });
});

// Track current file path for download
let currentFilePath = null;

async function initWorkspace() {
    // Load the file tree when workspace tab is activated
    const workspaceTab = document.querySelector('.tab-button[data-tab="workspace"]');
    if (workspaceTab) {
        workspaceTab.addEventListener('click', function() {
            logger.debug('Workspace tab clicked, loading file tree...');
            loadFileTree();
        });
    }

    // Add refresh button handler
    const refreshButton = document.getElementById('refresh-tree-btn');
    if (refreshButton) {
        refreshButton.addEventListener('click', function() {
            logger.debug('Refresh button clicked, reloading file tree...');
            loadFileTree(true); // Force refresh
        });
    }

    // Add download button handler
    const downloadBtn = document.getElementById('download-file-btn');
    if (downloadBtn) {
        downloadBtn.addEventListener('click', function() {
            if (currentFilePath) {
                logger.debug('Download button clicked for:', currentFilePath);
                downloadFile(currentFilePath);
            }
        });
    }

    // NOTE: Upload functionality moved to file-attachments.js (chat integration)
    // Files are now attached to messages in chat, not uploaded directly in workspace

    // Automatically load file tree on initial load if workspace tab is active
    if (document.querySelector('.tab-button[data-tab="workspace"].active')) {
        logger.debug('Workspace tab is active on load, loading file tree...');
        loadFileTree();
    }
}

// NOTE: Upload functionality has been moved to file-attachments.js
// Files are now attached to chat messages, not uploaded directly in workspace
// This keeps workspace focused on file tree display and preview

async function loadFileTree(forceRefresh = false) {
    try {
        // Use the centralized file tree loader
        await loadTree(forceRefresh);
    } catch (error) {
        logger.error('Error in workspace loadFileTree:', error);
    }
}

function renderFileTree(data) {
    const treeContent = document.getElementById('file-tree-content');
    
    if (!treeContent) {
        logger.error('Tree content element not found');
        return;
    }
    
    try {
        logger.debug('Rendering file tree data');
        
        // Check if data is empty or has no children
        if (!data || (data.type === 'dir' && (!data.children || data.children.length === 0))) {
            logger.warn('Empty directory structure received');
            treeContent.innerHTML = '<div class="empty-state">No files found in workspace</div>';
            treeContent.classList.add('loaded');
            return;
        }
        
        // Build the tree DOM. If the root directory is named "workspace",
        // render its children directly so the extra root folder is not shown
        let treeHtml = '';
        if (data.type === 'dir' && data.name === 'workspace') {
            treeHtml = (data.children || []).map(child => buildFileTree(child, '')).join('');
        } else {
            treeHtml = buildFileTree(data, '');
        }
        
        // Update the content
        treeContent.innerHTML = treeHtml || '<div class="empty-state">No files found</div>';
        treeContent.classList.add('loaded');
        
        // Add event listeners to directory toggles
        addTreeEventListeners();

        // Adjust file tree height based on content (mobile only)
        if (window.innerWidth <= 768) {
            adjustFileTreeHeight();
        }

        logger.debug('File tree rendered successfully');
    } catch (error) {
        logger.error('Error rendering file tree:', error);
        treeContent.innerHTML = '<div class="error-message">Error rendering file tree</div>';
    }
}

/**
 * Dynamically adjust file tree height based on content (mobile only)
 * Uses ResizeObserver to watch for content changes
 */
function adjustFileTreeHeight() {
    const treeContent = document.getElementById('file-tree-content');
    const fileTree = document.querySelector('.file-tree');

    if (!treeContent || !fileTree) {
        logger.warn('File tree elements not found for height adjustment');
        return;
    }

    // Disconnect any existing observer
    if (window.fileTreeResizeObserver) {
        window.fileTreeResizeObserver.disconnect();
    }

    // Create new ResizeObserver
    window.fileTreeResizeObserver = new ResizeObserver(() => {
        const contentHeight = treeContent.scrollHeight;
        const maxHeight = window.innerHeight * 0.5; // 50vh
        const minHeight = 150;

        const targetHeight = Math.max(minHeight, Math.min(contentHeight + 40, maxHeight));
        fileTree.style.height = `${targetHeight}px`;

        logger.debug(`File tree height adjusted to ${targetHeight}px (content: ${contentHeight}px)`);
    });

    window.fileTreeResizeObserver.observe(treeContent);

    // Initial adjustment
    const contentHeight = treeContent.scrollHeight;
    const maxHeight = window.innerHeight * 0.5;
    const minHeight = 150;
    const targetHeight = Math.max(minHeight, Math.min(contentHeight + 40, maxHeight));
    fileTree.style.height = `${targetHeight}px`;
}

function buildFileTree(node, parentPath = '') {
    if (!node) {
        logger.warn('Null node passed to buildFileTree');
        return '';
    }
    
    logger.debug('Building node:', node.type, node.name);
    
    // Build the full path for this node
    const nodePath = parentPath ? `${parentPath}/${node.name}` : node.name;
    
    if (node.type === 'file') {
        const icon = getFileIcon(node.name);
        const ext = node.name.split('.').pop().toLowerCase();
        return `<div class="file-item" data-path="${nodePath}" data-ext="${ext}">
            <span class="file-item-icon">${icon}</span>
            <span class="file-item-name">${node.name}</span>
        </div>`;
    } else if (node.type === 'dir') {
        const children = node.children || [];
        logger.debug(`Directory ${node.name} has ${children.length} children`);
        
        const childrenHtml = children.map(child => buildFileTree(child, nodePath)).join('');
        const isEmpty = children.length === 0;
        
        return `<div class="dir-item">
            <div class="file-item dir-header">
                <span class="dir-toggle ${isEmpty ? 'empty' : ''}">${isEmpty ? '•' : '▶'}</span>
                <span class="file-item-icon">📁</span>
                <span class="file-item-name">${node.name}</span>
                ${isEmpty ? '<span class="empty-dir-label">(empty)</span>' : ''}
            </div>
            <div class="dir-contents">
                ${childrenHtml}
            </div>
        </div>`;
    }
    
    logger.warn('Unknown node type:', node.type);
    return '';
}

function getFileIcon(filename) {
    const ext = filename.split('.').pop().toLowerCase();
    
    const iconMap = {
        // Code files
        'js': '📄',
        'py': '📄', 
        'html': '📄',
        'css': '📄',
        'json': '📄',
        'md': '📄',
        
        // Data files
        'csv': '📊',
        'xlsx': '📊',
        
        // Image files
        'png': '🖼️',
        'jpg': '🖼️',
        'jpeg': '🖼️',
        'gif': '🖼️',
        'svg': '🖼️',
        
        // Document files
        'pdf': '📑',
        'doc': '📝',
        'docx': '📝',
        'txt': '📝',
        
        // Archive files
        'zip': '📦',
        'tar': '📦',
        'gz': '📦',
    };
    
    // Return only the icon without appending file extension text
    return iconMap[ext] || '📄';
}

function addTreeEventListeners() {
    logger.debug('Adding tree event listeners');
    
    // Add event listeners to directory toggles
    document.querySelectorAll('.dir-toggle:not(.empty)').forEach(toggle => {
        toggle.addEventListener('click', function(e) {
            e.stopPropagation();
            
            const dirItem = this.closest('.dir-item');
            const contents = dirItem.querySelector('.dir-contents');
            
            this.classList.toggle('open');
            contents.classList.toggle('open');
            
            // Change the toggle icon
            this.textContent = this.classList.contains('open') ? '▼' : '▶';
            
            logger.debug(`Toggled directory: ${dirItem.querySelector('.file-item-name').textContent}`);
        });
    });
    
    // Add event listeners to directory headers (to toggle as well)
    document.querySelectorAll('.dir-header').forEach(header => {
        header.addEventListener('click', function(e) {
            // Don't trigger if they clicked directly on the toggle
            if (e.target.classList.contains('dir-toggle')) return;
            
            const toggle = this.querySelector('.dir-toggle:not(.empty)');
            if (toggle) {
                toggle.click();
            }
        });
    });
    
    // Add event listeners to file items
    document.querySelectorAll('.file-item[data-path]').forEach(item => {
        item.addEventListener('click', function() {
            const path = this.getAttribute('data-path');
            logger.debug(`File clicked: ${path}`);
            loadFilePreview(path);

            // Mark this file as active
            document.querySelectorAll('.file-item.active').forEach(el => {
                el.classList.remove('active');
            });
            this.classList.add('active');
        });
    });

    logger.debug('Tree event listeners added');
}

/**
 * Download a file from the workspace
 * @param {string} path - Path to the file
 */
async function downloadFile(path) {
    const sessionId = sessionState.sessionId;

    if (!path || !sessionId) {
        logger.error('Cannot download: path or sessionId missing');
        return;
    }

    try {
        logger.debug(`Downloading file: ${path}`);

        // Build download URL
        const url = `/api/session/${sessionId}/workspace/file?path=${encodeURIComponent(path)}`;

        // Fetch file as blob
        const response = await fetch(url);

        if (!response.ok) {
            throw new Error(`Failed to download: ${response.status} ${response.statusText}`);
        }

        // Get blob and create download link
        const blob = await response.blob();
        const blobUrl = URL.createObjectURL(blob);

        // Extract filename from path
        const filename = path.split('/').pop() || 'download';

        // Create temporary link and trigger download
        const link = document.createElement('a');
        link.href = blobUrl;
        link.download = filename;
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);

        // Clean up blob URL
        setTimeout(() => URL.revokeObjectURL(blobUrl), 100);

        logger.debug(`Download initiated: ${filename}`);
    } catch (error) {
        logger.error('Error downloading file:', error);
        // Could show a toast notification here
    }
}

async function loadFilePreview(path) {
    const previewContent = document.getElementById('file-preview-content');
    const currentFileName = document.getElementById('current-file-name');
    const previewError = document.getElementById('preview-error');
    const expandBtn = document.getElementById('expand-preview-btn');
    const downloadBtn = document.getElementById('download-file-btn');

    if (!previewContent || !path) {
        logger.error('Preview content element not found or path is empty');
        return;
    }

    // Store current path for download
    currentFilePath = path;

    try {
        logger.debug(`Loading file preview for: ${path}`);

        // Hide any previous error
        if (previewError) previewError.style.display = 'none';

        // Hide buttons while loading
        if (expandBtn) expandBtn.style.display = 'none';
        if (downloadBtn) downloadBtn.style.display = 'none';

        // Show loading indicator
        previewContent.innerHTML = '<div class="loading-indicator"><div class="loading-spinner"></div><div>Loading file...</div></div>';

        // Update the file name display (will be enhanced with toggle buttons if needed)
        if (currentFileName) {
            currentFileName.textContent = path;
            // Clear any previous toggle buttons
            currentFileName.className = 'current-file-name';
        }
        
        // Use the centralized file loader
        const response = await loadFile(path);
        
        if (!response.ok) {
            throw new Error(`Failed to load file: ${response.status} ${response.statusText}`);
        }
        
        // Check content type to determine how to display
        const contentType = response.headers.get('Content-Type') || '';
        logger.debug(`File content type: ${contentType}`);
        
        if (contentType.startsWith('image/')) {
            // Handle image files
            const blobUrl = URL.createObjectURL(await response.blob());
            previewContent.innerHTML = `<div class="image-preview"><img src="${blobUrl}" alt="${path}"></div>`;
        } else if (contentType.includes('application/pdf')) {
            // Handle PDF files
            const blobUrl = URL.createObjectURL(await response.blob());
            previewContent.innerHTML = `<div class="pdf-preview"><iframe src="${blobUrl}" width="100%" height="600px"></iframe></div>`;
        } else if (contentType.includes('text/html')) {
            // Handle HTML files - render in sandboxed iframe with source toggle

            // Check file size to prevent hanging
            const contentLength = response.headers.get('Content-Length');
            const fileSizeKB = contentLength ? Math.round(parseInt(contentLength) / 1024) : 0;

            // Show warning for large files (>500KB)
            if (fileSizeKB > 500) {
                previewContent.innerHTML = `
                    <div class="loading-indicator">
                        <div class="loading-spinner"></div>
                        <div>Loading large HTML file (${fileSizeKB}KB)...</div>
                        <div style="font-size: 11px; margin-top: 8px; opacity: 0.7;">This may take a moment</div>
                    </div>
                `;
                // Give browser time to show the loading indicator
                await new Promise(resolve => setTimeout(resolve, 100));
            }

            const htmlContent = await response.text();

            // Add toggle button to header (and Present button for presentations)
            const isPresentation = isPresentationFile(path);
            if (currentFileName) {
                currentFileName.innerHTML = `
                    <span class="file-name-text">${path}</span>
                    <button class="header-toggle-btn" data-view="rendered">
                        <span class="view-rendered active">Rendered</span>
                        <span class="view-divider">|</span>
                        <span class="view-source">Source</span>
                    </button>
                    ${isPresentation ? '<button class="present-btn" title="Present fullscreen">Present</button>' : ''}
                `;
            }

            // Use /serve/ endpoint for HTML to enable relative paths (presentations, multi-file HTML)
            // This serves files directly instead of embedding via srcdoc
            // Don't encode the path - keep directory structure for relative path resolution
            const serveUrl = `/api/session/${sessionState.sessionId}/workspace/serve/${path}`;

            // Show rendering indicator
            previewContent.innerHTML = `
                <div class="loading-indicator">
                    <div class="loading-spinner"></div>
                    <div>Rendering HTML...</div>
                </div>
            `;

            // Use requestAnimationFrame to prevent blocking
            await new Promise(resolve => requestAnimationFrame(resolve));

            previewContent.innerHTML = `
                <div class="html-preview-container" style="display: block;">
                    <iframe
                        src="${serveUrl}"
                        class="html-iframe"
                        sandbox="allow-scripts allow-same-origin allow-forms allow-popups"
                        style="display: block;">
                    </iframe>
                </div>
                <div class="html-source-container" style="display: none;">
                    <pre class="file-content code-html"><code>${escapeHtml(htmlContent)}</code></pre>
                </div>
            `;

            // Add toggle functionality to header button
            const toggleBtn = currentFileName.querySelector('.header-toggle-btn');
            if (toggleBtn) {
                toggleBtn.addEventListener('click', function(e) {
                    e.stopPropagation();
                    const container = previewContent.querySelector('.html-preview-container');
                    const source = previewContent.querySelector('.html-source-container');
                    const renderedSpan = this.querySelector('.view-rendered');
                    const sourceSpan = this.querySelector('.view-source');

                    if (container.style.display === 'none') {
                        container.style.display = 'block';
                        source.style.display = 'none';
                        renderedSpan.classList.add('active');
                        sourceSpan.classList.remove('active');
                    } else {
                        container.style.display = 'none';
                        source.style.display = 'block';
                        renderedSpan.classList.remove('active');
                        sourceSpan.classList.add('active');
                    }
                });
            }

            // Add Present button functionality for presentations
            const presentBtn = currentFileName.querySelector('.present-btn');
            if (presentBtn) {
                presentBtn.addEventListener('click', function(e) {
                    e.stopPropagation();
                    const iframe = previewContent.querySelector('.html-iframe');
                    if (iframe) {
                        // Request fullscreen on the iframe
                        if (iframe.requestFullscreen) {
                            iframe.requestFullscreen();
                        } else if (iframe.webkitRequestFullscreen) {
                            iframe.webkitRequestFullscreen();
                        } else if (iframe.mozRequestFullScreen) {
                            iframe.mozRequestFullScreen();
                        }
                    }
                });
            }
        } else {
            // Handle text files
            const content = await response.text();

            // Determine file type for syntax highlighting
            const fileExt = path.split('.').pop().toLowerCase();
            const languageClass = getLanguageClass(fileExt);

            // Special handling for Markdown files
            if (fileExt === 'md' || contentType.includes('markdown')) {
                const renderedHtml = await renderMarkdown(content);

                // Add toggle button to header
                if (currentFileName) {
                    currentFileName.innerHTML = `
                        <span class="file-name-text">${path}</span>
                        <button class="header-toggle-btn" data-view="rendered">
                            <span class="view-rendered active">Rendered</span>
                            <span class="view-divider">|</span>
                            <span class="view-source">Source</span>
                        </button>
                    `;
                }

                previewContent.innerHTML = `
                    <div class="markdown-rendered" style="display: block;">
                        ${renderedHtml}
                    </div>
                    <div class="markdown-source" style="display: none;">
                        <pre class="file-content code-md"><code>${escapeHtml(content)}</code></pre>
                    </div>
                `;

                // Add toggle functionality to header button
                const toggleBtn = currentFileName.querySelector('.header-toggle-btn');
                if (toggleBtn) {
                    toggleBtn.addEventListener('click', function(e) {
                        e.stopPropagation();
                        const rendered = previewContent.querySelector('.markdown-rendered');
                        const source = previewContent.querySelector('.markdown-source');
                        const renderedSpan = this.querySelector('.view-rendered');
                        const sourceSpan = this.querySelector('.view-source');

                        if (rendered.style.display === 'none') {
                            rendered.style.display = 'block';
                            source.style.display = 'none';
                            renderedSpan.classList.add('active');
                            sourceSpan.classList.remove('active');
                        } else {
                            rendered.style.display = 'none';
                            source.style.display = 'block';
                            renderedSpan.classList.remove('active');
                            sourceSpan.classList.add('active');
                        }
                    });
                }
            }
            // Special handling for CSV files
            else if (fileExt === 'csv' || contentType.includes('csv')) {
                // Check if Papa Parse is available
                if (typeof Papa === 'undefined') {
                    logger.warn('Papa Parse not loaded, showing raw CSV');
                    previewContent.innerHTML = `<pre class="file-content code-csv"><code>${escapeHtml(content)}</code></pre>`;
                } else {
                    // Parse CSV
                    const parsed = Papa.parse(content, {
                        header: false,
                        skipEmptyLines: true
                    });

                    const tableHtml = renderCsvTable(parsed);

                    // Add toggle button to header
                    if (currentFileName) {
                        currentFileName.innerHTML = `
                            <span class="file-name-text">${path}</span>
                            <button class="header-toggle-btn" data-view="table">
                                <span class="view-table active">Table</span>
                                <span class="view-divider">|</span>
                                <span class="view-raw">Raw</span>
                            </button>
                        `;
                    }

                    previewContent.innerHTML = `
                        <div class="csv-table-view" style="display: block;">
                            ${tableHtml}
                        </div>
                        <div class="csv-raw-view" style="display: none;">
                            <pre class="file-content code-csv"><code>${escapeHtml(content)}</code></pre>
                        </div>
                    `;

                    // Add toggle functionality to header button
                    const toggleBtn = currentFileName.querySelector('.header-toggle-btn');
                    if (toggleBtn) {
                        toggleBtn.addEventListener('click', function(e) {
                            e.stopPropagation();
                            const tableView = previewContent.querySelector('.csv-table-view');
                            const rawView = previewContent.querySelector('.csv-raw-view');
                            const tableSpan = this.querySelector('.view-table');
                            const rawSpan = this.querySelector('.view-raw');

                            if (tableView.style.display === 'none') {
                                tableView.style.display = 'block';
                                rawView.style.display = 'none';
                                tableSpan.classList.add('active');
                                rawSpan.classList.remove('active');
                            } else {
                                tableView.style.display = 'none';
                                rawView.style.display = 'block';
                                tableSpan.classList.remove('active');
                                rawSpan.classList.add('active');
                            }
                        });
                    }

                    // Add sorting functionality
                    const sortableHeaders = previewContent.querySelectorAll('.sortable');
                    sortableHeaders.forEach(header => {
                        header.addEventListener('click', function() {
                            const columnIndex = parseInt(this.getAttribute('data-column'));
                            sortCsvTable(previewContent, columnIndex);
                        });
                    });

                    // Add export functionality
                    const exportBtn = previewContent.querySelector('.csv-export-btn');
                    if (exportBtn) {
                        exportBtn.addEventListener('click', function() {
                            const blob = new Blob([content], { type: 'text/csv' });
                            const url = URL.createObjectURL(blob);
                            const a = document.createElement('a');
                            a.href = url;
                            a.download = path.split('/').pop() || 'export.csv';
                            a.click();
                            URL.revokeObjectURL(url);
                        });
                    }
                }
            }
            // Default text file handling
            else if (content.length > 100000) {
                // Check if content is too large (more than 100KB)
                previewContent.innerHTML = `
                    <div class="file-content-warning">
                        <p>⚠️ This file is very large (${(content.length / 1024).toFixed(1)} KB). Showing the first 10,000 characters:</p>
                    </div>
                    <pre class="file-content ${languageClass}"><code>${formatContentWithUrls(content.substring(0, 10000))}
... [file truncated, ${(content.length / 1024).toFixed(1)} KB total] ...</code></pre>`;
            } else {
                // Display the content with clickable URLs
                const processedContent = formatContentWithUrls(content);

                // For text files, use pre to preserve formatting
                if (fileExt === 'txt') {
                    previewContent.innerHTML = `<pre class="file-content file-content-text ${languageClass}"><code>${processedContent}</code></pre>`;
                } else {
                    previewContent.innerHTML = `<pre class="file-content ${languageClass}"><code>${processedContent}</code></pre>`;
                }
            }
        }

        // Show action buttons after file is loaded successfully
        if (downloadBtn) {
            downloadBtn.style.display = 'flex';
        }
        if (expandBtn) {
            expandBtn.style.display = 'flex';
        }

        logger.debug('File preview loaded successfully');
    } catch (error) {
        logger.error('Error loading file preview:', error);
        previewContent.innerHTML = '';

        // Hide buttons on error
        if (downloadBtn) downloadBtn.style.display = 'none';
        if (expandBtn) expandBtn.style.display = 'none';

        // Clear current file path on error
        currentFilePath = null;

        if (previewError) {
            previewError.textContent = `Error loading file: ${error.message}`;
            previewError.style.display = 'block';
        } else {
            previewContent.innerHTML = `<div class="error-message">Error loading file: ${error.message}</div>`;
        }
    }
}

function getLanguageClass(fileExt) {
    const languageMap = {
        // Code files
        'js': 'code-js',
        'py': 'code-py',
        'html': 'code-html',
        'css': 'code-css',
        'json': 'code-json',
        'md': 'code-md',
        'sh': 'code-sh',
        'bash': 'code-bash',

        // Data files
        'csv': 'code-csv',
        'tsv': 'code-csv',

        // Config files
        'yml': 'code-yaml',
        'yaml': 'code-yaml',
        'toml': 'code-toml',
        'ini': 'code-ini',

        // Simple text
        'txt': 'code-txt',
        'log': 'code-log'
    };

    return languageMap[fileExt] || '';
}

// Render Markdown content to HTML
async function renderMarkdown(content) {
    // Check if marked is available
    if (typeof marked === 'undefined') {
        logger.warn('Marked.js not loaded, falling back to plain text');
        return escapeHtml(content);
    }

    // Configure marked with safe defaults
    marked.setOptions({
        breaks: true,
        gfm: true,
        headerIds: true,
        mangle: false,
        sanitize: false // We trust workspace files
    });

    return marked.parse(content);
}

// Render CSV data as an HTML table
function renderCsvTable(parsedData) {
    if (!parsedData || !parsedData.data || parsedData.data.length === 0) {
        return '<div class="empty-state">No data in CSV file</div>';
    }

    const headers = parsedData.data[0];
    const rows = parsedData.data.slice(1);

    let html = `
        <div class="csv-table-container">
            <div class="csv-controls">
                <div class="csv-info">
                    ${rows.length} rows × ${headers.length} columns
                </div>
                <div class="csv-actions">
                    <button class="csv-export-btn" title="Export as CSV">
                        💾 Export
                    </button>
                </div>
            </div>
            <div class="csv-table-wrapper">
                <table class="csv-table">
                    <thead>
                        <tr>
    `;

    // Add headers with sort indicators
    headers.forEach((header, index) => {
        html += `
            <th class="sortable" data-column="${index}">
                ${escapeHtml(header || `Column ${index + 1}`)}
                <span class="sort-indicator">⇅</span>
            </th>
        `;
    });

    html += '</tr></thead><tbody>';

    // Add data rows
    rows.forEach(row => {
        if (!row || row.every(cell => !cell)) return; // Skip empty rows

        html += '<tr>';
        headers.forEach((_, index) => {
            const cell = row[index] || '';
            html += `<td>${escapeHtml(cell)}</td>`;
        });
        html += '</tr>';
    });

    html += '</tbody></table></div></div>';

    return html;
}

// Sort a CSV table by column
function sortCsvTable(container, columnIndex) {
    const table = container.querySelector('.csv-table tbody');
    const rows = Array.from(table.querySelectorAll('tr'));

    // Determine sort direction
    const header = container.querySelector(`th[data-column="${columnIndex}"]`);
    const currentSort = header.getAttribute('data-sort') || 'none';
    const newSort = currentSort === 'asc' ? 'desc' : 'asc';

    // Clear all sort indicators
    container.querySelectorAll('.sortable').forEach(h => {
        h.setAttribute('data-sort', 'none');
        h.querySelector('.sort-indicator').textContent = '⇅';
    });

    // Set new sort
    header.setAttribute('data-sort', newSort);
    header.querySelector('.sort-indicator').textContent = newSort === 'asc' ? '↑' : '↓';

    // Sort rows
    rows.sort((a, b) => {
        const aValue = a.querySelectorAll('td')[columnIndex]?.textContent || '';
        const bValue = b.querySelectorAll('td')[columnIndex]?.textContent || '';

        // Try numeric comparison first
        const aNum = parseFloat(aValue);
        const bNum = parseFloat(bValue);

        if (!isNaN(aNum) && !isNaN(bNum)) {
            return newSort === 'asc' ? aNum - bNum : bNum - aNum;
        }

        // Fall back to string comparison
        return newSort === 'asc'
            ? aValue.localeCompare(bValue)
            : bValue.localeCompare(aValue);
    });

    // Re-append sorted rows
    rows.forEach(row => table.appendChild(row));
}

// Export functions for use in other modules
export { loadFileTree, loadFilePreview }; 
/**
 * Initialize preview expand functionality
 */
function initPreviewExpand() {
    const expandBtn = document.getElementById('expand-preview-btn');
    const closeBtn = document.getElementById('close-modal-btn');
    const modal = document.getElementById('preview-modal');
    const modalContent = document.getElementById('preview-modal-content');
    const modalFileName = document.getElementById('modal-file-name');
    const previewContent = document.getElementById('file-preview-content');
    const currentFileName = document.getElementById('current-file-name');

    if (!expandBtn || !closeBtn || !modal) {
        logger.warn('Preview expand elements not found');
        return;
    }

    // Expand preview to fullscreen
    expandBtn.addEventListener('click', function() {
        // Copy content from regular preview to modal
        if (modalContent && previewContent) {
            modalContent.innerHTML = previewContent.innerHTML;
        }

        // Copy filename - check for .file-name-text span first (for files with toggle buttons)
        if (modalFileName && currentFileName) {
            const fileNameText = currentFileName.querySelector('.file-name-text');
            modalFileName.textContent = fileNameText ? fileNameText.textContent : (currentFileName.textContent || currentFileName.innerText);
        }

        // Show modal
        modal.style.display = 'flex';

        // Prevent body scroll
        document.body.style.overflow = 'hidden';
    });

    // Close modal
    closeBtn.addEventListener('click', closeModal);

    // Close on ESC key
    document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape' && modal.style.display === 'flex') {
            closeModal();
        }
    });

    // Close on background click
    modal.addEventListener('click', function(e) {
        if (e.target === modal) {
            closeModal();
        }
    });

    function closeModal() {
        modal.style.display = 'none';
        document.body.style.overflow = '';
    }
}
