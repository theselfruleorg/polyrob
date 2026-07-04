/**
 * EventStore - Single Source of Truth for Session Events
 *
 * Centralized event management with:
 * - Guaranteed ordering by sequence number (_seq)
 * - Lossless deduplication by event ID (_id)
 * - Memory-bounded storage with oldest eviction
 * - Subscriber pattern for reactive updates
 * - Derived session state (computed, not stored)
 *
 * Usage:
 *   import { eventStore } from './event-store.js';
 *
 *   eventStore.insert(event);
 *   eventStore.subscribe('all', (change) => { ... });
 *   const state = eventStore.getSessionState();
 */

const DEBUG = localStorage.getItem('debug') === 'true';

function log(...args) {
    if (DEBUG) console.log('[EventStore]', ...args);
}

/**
 * EventStore - Centralized event storage with ordering and deduplication
 */
export class EventStore {
    constructor(maxEvents = 5000) {
        this.maxEvents = maxEvents;
        this._events = new Map();      // _id -> event (for O(1) dedup lookup)
        this._ordered = [];            // Array of events sorted by _seq
        this._subscribers = new Map(); // channel -> Set of callbacks
        this._lastSeq = 0;             // Track highest sequence for delta sync
    }

    /**
     * Insert a single event with deduplication and ordering.
     * @param {Object} event - Event with _seq, _ts_ms, _id fields
     * @returns {boolean} true if inserted, false if duplicate
     */
    insert(event) {
        if (!event) return false;

        // Get or generate event ID for deduplication
        const eventId = event._id || this._generateFallbackId(event);

        // Deduplicate by _id
        if (this._events.has(eventId)) {
            log(`Duplicate event ${eventId}, skipping`);
            return false;
        }

        // Get sequence number for ordering
        const seq = event._seq || this._fallbackSeq(event);

        // Store with ID for dedup lookup
        this._events.set(eventId, event);

        // Insert in order using binary search
        this._insertOrdered(event, seq);

        // Track highest sequence
        if (seq > this._lastSeq) {
            this._lastSeq = seq;
        }

        // Enforce memory limit
        this._enforceLimit();

        // Notify subscribers
        this._notify('all', { action: 'insert', event, seq });
        if (event.type) {
            this._notify(event.type, { action: 'insert', event, seq });
        }

        log(`Inserted event _seq=${seq}, type=${event.type}, total=${this._ordered.length}`);
        return true;
    }

    /**
     * Insert multiple events efficiently (single notification).
     * @param {Array} events - Array of events to insert
     * @returns {Array} Array of successfully inserted events
     */
    insertBatch(events) {
        if (!events || !Array.isArray(events)) return [];

        const inserted = [];
        const byType = new Map();

        for (const event of events) {
            const eventId = event._id || this._generateFallbackId(event);

            if (this._events.has(eventId)) {
                continue; // Skip duplicates
            }

            const seq = event._seq || this._fallbackSeq(event);
            this._events.set(eventId, event);
            this._insertOrdered(event, seq);

            if (seq > this._lastSeq) {
                this._lastSeq = seq;
            }

            inserted.push(event);

            // Track by type for notifications
            if (event.type) {
                if (!byType.has(event.type)) {
                    byType.set(event.type, []);
                }
                byType.get(event.type).push(event);
            }
        }

        // Enforce memory limit once
        this._enforceLimit();

        // Notify once per channel
        if (inserted.length > 0) {
            this._notify('all', { action: 'batch', events: inserted, count: inserted.length });
            for (const [type, typeEvents] of byType) {
                this._notify(type, { action: 'batch', events: typeEvents, count: typeEvents.length });
            }
        }

        log(`Batch inserted ${inserted.length}/${events.length} events`);
        return inserted;
    }

    /**
     * Get all events, optionally filtered by type.
     * @param {Object} options - { type: string }
     * @returns {Array} Events in sequence order
     */
    getAll(options = {}) {
        if (options.type) {
            return this._ordered.filter(e => e.type === options.type);
        }
        return [...this._ordered];
    }

    /**
     * Get events after a specific sequence number (for delta sync).
     * @param {number} seq - Sequence number
     * @returns {Array} Events with _seq > seq
     */
    getAfter(seq) {
        // Binary search for first event with _seq > seq
        let left = 0, right = this._ordered.length;
        while (left < right) {
            const mid = Math.floor((left + right) / 2);
            const eventSeq = this._ordered[mid]._seq || 0;
            if (eventSeq <= seq) {
                left = mid + 1;
            } else {
                right = mid;
            }
        }
        return this._ordered.slice(left);
    }

    /**
     * Get the last sequence number (for delta sync).
     * @returns {number} Highest sequence number
     */
    getLastSeq() {
        return this._lastSeq;
    }

