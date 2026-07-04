/**
 * Performance Optimization Utilities
 *
 * Provides debounce, throttle, and other performance optimization utilities.
 */

/**
 * Debounce function - delays execution until after wait period has elapsed
 * since last call. Useful for input handlers, resize events.
 *
 * @param {Function} func - Function to debounce
 * @param {number} wait - Wait time in milliseconds
 * @param {boolean} immediate - Trigger on leading edge instead of trailing
 * @returns {Function} Debounced function
 *
 * Example:
 *   const debouncedSave = debounce(saveForm, 500);
 *   input.addEventListener('input', debouncedSave);
 */
export function debounce(func, wait, immediate = false) {
    let timeout;

    return function executedFunction(...args) {
        const context = this;

        const later = function() {
            timeout = null;
            if (!immediate) func.apply(context, args);
        };

        const callNow = immediate && !timeout;

        clearTimeout(timeout);
        timeout = setTimeout(later, wait);

        if (callNow) func.apply(context, args);
    };
}

/**
 * Throttle function - limits execution to once per specified period.
 * Useful for scroll handlers, mouse move events.
 *
 * @param {Function} func - Function to throttle
 * @param {number} limit - Time limit in milliseconds
 * @returns {Function} Throttled function
 *
 * Example:
 *   const throttledScroll = throttle(handleScroll, 100);
 *   window.addEventListener('scroll', throttledScroll);
 */
export function throttle(func, limit) {
    let inThrottle;
    let lastResult;

    return function(...args) {
        const context = this;

        if (!inThrottle) {
            lastResult = func.apply(context, args);
            inThrottle = true;

            setTimeout(() => {
                inThrottle = false;
            }, limit);
        }

        return lastResult;
    };
}

/**
 * RequestAnimationFrame-based throttle for smooth animations.
 * Ensures function runs at most once per frame.
 *
 * @param {Function} func - Function to throttle
 * @returns {Function} RAF-throttled function
 *
 * Example:
 *   const rafScroll = rafThrottle(updateScrollPosition);
 *   window.addEventListener('scroll', rafScroll);
 */
export function rafThrottle(func) {
    let rafId = null;
    let lastArgs;

    return function(...args) {
        const context = this;
        lastArgs = args;

        if (rafId === null) {
            rafId = requestAnimationFrame(() => {
                func.apply(context, lastArgs);
                rafId = null;
            });
        }
    };
}

/**
 * Lazy loader for images - loads images when they enter viewport
 *
 * @param {HTMLImageElement} img - Image element to lazy load
 * @param {string} src - Image source URL
 * @param {Object} options - IntersectionObserver options
 */
export function lazyLoadImage(img, src, options = {}) {
    const defaultOptions = {
        root: null,
        rootMargin: '50px',
        threshold: 0.01
    };

    const observerOptions = { ...defaultOptions, ...options };

    const observer = new IntersectionObserver((entries, obs) => {
        entries.forEach(entry => {
            if (entry.isIntersecting) {
                const image = entry.target;
                image.src = src;
                image.classList.add('loaded');
                obs.unobserve(image);
            }
        });
    }, observerOptions);

    observer.observe(img);
}

/**
 * Batch DOM updates for better performance
 *
 * @param {Function} callback - Function containing DOM updates
 * @returns {Promise} Promise that resolves after updates
 *
 * Example:
 *   await batchDOMUpdates(() => {
 *       element1.textContent = 'foo';
 *       element2.classList.add('bar');
 *   });
 */
export function batchDOMUpdates(callback) {
    return new Promise(resolve => {
        requestAnimationFrame(() => {
            callback();
            resolve();
        });
    });
}

/**
 * Measure performance of a function
 *
 * @param {string} name - Name for the measurement
 * @param {Function} func - Function to measure
 * @returns {*} Return value of func
 *
 * Example:
 *   const result = measurePerformance('dataProcessing', () => {
 *       return processLargeDataset(data);
 *   });
 */
export function measurePerformance(name, func) {
    const start = performance.now();
    const result = func();
    const end = performance.now();

    console.log(`[Performance] ${name}: ${(end - start).toFixed(2)}ms`);

    return result;
}

/**
 * Check if user prefers reduced motion
 *
 * @returns {boolean} True if reduced motion is preferred
 */
export function prefersReducedMotion() {
    const mediaQuery = window.matchMedia('(prefers-reduced-motion: reduce)');
    return mediaQuery.matches;
}

/**
 * Execute callback when idle (using requestIdleCallback if available)
 *
 * @param {Function} callback - Callback to execute
 * @param {Object} options - Options for requestIdleCallback
 */
export function runWhenIdle(callback, options = {}) {
    if ('requestIdleCallback' in window) {
        requestIdleCallback(callback, options);
    } else {
        // Fallback to setTimeout
        setTimeout(callback, 1);
    }
}
