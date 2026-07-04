/**
 * File Attachment Manager
 * Handles file uploads and attachment state for chat messages
 */
import { logger } from '/static/js/ui-utils.js?v=2';

export class FileAttachmentManager {
    constructor() {
        // Store files client-side until session is created
        this.pendingFiles = new Map(); // tempId -> {file: File, name, size, canInject}

        // Limits
        this.MAX_FILE_SIZE = 10 * 1024 * 1024; // 10MB (matches backend)
        this.MAX_FILES_COUNT = 10; // Max files per message
        this.MAX_TOTAL_SIZE = 50 * 1024 * 1024; // 50MB total (prevent context overflow)
        this.INJECT_SIZE_THRESHOLD = 30 * 1024; // 30KB threshold for auto-injection

        // DOM elements
        this.uploadBtn = document.getElementById('upload-btn');
        this.fileInput = document.getElementById('file-upload-input');
        this.container = document.getElementById('attached-files-container');
        this.list = document.getElementById('attached-files-list');
        this.progress = document.getElementById('upload-progress');
        this.progressBar = document.getElementById('upload-progress-bar');
        this.progressText = document.getElementById('upload-progress-text');
        this.badge = this.uploadBtn ? this.uploadBtn.querySelector('.file-count-badge') : null;

        logger.debug('[FileAttachments] DOM elements check:', {
            uploadBtn: !!this.uploadBtn,
            fileInput: !!this.fileInput,
            container: !!this.container,
            list: !!this.list,
            badge: !!this.badge,
            domReady: document.readyState
        });

        // Verify critical elements exist
        if (!this.uploadBtn || !this.fileInput) {
            logger.error('[FileAttachments] CRITICAL: Required DOM elements not found!', {
                uploadBtn: !!this.uploadBtn,
                fileInput: !!this.fileInput,
                uploadBtnId: this.uploadBtn ? this.uploadBtn.id : 'NOT FOUND',
                fileInputId: this.fileInput ? this.fileInput.id : 'NOT FOUND',
                domReady: document.readyState,
                bodyHasSession: !!document.body.dataset.sessionId
            });
            throw new Error('FileAttachmentManager: Required DOM elements not found');
        }

        this.initListeners();
        logger.debug('[FileAttachments] ✓ Initialized successfully (client-side storage)');
    }

    initListeners() {
        logger.debug('[FileAttachments] Attaching event listeners...');

        // Upload button opens file picker
        this.uploadBtn.addEventListener('click', (e) => {
            logger.debug('[FileAttachments] ✓ Upload button clicked!', e);
            e.preventDefault();
            e.stopPropagation();
            logger.debug('[FileAttachments] Opening file picker...');
            this.fileInput.click();
        });

        // Handle file selection
        this.fileInput.addEventListener('change', (event) => {
            const files = Array.from(event.target.files);
            if (files.length === 0) {
                logger.debug('[FileAttachments] No files selected');
                return;
            }

            logger.debug(`[FileAttachments] ✓ Selected ${files.length} file(s)`, files.map(f => f.name));

            for (const file of files) {
                this.addFile(file);
            }

            // Clear input for re-selection
            this.fileInput.value = '';
        });

        logger.debug('[FileAttachments] ✓ Event listeners attached successfully');
    }

    addFile(file) {
        try {
            logger.debug(`[FileAttachments] Adding file: ${file.name}`);

            // Validate file size
            if (file.size > this.MAX_FILE_SIZE) {
                throw new Error(`File too large (${(file.size / 1024 / 1024).toFixed(1)}MB). Max: ${this.MAX_FILE_SIZE / 1024 / 1024}MB`);
            }

            // Check max files count
            if (this.pendingFiles.size >= this.MAX_FILES_COUNT) {
                throw new Error(`Maximum ${this.MAX_FILES_COUNT} files per message. Remove some files first.`);
            }

            // Check total size
            const currentTotal = Array.from(this.pendingFiles.values()).reduce((sum, f) => sum + f.size, 0);
            if (currentTotal + file.size > this.MAX_TOTAL_SIZE) {
                const totalMB = ((currentTotal + file.size) / 1024 / 1024).toFixed(1);
                const maxMB = (this.MAX_TOTAL_SIZE / 1024 / 1024).toFixed(1);
                throw new Error(`Total size ${totalMB}MB exceeds limit of ${maxMB}MB`);
            }

            // Create temporary ID for client-side storage
            const tempId = `temp_${Date.now()}_${file.name}`;

            // Determine if file can be auto-injected based on size
            const canInject = file.size < this.INJECT_SIZE_THRESHOLD;

            // Store File object with metadata
            this.pendingFiles.set(tempId, {
                file: file,
                name: file.name,
                size: file.size,
                canInject: canInject
            });

            logger.debug(`[FileAttachments] File added (client-side): ${file.name}, canInject: ${canInject}`);

            // Update UI
            this.render();
            this.showNotification(`✓ ${file.name} ready to attach`, 'success');

        } catch (error) {
            logger.error('[FileAttachments] Add file error:', error);
            this.showNotification(`✗ ${error.message}`, 'error');
        }
    }

