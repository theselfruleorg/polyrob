/**
 * EventFilter - Simple Event Filtering for Display
 *
 * Determines which events should be shown in different UI contexts.
 * NOTE: Deduplication is handled by EventStore, this module only handles
 * display filtering logic.
 *
 * Usage:
 *   import { feedFilter, chatFilter } from './event-filter.js';
 *
 *   if (feedFilter.shouldShow(event)) {
 *       renderEvent(event);
 *   }
 */

const DEBUG = localStorage.getItem('debug') === 'true';

function log(...args) {
    if (DEBUG) console.log('[EventFilter]', ...args);
}

/**
 * EventFilter - Determines which events to show in UI
 */
export class EventFilter {
    constructor(config = {}) {
        this.config = {
            // Event types to completely skip
            skipTypes: config.skipTypes || [],
            // Event types to allow (if set, only these are shown)
            allowTypes: config.allowTypes || null,
            // Filter technical/internal events
            filterTechnical: config.filterTechnical ?? true,
            // Filter empty events
            filterEmpty: config.filterEmpty ?? true,
            ...config
        };
    }

    /**
     * Check if event should be shown.
     * @param {Object} event - Event to check
     * @returns {boolean} true if event should be shown
     */
    shouldShow(event) {
        if (!event || !event.type) {
            return false; // Invalid events never shown
        }

        // Check skip list first
        if (this.config.skipTypes.includes(event.type)) {
            return false;
        }

        // If allowTypes is set, only show those types
        if (this.config.allowTypes && !this.config.allowTypes.includes(event.type)) {
            return false;
        }

        // Filter technical/internal events
        if (this.config.filterTechnical) {
            const technicalTypes = [
                'controller_registered_functions',
                'registered_functions',
                'streaming_output',
                'screenshot_saved',
                'agent_end',
                'agent_started',
                'session_completion',
                'available_actions',
                'service_actions'
            ];
            if (technicalTypes.includes(event.type)) {
                return false;
            }
        }

        // Filter empty generic events
        if (this.config.filterEmpty && event.type === 'event') {
            if (!event.data || Object.keys(event.data).length === 0) {
                return false;
            }
        }

        return true;
    }
}

/**
 * Feed filter - shows ALL telemetry (developer/debug view)
 *
 * Only filters available_actions and truly technical events.
 */
export const feedFilter = new EventFilter({
    skipTypes: ['available_actions'],
    filterTechnical: false, // Show most events
    filterEmpty: true
});

/**
 * Chat filter - shows user-facing events only
 *
 * Chat is a conversational interface showing:
 * - User messages
 * - Agent thinking (step events)
 * - Agent responses
 * - Session status changes
 */
export const chatFilter = new EventFilter({
    skipTypes: [
        // Technical events not needed in chat
        'service_actions',
        'registered_functions',
        'controller_registered_functions',
        'screenshot_saved',
        'available_actions',
        'session_start',
        'session_completion',
        'agent_registration',
        'task_update',
        'user_message_during_execution'
    ],
    filterTechnical: false, // We handle this via skipTypes
    filterEmpty: true
});

/**
 * Create a custom filter for specific use cases.
 * @param {Object} config - Filter configuration
 * @returns {EventFilter}
 */
export function createFilter(config) {
    return new EventFilter(config);
}

// Export for compatibility
export default EventFilter;
