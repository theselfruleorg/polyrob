/**
 * Admin Dashboard Shared Utilities
 * Common JavaScript functions for all admin pages to avoid duplication
 */

// Use IIFE to avoid polluting global scope, but export to window.AdminUtils
(function() {
    'use strict';

    /**
     * Get authentication token from cookie or localStorage
     * @returns {string|null} Auth token or null if not found
     */
    function getAuthToken() {
        // Try cookie first
        const cookies = document.cookie.split(';');
        for (let cookie of cookies) {
            const [name, value] = cookie.trim().split('=');
            if (name === 'auth_token') {
                return value;
            }
        }
        // Fallback to localStorage
        return localStorage.getItem('auth_token');
    }

    /**
     * Make an authenticated API call
     * @param {string} endpoint - API endpoint (without /api prefix)
     * @param {Object} options - Fetch options
     * @returns {Promise<Object>} Response JSON
     * @throws {Error} If request fails
     */
    async function apiCall(endpoint, options = {}) {
        const token = getAuthToken();
        const headers = {
            'Content-Type': 'application/json',
            ...options.headers
        };
        if (token) {
            headers['Authorization'] = `Bearer ${token}`;
        }

        const response = await fetch(`/api${endpoint}`, {
            ...options,
            headers
        });

        if (!response.ok) {
            const error = await response.json().catch(() => ({ detail: 'Unknown error' }));
            throw new Error(error.detail || `HTTP ${response.status}`);
        }

        return response.json();
    }

    /**
     * Format a number with thousand separators
     * @param {number} num - Number to format
     * @returns {string} Formatted number or '-' if null/undefined
     */
    function formatNumber(num) {
        if (num === null || num === undefined) return '-';
        return num.toLocaleString();
    }

    /**
     * Format currency (USD)
     * @param {number} num - Amount to format
     * @returns {string} Formatted currency string
     */
    function formatCurrency(num) {
        if (num === null || num === undefined) return '$-';
        return '$' + num.toLocaleString(undefined, {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2
        });
    }

    /**
     * Format credits
     * @param {number} num - Credit amount
     * @returns {string} Formatted credits or '-' if null
     */
    function formatCredits(num) {
        if (num === null || num === undefined) return '-';
        return num.toLocaleString();
    }

    /**
     * Format a wallet address (truncated)
     * @param {string} address - Full wallet address
     * @param {number} startChars - Characters to show at start (default 6)
     * @param {number} endChars - Characters to show at end (default 4)
     * @returns {string} Truncated address or '-' if empty
     */
    function formatWallet(address, startChars = 6, endChars = 4) {
        if (!address) return '-';
        return `${address.slice(0, startChars)}...${address.slice(-endChars)}`;
    }

    /**
     * Format date string to locale date
     * @param {string} dateStr - ISO date string
     * @returns {string} Formatted date or '-' if empty
     */
    function formatDate(dateStr) {
        if (!dateStr) return '-';
        const date = new Date(dateStr);
        return date.toLocaleDateString();
    }

    /**
     * Format date string to locale date and time
     * @param {string} dateStr - ISO date string
     * @returns {string} Formatted datetime or '-' if empty
     */
    function formatDateTime(dateStr) {
        if (!dateStr) return '-';
        const date = new Date(dateStr);
        return date.toLocaleString();
    }

    /**
     * Format timestamp to time only (HH:MM)
     * @param {string} timestamp - ISO timestamp
     * @returns {string} Formatted time or '-' if empty
     */
    function formatTime(timestamp) {
        if (!timestamp) return '-';
        const date = new Date(timestamp);
        return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    }

    /**
     * Format timestamp to relative time (e.g., "2 hours ago")
     * @param {string} dateStr - ISO date string
     * @returns {string} Relative time string
     */
    function timeAgo(dateStr) {
        if (!dateStr) return '';
        const date = new Date(dateStr);
        const now = new Date();
        const diffMs = now - date;
        const diffHours = Math.floor(diffMs / (1000 * 60 * 60));
        const diffDays = Math.floor(diffHours / 24);

        if (diffDays > 0) return `(${diffDays} days ago)`;
        if (diffHours > 0) return `(${diffHours} hours ago)`;
        return '(just now)';
    }

    /**
     * Truncate text to specified length
     * @param {string} text - Text to truncate
     * @param {number} maxLength - Maximum length
     * @returns {string} Truncated text with '...' or '-' if empty
     */
    function truncateText(text, maxLength) {
        if (!text) return '-';
        if (text.length <= maxLength) return text;
        return text.substring(0, maxLength) + '...';
    }

    /**
     * Format ID (truncated for display)
     * @param {string} id - ID string
     * @param {number} maxLength - Max length before truncation (default 12)
     * @returns {string} Formatted ID or '-' if empty
     */
    function formatId(id, maxLength = 12) {
        if (!id) return '-';
        if (id.length > maxLength) {
            return id.substring(0, 8) + '...';
        }
        return id;
    }

    /**
     * Show an alert message (can be replaced with better UI later)
     * @param {string} message - Message to show
     */
    function showAlert(message) {
        alert(message);
    }

    /**
     * Show a confirmation dialog
     * @param {string} message - Confirmation message
     * @returns {boolean} True if confirmed
     */
    function showConfirm(message) {
        return confirm(message);
    }

    // Export to global AdminUtils namespace
    window.AdminUtils = {
        getAuthToken,
        apiCall,
        formatNumber,
        formatCurrency,
        formatCredits,
        formatWallet,
        formatDate,
        formatDateTime,
        formatTime,
        timeAgo,
        truncateText,
        formatId,
        showAlert,
        showConfirm
    };

})();