    async uploadAllFiles(sessionId) {
        if (this.pendingFiles.size === 0) {
            logger.debug('[FileAttachments] No files to upload');
            return [];
        }

        logger.debug(`[FileAttachments] Uploading ${this.pendingFiles.size} file(s) to session ${sessionId}`);

        const uploadedFiles = [];
        const filesToUpload = Array.from(this.pendingFiles.values());

        try {
            // Get auth token from localStorage
            const token = localStorage.getItem('auth_token');

            for (let i = 0; i < filesToUpload.length; i++) {
                const fileData = filesToUpload[i];
                const file = fileData.file;

                // Show progress
                this.showProgress(`Uploading ${file.name}... (${i + 1}/${filesToUpload.length})`);

                // Create form data
                const formData = new FormData();
                formData.append('file', file);

                // Prepare headers with authentication
                const headers = {};
                if (token) {
                    headers['Authorization'] = `Bearer ${token}`;
                }

                // CRITICAL: Do NOT set Content-Type header!
                // Browser auto-generates: multipart/form-data; boundary=----WebKitFormBoundary...
                // Manual setting breaks boundary parameter, causing 400 Bad Request
                const response = await fetch(`/api/task/sessions/${sessionId}/workspace/upload`, {
                    method: 'POST',
                    headers: headers,
                    credentials: 'include',
                    body: formData
                });

                if (!response.ok) {
                    const error = await response.json().catch(() => ({ detail: 'Upload failed' }));
                    throw new Error(`Failed to upload ${file.name}: ${error.detail || 'Unknown error'}`);
                }

                const result = await response.json();
                logger.debug(`[FileAttachments] Uploaded ${file.name} -> ${result.path}`);

                uploadedFiles.push(result.path);
            }

            this.hideProgress();
            this.showNotification(`✓ ${uploadedFiles.length} file(s) uploaded`, 'success');

            return uploadedFiles;

        } catch (error) {
            logger.error('[FileAttachments] Upload error:', error);
            this.hideProgress();

            // Provide specific error messages based on HTTP status
            let userMessage = error.message;

            if (error.message.includes('403')) {
                userMessage = '🔒 Authentication failed. Please sign in again.';
                // Optionally redirect to login after a delay
                setTimeout(() => {
                    if (confirm('Session expired. Sign in again?')) {
                        window.location.href = '/signin';
                    }
                }, 2000);
            } else if (error.message.includes('413')) {
                userMessage = `📦 File too large (max ${this.MAX_FILE_SIZE/1024/1024}MB)`;
            } else if (error.message.includes('400')) {
                userMessage = '⚠️ Invalid file format. Check file type and try again.';
            } else if (error.message.includes('404')) {
                userMessage = '❓ Session not found. Try creating a new session.';
            } else if (error.message.includes('500')) {
                userMessage = '💥 Server error. Please try again or contact support.';
            } else if (!navigator.onLine) {
                userMessage = '📡 No internet connection. Check your network and retry.';
            }

            this.showNotification(`✗ ${userMessage}`, 'error');
            throw error; // Re-throw to let caller handle it
        }
    }

    hasPendingFiles() {
        return this.pendingFiles.size > 0;
    }

    removeFile(tempId) {
        logger.debug('[FileAttachments] Removing file:', tempId);
        this.pendingFiles.delete(tempId);
        this.render();
    }

