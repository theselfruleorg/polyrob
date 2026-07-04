/* Centralized file-tree loader for WebView
 * 
 * This module provides a single source of truth for loading the workspace file tree,
 * avoiding duplicate API calls and race conditions between different components.
 */
import { fetchJSON, on, emit, logger, sessionState } from '/static/js/ui-utils.js?v=2';

// For tracking in-flight requests
let _currentTreePromise = null;
let _treeData = null;
let _lastTreeLoadTime = 0;
const TREE_CACHE_TTL = 30000; // 30 seconds TTL for cache

/**
 * Load the file tree for the current session
 * @param {boolean} forceRefresh - Whether to force a refresh even if cached data exists
 * @returns {Promise<Object>} - A promise that resolves to the file tree data
 */
async function loadFileTree(forceRefresh = false) {
    const sessionId = sessionState.sessionId;
    
    logger.debug(`[FileLoader] Request to load file tree, force=${forceRefresh}`);

    // Don't try to load file tree for new sessions (empty state)
    if (!sessionId || sessionState.isNew) {
        logger.debug('[FileLoader] Skipping file tree load - no active session yet');
        return { status: 'ok', files: [] };
    }

    // Check if we have a cached response that's still fresh
    const now = Date.now();
    if (!forceRefresh && _treeData && now - _lastTreeLoadTime < TREE_CACHE_TTL) {
        logger.debug('[file-loader] Using cached file tree data');
        return _treeData;
    }

    // If we already have a request in flight, return that promise
    if (_currentTreePromise && !forceRefresh) {
        logger.debug('[file-loader] Request already in flight, reusing promise');
        return _currentTreePromise;
    }

    // Start a new request
    logger.debug(`[file-loader] Starting new file tree request for session: ${sessionId}`);
    
    try {
        // Create a new promise for this request
        _currentTreePromise = (async () => {
            // Emit event to notify that tree loading has started
            emit('filetree:loading');
            
            // Fetch file tree data with cache-busting parameter if forcing refresh
            const url = `/api/session/${sessionId}/workspace/tree${forceRefresh ? '?_=' + now : ''}`;
            logger.debug(`[file-loader] Fetching from: ${url}`);
            
            try {
                // Use the utility function to fetch data
                const data = await fetchJSON(url);
                logger.debug('[file-loader] Tree data received');
                
                // Cache the response
                _treeData = data;
                _lastTreeLoadTime = now;
                
                // Emit event to notify that tree is loaded
                emit('filetree:loaded', { data });
                
                return data;
            } catch (error) {
                logger.error('[file-loader] Error loading file tree:', error);
                
                // Emit event to notify of error
                emit('filetree:error', { error });
                
                // Rethrow to propagate the error
                throw error;
            } finally {
                // Clear the current promise
                _currentTreePromise = null;
            }
        })();
        
        // Return the promise
        return _currentTreePromise;
    } catch (error) {
        logger.error('[file-loader] Error initiating file tree load:', error);
        _currentTreePromise = null;
        throw error;
    }
}

/**
 * Get the cached file tree data if available
 * @returns {Object|null} - The cached file tree data or null if not available
 */
function getCachedFileTree() {
    const now = Date.now();
    if (_treeData && now - _lastTreeLoadTime < TREE_CACHE_TTL) {
        return _treeData;
    }
    return null;
}

/**
 * Load a file from the workspace
 * @param {string} path - Path to the file
 * @returns {Promise<Object>} - A promise that resolves to the file content response
 */
async function loadFile(path) {
    const sessionId = sessionState.sessionId;
    
    if (!path) {
        throw new Error('Path is required');
    }
    
    logger.debug(`[FileLoader] Loading file: ${path}`);
    
    try {
        // Emit event to notify that file loading has started
        emit('file:loading', { path });
        
        // Fetch file content
        const url = `/api/session/${sessionId}/workspace/file?path=${encodeURIComponent(path)}`;
        logger.debug(`[FileLoader] Fetching file from: ${url}`);
        
        // For file content, we need the raw response to check content type
        const response = await fetch(url);
        
        if (!response.ok) {
            throw new Error(`Failed to load file: ${response.status} ${response.statusText}`);
        }
        
        // Emit event to notify that file is loaded
        emit('file:loaded', { path, response });
        
        return response;
    } catch (error) {
        logger.error('[file-loader] Error loading file:', error);
        
        // Emit event to notify of error
        emit('file:error', { path, error });
        
        // Rethrow to propagate the error
        throw error;
    }
}

// Listen for tab activation to preload data
document.addEventListener('DOMContentLoaded', function() {
    // Listen for tab activation events
    on('tab:activated', (event) => {
        const tabId = event.detail.tabId;
        
        // If the workspace tab is activated, load the file tree
        if (tabId === 'workspace-tab') {
            logger.debug('[file-loader] Workspace tab activated, preloading file tree');
            loadFileTree();
        }
    });
});

// Export functions for use in other modules
export { loadFileTree, loadFile, getCachedFileTree }; 