    /**
     * Get event by ID.
     * @param {string} id - Event ID (_id)
     * @returns {Object|undefined} Event or undefined
     */
    getById(id) {
        return this._events.get(id);
    }

    /**
     * Subscribe to event changes.
     * @param {string} channel - 'all' or event type (e.g., 'step', 'status')
     * @param {Function} callback - (change) => void
     * @returns {Function} Unsubscribe function
     */
    subscribe(channel, callback) {
        if (!this._subscribers.has(channel)) {
            this._subscribers.set(channel, new Set());
        }
        this._subscribers.get(channel).add(callback);

        return () => {
            const subs = this._subscribers.get(channel);
            if (subs) {
                subs.delete(callback);
            }
        };
    }

    /**
     * Derive session state from stored events.
     * @returns {Object} { status, isPaused, isComplete, queuePosition, lastStep }
     */
    getSessionState() {
        let status = 'unknown';
        let isPaused = false;
        let isComplete = false;
        let queuePosition = null;
        let lastStep = 0;

        // Process events in order to get final state
        for (const event of this._ordered) {
            switch (event.type) {
                case 'status':
                    status = event.data?.status || event.status || status;
                    if (status === 'paused') isPaused = true;
                    if (status === 'resumed' || status === 'running') isPaused = false;
                    if (status === 'complete' || status === 'done') isComplete = true;
                    break;
                case 'session_paused':
                    isPaused = true;
                    break;
                case 'session_resumed':
                    isPaused = false;
                    break;
                case 'session_complete':
                case 'session_done':
                case 'task_complete':
                    isComplete = true;
                    break;
                case 'queue_status':
                    queuePosition = event.data?.position ?? event.position ?? null;
                    break;
                case 'step':
                case 'agent_step':
                case 'task_progress':
                    const step = event.step || event.data?.step || 0;
                    if (step > lastStep) lastStep = step;
                    break;
            }
        }

        return { status, isPaused, isComplete, queuePosition, lastStep };
    }

    /**
     * Clear all events (for session switch or reset).
     */
    clear() {
        const count = this._events.size;
        this._events.clear();
        this._ordered = [];
        this._lastSeq = 0;
        this._notify('all', { action: 'clear', previousCount: count });
        log(`Cleared ${count} events`);
    }

    /**
     * Get total event count.
     * @returns {number}
     */
    get size() {
        return this._events.size;
    }

    // --- Private methods ---

    /**
     * Insert event in correct position by _seq using binary search.
     */
    _insertOrdered(event, seq) {
        // Binary search for insertion point
        let left = 0, right = this._ordered.length;
        while (left < right) {
            const mid = Math.floor((left + right) / 2);
            const midSeq = this._ordered[mid]._seq || 0;
            if (midSeq < seq) {
                left = mid + 1;
            } else {
                right = mid;
            }
        }
        this._ordered.splice(left, 0, event);
    }

    /**
     * Generate fallback ID for events without _id.
     */
    _generateFallbackId(event) {
        // Use combination of type, timestamp, and step for uniqueness
        const type = event.type || 'unknown';
        const ts = event._ts_ms || event.timestamp || Date.now();
        const step = event.step || '';
        const agent = event.agent_id || '';
        return `${type}_${ts}_${step}_${agent}`;
    }

    /**
     * Generate fallback sequence for events without _seq.
     */
    _fallbackSeq(event) {
        // Use _ts_ms or timestamp * 1000 as fallback ordering
        if (event._ts_ms) return event._ts_ms;
        if (event.timestamp) return Math.floor(event.timestamp * 1000);
        // Use current timestamp + small offset to ensure newer events come after
        return Date.now() + (Math.random() * 1000);
    }

    /**
     * Enforce memory limit by evicting oldest events.
     */
    _enforceLimit() {
        if (this._events.size <= this.maxEvents) return;

        // Evict oldest 20% when limit exceeded
        const evictCount = Math.floor(this.maxEvents * 0.2);
        const toEvict = this._ordered.slice(0, evictCount);

        for (const event of toEvict) {
            const id = event._id || this._generateFallbackId(event);
            this._events.delete(id);
        }

        this._ordered = this._ordered.slice(evictCount);
        log(`Evicted ${evictCount} oldest events, now at ${this._events.size}`);
    }

    /**
     * Notify subscribers on a channel.
     */
    _notify(channel, change) {
        const subs = this._subscribers.get(channel);
        if (!subs) return;

        for (const callback of subs) {
            try {
                callback(change);
            } catch (err) {
                console.error('[EventStore] Subscriber error:', err);
            }
        }
    }
}

// Singleton instance for global use
export const eventStore = new EventStore();

// Export for testing
export default EventStore;