    clear() {
        logger.debug('[FileAttachments] Clearing all pending files');
        this.pendingFiles.clear();
        this.render();
    }

    render() {
        const count = this.pendingFiles.size;

        // Update badge
        if (count > 0) {
            this.badge.textContent = count;
            this.badge.style.display = 'block';
        } else {
            this.badge.style.display = 'none';
        }

        // Update container visibility
        if (count === 0) {
            this.container.style.display = 'none';
            return;
        }

        this.container.style.display = 'block';

        // Render chips
        this.list.innerHTML = '';
        for (const [tempId, fileData] of this.pendingFiles.entries()) {
            const chip = this.createChip(tempId, fileData);
            this.list.appendChild(chip);
        }

        logger.debug(`[FileAttachments] Rendered ${count} file(s)`);
    }

    createChip(tempId, fileData) {
        const chip = document.createElement('div');
        chip.className = 'file-chip';
        if (!fileData.canInject) {
            chip.classList.add('large-file');
        }
        chip.dataset.tempId = tempId;

        const icon = this.getFileIcon(fileData.name);
        const sizeStr = fileData.size < 1024
            ? `${fileData.size}B`
            : fileData.size < 1024 * 1024
            ? `${(fileData.size / 1024).toFixed(1)}KB`
            : `${(fileData.size / 1024 / 1024).toFixed(1)}MB`;

        chip.innerHTML = `
            <span class="file-icon">${icon}</span>
            <span class="file-info">
                <span class="file-name" title="${fileData.name}">${fileData.name}</span>
                <span class="file-size">${sizeStr}</span>
            </span>
            <button class="file-remove" title="Remove">×</button>
        `;

        // Add remove handler
        chip.querySelector('.file-remove').addEventListener('click', () => {
            this.removeFile(tempId);
        });

        return chip;
    }

    getFileIcon(filename) {
        const ext = filename.split('.').pop().toLowerCase();
        const icons = {
            pdf: '📄',
            docx: '📝', doc: '📝',
            txt: '📃', md: '📃',
            csv: '📊', xlsx: '📊', xls: '📊',
            json: '📋', xml: '📋',
            jpg: '🖼️', png: '🖼️', gif: '🖼️',
            py: '🐍', js: '📜', html: '🌐', css: '🎨'
        };
        return icons[ext] || '📎';
    }

    showProgress(text) {
        this.progress.style.display = 'block';
        this.progressText.textContent = text;
        this.progressBar.style.width = '50%';
    }

    hideProgress() {
        setTimeout(() => {
            this.progress.style.display = 'none';
        }, 1000);
    }

    showNotification(message, type = 'info') {
        // Remove any existing notifications
        const existing = document.querySelectorAll('.upload-notification');
        existing.forEach(n => n.remove());

        // Create notification
        const notification = document.createElement('div');
        notification.className = `upload-notification ${type}`;
        notification.textContent = message;

        const bgColor = type === 'success' ? 'rgba(40, 180, 100, 0.2)' : 'rgba(220, 80, 80, 0.2)';
        const borderColor = type === 'success' ? 'var(--accent-green)' : 'var(--accent-red)';
        const textColor = type === 'success' ? 'var(--accent-green)' : 'var(--accent-red)';

        notification.style.cssText = `
            position: fixed;
            top: 80px;
            right: 20px;
            padding: 12px 20px;
            background: ${bgColor};
            border: 1px solid ${borderColor};
            border-left: 3px solid ${borderColor};
            border-radius: 4px;
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.5);
            z-index: 9999;
            font-size: 12px;
            font-family: var(--code-font);
            color: ${textColor};
            max-width: 400px;
            word-wrap: break-word;
            animation: slideInRight 0.3s ease-out;
        `;

        document.body.appendChild(notification);

        // Auto-dismiss
        setTimeout(() => {
            notification.style.transition = 'opacity 0.3s ease, transform 0.3s ease';
            notification.style.opacity = '0';
            notification.style.transform = 'translateX(100px)';
            setTimeout(() => notification.remove(), 300);
        }, type === 'error' ? 5000 : 3000); // Errors stay longer
    }
}

// Export for use in other modules
window.FileAttachmentManager = FileAttachmentManager;
